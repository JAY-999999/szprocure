"""Batch Manifest: per-batch JSON state record (Phase P0 — JSON only, no DB).

Crash-safety contract
---------------------
* The manifest is rewritten **atomically after every step** (temp file in the
  same directory, then os.replace). If the process dies mid-write the previous
  manifest survives — a crashed batch never loses its whole state.
* Every transition is validated against an explicit state machine, so a
  partially-run batch cannot jump to a nonsensical status.
* `content_enrichment` is a RESERVED field only (default NOT_AVAILABLE).
  PDF -> article generation is NOT implemented in this phase and can never
  block the pipeline.
"""
import json
import os
import tempfile
from datetime import datetime

from . import DEFAULT_BATCH_ROOT
from .ids import validate_batch_id, manifest_path

SCHEMA_VERSION = 1

# ---- state machine -------------------------------------------------------
INTAKE = "INTAKE"
QUALIFICATION = "QUALIFICATION"
DEDUP = "DEDUP"
BACKUP = "BACKUP"
PROCESSING = "PROCESSING"
BUILT = "BUILT"
AUDITED = "AUDITED"
READY_FOR_RELEASE = "READY_FOR_RELEASE"
APPROVED = "APPROVED"
DEPLOYED = "DEPLOYED"
FAILED = "FAILED"

ALLOWED_TRANSITIONS = {
    INTAKE: {QUALIFICATION, FAILED},
    QUALIFICATION: {DEDUP, FAILED},
    DEDUP: {BACKUP, FAILED},
    BACKUP: {PROCESSING, FAILED},
    PROCESSING: {BUILT, FAILED},
    BUILT: {AUDITED, FAILED},
    AUDITED: {READY_FOR_RELEASE, FAILED},
    READY_FOR_RELEASE: {APPROVED, FAILED},
    APPROVED: {DEPLOYED, FAILED},
    DEPLOYED: set(),
    FAILED: {INTAKE},          # allow a retry to restart from INTAKE
}

TERMINAL_STATES = {DEPLOYED}

# Exception severities (see gate.py for the code -> severity table)
AUTO_PASS = "AUTO_PASS"
AUTO_SKIP = "AUTO_SKIP"
WARNING = "WARNING"
STOP = "STOP"


class ManifestError(Exception):
    pass


class BatchManifest:
    """Atomic, crash-safe per-batch manifest."""

    def __init__(self, data, root=None):
        self._root = root or DEFAULT_BATCH_ROOT
        self.data = data

    # ---------- construction -------------------------------------------
    @classmethod
    def create(cls, batch_id, strategy, category=None, root=None, **extra):
        validate_batch_id(batch_id)
        now = _now()
        data = {
            "batch_id": batch_id,
            "schema_version": SCHEMA_VERSION,
            "strategy": str(strategy).strip().upper(),
            "category": category,
            "status": INTAKE,
            "counts": {
                "input_count": 0, "new_sku_count": 0, "duplicate_count": 0,
                "rejected_count": 0, "datasheet_found": 0, "datasheet_missing": 0,
                "r2_uploaded": 0, "r2_skipped": 0, "r2_failed": 0,
            },
            "stage_log": [],
            "exceptions": [],
            "backup": {"taken": False, "path": None, "files": 0, "verified": False},
            "master": {"before": None, "after": None, "atomic_write": False,
                       "old_rows_unchanged": None},
            "publish": {"rows": None, "residual_cjk": None},
            "build": {"pages": None, "parts_json": None, "sitemap_urls": None,
                      "reconciled": False},
            "audit": {"batch_audit": None, "full_audit": None},
            # RESERVED for a future PDF->article pipeline. Never blocks.
            "content_enrichment": "NOT_AVAILABLE",
            "skus": [],
            "git": {"commit": None, "pushed": False},
            "created_at": now,
            "updated_at": now,
        }
        data.update(extra)
        m = cls(data, root=root)
        m.save()
        return m

    @classmethod
    def load(cls, batch_id, root=None):
        p = manifest_path(batch_id, root or DEFAULT_BATCH_ROOT)
        if not os.path.exists(p):
            raise ManifestError(f"manifest not found: {p}")
        with open(p, encoding="utf-8") as f:
            return cls(json.load(f), root=root)

    # ---------- persistence (atomic) -----------------------------------
    @property
    def path(self):
        return manifest_path(self.data["batch_id"], self._root)

    def save(self):
        """Write the manifest atomically: temp (same dir) -> os.replace."""
        os.makedirs(self._root, exist_ok=True)
        self.data["updated_at"] = _now()
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        target = self.path
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target) or ".",
                                   prefix=".manifest_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            # sanity: the temp file must parse back before we swap it in
            with open(tmp, encoding="utf-8") as f:
                json.load(f)
            os.replace(tmp, target)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ---------- state machine ------------------------------------------
    @property
    def status(self):
        return self.data.get("status")

    def set_status(self, new_status, note=None):
        cur = self.status
        if new_status not in ALLOWED_TRANSITIONS:
            raise ManifestError(f"unknown status {new_status!r}")
        if cur != new_status and new_status not in ALLOWED_TRANSITIONS.get(cur, set()):
            raise ManifestError(
                f"illegal transition {cur} -> {new_status} "
                f"(allowed: {sorted(ALLOWED_TRANSITIONS.get(cur, set()))})")
        self.data["status"] = new_status
        self.record_stage(new_status, ok=(new_status != FAILED), note=note)
        self.save()

    # ---------- logging -------------------------------------------------
    def record_stage(self, stage, ok=True, note=None):
        self.data.setdefault("stage_log", []).append(
            {"stage": stage, "at": _now(), "ok": bool(ok), "note": note})
        self.data["updated_at"] = _now()

    def add_exception(self, code, severity, mpn=None, message=""):
        self.data.setdefault("exceptions", []).append(
            {"code": code, "severity": severity, "mpn": mpn,
             "message": message, "at": _now()})
        self.data["updated_at"] = _now()

    def bump(self, key, n=1):
        c = self.data.setdefault("counts", {})
        c[key] = c.get(key, 0) + n
        self.data["updated_at"] = _now()

    def set_counts(self, **kw):
        self.data.setdefault("counts", {}).update(kw)
        self.data["updated_at"] = _now()

    def add_skus(self, skus):
        """skus: list of dicts. content_enrichment defaults to NOT_AVAILABLE."""
        for s in skus:
            s.setdefault("content_enrichment", "NOT_AVAILABLE")
        self.data.setdefault("skus", []).extend(skus)

    # ---------- queries -------------------------------------------------
    def exceptions_by_severity(self, severity):
        return [e for e in self.data.get("exceptions", []) if e.get("severity") == severity]

    def has_stop(self):
        return bool(self.exceptions_by_severity(STOP))

    def summary(self):
        return {
            "batch_id": self.data["batch_id"],
            "status": self.status,
            "counts": self.data.get("counts", {}),
            "backup": self.data.get("backup", {}),
            "exceptions": len(self.data.get("exceptions", [])),
            "stops": len(self.exceptions_by_severity(STOP)),
            "warnings": len(self.exceptions_by_severity(WARNING)),
            "content_enrichment": self.data.get("content_enrichment"),
        }


def _now():
    return datetime.now().isoformat(timespec="seconds")
