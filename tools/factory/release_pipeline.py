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

import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime

from . import MASTER_COLS, REQUIRED_FIELDS, manifest as MAN
from . import master_io, dedup, gate, pool, product_data, category, faq_policy
from . import datasheet_hosts
from . import datasheet_clones
from .product_data import master_row
from .category import UNKNOWN_CATEGORY


# --------------------------------------------------------------------------
# release-specific gate codes (in addition to the shared gate.py codes)
# --------------------------------------------------------------------------
NO_HUMAN_APPROVAL = "NO_HUMAN_APPROVAL"
MASTER_HASH_MISMATCH = "MASTER_HASH_MISMATCH"
CONSISTENCY_FAIL = "CONSISTENCY_FAIL"
BUILD_GATE_FAIL = "BUILD_GATE_FAIL"
SPEC_INTEGRITY_FAIL = "SPEC_INTEGRITY_FAIL"
DATASHEET_IDENTITY_FAIL = "DATASHEET_IDENTITY_FAIL"
DATASHEET_NOT_R2_FAIL = "DATASHEET_NOT_R2_FAIL"
ALT_PARTS_ASIS_FAIL = "ALT_PARTS_ASIS_FAIL"
CLONE_PART_MPN_KEY_FAIL = "CLONE_PART_MPN_KEY_FAIL"
FAQ_FABRICATED_FAIL = "FAQ_FABRICATED_FAIL"

# 01-collection output holding the per-SKU RAW envelopes (the only place the
# real LCSC `faqs` field lives). Overridable so a future move of the RAW tree
# does not silently turn this gate into a blanket STOP.
FAQ_RAW_DIR = os.environ.get("SZP_RAW_DIR", r"D:\SZ Procure\采集流水线\基础数据")

# Fields an authorised in-place MASTER correction is allowed to change.
# 02 owns the spec payload and the classification; everything else belongs to
# another stage (brand canonicalisation, R2 datasheet binding, native_l1 chain,
# copywriting) and must not be reverted by a spec fix.
ROW_UPDATE_FIELDS = ("attributes_json",)

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
    spec_integrity_report: list = field(default_factory=list)
    update_mpns: list = field(default_factory=list)   # authorised corrections
    datasheet_identity: list = field(default_factory=list)  # [level,kind,msg]
    # R23 host policy (原厂豁免 / LCSC -> R2 only): [level,kind,msg]. The
    # gate commits rewrites itself; these entries are its audit trail.
    datasheet_host_policy: list = field(default_factory=list)
    # R24 clone-part gate: [level,kind,msg] for "same MPN, several raw
    # manufacturers, bound through an MPN-derived R2 object".
    clone_part_key: list = field(default_factory=list)
    alternate_parts_report: list = field(default_factory=list)  # AS-IS gate log
    # R25 FAQ provenance: [level,kind,msg] for "non-empty faq that RAW cannot
    # trace back to source_raw.main_product.faqs" (STOP) or a human-verified
    # MPN sitting in faq_policy.VERIFIED_MPNS (WARN).
    faq_provenance: list = field(default_factory=list)

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
            "spec_integrity_report": self.spec_integrity_report,
            "update_mpns": self.update_mpns,
            "datasheet_identity": self.datasheet_identity,
            "datasheet_host_policy": self.datasheet_host_policy,
            "clone_part_key": self.clone_part_key,
            "alternate_parts_report": self.alternate_parts_report,
            "faq_provenance": self.faq_provenance,
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
    # 2026-09-25: number of pre-existing rows the human explicitly authorised
    # for an in-place correction (0 unless allow_row_update_mpns was passed).
    authorised_row_updates: int = 0


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
# datasheet identity gate (2026-09-25, formal production rule)
# --------------------------------------------------------------------------
# Binding identity is the TRIPLE (mpn, manufacturer, supplier_reference).
# A datasheet may only be bound to one identity, and one identity may only
# ever resolve to ONE datasheet. The historical datasheet_map.csv is keyed by
# MPN alone, which is exactly what lets a clone part (same MPN, different
# manufacturer) inherit somebody else's PDF. This gate makes that failure
# IMPOSSIBLE to ship: it stops the release instead of relying on a human
# noticing the mismatch.
#
#   R1  same (mpn, manufacturer) -> >1 datasheet           -> STOP
#   R2  same mpn, different manufacturers, but they all
#       resolve to the SAME datasheet file (MPN-only
#       binding leaking across manufacturers)              -> STOP
#   R3  same mpn under >1 manufacturer (legitimate clone
#       family) -> reported as a WARNING; each member must
#       carry its own (manufacturer, datasheet) pair
# --------------------------------------------------------------------------
_DS_FIELDS = ("datasheet_url", "source_datasheet_url", "local_file",
              "r2_url", "r2_key", "pdf_url")


