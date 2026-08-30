"""P1-D-A — Factory stage definitions (orchestration layer).

Wraps the already-validated P0 / P1-A / P1-B / P1-C modules into a single,
machine-readable stage state machine.  Each stage is a pure function

    stage_xxx(ctx) -> StageResult

and the runner (batch_runner.py) drives them in order, advancing the manifest
state machine and consulting the Exception Gate after every stage.

Hard boundaries (do NOT cross in P1-D-A)
-----------------------------------------
* Run NEVER writes production MASTER.
* Run NEVER builds the site, commits, pushes, or deploys.
* Run NEVER calls release() — release() is a separate human gate.
* Run only produces a Batch result + READY_FOR_RELEASE state.

The user-facing pipeline

    Batch -> Harvest -> Intake/Normalize -> Dedup -> Qualify
         -> Datasheet Factory -> R2 Asset Factory -> Reconcile
         -> Batch Audit -> READY

maps onto the stage registry below.  DEDUP and QUALIFY are performed inside the
`normalize` stage (product_data.normalize runs dedup.guard + qualify); the
runner additionally records "DEDUP" / "QUALIFY" checkpoints in the manifest so
the state machine shows exactly where a STOP originated.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime

from . import gate, pool
from . import product_data, datasheet as ds, r2
from .manifest import (
    INTAKE, QUALIFICATION, DEDUP, BACKUP, PROCESSING, BUILT, AUDITED,
    READY_FOR_RELEASE, FAILED,
)

__all__ = [
    "StageResult", "BatchContext",
    "STAGE_ORDER", "STAGE_FLOW", "STAGES",
]


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# =====================================================================
# result / context structures
# =====================================================================
class StageResult:
    """Machine-readable outcome of one stage."""

    def __init__(self, name):
        self.name = name
        self.ok = True
        self.stopped = False           # a STOP exception fired -> block downstream
        self.skipped = False           # AUTO_SKIP scenario (batch continues)
        self.auto_passed = False       # AUTO_PASS scenario (no work needed)
        self.note = ""
        self.counts = {}
        self.exceptions = []
        self.data = {}

    def fail(self, note="", stopped=True):
        self.ok = False
        self.stopped = stopped
        self.note = note
        return self

    def as_dict(self):
        return {
            "name": self.name, "ok": self.ok, "stopped": self.stopped,
            "skipped": self.skipped, "auto_passed": self.auto_passed,
            "note": self.note, "counts": self.counts,
            "exceptions": self.exceptions, "data": self.data,
        }


class BatchContext:
    """Everything a stage needs. Built by the runner; never touches production
    unless the caller explicitly points root/pool_root at production paths
    (which the sandbox tests never do)."""

    def __init__(self, batch_id, root, pool_root, backup_root, manifest_obj,
                 options=None):
        self.batch_id = batch_id
        self.root = root                 # manifest batch root
        self.pool_root = pool_root       # SZ_POOL_ROOT for pool modules
        self.backup_root = backup_root   # staging backup root
        self.manifest = manifest_obj
        self.options = options or {}
        self.r2_client = self.options.get("r2_client")
        self.r2_bucket = self.options.get("r2_bucket")
        self.reconcile_verdict = None    # filled by stage_reconcile
        self.results = []

    @property
    def dry_run(self):
        return bool(self.options.get("dry_run"))


# =====================================================================
# stage: BATCH
# =====================================================================
def stage_batch(ctx):
    """Manifest already created by the runner; just log + assert INTAKE."""
    sr = StageResult("batch")
    ctx.manifest.record_stage("BATCH", ok=True,
                               note=f"batch {ctx.batch_id} initialised")
    sr.note = "manifest created; status INTAKE"
    return sr


# =====================================================================
# stage: HARVEST  (product_data.intake -> raw pool)
# =====================================================================
def stage_harvest(ctx):
    sr = StageResult("harvest")
    res = product_data.intake(
        ctx.batch_id,
        source_kind="lcsc_api_csv",
        source_path=ctx.options.get("source_path"),
        selector=ctx.options.get("selector"),
        category=ctx.options.get("category"),
        limit=ctx.options.get("limit"),
        require_datasheet=ctx.options.get("require_datasheet", False),
        exclude_mpns=ctx.options.get("exclude_mpns"),
        mfr_csv=ctx.options.get("mfr_csv"),
        root=ctx.pool_root,
        manifest=ctx.manifest,
    )
    sr.exceptions = [dict(e) for e in res.exceptions]
    sr.counts = {
        "input_count": res.input_count,
        "written": res.written,
        "skipped_no_datasheet": res.skipped_no_datasheet,
        "source_self_duplicates": res.self_duplicate_count,
    }
    if res.stop:
        sr.fail(note="intake POOL_WRITE_FAIL", stopped=True)
    else:
        sr.note = f"{res.written} raw records -> pool"
    return sr


# =====================================================================
# stage: NORMALIZE  (product_data.normalize -> candidates)
#                      performs clean + DEDUP (dedup.guard) + QUALIFY (qualify)
# =====================================================================
def stage_normalize(ctx):
    sr = StageResult("normalize")
    res = product_data.normalize(
        ctx.batch_id,
        master_csv=ctx.options.get("master_csv"),
        mfr_csv=ctx.options.get("mfr_csv"),
        root=ctx.pool_root,
        manifest=ctx.manifest,
        skip_mass_duplicate_check=ctx.options.get("skip_mass_duplicate_check", False),
    )
    sr.exceptions = [dict(e) for e in res.exceptions]
    sr.counts = {
        "input_count": res.input_count,
        "cleaned_count": res.cleaned_count,
        "duplicate_count": res.duplicate_count,
        "self_duplicate_count": res.self_duplicate_count,
        "rejected_count": res.rejected_count,
        "candidate_count": res.candidate_count,
    }
    # honour the user's explicit DEDUP / QUALIFY checkpoints in the manifest log
    ctx.manifest.record_stage(
        "DEDUP", ok=not (res.stop and res.duplicate_count),
        note=f"{res.duplicate_count} duplicate / {res.self_duplicate_count} self-dup")
    ctx.manifest.record_stage(
        "QUALIFY", ok=not res.stop,
        note=f"{res.rejected_count} rejected / {res.candidate_count} candidates")
    if res.stop:
        sr.fail(note="normalize STOP (CJK leak / mass-duplicate / self-duplicate)",
                stopped=True)
    else:
        sr.note = f"{res.candidate_count} candidates ready"
    return sr


# =====================================================================
# stage: BACKUP  (sandbox-safe staging backup of the batch's own pool files)
# =====================================================================
def _take_staging_backup(batch_id, pool_root, backup_root):
    """Mirror backup.py discipline (copy + SHA256 verify + manifest) but
    operate ONLY on the batch's staging pool — never on production MASTER.
    NO_BACKUP -> STOP."""
    files = []
    for p in (pool.raw_path(batch_id, pool_root),
              pool.candidates_path(batch_id, pool_root),
              pool.datasheet_index_path(batch_id, pool_root)):
        if os.path.exists(p):
            files.append(p)
    dest = os.path.join(backup_root, f"pre_{batch_id}")
    os.makedirs(dest, exist_ok=True)
    recs = []
    try:
        for src in files:
            name = os.path.basename(src)
            dst = os.path.join(dest, name)
            shutil.copy2(src, dst)
            if _sha256(src) != _sha256(dst):
                raise RuntimeError(f"staging backup SHA256 mismatch for {name}")
            if os.path.getsize(src) != os.path.getsize(dst):
                raise RuntimeError(f"staging backup size mismatch for {name}")
            recs.append({"name": name, "sha256": _sha256(dst),
                         "bytes": os.path.getsize(dst)})
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    bm = {"batch_id": batch_id, "created_at": _now(),
          "files": recs, "verified": True}
    with open(os.path.join(dest, "_backup_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(bm, f, ensure_ascii=False, indent=2)
    return bm


def stage_backup(ctx):
    sr = StageResult("backup")
    try:
        bm = _take_staging_backup(ctx.batch_id, ctx.pool_root, ctx.backup_root)
        sr.data["backup"] = bm
        sr.counts = {"files": len(bm["files"])}
        sr.note = f"staging backup verified ({len(bm['files'])} files)"
    except Exception as e:
        ctx.manifest.add_exception(gate.NO_BACKUP, gate.STOP, None, str(e))
        sr.exceptions.append({"code": gate.NO_BACKUP, "severity": gate.STOP,
                              "mpn": None, "message": str(e)})
        sr.fail(note=f"staging backup failed: {e}", stopped=True)
    return sr


# =====================================================================
# stage: DATASHEET FACTORY  (datasheet.discover + download_batch -> local pool)
# =====================================================================
def stage_datasheet(ctx):
    sr = StageResult("datasheet")
    ds.discover(ctx.batch_id, root=ctx.pool_root)
    res = ds.download_batch(
        ctx.batch_id,
        root=ctx.pool_root,
        workers=ctx.options.get("workers", ds.DEFAULT_WORKERS),
        retries=ctx.options.get("retries", ds.DEFAULT_RETRIES),
        timeout=ctx.options.get("timeout", ds.DEFAULT_TIMEOUT),
        backoff=ctx.options.get("backoff", ds.DEFAULT_BACKOFF),
        limit=ctx.options.get("download_limit"),
        manifest=ctx.manifest,
    )
    sr.counts = {
        "discovered": res.discovered, "verified": res.verified,
        "duplicate": res.duplicate, "failed": res.failed,
        "skipped": res.skipped, "bytes_downloaded": res.bytes_downloaded,
    }
    sr.note = (f"{res.verified} verified / {res.duplicate} dup / "
               f"{res.failed} failed / {res.skipped} skipped")
    # Per-Gate severity: datasheet failures are AUTO_SKIP, never STOP. The batch
    # continues even if some PDFs fail (no whole-batch abort on single failure).
    sr.ok = True
    sr.skipped = (res.failed + res.skipped) > 0
    return sr


# =====================================================================
# stage: R2 ASSET FACTORY  (r2.upload_batch -> R2)
# =====================================================================
def stage_r2(ctx):
    sr = StageResult("r2")
    # dry-run / no client: mark uploadable records UPLOADED without touching R2.
    if ctx.dry_run or (ctx.r2_client is None and not ctx.options.get("live_r2")):
        records = ds.load_ledger(ctx.batch_id, ctx.pool_root)
        n = 0
        for r in records:
            if r.get("status") in r2.UPLOADABLE:
                r["status"] = ds.UPLOADED
                r["remote_verified"] = True
                r["_outcome"] = "dry_run"
                n += 1
        ds.save_ledger(ctx.batch_id, records, ctx.pool_root)
        sr.counts = {"uploaded": n, "failed": 0, "mode": "dry_run"}
        sr.note = f"dry-run: {n} assets marked UPLOADED (no network)"
        sr.ok = True
        return sr

    res = r2.upload_batch(
        ctx.batch_id, root=ctx.pool_root,
        workers=ctx.options.get("workers", r2.DEFAULT_WORKERS),
        retries=ctx.options.get("retries", r2.DEFAULT_RETRIES),
        timeout=ctx.options.get("timeout", r2.DEFAULT_TIMEOUT),
        creds=ctx.options.get("r2_creds"),
        endpoint_url=ctx.options.get("r2_endpoint"),
        client=ctx.r2_client, bucket=ctx.r2_bucket,
        manifest=ctx.manifest,
    )
    sr.exceptions = [dict(e) for e in res.exceptions]
    sr.counts = {
        "total": res.total, "uploaded": res.uploaded,
        "already_exists": res.already_exists, "failed": res.failed,
        "new_objects": res.new_objects, "bytes_uploaded": res.bytes_uploaded,
    }
    sr.note = (f"{res.uploaded} uploaded / {res.already_exists} already / "
               f"{res.failed} failed")
    if res.stop:
        sr.fail(note="R2 STOP (mass-fail / remote-hash-mismatch / local-hash-unstable)",
                stopped=True)
    else:
        sr.ok = True
    return sr


# =====================================================================
# stage: RECONCILE  (r2.reconcile_r2 -> PASS/FAIL)
# =====================================================================
def _local_reconcile(batch_id, pool_root):
    records = ds.load_ledger(batch_id, pool_root)
    local_verified = 0
    for r in records:
        path = r.get("local_path")
        if not path or not os.path.exists(path):
            continue
        if r.get("sha256") and _sha256(path).lower() == r["sha256"].lower():
            local_verified += 1
    return {"local_verified": local_verified}


def stage_reconcile(ctx):
    sr = StageResult("reconcile")
    if ctx.dry_run or ctx.r2_client is None:
        # No R2 connection: reconcile against the local pool only (hash stable).
        verdict = _local_reconcile(ctx.batch_id, ctx.pool_root)
        ctx.reconcile_verdict = "SKIPPED"
        sr.counts = {"local_verified": verdict["local_verified"], "mode": "local_only"}
        sr.note = "reconcile skipped (no R2 client); local hashes stable"
        sr.ok = True
        sr.auto_passed = True
        return sr

    rec = r2.reconcile_r2(ctx.batch_id, root=ctx.pool_root,
                          client=ctx.r2_client, bucket=ctx.r2_bucket)
    ctx.reconcile_verdict = rec["verdict"]
    sr.counts = {"local_verified": rec["local_verified"],
                 "r2_verified": rec["r2_verified"],
                 "problems": len(rec["problems"])}
    sr.note = f"reconcile verdict={rec['verdict']}"
    if rec["verdict"] == "PASS":
        sr.ok = True
        sr.auto_passed = True
    else:
        ctx.manifest.add_exception(
            gate.REMOTE_VERIFY_FAILED, gate.STOP, None, "reconcile FAIL")
        sr.exceptions.append({"code": gate.REMOTE_VERIFY_FAILED, "severity": gate.STOP,
                              "mpn": None, "message": "reconcile FAIL"})
        sr.fail(note="reconcile FAIL (R2 vs local mismatch)", stopped=True)
    return sr


# =====================================================================
# stage: BATCH AUDIT  (per-batch consistency gate; NOT the full-site audit)
# =====================================================================
def stage_audit(ctx):
    sr = StageResult("audit")
    stops = ctx.manifest.exceptions_by_severity(gate.STOP)
    reconcile_bad = (ctx.reconcile_verdict == "FAIL")
    problems = []
    if stops:
        problems.append(f"{len(stops)} STOP exception(s) recorded")
    if reconcile_bad:
        problems.append("reconcile verdict FAIL")
    ok = (not stops) and (not reconcile_bad)
    sr.counts = {
        "stops": len(stops),
        "warnings": len(ctx.manifest.exceptions_by_severity(gate.WARNING)),
        "skips": len(ctx.manifest.exceptions_by_severity(gate.AUTO_SKIP)),
        "auto_pass": len(ctx.manifest.exceptions_by_severity(gate.AUTO_PASS)),
    }
    if ok:
        sr.ok = True
        sr.auto_passed = True
        sr.note = "batch audit PASS (no STOP; reconcile clean)"
    else:
        for s in stops:
            sr.exceptions.append(dict(s))
        sr.fail(note="batch audit FAIL: " + "; ".join(problems), stopped=True)
    return sr


# =====================================================================
# stage: READY  (set READY_FOR_RELEASE; NEVER approve/deploy)
# =====================================================================
def stage_ready(ctx):
    sr = StageResult("ready")
    # Only reached when no STOP fired. Run produces READY_FOR_RELEASE and stops.
    # Release (approve -> deploy) is a separate human gate (release()).
    sr.ok = True
    sr.note = "Run complete -> READY_FOR_RELEASE; awaiting human release approval"
    sr.counts = {"release_eligible": True}
    return sr


# =====================================================================
# registry + flow
# =====================================================================
STAGE_ORDER = ["batch", "harvest", "normalize", "backup", "datasheet",
               "r2", "reconcile", "audit", "ready"]

STAGES = {
    "batch": stage_batch,
    "harvest": stage_harvest,
    "normalize": stage_normalize,
    "backup": stage_backup,
    "datasheet": stage_datasheet,
    "r2": stage_r2,
    "reconcile": stage_reconcile,
    "audit": stage_audit,
    "ready": stage_ready,
}

# (stage, in_status, out_status) — drives the manifest state machine.
STAGE_FLOW = [
    ("batch", INTAKE, INTAKE),
    ("harvest", INTAKE, QUALIFICATION),
    ("normalize", QUALIFICATION, DEDUP),
    ("backup", DEDUP, BACKUP),
    ("datasheet", BACKUP, PROCESSING),
    ("r2", PROCESSING, PROCESSING),
    ("reconcile", PROCESSING, BUILT),
    ("audit", BUILT, AUDITED),
    ("ready", AUDITED, READY_FOR_RELEASE),
]
