"""Release Pipeline — READY_FOR_RELEASE -> Release Candidate -> MASTER staging -> consistency -> Build/Deploy candidate.

SAFETY CONTRACT (mirrors the frozen-layer rules)
------------------------------------------------
* This module NEVER calls gen_parts / publish_normalizer / build_datasheet_map
  / apply_datasheet_map / upload_datasheets / pre_deploy_audit.
* It writes ONLY to a MASTER path that is **injected** by the caller. In design /
  sandbox runs that path is a tempfile copy; the real production MASTER is never
  passed during those phases.
* Every MASTER mutation goes through master_io.append_rows_atomically(), which
  guarantees the pre-existing rows (e.g. the 540) are byte-for-field unchanged
  and fails closed (MASTER untouched) on any validation error.
* release() is a HUMAN GATE: it requires ``approved_by`` and by default performs
  NO build / deploy. Build/Deploy preparation is a separate, opt-in step.
* run() (batch_runner) never imports or calls release(); release() never calls
  build/deploy unless the caller flips the explicit also_prepare_build /
  also_prepare_deploy switches.

The pipeline produces, but does not execute, the Build and Deploy artifacts.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime

from . import MASTER_COLS, REQUIRED_FIELDS, manifest as MAN
from . import master_io, dedup, gate, pool, product_data, category
from .product_data import master_row
from .category import UNKNOWN_CATEGORY


# --------------------------------------------------------------------------
# release-specific gate codes (in addition to the shared gate.py codes)
# --------------------------------------------------------------------------
NO_HUMAN_APPROVAL = "NO_HUMAN_APPROVAL"
MASTER_HASH_MISMATCH = "MASTER_HASH_MISMATCH"
CONSISTENCY_FAIL = "CONSISTENCY_FAIL"
BUILD_GATE_FAIL = "BUILD_GATE_FAIL"

RELEASE_SCOPE = (
    "Release = append READY candidates to MASTER under a human gate. "
    "Build and Deploy are OUT OF SCOPE unless explicitly enabled."
)


class ReleaseError(Exception):
    pass


class ReleaseStop(ReleaseError):
    """A blocking gate failure. The release must not proceed."""

    def __init__(self, code, message, mpn=None):
        self.code = code
        self.message = message
        self.mpn = mpn
        super().__init__(f"[{code}] {message}")


def _now():
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# ReleasePlan — the Release Candidate (described, not yet applied)
# --------------------------------------------------------------------------
@dataclass
class ReleasePlan:
    batch_id: str = ""
    candidate_count: int = 0
    selected_count: int = 0
    new_mpns: list = field(default_factory=list)
    already_released_mpns: list = field(default_factory=list)
    intra_batch_duplicates: list = field(default_factory=list)
    new_rows: list = field(default_factory=list)        # MASTER-shaped rows to append
    projected_master_rows: list = field(default_factory=list)  # old + new
    before_count: int = 0
    after_count: int = 0
    before_mpns: set = field(default_factory=set)
    after_mpns: set = field(default_factory=set)
    before_sha256: str = ""
    after_sha256: str = ""
    gate_ok: bool = True
    stops: list = field(default_factory=list)            # [{code,mpn,message}]
    warnings: list = field(default_factory=list)

    def add_stop(self, code, message, mpn=None):
        self.stops.append({"code": code, "mpn": mpn, "message": message})
        self.gate_ok = False

    def add_warning(self, code, message, mpn=None):
        self.warnings.append({"code": code, "mpn": mpn, "message": message})

    def as_dict(self):
        return {
            "batch_id": self.batch_id,
            "candidate_count": self.candidate_count,
            "selected_count": self.selected_count,
            "new_mpns": self.new_mpns,
            "already_released_mpns": self.already_released_mpns,
            "intra_batch_duplicates": self.intra_batch_duplicates,
            "before_count": self.before_count,
            "after_count": self.after_count,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "gate_ok": self.gate_ok,
            "stops": self.stops,
            "warnings": self.warnings,
        }


@dataclass
class MasterStagingResult:
    master_path: str
    backup_path: str
    before_count: int
    after_count: int
    before_sha256: str
    after_sha256: str
    new_mpns: list
    old_rows_unchanged: bool
    atomic_write: bool


@dataclass
class ConsistencyReport:
    after_count: int
    after_mpns: set
    old_rows_unchanged: bool
    ok: bool
    problems: list = field(default_factory=list)


@dataclass
class BuildArtifacts:
    build_dir: str
    build_input_master: str
    build_manifest: str


@dataclass
class DeployCandidate:
    deploy_candidate: str
    commit_message: str


@dataclass
class ReleaseOutcome:
    batch_id: str
    released: bool
    new_count: int
    already_count: int
    skipped_count: int
    before_count: int
    after_count: int
    master_before_sha: str
    master_after_sha: str
    status: str
    build_prepared: bool
    deploy_prepared: bool
    artifacts: dict = field(default_factory=dict)
    stops: list = field(default_factory=list)


# --------------------------------------------------------------------------
# collect candidates
# --------------------------------------------------------------------------
def collect_candidates(batch_id, root=None):
    """Read the candidate pool for a batch (READ-ONLY)."""
    path = pool.candidates_path(batch_id, root)
    data = pool.read_json(path)
    if not data:
        raise ReleaseError(f"candidates not found: {path}")
    return data.get("rows", []), data


# --------------------------------------------------------------------------
# plan_release — build the Release Candidate (no writes)
# --------------------------------------------------------------------------
def plan_release(master_path, rows, subset_mpns=None, batch_id=""):
    """Validate + project candidates into a ReleasePlan.

    Guarantees encoded here:
      * pre-existing MASTER rows are untouched (we only ever append).
      * intra-batch duplicate MPN -> STOP (BATCH_SELF_DUPLICATE).
      * candidate already in MASTER -> idempotent AUTO_SKIP (not a stop).
      * synthetic / CJK leak / missing required field / UNKNOWN category -> STOP.
      * SPEC_THIN / BRAND_UNMAPPED -> WARNING (non-blocking, per readiness review).
    """
    cols, old_rows = master_io.read_master(master_path, MASTER_COLS)
    before_mpns = master_io.mpn_set(old_rows)
    before_count = len(old_rows)
    before_sha = master_io.sha256_of(master_path)

    plan = ReleasePlan(batch_id=batch_id, candidate_count=len(rows),
                       before_count=before_count, before_mpns=before_mpns,
                       before_sha256=before_sha)

    selected = list(rows)
    if subset_mpns is not None:
        want = {m.strip().upper() for m in subset_mpns}
        selected = [r for r in selected
                    if (r.get("mpn") or "").strip().upper() in want]
    plan.selected_count = len(selected)

    # ---- release-specific qualification (rejects -> STOP) ----------------
    qualified = []
    for r in selected:
        mpn = r.get("mpn", "")
        syn = product_data.looks_synthetic(mpn, r.get("brand", ""))
        if syn:
            plan.add_stop(gate.SYNTHETIC_MPN, syn, mpn)
            continue
        if product_data.has_cjk(r):
            plan.add_stop(gate.CJK_LEAK,
                          "non-ASCII survived normalisation", mpn)
            continue
        bad_field = None
        for f in REQUIRED_FIELDS:
            if not (r.get(f) or "").strip():
                bad_field = f
                break
        if bad_field is not None:
            plan.add_stop(gate.SPEC_THIN,
                          f"missing required field '{bad_field}'", mpn)
            continue
        if r.get("category") == UNKNOWN_CATEGORY:
            plan.add_stop(gate.UNMAPPED_CATEGORY,
                          f"category not mapped to an adapter: {r.get('category')}", mpn)
            continue
        # non-blocking warnings
        cat_name = r.get("category", "")
        adapter = category.REGISTRY.get(cat_name)
        min_specs = adapter.min_specs if adapter else 2
        if (r.get(product_data.F_SPEC_KEYS) or 0) < min_specs:
            plan.add_warning(gate.SPEC_THIN,
                             f"only {r.get(product_data.F_SPEC_KEYS) or 0} "
                             f"structured specs (min {min_specs}) for {cat_name}", mpn)
        qualified.append(r)

    # ---- project to MASTER shape + dedup -------------------------------
    new_rows = [master_row(r) for r in qualified]
    mpns = [r["mpn"] for r in new_rows]
    dres = dedup.guard(mpns, before_mpns)

    row_by_mpn = {(r["mpn"] or "").strip().upper(): r for r in new_rows}
    truly_new = [row_by_mpn[m.upper()] for m in dres.new]
    plan.already_released_mpns = list(dres.duplicates)

    # intra-batch / mass duplicate -> hard stop (overrides any append)
    for it in dres.exceptions:
        if it["severity"] == gate.STOP:
            if it["code"] == gate.MASS_DUPLICATE and not dres.new:
                # Fully idempotent re-release: every candidate is already in
                # MASTER (new is empty). That is benign, NOT a broken batch, so
                # we skip the MASS_DUPLICATE gate and let it be a no-op.
                continue
            plan.add_stop(it["code"], it["message"], it.get("mpn"))

    plan.new_rows = truly_new
    plan.new_mpns = [r["mpn"] for r in truly_new]
    plan.projected_master_rows = old_rows + truly_new
    plan.after_count = before_count + len(truly_new)
    plan.after_mpns = before_mpns | {(m or "").strip().upper() for m in plan.new_mpns}
    return plan


# --------------------------------------------------------------------------
# stage_master — atomic append under human approval (the only MASTER writer)
# --------------------------------------------------------------------------
def stage_master(plan, master_path, approved_by, backup_dir=None):
    """Append plan.new_rows to master_path atomically.

    Pre-conditions (any failure -> ReleaseStop, MASTER untouched):
      * approved_by is truthy (human gate)
      * plan.gate_ok
      * master still matches plan.before_sha256 (no concurrent modification)
    A backup copy is taken before the write so a later consistency failure can
    roll back.
    """
    if not approved_by:
        raise ReleaseStop(NO_HUMAN_APPROVAL,
                          "release requires approved_by (human gate)")
    if not plan.gate_ok:
        stops = "; ".join(f"{s['code']}:{s['message']}" for s in plan.stops)
        raise ReleaseStop(CONSISTENCY_FAIL, f"plan gate not ok: {stops}")

    cur_sha = master_io.sha256_of(master_path)
    if cur_sha != plan.before_sha256:
        raise ReleaseStop(MASTER_HASH_MISMATCH,
                          f"MASTER changed since plan was built "
                          f"(before={plan.before_sha256[:12]}.. "
                          f"now={cur_sha[:12]}..)")

    backup_dir = backup_dir or (os.path.dirname(master_path) or ".")
    os.makedirs(backup_dir, exist_ok=True)
    bak = os.path.join(backup_dir,
                       os.path.basename(master_path) + ".release.bak")
    shutil.copy2(master_path, bak)

    try:
        master_io.append_rows_atomically(master_path, plan.new_rows,
                                         expected_cols=MASTER_COLS)
    except master_io.MasterWriteError as e:
        # validation failed inside append -> restore backup, MASTER untouched
        if os.path.exists(bak):
            shutil.copy2(bak, master_path)
        raise ReleaseStop(gate.MASTER_CORRUPT,
                          f"append rejected, MASTER restored: {e}")

    after_sha = master_io.sha256_of(master_path)
    plan.after_sha256 = after_sha
    return MasterStagingResult(
        master_path=master_path, backup_path=bak,
        before_count=plan.before_count, after_count=plan.after_count,
        before_sha256=plan.before_sha256, after_sha256=after_sha,
        new_mpns=plan.new_mpns, old_rows_unchanged=True, atomic_write=True)


# --------------------------------------------------------------------------
# verify_consistency — post-staging integrity check
# --------------------------------------------------------------------------
def verify_consistency(master_path, plan):
    """Assert MASTER after staging matches the plan exactly."""
    cols, rows = master_io.read_master(master_path, MASTER_COLS)
    after_count = len(rows)
    after_mpns = master_io.mpn_set(rows)
    problems = []

    if after_count != plan.after_count:
        problems.append(f"row count {plan.before_count}->{after_count} "
                        f"!= expected {plan.after_count}")

    if after_mpns != plan.after_mpns:
        missing = plan.after_mpns - after_mpns
        extra = after_mpns - plan.after_mpns
        if missing:
            problems.append(f"missing MPNs: {sorted(missing)[:5]}")
        if extra:
            problems.append(f"unexpected MPNs: {sorted(extra)[:5]}")

    # pre-existing rows byte-for-field unchanged
    old_fp = master_io.row_fingerprint(
        plan.projected_master_rows[:plan.before_count], cols)
    act_fp = master_io.row_fingerprint(rows[:plan.before_count], cols)
    old_unchanged = (old_fp == act_fp)
    if not old_unchanged:
        problems.append("pre-existing rows modified after staging")

    # every new row present and field-identical to projection
    proj_by_mpn = {(r["mpn"] or "").strip().upper(): r
                  for r in plan.projected_master_rows}
    for r in rows:
        m = (r.get("mpn") or "").strip().upper()
        if m in proj_by_mpn:
            p = proj_by_mpn[m]
            for c in MASTER_COLS:
                if (r.get(c) or "") != (p.get(c) or ""):
                    problems.append(f"row {r.get('mpn')} field {c} mismatch "
                                    f"after staging")
                    break

    if problems:
        raise ReleaseStop(CONSISTENCY_FAIL, "; ".join(problems))
    return ConsistencyReport(after_count=after_count, after_mpns=after_mpns,
                             old_rows_unchanged=old_unchanged, ok=True)


# --------------------------------------------------------------------------
# rollback — restore master from backup
# --------------------------------------------------------------------------
def rollback_master(master_path, backup_path):
    if not os.path.exists(backup_path):
        return False
    shutil.copy2(backup_path, master_path)
    return True


# --------------------------------------------------------------------------
# prepare_build — write Build artifacts ONLY (never executes gen_parts)
# --------------------------------------------------------------------------
def prepare_build(staged_master_path, out_dir, plan=None):
    if plan is not None and not plan.gate_ok:
        raise ReleaseStop(BUILD_GATE_FAIL, "plan gate not ok; cannot prepare build")
    if not os.path.exists(staged_master_path):
        raise ReleaseStop(BUILD_GATE_FAIL,
                          f"staged master missing: {staged_master_path}")
    if plan is not None and getattr(plan, "after_sha256", None):
        if master_io.sha256_of(staged_master_path) != plan.after_sha256:
            raise ReleaseStop(BUILD_GATE_FAIL,
                              "staged master tampered since staging")
    os.makedirs(out_dir, exist_ok=True)
    build_input = os.path.join(out_dir, "build_input")
    os.makedirs(build_input, exist_ok=True)
    dst = os.path.join(build_input, "master_parts.csv")
    shutil.copy2(staged_master_path, dst)
    bm = {
        "generated_at": _now(),
        "source_master": staged_master_path,
        "commands": [
            "python tools/publish_normalizer.py --master <staged> --out master_parts_publish.csv",
            "python tools/build_datasheet_map.py",
            "python tools/apply_datasheet_map.py --map datasheet_map.csv --target master_parts_publish.csv",
            "python tools/gen_parts.py --csv master_parts_publish.csv --out 04_EXPORT/website_build",
            "python tools/pre_deploy_audit.py",
        ],
        "note": "Commands are documented ONLY; Release Pipeline does not execute them.",
    }
    bm_path = os.path.join(out_dir, "build_manifest.json")
    pool.atomic_write_json(bm_path, bm)
    return BuildArtifacts(build_dir=out_dir, build_input_master=dst,
                          build_manifest=bm_path)


# --------------------------------------------------------------------------
# prepare_deploy — write Deploy candidate ONLY (never pushes/deploys)
# --------------------------------------------------------------------------
def prepare_deploy(build_dir, out_dir, plan=None):
    if plan is not None and not plan.gate_ok:
        raise ReleaseStop(BUILD_GATE_FAIL, "plan gate not ok; cannot prepare deploy")
    if not os.path.isdir(build_dir):
        raise ReleaseStop(BUILD_GATE_FAIL, f"build dir missing: {build_dir}")
    os.makedirs(out_dir, exist_ok=True)
    n = len(plan.new_mpns) if plan else "?"
    bid = plan.batch_id if plan else "batch"
    dc = {
        "generated_at": _now(),
        "commit_message": f"release: {bid} (+{n} SKUs)",
        "files": ["data/production/master_parts_v2.1.csv",
                  "products/", "parts.json", "sitemap*.xml"],
        "trigger": "git push origin main -> Vercel auto-deploy",
        "note": "Deploy candidate only; Release Pipeline does not push or deploy.",
    }
    dc_path = os.path.join(out_dir, "deploy_candidate.json")
    pool.atomic_write_json(dc_path, dc)
    return DeployCandidate(deploy_candidate=dc_path,
                           commit_message=dc["commit_message"])


# --------------------------------------------------------------------------
# list_ready_batches — what is waiting in READY_FOR_RELEASE
# --------------------------------------------------------------------------
def list_ready_batches(root=None):
    import glob
    root = root or MAN.DEFAULT_BATCH_ROOT
    out = []
    for mpath in glob.glob(os.path.join(root, "*.json")):
        try:
            m = MAN.BatchManifest.load(
                os.path.splitext(os.path.basename(mpath))[0], root)
        except Exception:
            continue
        if m.status == MAN.READY_FOR_RELEASE:
            out.append(m.data["batch_id"])
    return out


# --------------------------------------------------------------------------
# release — the orchestrator (human gate; default: NO build/deploy)
# --------------------------------------------------------------------------
def release(batch_id, master_path, root=None, approved_by=None,
            subset_mpns=None, also_prepare_build=False,
            also_prepare_deploy=False, manifest=None, backup_dir=None,
            release_dir=None, verify_fn=None):
    """READY_FOR_RELEASE -> MASTER staging -> consistency -> APPROVED.

    Build/Deploy artifacts are prepared ONLY when the explicit switches are on.
    By default this function touches MASTER (append) and NOTHING else.
    """
    verify_fn = verify_fn or verify_consistency

    rows, _meta = collect_candidates(batch_id, root)
    plan = plan_release(master_path, rows, subset_mpns=subset_mpns,
                        batch_id=batch_id)

    if not plan.gate_ok:
        stops = [f"{s['code']}:{s['message']}" for s in plan.stops]
        if manifest is not None:
            for s in plan.stops:
                manifest.add_exception(s["code"], gate.STOP, s.get("mpn"),
                                       s["message"])
            manifest.set_status(MAN.FAILED)
        raise ReleaseStop(CONSISTENCY_FAIL, "plan gate not ok: " + "; ".join(stops))

    staging = stage_master(plan, master_path, approved_by, backup_dir=backup_dir)

    # consistency gate -> rollback on failure
    try:
        verify_fn(master_path, plan)
    except ReleaseStop as e:
        rollback_master(master_path, staging.backup_path)
        if manifest is not None:
            manifest.add_exception(e.code, gate.STOP, None, e.message)
            manifest.set_status(MAN.FAILED)
        raise

    build_prepared = deploy_prepared = False
    artifacts = {"release_plan": None, "build": None, "deploy": None}
    release_dir = release_dir or os.path.join(
        os.path.dirname(master_path) or ".", "releases", batch_id)
    os.makedirs(release_dir, exist_ok=True)
    plan_path = os.path.join(release_dir, "release_plan.json")
    pool.atomic_write_json(plan_path, plan.as_dict())
    artifacts["release_plan"] = plan_path

    if also_prepare_build:
        ba = prepare_build(master_path, os.path.join(release_dir, "build"), plan)
        build_prepared = True
        artifacts["build"] = ba.build_manifest
    if also_prepare_deploy:
        dc = prepare_deploy(os.path.join(release_dir, "build"),
                            os.path.join(release_dir, "deploy"), plan)
        deploy_prepared = True
        artifacts["deploy"] = dc.deploy_candidate

    if manifest is not None:
        manifest.data["master"] = {
            "before": {"rows": plan.before_count, "sha256": plan.before_sha256},
            "after": {"rows": plan.after_count, "sha256": plan.after_sha256},
            "atomic_write": True,
            "old_rows_unchanged": True,
        }
        manifest.set_status(MAN.APPROVED,
                            note=f"human release by {approved_by}")

    return ReleaseOutcome(
        batch_id=batch_id, released=True,
        new_count=len(plan.new_mpns),
        already_count=len(plan.already_released_mpns),
        skipped_count=len(plan.intra_batch_duplicates),
        before_count=plan.before_count, after_count=plan.after_count,
        master_before_sha=plan.before_sha256,
        master_after_sha=plan.after_sha256,
        status=MAN.APPROVED, build_prepared=build_prepared,
        deploy_prepared=deploy_prepared, artifacts=artifacts,
        stops=plan.stops)


__all__ = [
    "ReleaseError", "ReleaseStop",
    "ReleasePlan", "MasterStagingResult", "ConsistencyReport",
    "BuildArtifacts", "DeployCandidate", "ReleaseOutcome",
    "NO_HUMAN_APPROVAL", "MASTER_HASH_MISMATCH", "CONSISTENCY_FAIL",
    "BUILD_GATE_FAIL", "RELEASE_SCOPE",
    "collect_candidates", "plan_release", "stage_master",
    "verify_consistency", "rollback_master", "prepare_build",
    "prepare_deploy", "list_ready_batches", "release",
]
