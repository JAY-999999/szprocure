"""P1-D-C — Wave / Slice Orchestrator (Ingest-Once → Immutable RAW Pool → Slice).

This is the UPPER orchestration layer that sits ON TOP of the frozen,
single-batch pipeline (batch_runner + stages + product_data + datasheet + r2).

Design (from the confirmed P1-D-C READ-ONLY GAP Report)
-------------------------------------------------------
* INGEST ONCE: the full RAW source is scanned exactly once.  The filtered,
  de-duplicated, MASTER-excluded result is materialised as an IMMUTABLE
  ``_all.csv`` inside ``<pool_root>/raw_pool/<wave_id>/``.  A second ingest()
  call is a no-op (does NOT re-scan RAW, does NOT regenerate slices).
* PRE-MATERIALISED CSV SLICES: ``plan_slices()`` splits ``_all.csv`` into
  ``slice_NNNN.csv`` files of ``slice_size`` rows each.  Each slice is a fully
  self-contained RAW fragment that the existing ``product_data.intake()`` can
  read directly — so NO ``offset`` parameter is added to the lower layers and
  NO frozen code is touched.
* PROCESS-BY-SLICE: each slice is an independent Batch driven through the
  existing ``batch_runner.run()`` with batch_id ``<wave_id>_<idx>``
  (YYYYMMDD_STRATEGY_idx — ids.py is unchanged).
* SLICE STATE MACHINE:  PLANNED -> READY -> RUNNING -> READY_FOR_RELEASE,
  with FAILED / SKIPPED as exceptions.  State lives in ``wave_manifest.json``
  which is INDEPENDENT of the per-batch BatchManifest schema (no Manifest
  schema change).
* RESUME: a failed / partial slice can be re-run alone via ``resume_slice()``;
  a READY slice is never re-processed (idempotent skip).
* WAVE REPORT: aggregates the existing per-slice
  ``datasheet_download_report.jsonl`` + ``r2_upload_report.jsonl`` + per-batch
  manifests into one 12-category report.

Hard boundaries (do NOT cross in P1-D-C)
----------------------------------------
* Production MASTER is READ-ONLY.  It is read only to seed the exclude set
  (dedup.load_master_mpns).  slice_planner NEVER writes MASTER.
* None of the frozen layers are modified: gen_parts.py, publish_normalizer.py,
  build_datasheet_map.py, pre_deploy_audit.py — and the lower factory modules
  batch_runner / stages / product_data / datasheet / r2 / manifest / ids are
  invoked, never edited.
* run() never builds / commits / pushes / deploys; release() is a separate
  human gate and is never called here.
"""
from __future__ import annotations

import csv
import json
import os
from datetime import datetime

from . import ids, manifest as MAN, gate, pool, dedup, product_data
from . import batch_runner as br

__all__ = [
    "SlicePlannerError", "IngestResult",
    "make_wave_id", "ingest", "plan_slices",
    "process_slice", "process_wave", "resume_slice", "wave_report",
]

# ---- wave / slice states (wave_manifest.json, independent of BatchManifest) --
WAVE_PLANNED = "PLANNED"
WAVE_INGESTED = "INGESTED"
WAVE_RUNNING = "RUNNING"
WAVE_READY = "READY"
WAVE_FAILED = "FAILED"

SLICE_PLANNED = "PLANNED"
SLICE_READY = "READY"
SLICE_RUNNING = "RUNNING"
SLICE_READY_FOR_RELEASE = "READY_FOR_RELEASE"
SLICE_FAILED = "FAILED"
SLICE_SKIPPED = "SKIPPED"

# default run params (per GAP report; workers=3 == per-host concurrency <=3)
DEF_SLICE_SIZE = 500
DEF_WORKERS = 3
DEF_RETRIES = 5
DEF_TIMEOUT = 60
DEF_BACKOFF = 2
DEF_REQUIRE_DS = True


class SlicePlannerError(Exception):
    pass