def _ds_ref(r):
    for f in _DS_FIELDS:
        v = (r.get(f) or "").strip()
        if v:
            return f, v
    return None, ""


def check_datasheet_identity(rows, old_rows=None):
    """Return a list of (level, kind, message). Levels: STOP | WARN.

    Pure function: reads rows, touches nothing."""
    out = []

    def norm_mpn(pn):
        return re.sub(r"\s+", "", (pn or "").strip().lower())

    def norm_mfr(b):
        return re.sub(r"[^a-z0-9]", "", (b or "").strip().lower())

    # mpn -> {manufacturer -> {(field, ref) -> set(cid)}}
    grouped = {}
    cid_by_identity = {}
    for r in rows:
        m = norm_mpn(r.get("mpn"))
        if not m:
            continue
        b = norm_mfr(r.get("manufacturer") or r.get("brand"))
        cid = (r.get("supplier_reference") or "").strip()
        f, ref = _ds_ref(r)
        slot = grouped.setdefault(m, {}).setdefault(b, {})
        if ref:
            slot.setdefault((f, ref), set()).add(cid)
        cid_by_identity[(m, b, f, ref)] = cid

    for m, by_mfr in sorted(grouped.items()):
        if len(by_mfr) > 1:
            # R3 - clone family: legitimate, but every member must own its ds
            for b, slot in by_mfr.items():
                if len(slot) > 1:
                    refs = "; ".join(sorted(f"{k[0]}={k[1]} (cid={','.join(sorted(v))})"
                                            for k, v in slot.items()))
                    out.append(("WARN", "DATASHEET_CLONE_MPN",
                                f"mpn={m!r} manufacturer={b!r} binds "
                                f"{len(slot)} different datasheets: {refs}"))
            # Union of refs across ALL manufacturers: exactly one => a single
            # datasheet is being shared by identities that are NOT identical
            # (the MPN-only binding hazard). More than one => each manufacturer
            # genuinely owns its own PDF, which is the correct state.
            all_refs = {f"{k[0]}:{k[1]}"
                        for _b, slot in by_mfr.items() for k in slot}
            mfrs = sorted(by_mfr)
            if len(all_refs) == 1:
                # R2 - all manufacturers point at the SAME datasheet file
                ref = next(iter(next(iter(by_mfr.values()))))
                out.append(("STOP", "DATASHEET_IDENTITY_FAIL",
                            f"mpn={m!r} is split across manufacturers "
                            f"{mfrs} but all resolve to the same datasheet "
                            f"{ref[0]}={ref[1]!r} -- MPN-only binding would "
                            f"cross-contaminate manufacturers"))
            else:
                # R3 - each manufacturer owns a distinct datasheet (correct), but
                # the operator must still see that datasheet_map.csv keys by MPN
                # alone and would collapse these identities.
                detail = "; ".join(
                    f"{b} -> " + (", ".join(sorted(f"{k[1]}" for k in slot)) or "<none>")
                    for b, slot in sorted(by_mfr.items()))
                out.append(("WARN", "DATASHEET_CLONE_MPN",
                            f"mpn={m!r} is split across {len(mfrs)} manufacturers "
                            f"({detail}); each owns its own datasheet (OK) but "
                            f"datasheet_map.csv is MPN-keyed -- bind by "
                            f"(mpn, manufacturer) instead"))
        else:
            # single manufacturer -> R1
            b = next(iter(by_mfr))
            slot = by_mfr[b]
            if len(slot) > 1:
                refs = "; ".join(sorted(f"{k[0]}={k[1]}" for k in slot))
                out.append(("STOP", "DATASHEET_IDENTITY_FAIL",
                            f"mpn={m!r} manufacturer={b!r} binds "
                            f"{len(slot)} datasheets: {refs} -- one identity "
                            f"cannot have two datasheets"))

    # historical rows already in MASTER: report only (cannot block the past)
    for r in (old_rows or []):
        m = norm_mpn(r.get("mpn"))
        if not m or m not in grouped:
            continue
        b = norm_mfr(r.get("manufacturer") or r.get("brand"))
        if b in grouped[m] and len(grouped[m]) > 1:
            out.append(("WARN", "DATASHEET_CLONE_IN_MASTER",
                        f"mpn={m!r} already in MASTER under "
                        f"{len(grouped[m])} manufacturers: "
                        f"{sorted(grouped[m])}"))
            break
    return out


# --------------------------------------------------------------------------
# datasheet HOST policy gate (R23, 2026-09-24)
# --------------------------------------------------------------------------
# Formal production rule, decided by the operator:
#
#     "以后 原厂链接可以直接豁免，LCSC链接只能R2"
#
# i.e. an official/manufacturer link stays verbatim (it is the authority, the
# third party is not), while an LCSC link may only exist as an R2-hosted copy.
# Anything else is an unapproved third party and must be looked at, not
# shipped. This is an ALLOW-LIST on purpose: the previous guard was a one-host
# blacklist (lcsc.com) which silently passed every other vendor.
#
# It is deliberately a SEPARATE gate from check_datasheet_identity(). Identity
# asks "which PDF belongs to this part"; HOST asks "may this URL be rendered".
# Conflating them is how a wrong-but-R2 PDF would slip through, and vice
# versa.
#
# The rewrite is written back into the row under the SAME key it was read
# from (`source_datasheet_url` for 02 candidates). That is not incidental:
# product_data.master_row() derives the MASTER column from that key
# (product_data.py:342), so writing the R2 URL anywhere else would be
# overwritten on the way into MASTER and the LCSC link would survive.
# --------------------------------------------------------------------------
def check_datasheet_host_policy(rows, index=None):
    """Return a list of (level, kind, message). Levels: STOP | WARN.

    Side effect: a proven LCSC->R2 rewrite is committed into the row. Returns
    STOP for an LCSC link with no verified R2 mapping, for any unapproved
    third-party host, and for a broken URL shape. An official/OEM link SHIPs
    untouched. A row with no datasheet at all is ignored (matches the old
    "only non-empty URLs are policed" semantics)."""
    out = []
    for r in rows or ():
        res = datasheet_hosts.check_row(r, index,
                                        fields=datasheet_hosts.CANDIDATE_DS_FIELDS)
        mpn = (r.get("mpn") or "").strip()
        cid = (r.get("supplier_reference") or "").strip()
        who = "%s / %s" % (mpn, cid)
        act = res["action"]

        if act == "NODATA":
            continue

        if act == "REWRITE":
            f = res["field"]
            if f:
                r[f] = res["rewritten"]           # master_row() picks this up
            if res["rewrite_path"] == "mpn":
                out.append(("WARN", "DATASHEET_R2_BY_MPN",
                            f"{who}: {res['url']} -> {res['rewritten']}; R2 "
                            f"proven by MPN only, confirm the manufacturer "
                            f"owns that PDF"))
            else:
                out.append(("WARN", "DATASHEET_R2_BY_CID",
                            f"{who}: {res['url']} -> {res['rewritten']}; "
                            f"C#-exact R2 mapping"))
            continue

        if act == "STOP":
            out.append(("STOP", DATASHEET_NOT_R2_FAIL,
                        f"{who}: {res['why']} "
                        f"({res['field'] or 'datasheet_url'}={res['url']})"))
    return out


# --------------------------------------------------------------------------
# FAQ provenance gate (R25, 2026-09-26)
# --------------------------------------------------------------------------
# "Frequently Asked Questions 主要就是根据 LCSC 的数据源，没有的我们自己不
#  生成." A candidate may enter MASTER with a non-empty `faq` only when that
#  text is traceable to `source_raw.main_product.faqs` of the SAME C#.
#
# The historical hole: 02 cleaned the column with f-string template factories
# (category.py::_faq -> also used by lcsc_http_adapter), and gen_parts.py's
# "Pass B" then rendered MASTER.faq whenever RAW had no real FAQ. That is how
# 'HGC0805R5106K500NSLJ is a 9.999999999999999e-06 F capacitor' and '3296W-1-103
# is a 10000.0 ohm resistor' reached 8 live pages. Both ends are now closed;
# this gate is the third end, so re-opening either end stops the batch.
#
# Scoped to the CANDIDATES of this batch on purpose: 2540 already-published
# MASTER rows still carry the fabricated text (clearing them is a separate,
# batch-by-batch job), and a candidate that is already in MASTER is AUTO_SKIP'd
# anyway. Nobody can re-ship a fabricated FAQ through a new batch.
_RAW_FAQ_INDEX = None