def _now():
    return datetime.now().isoformat(timespec="seconds")


def make_wave_id(strategy, when=None):
    """wave_id = YYYYMMDD_STRATEGY (e.g. 20260830_OPAMP)."""
    when = when or datetime.now()
    strat = str(strategy).strip().upper()
    return f"{when:%Y%m%d}_{strat}"


def wave_root(pool_root=None, wave_id=None):
    pool_root = pool.pool_root(pool_root)
    if wave_id is None:
        return os.path.join(pool_root, "raw_pool")
    return os.path.join(pool_root, "raw_pool", wave_id)


def wave_manifest_path(pool_root=None, wave_id=None):
    return os.path.join(wave_root(pool_root, wave_id), "wave_manifest.json")


def _read_wave_manifest(path):
    if not os.path.exists(path):
        raise SlicePlannerError(f"wave manifest not found: {path}")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_wave_manifest(path, wm):
    return pool.atomic_write_json(path, wm)


def _slices_present(wm, wr):
    for sl in wm.get("slices", []):
        p = sl.get("path")
        if not p or not os.path.exists(p):
            return False
    return True


def _find_slice(wm, slice_idx):
    for sl in wm.get("slices", []):
        if sl.get("slice_idx") == slice_idx:
            return sl
    return None


def _materialize_slices(wave_id, wr, rows, fieldnames, slice_size):
    """Write slice_NNNN.csv files.  Returns the slice-entry list (state PLANNED)."""
    slices = []
    n = len(rows)
    n_slices = (n + slice_size - 1) // slice_size if n else 0
    for i in range(n_slices):
        idx = i + 1
        chunk = rows[i * slice_size:(i + 1) * slice_size]
        path = os.path.join(wr, f"slice_{idx:04d}.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in chunk:
                w.writerow(r)
        slices.append({
            "slice_idx": idx,
            "batch_id": f"{wave_id}_{idx}",
            "path": path,
            "mpn_count": len(chunk),
            "state": SLICE_PLANNED,
            "batch_status": None,
            "release_eligible": False,
            "started_at": None,
            "finished_at": None,
        })
    return slices


# =====================================================================
# ingest — scan RAW once -> immutable _all.csv -> slices (idempotent)
# =====================================================================
class IngestResult:
    def __init__(self):
        self.wave_id = None
        self.idempotent = False
        self.input_total = 0
        self.selected = 0
        self.excluded = 0
        self.internal_dups = 0
        self.no_datasheet = 0
        self.slice_count = 0
        self.slice_size = DEF_SLICE_SIZE
        self.all_csv = None

    def summary(self):
        return {
            "wave_id": self.wave_id,
            "idempotent": self.idempotent,
            "input_total": self.input_total,
            "selected": self.selected,
            "excluded": self.excluded,
            "internal_dups": self.internal_dups,
            "no_datasheet": self.no_datasheet,
            "slice_count": self.slice_count,
            "slice_size": self.slice_size,
            "all_csv": self.all_csv,
        }


def ingest(raw_source=None, strategy="WAVE", when=None, selector=None,
           category=None, exclude_mpns=None, master_csv=None, limit=None,
           require_datasheet=DEF_REQUIRE_DS, slice_size=DEF_SLICE_SIZE,
           pool_root=None, root=None):
    """Scan the RAW source ONCE, materialise an immutable ``_all.csv`` plus
    pre-materialised slice CSVs, and write ``wave_manifest.json``.

    Idempotent: a second call with an already-ingested wave returns the prior
    result WITHOUT re-scanning RAW or regenerating slices (the RAW source may
    even be deleted between calls and ingest() still succeeds from the cached
    ``_all.csv``).
    """
    when = when or datetime.now()
    strategy = str(strategy).strip().upper()
    wave_id = make_wave_id(strategy, when)
    pool_root = pool.pool_root(pool_root)
    root = root or MAN.DEFAULT_BATCH_ROOT
    wr = wave_root(pool_root, wave_id)
    all_csv = os.path.join(wr, "_all.csv")
    wm_path = wave_manifest_path(pool_root, wave_id)

    # ---- idempotency: already ingested -> no re-scan, no re-gen ------------
    if os.path.exists(all_csv) and os.path.exists(wm_path):
        wm = _read_wave_manifest(wm_path)
        if wm.get("status") in (WAVE_INGESTED, WAVE_RUNNING, WAVE_READY,
                                 WAVE_FAILED) and _slices_present(wm, wr):
            res = IngestResult()
            res.wave_id = wave_id
            res.idempotent = True
            ig = wm.get("ingest", {})
            res.input_total = ig.get("input_total", 0)
            res.selected = ig.get("selected", 0)
            res.excluded = ig.get("exclude_count", 0)
            res.internal_dups = ig.get("internal_dups", 0)
            res.no_datasheet = ig.get("no_datasheet", 0)
            res.slice_count = len(wm.get("slices", []))
            res.slice_size = wm.get("slice_size", slice_size)
            res.all_csv = all_csv
            return res

    # ---- first ingest: scan RAW once ---------------------------------------
    raw_source = raw_source or product_data.DEFAULT_RAW_SOURCE
    if not os.path.exists(raw_source):
        raise SlicePlannerError(f"raw source not found: {raw_source}")

    master_set = set()
    if master_csv and os.path.exists(master_csv):
        master_set |= {m.strip().upper() for m in dedup.load_master_mpns(master_csv)}
    if exclude_mpns:
        master_set |= {m.strip().upper() for m in exclude_mpns}
    sel = {m.strip().upper() for m in selector} if selector else None
    cat = (category or "").strip().lower() or None

    os.makedirs(wr, exist_ok=True)
    with open(raw_source, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows, seen = [], set()
        input_total = excluded = internal_dups = no_ds = 0
        for r in reader:
            mpn = (r.get("mpn") or "").strip()
            if not mpn:
                continue
            input_total += 1
            if sel is not None and mpn.upper() not in sel:
                continue
            if master_set and mpn.upper() in master_set:
                excluded += 1
                continue
            if cat and (r.get("category") or "").strip().lower() != cat:
                continue
            if require_datasheet and not (r.get("source_datasheet_url") or "").strip():
                no_ds += 1
                continue
            key = mpn.upper()
            if key in seen:
                internal_dups += 1
                continue
            seen.add(key)
            rows.append(r)
            if limit and len(rows) >= limit:
                break

    # immutable _all.csv
    with open(all_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    slices = _materialize_slices(wave_id, wr, rows, fieldnames, slice_size)

    wm = {
        "wave_id": wave_id,
        "schema_version": 1,
        "strategy": strategy,
        "date": f"{when:%Y%m%d}",
        "raw_source": os.path.abspath(raw_source),
        "slice_size": slice_size,
        "status": WAVE_INGESTED,
        "ingest": {
            "input_total": input_total,
            "selected": len(rows),
            "exclude_count": excluded,
            "internal_dups": internal_dups,
            "no_datasheet": no_ds,
            "category": category,
            "require_datasheet": bool(require_datasheet),
            "all_csv": all_csv,
        },
        "slices": slices,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _write_wave_manifest(wm_path, wm)

    res = IngestResult()
    res.wave_id = wave_id
    res.idempotent = False
    res.input_total = input_total
    res.selected = len(rows)
    res.excluded = excluded
    res.internal_dups = internal_dups
    res.no_datasheet = no_ds
    res.slice_count = len(slices)
    res.slice_size = slice_size
    res.all_csv = all_csv
    return res


# =====================================================================
# plan_slices — (re)materialise slice CSVs; usually called inside ingest()
# =====================================================================
def plan_slices(wave_id, slice_size=None, pool_root=None, root=None, force=False):
    """Split the wave's ``_all.csv`` into ``slice_NNNN.csv`` files.

    Idempotent: if the slices already exist and the count is unchanged the
    existing plan is returned without rewriting anything (``force`` overrides).
    """
    pool_root = pool.pool_root(pool_root)
    wr = wave_root(pool_root, wave_id)
    all_csv = os.path.join(wr, "_all.csv")
    wm_path = wave_manifest_path(pool_root, wave_id)
    if not os.path.exists(all_csv):
        raise SlicePlannerError(
            f"wave {wave_id} not ingested: {all_csv} missing; run ingest() first")
    wm = _read_wave_manifest(wm_path) if os.path.exists(wm_path) else None
    if slice_size is None:
        slice_size = (wm or {}).get("slice_size", DEF_SLICE_SIZE)

    with open(all_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    if not force and wm and _slices_present(wm, wr) \
            and len(wm.get("slices", [])) == ((len(rows) + slice_size - 1) // slice_size):
        return {"wave_id": wave_id, "slice_size": slice_size,
                "slice_count": len(wm["slices"]), "slices": wm["slices"],
                "idempotent": True}

    slices = _materialize_slices(wave_id, wr, rows, fieldnames, slice_size)
    if wm is None:
        wm = {
            "wave_id": wave_id, "schema_version": 1,
            "strategy": wave_id.split("_")[1], "date": wave_id.split("_")[0],
            "slice_size": slice_size, "status": WAVE_INGESTED,
            "ingest": {}, "slices": slices,
            "created_at": _now(), "updated_at": _now(),
        }
    else:
        wm["slices"] = slices
        wm["slice_size"] = slice_size
        wm["status"] = WAVE_INGESTED
        wm["updated_at"] = _now()
    _write_wave_manifest(wm_path, wm)
    return {"wave_id": wave_id, "slice_size": slice_size,
            "slice_count": len(slices), "slices": slices, "idempotent": False}


# =====================================================================
# process_slice — run one slice as an independent Batch (idempotent)
# =====================================================================
def process_slice(wave_id, slice_idx, pool_root=None, root=None, backup_root=None,
                  master_csv=None, mfr_csv=None, exclude_mpns=None,
                  r2_client=None, r2_bucket=None, r2_creds=None, r2_endpoint=None,
                  workers=DEF_WORKERS, retries=DEF_RETRIES, timeout=DEF_TIMEOUT,
                  backoff=DEF_BACKOFF, require_datasheet=None,
                  dry_run=False, live_r2=False, force=False):
    """Process a single slice as its own Batch through batch_runner.run().

    Idempotent: a slice whose manifest is already READY_FOR_RELEASE (or beyond)
    is skipped (returned, not re-run).  ``force=True`` re-runs it anyway.
    """
    pool_root = pool.pool_root(pool_root)
    root = root or MAN.DEFAULT_BATCH_ROOT
    backup_root = backup_root or os.path.join(root, "backups")
    wm_path = wave_manifest_path(pool_root, wave_id)
    wm = _read_wave_manifest(wm_path)
    sl = _find_slice(wm, slice_idx)
    if sl is None:
        raise SlicePlannerError(f"slice {slice_idx} not found in wave {wave_id}")

    batch_id = sl["batch_id"]
    bid_exists = ids.batch_exists(batch_id, root)

    # idempotency: READY (or released) slices are never re-processed
    if not force and bid_exists:
        m = MAN.BatchManifest.load(batch_id, root)
        if m.status in (MAN.READY_FOR_RELEASE, MAN.APPROVED, MAN.DEPLOYED):
            sl["state"] = (SLICE_READY_FOR_RELEASE
                           if m.status == MAN.READY_FOR_RELEASE else m.status)
            sl["batch_status"] = m.status
            sl["release_eligible"] = (m.status == MAN.READY_FOR_RELEASE)
            _write_wave_manifest(wm_path, wm)
            rr = br.RunResult(batch_id)
            rr.ok = True
            rr.manifest_status = m.status
            rr.release_eligible = (m.status == MAN.READY_FOR_RELEASE)
            return rr

    sl["state"] = SLICE_RUNNING
    sl["started_at"] = _now()
    _write_wave_manifest(wm_path, wm)

    opts = {
        "source_path": sl["path"],
        "master_csv": master_csv,
        "mfr_csv": mfr_csv,
        "require_datasheet": (bool(require_datasheet)
                              if require_datasheet is not None else DEF_REQUIRE_DS),
        "exclude_mpns": exclude_mpns,
        "workers": workers,
        "retries": retries,
        "timeout": timeout,
        "backoff": backoff,
        "r2_client": r2_client,
        "r2_bucket": r2_bucket,
        "r2_creds": r2_creds,
        "r2_endpoint": r2_endpoint,
        "dry_run": dry_run,
        "live_r2": live_r2,
    }
    rr = br.run(batch_id=batch_id, strategy=wm["strategy"],
                create=not bid_exists, root=root, pool_root=pool_root,
                backup_root=backup_root, options=opts)

    if rr.ok:
        sl["state"] = SLICE_READY_FOR_RELEASE
        sl["release_eligible"] = True
    else:
        sl["state"] = SLICE_FAILED
    sl["batch_status"] = rr.manifest_status
    sl["finished_at"] = _now()
    _write_wave_manifest(wm_path, wm)
    return rr


# =====================================================================
# process_wave — ingest -> plan -> process all slices (sequential)
# =====================================================================
def process_wave(raw_source=None, strategy="WAVE", when=None, selector=None,
                 category=None, exclude_mpns=None, master_csv=None, mfr_csv=None,
                 slice_size=DEF_SLICE_SIZE, pool_root=None, root=None,
                 backup_root=None, r2_client=None, r2_bucket=None, r2_creds=None,
                 r2_endpoint=None, workers=DEF_WORKERS, retries=DEF_RETRIES,
                 timeout=DEF_TIMEOUT, backoff=DEF_BACKOFF,
                 require_datasheet=DEF_REQUIRE_DS, dry_run=False, live_r2=False,
                 parallel=1, limit=None):
    """Full wave orchestration: ingest once, plan slices, process each slice.

    Idempotent across calls — already-READY slices are skipped and only failed
    / partial slices are retried.  Never builds / commits / pushes / deploys.
    """
    res = ingest(raw_source=raw_source, strategy=strategy, when=when,
                 selector=selector, category=category, exclude_mpns=exclude_mpns,
                 master_csv=master_csv, limit=limit,
                 require_datasheet=require_datasheet, slice_size=slice_size,
                 pool_root=pool_root, root=root)
    wave_id = res.wave_id
    wm_path = wave_manifest_path(pool_root, wave_id)
    wm = _read_wave_manifest(wm_path)
    wm["status"] = WAVE_RUNNING
    _write_wave_manifest(wm_path, wm)

    results = []
    for sl in wm["slices"]:
        rr = process_slice(
            wave_id, sl["slice_idx"], pool_root=pool_root, root=root,
            backup_root=backup_root, master_csv=master_csv, mfr_csv=mfr_csv,
            exclude_mpns=exclude_mpns, r2_client=r2_client, r2_bucket=r2_bucket,
            r2_creds=r2_creds, r2_endpoint=r2_endpoint, workers=workers,
            retries=retries, timeout=timeout, backoff=backoff,
            require_datasheet=require_datasheet, dry_run=dry_run, live_r2=live_r2)
        results.append((sl["slice_idx"], rr.ok, rr.manifest_status))

    wm = _read_wave_manifest(wm_path)
    ready = sum(1 for s in wm["slices"] if s["state"] == SLICE_READY_FOR_RELEASE)
    failed = sum(1 for s in wm["slices"] if s["state"] == SLICE_FAILED)
    total = len(wm["slices"])
    if failed == 0 and ready == total:
        wm["status"] = WAVE_READY
    elif failed:
        wm["status"] = WAVE_FAILED
    else:
        wm["status"] = WAVE_RUNNING
    _write_wave_manifest(wm_path, wm)

    return {
        "wave_id": wave_id,
        "results": results,
        "ready": ready,
        "failed": failed,
        "total": total,
        "status": wm["status"],
    }


# =====================================================================
# resume_slice — re-run a single (failed / partial) slice alone
# =====================================================================
def resume_slice(wave_id, slice_idx, **kw):
    """Re-run one slice in isolation.  ``force=True`` bypasses the idempotent
    skip, so a FAILED / partial slice recovers without touching the others."""
    kw.setdefault("force", True)
    return process_slice(wave_id, slice_idx, **kw)


# =====================================================================
# wave_report — aggregate the 12 required metric categories
# =====================================================================
def _iter_jsonl(path):
    if not os.path.exists(path):
        return
    records, _ = pool.read_jsonl(path)
    for rec in records:
        yield rec


def wave_report(wave_id, pool_root=None, root=None):
    """Aggregate per-slice datasheet / r2 reports + manifests into one report.

    The 12 top-level categories answer:
      1. wave            — identity & config
      2. ingest          — RAW scan outcome (exclude / dups / datasheet filter)
      3. slices          — per-slice state + batch status
      4. datasheet_total — download totals across the wave
      5. r2_total        — upload totals across the wave
      6. datasheet_failures — error_code distribution
      7. r2_failures     — upload outcome / error distribution
      8. idempotency     — evidence that re-runs did NOT re-download / re-upload
      9. pdf_dedup       — content-addressed PDF de-duplication evidence
     10. throughput      — per-slice + wave durations / workers
     11. release_readiness — how many slices are release-eligible / blocked
     12. exceptions      — aggregated STOP / WARNING / AUTO_SKIP counts
    """
    pool_root = pool.pool_root(pool_root)
    root = root or MAN.DEFAULT_BATCH_ROOT
    wm_path = wave_manifest_path(pool_root, wave_id)
    wm = _read_wave_manifest(wm_path)

    ds_total = {"discovered": 0, "verified": 0, "duplicate": 0,
                "failed": 0, "skipped": 0, "bytes_downloaded": 0}
    r2_total = {"total": 0, "uploaded": 0, "already_exists": 0,
                "failed": 0, "new_objects": 0, "bytes_uploaded": 0}
    ds_fail, r2_fail = {}, {}
    per_slice = []
    stops = warns = skips = 0
    blocked = []

    for sl in wm.get("slices", []):
        bid = sl["batch_id"]
        entry = {
            "slice_idx": sl["slice_idx"], "batch_id": bid,
            "mpn_count": sl["mpn_count"], "state": sl["state"],
            "batch_status": sl.get("batch_status"),
        }
        if ids.batch_exists(bid, root):
            m = MAN.BatchManifest.load(bid, root)
            entry["batch_status"] = m.status
            entry["release_eligible"] = (m.status == MAN.READY_FOR_RELEASE)
            stops += len(m.exceptions_by_severity(gate.STOP))
            warns += len(m.exceptions_by_severity(gate.WARNING))
            skips += len(m.exceptions_by_severity(gate.AUTO_SKIP))
            if m.has_stop():
                blocked.append({"batch_id": bid, "status": m.status,
                                "stops": [e.get("code") for e in
                                          m.exceptions_by_severity(gate.STOP)]})

        # datasheet summary (preferred) or fall back to JSONL
        dsum = pool.read_json(pool.summary_path(bid, pool_root), default=None)
        if dsum:
            for k in ("discovered", "verified", "duplicate", "failed", "skipped",
                      "bytes_downloaded"):
                ds_total[k] += dsum.get(k, 0)
            entry["datasheet"] = {k: dsum.get(k) for k in (
                "discovered", "verified", "duplicate", "failed", "skipped",
                "new_downloads", "bytes_downloaded", "duration_s", "workers")}

        # r2 summary
        r2sum = pool.read_json(
            os.path.join(pool.report_dir(bid, pool_root), "r2_batch_summary.json"),
            default=None)
        if r2sum:
            for k in ("total", "uploaded", "already_exists", "failed",
                      "new_objects", "bytes_uploaded"):
                r2_total[k] += r2sum.get(k, 0)
            entry["r2"] = {k: r2sum.get(k) for k in (
                "total", "uploaded", "already_exists", "failed", "new_objects",
                "bytes_uploaded", "duration_s", "workers")}

        # failure classification from the incremental JSONL reports
        for rec in _iter_jsonl(pool.download_report_path(bid, pool_root)):
            ec = rec.get("error_code")
            if ec:
                ds_fail[ec] = ds_fail.get(ec, 0) + 1
        for rec in _iter_jsonl(os.path.join(pool.report_dir(bid, pool_root),
                                            "r2_upload_report.jsonl")):
            oc = rec.get("_outcome") or rec.get("upload_error") or rec.get("status")
            if oc:
                r2_fail[oc] = r2_fail.get(oc, 0) + 1

        per_slice.append(entry)

    ready = sum(1 for s in wm.get("slices", []) if s["state"] == SLICE_READY_FOR_RELEASE)
    total = len(wm.get("slices", []))

    report = {
        "wave": {
            "wave_id": wave_id,
            "strategy": wm.get("strategy"),
            "date": wm.get("date"),
            "raw_source": wm.get("raw_source"),
            "slice_size": wm.get("slice_size"),
            "slice_count": total,
            "status": wm.get("status"),
        },
        "ingest": wm.get("ingest", {}),
        "slices": per_slice,
        "datasheet_total": ds_total,
        "r2_total": r2_total,
        "datasheet_failures": ds_fail,
        "r2_failures": r2_fail,
        "idempotency": {
            "r2_already_exists": r2_total["already_exists"],
            "datasheet_new_downloads_total": sum(
                s.get("datasheet", {}).get("new_downloads", 0) for s in per_slice),
            "note": ("r2_already_exists == r2_total.uploaded on a re-run proves no "
                     "object was re-uploaded; new_downloads == 0 proves no PDF was "
                     "re-downloaded."),
        },
        "pdf_dedup": {
            "content_shared_pdfs": ds_total["duplicate"],
            "note": ("two MPNs resolving to the same datasheet share one physical "
                     "file (datasheets/pdf/<aa>/<sha256>.pdf); duplicate counts the "
                     "hash-shared secondaries."),
        },
        "throughput": {
            "per_slice": [
                {"slice_idx": s["slice_idx"],
                 "datasheet_duration_s": s.get("datasheet", {}).get("duration_s"),
                 "r2_duration_s": s.get("r2", {}).get("duration_s"),
                 "datasheet_workers": s.get("datasheet", {}).get("workers"),
                 "r2_workers": s.get("r2", {}).get("workers")}
                for s in per_slice],
        },
        "release_readiness": {
            "ready_for_release": ready,
            "total": total,
            "not_ready": total - ready,
            "blocked_slices": blocked,
        },
        "exceptions": {
            "stop": stops, "warning": warns, "auto_skip": skips,
        },
        "generated_at": _now(),
    }
    return report


# =====================================================================
# minimal CLI (manual use only; never builds/commits/pushes/deploys)
# =====================================================================
def main(argv=None):
    import sys
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: slice_planner <ingest|plan|process|resume|report> ...")
        return 1
    cmd = args[0]
    pool_root = os.environ.get("SZ_POOL_ROOT")
    root = os.environ.get("SZ_BATCH_ROOT")
    if cmd == "ingest":
        r = ingest(strategy=args[1] if len(args) > 1 else "WAVE",
                   pool_root=pool_root, root=root)
        print(json.dumps(r.summary(), ensure_ascii=False, indent=2))
    elif cmd == "report":
        rep = wave_report(args[1], pool_root=pool_root, root=root)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(f"unknown command {cmd!r}")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