def _raw_faq_index(raw_dir=None):
    """{C#: True, productModel-upper: True} for every RAW record carrying faqs."""
    global _RAW_FAQ_INDEX
    if _RAW_FAQ_INDEX is not None:
        return _RAW_FAQ_INDEX
    idx = {}
    base = raw_dir or FAQ_RAW_DIR
    try:
        entries = os.listdir(base)
    except OSError:
        _RAW_FAQ_INDEX = idx
        return idx
    for fn in entries:
        if not fn.lower().endswith(".json"):
            continue
        cid = fn[:-5].upper()
        try:
            with open(os.path.join(base, fn), "r", encoding="utf-8",
                      errors="replace") as fh:
                d = json.load(fh)
        except Exception:
            continue
        mp = ((d.get("source_raw", {}) or {}).get("main_product", {}) or {})
        if mp.get("faqs"):
            idx[cid] = True
            model = mp.get("productModel")
            if model:
                idx[str(model).strip().upper()] = True
    _RAW_FAQ_INDEX = idx
    return idx


def check_faq_provenance(rows, raw_dir=None):
    """Return a list of (level, kind, message). Levels: STOP | WARN."""
    out = []
    todo = [r for r in (rows or ())
            if (r.get("faq") or "").strip()]
    if not todo:
        return out
    idx = _raw_faq_index(raw_dir)
    for r in todo:
        mpn = (r.get("mpn") or "").strip()
        cid = (r.get("supplier_reference") or "").strip().upper()
        who = "%s / %s" % (mpn, cid)
        if faq_policy.is_verified(mpn):
            out.append(("WARN", "FAQ_CURATED_VERIFIED",
                        f"{who}: non-source FAQ kept under faq_policy."
                        f".VERIFIED_MPNS ({mpn}) — human-verified, not a "
                        f"generated sentence"))
            continue
        if cid and idx.get(cid):
            continue                                    # sourced, ship it
        out.append(("STOP", FAQ_FABRICATED_FAIL,
                    f"{who}: non-empty faq but RAW has no 'faqs' for this C# "
                    f"— text is self-generated. FAQ must come from the LCSC "
                    f"source or be listed in faq_policy.VERIFIED_MPNS"))
    return out


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
def plan_release(master_path, rows, subset_mpns=None, batch_id="",
                allow_uncategorized_mpns=None, allow_row_update_mpns=None):
    """Validate + project candidates into a ReleasePlan.

    Guarantees encoded here:
      * pre-existing MASTER rows are untouched (we only ever append).
      * intra-batch duplicate MPN -> STOP (BATCH_SELF_DUPLICATE).
      * candidate already in MASTER -> idempotent AUTO_SKIP (not a stop).
      * synthetic / CJK leak / missing required field / UNKNOWN category -> STOP,
        EXCEPT for MPNs explicitly listed in ``allow_uncategorized_mpns`` (e.g. a
        rescued pure-numeric MPN that cleared the synthetic guard but carries no
        11-family signal). Those are released as Uncategorized with a WARNING.
      * SPEC_THIN / BRAND_UNMAPPED -> WARNING (non-blocking, per readiness review).
      * datasheet identity (mpn, manufacturer, supplier_reference): a split
        identity that would cross-bind datasheets -> STOP (DATASHEET_IDENTITY_FAIL).
      * datasheet HOST policy (R23): official/OEM links ship as-is, an LCSC link
        that has a proven R2 mapping is rewritten in place, and any remaining
        non-R2 / unapproved third-party / broken-URL datasheet -> STOP
        (DATASHEET_NOT_R2_FAIL).
      * clone-part datasheet ownership (R24): a MPN that RAW shows under more
        than one manufacturer may not be bound through an MPN-derived R2 key,
        because that object is shared with the other manufacturers -> STOP
        (CLONE_PART_MPN_KEY_FAIL). This is the AO3401A defect (AOS page
        rendering the UMW PDF) made impossible to ship again.
      * FAQ provenance (R25): a candidate with a non-empty `faq` that RAW
        cannot trace back to `source_raw.main_product.faqs` -> STOP
        (FAQ_FABRICATED_FAIL). A MPN listed in factory.faq_policy.VERIFIED_MPNS
        is the only allowed exception and comes back as a WARNING.
      * ``allow_row_update_mpns``: MPNs the human explicitly authorised for an
        in-place MASTER correction (e.g. a mapping-rule fix on an already
        released SKU). Default None = append-only, identical to previous
        behaviour. Authorised rows are exempt from the "pre-existing rows
        unchanged" assertion but still pass every other check.
    """
    allow_unc = {(m or "").strip().upper() for m in (allow_uncategorized_mpns or [])}
    allow_upd = {(m or "").strip().upper() for m in (allow_row_update_mpns or [])}
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

    # ---- spec-integrity pre-gate (Round 6, 2026-09-24: wired INTO the flow) --
    # The 5-check guard (completeness / collision / range / condition / identity
    # + unit-safety) is part of THIS pipeline, not a separate manual step.
    # Any FAIL stops the release before MASTER staging; WARNs (clone parts)
    # are recorded on the plan for the human gate to see.
    from .spec_integrity_check import check_cids
    cids = sorted({(r.get("supplier_reference") or "").strip()
                   for r in selected
                   if (r.get("supplier_reference") or "").strip()})
    if cids:
        fails, _warns, report = check_cids(cids)
        plan.spec_integrity_report = report
        if fails:
            plan.add_stop(
                SPEC_INTEGRITY_FAIL,
                "spec_integrity_check: %d FAIL across %d C#; report on "
                "plan.spec_integrity_report / rerun CLI: python "
                "tools/factory/spec_integrity_check.py --cids %s"
                % (fails, len(cids), ",".join(cids)))
        else:
            for ln in report:
                s = ln.strip()
                if s.startswith("WARN"):
                    plan.add_warning("SPEC_INTEGRITY_WARN", s)

    # ---- datasheet identity gate (in-flow, not a side script) ------------
    # Runs on the same candidates as spec_integrity so a cross-manufacturer
    # clone can never ship with someone else's datasheet.
    for level, kind, msg in check_datasheet_identity(selected, old_rows):
        plan.datasheet_identity.append((level, kind, msg))
        if level == "STOP":
            plan.add_stop(DATASHEET_IDENTITY_FAIL, f"{kind}: {msg}")

    # ---- datasheet HOST policy gate (R23, 2026-09-24) --------------------
    # "原厂链接可以直接豁免，LCSC 链接只能 R2"
    # The rewrite table is built from what is ALREADY in MASTER (existing R2
    # rows) plus datasheet_map.csv, i.e. only mappings that have been proven.
    # An LCSC candidate whose PDF is not (yet) on R2 stops the batch instead of
    # rendering a third-party link, so the fix is "upload the PDF", never
    # "delete the link".
    if selected:
        _ds_idx = datasheet_hosts.build_r2_index(old_rows)
        for level, kind, msg in check_datasheet_host_policy(selected, _ds_idx):
            plan.datasheet_host_policy.append((level, kind, msg))
            if level == "STOP":
                plan.add_stop(kind, msg)

    # ---- clone-part datasheet gate (R24, 2026-09-25) --------------------
    # "datasheets/<mpn>.pdf" is ONE R2 object shared by every manufacturer of
    # that MPN; whoever uploaded last owns it. check_datasheet_identity() only
    # compares candidates WITHIN one batch, so a clone family released across
    # different batches never collides and sails through. This gate asks RAW
    # instead: if the MPN belongs to >1 brand in the RAW envelope, the row may
    # not be bound through an MPN-derived key -> STOP before staging.
    # Runs AFTER the host policy so the URL it inspects is the FINAL one (the
    # host gate may have rewritten an LCSC link into its R2 mapping).
    for level, kind, msg in datasheet_clones.check_clone_part_key(selected):
        plan.clone_part_key.append((level, kind, msg))
        if level == "STOP":
            plan.add_stop(CLONE_PART_MPN_KEY_FAIL, f"{kind}: {msg}")

    # ---- FAQ provenance gate (R25, 2026-09-26) ---------------------------
    # "FAQ 只能来自 LCSC 数据源，没有的我们自己不生成". A candidate carrying a
    # non-empty `faq` that RAW cannot trace back to `source_raw.main_product.
    # faqs` stops the batch (FAQ_FABRICATED_FAIL). The only escape is a MPN a
    # human verified and recorded in faq_policy.VERIFIED_MPNS, which comes back
    # as a WARN instead. Runs after the datasheet gates because it is the last
    # text-level gate before MASTER staging.
    for level, kind, msg in check_faq_provenance(selected):
        plan.faq_provenance.append((level, kind, msg))
        if level == "STOP":
            plan.add_stop(kind, msg)

    # ---- Alternative Parts AS-IS gate (2026-09-25) ----------------------
    # The FORMAL rule is a pure AS-IS replay of the RAW alternatePartList, with
    # NO filter layer (no brand gate, no LCSC stock gate, no hasAlternatePart
    # gate, no self-exclusion, no de-duplication). This gate proves the 02
    # candidate really carries that replay: if it does not, the SKU would be
    # released into MASTER with a WRONG alternate list, so the batch stops
    # before staging. Candidates whose RAW file is absent elsewhere are only
    # warned about — a missing RAW must never block a release.
    from . import alternates as _alts
    _alt_fails, _alt_warns, _alt_report = _alts.audit_release_rows(selected)
    plan.alternate_parts_report = _alt_report
    if _alt_fails:
        plan.add_stop(
            ALT_PARTS_ASIS_FAIL,
            "alternate_parts is not the RAW AS-IS replay for %d C#; the "
            "alternative list would be wrong on the page. Fix the 02 adapter "
            "(it must call alternates.real_alternates) and re-plan. Report on "
            "plan.alternate_parts_report. Offending C#: %s"
            % (len(_alt_fails), ", ".join(c for c, _w, _g, _n in _alt_fails[:10])))
    else:
        for ln in _alt_report:
            s = ln.strip()
            if s.startswith("WARN"):
                plan.add_warning("ALT_PARTS_ASIS_WARN", s)

    # ---- release-specific qualification (rejects -> STOP) ----------------
    qualified = []
    for r in selected:
        mpn = r.get("mpn", "")
        syn = product_data.looks_synthetic(mpn, r.get("brand", ""), r)
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
            if (mpn.strip().upper() not in allow_unc):
                plan.add_stop(gate.UNMAPPED_CATEGORY,
                              f"category not mapped to an adapter: {r.get('category')}", mpn)
                continue
            # Explicitly approved hold-for-review record (rescued pure-numeric MPN
            # that cleared the synthetic guard but has no 11-family signal). Released
            # as Uncategorized, flagged for later family-expansion backlog.
            plan.add_warning(gate.UNMAPPED_CATEGORY,
                             "Uncategorized but explicitly approved for release "
                             "(allow_uncategorized_mpns)", mpn)
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

    # Authorised in-place corrections: an already-released SKU whose spec
    # mapping was wrong. Only MPNs explicitly listed in allow_row_update_mpns
    # are substituted; every other pre-existing row stays byte-identical.
    updated_old = []
    row_by_mpn_up = {(r["mpn"] or "").strip().upper(): r for r in new_rows}
    for old_r in old_rows:
        key = (old_r.get("mpn") or "").strip().upper()
        if key in allow_upd and key in row_by_mpn_up:
            new_r = dict(row_by_mpn_up[key])
            # Scope the correction: only 02-owned spec/classification fields move.
            for f in set(new_r) - set(ROW_UPDATE_FIELDS):
                if (old_r.get(f) or "").strip():
                    new_r[f] = old_r.get(f)
            updated_old.append(new_r)
            plan.update_mpns.append(old_r.get("mpn"))
        else:
            updated_old.append(old_r)
    plan.update_mpns = [m for m in plan.update_mpns if m]

    plan.projected_master_rows = updated_old + truly_new
    plan.after_count = before_count + len(truly_new)
    plan.after_mpns = before_mpns | {(m or "").strip().upper() for m in plan.new_mpns}
    return plan


# --------------------------------------------------------------------------
# stage_master — atomic append under human approval (the only MASTER writer)
# --------------------------------------------------------------------------
def stage_master(plan, master_path, approved_by, backup_dir=None,
                 allow_row_update_mpns=None):
    """Append plan.new_rows to master_path atomically.

    Pre-conditions (any failure -> ReleaseStop, MASTER untouched):
      * approved_by is truthy (human gate)
      * plan.gate_ok
      * master still matches plan.before_sha256 (no concurrent modification)
    A backup copy is taken before the write so a later consistency failure can
    roll back.

    ``allow_row_update_mpns`` is forwarded to master_io so MPNs explicitly
    authorised by the human may be corrected in place; the default (None)
    keeps MASTER append-only.
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
        written = master_io.append_rows_atomically(
            master_path, plan.new_rows,
            expected_cols=MASTER_COLS,
            allow_row_update_mpns=allow_row_update_mpns)
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
        new_mpns=plan.new_mpns,
        old_rows_unchanged=not plan.update_mpns, atomic_write=True,
        authorised_row_updates=int((written or {}).get("authorised_row_updates", 0)
                                  or 0))


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
