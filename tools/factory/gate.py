"""Exception Gate — one place that decides PASS / SKIP / WARN / STOP.

Principle: ordinary problems must never block the pipeline. Only data
pollution, data corruption, mass anomalies and critical gate failures STOP.

Mapping (from the approved Implementation Plan §I):

  AUTO SKIP   DUPLICATE_SKIP, DATASHEET_404, DATASHEET_INVALID,
              DATASHEET_TIMEOUT, SYNTHETIC_MPN, SOURCE_MISSING,
              R2_UPLOAD_ERROR, R2_TIMEOUT, R2_NETWORK
  AUTO PASS   R2_OBJECT_EXISTS
  WARNING     BRAND_UNMAPPED, SPEC_THIN, ATTR_UNKNOWN, EXEMPT_100MHZ,
              RELATED_DRIFT, DATASHEET_THIN
  STOP        NO_BACKUP, BACKUP_VERIFY_FAIL, DUPLICATE_ABORT,
              BATCH_SELF_DUPLICATE, MASS_DUPLICATE, MASTER_CORRUPT,
              CJK_LEAK, LCSC_LEAK, BUILD_COUNT_MISMATCH, R2_MASS_FAIL,
              R2_AUTH_FAILED, LOCAL_HASH_UNSTABLE, REMOTE_HASH_MISMATCH,
              REMOTE_VERIFY_FAILED, POOL_WRITE_FAIL, AUDIT_FAIL,
              MANIFEST_ERROR
"""
from datetime import datetime

AUTO_PASS = "AUTO_PASS"
AUTO_SKIP = "AUTO_SKIP"
WARNING = "WARNING"
STOP = "STOP"

SEVERITIES = (AUTO_PASS, AUTO_SKIP, WARNING, STOP)

# --- codes -----------------------------------------------------------------
DUPLICATE_SKIP = "DUPLICATE_SKIP"
DATASHEET_404 = "DATASHEET_404"
DATASHEET_INVALID = "DATASHEET_INVALID"
DATASHEET_TIMEOUT = "DATASHEET_TIMEOUT"
DATASHEET_THIN = "DATASHEET_THIN"

R2_OBJECT_EXISTS = "R2_OBJECT_EXISTS"

# --- P1-A Product Data -----------------------------------------------------
# A candidate that looks machine-generated is dropped from the batch
# (AUTO_SKIP) rather than aborting it, because one bad source row is not
# evidence that the whole harvest is broken.
SYNTHETIC_MPN = "SYNTHETIC_MPN"
SOURCE_MISSING = "SOURCE_MISSING"
# The local pool is the Factory's warehouse. Losing writes there corrupts the
# inventory silently -> STOP.
POOL_WRITE_FAIL = "POOL_WRITE_FAIL"

# --- P1-C R2 Asset ---------------------------------------------------------
# Per-object upload problems are isolated (AUTO_SKIP); the batch-level
# R2_MASS_FAIL gate is what turns a systemic failure into a STOP.
R2_UPLOAD_ERROR = "R2_UPLOAD_ERROR"
R2_TIMEOUT = "R2_TIMEOUT"
R2_NETWORK = "R2_NETWORK"
# Integrity / authentication problems are always STOP: they mean we cannot
# prove what is in the bucket, or we must not touch it at all.
R2_AUTH_FAILED = "R2_AUTH_FAILED"
LOCAL_HASH_UNSTABLE = "LOCAL_HASH_UNSTABLE"
REMOTE_HASH_MISMATCH = "REMOTE_HASH_MISMATCH"
REMOTE_VERIFY_FAILED = "REMOTE_VERIFY_FAILED"

BRAND_UNMAPPED = "BRAND_UNMAPPED"
SPEC_THIN = "SPEC_THIN"
ATTR_UNKNOWN = "ATTR_UNKNOWN"
EXEMPT_100MHZ = "EXEMPT_100MHZ"
RELATED_DRIFT = "RELATED_DRIFT"

# P1-E: a SKU whose category could not be mapped to any adapter is kept in
# the candidate pool but flagged for human review and must NOT be auto-released.
UNMAPPED_CATEGORY = "UNMAPPED_CATEGORY"

NO_BACKUP = "NO_BACKUP"
BACKUP_VERIFY_FAIL = "BACKUP_VERIFY_FAIL"
DUPLICATE_ABORT = "DUPLICATE_ABORT"
BATCH_SELF_DUPLICATE = "BATCH_SELF_DUPLICATE"
MASS_DUPLICATE = "MASS_DUPLICATE"
MASTER_CORRUPT = "MASTER_CORRUPT"
CJK_LEAK = "CJK_LEAK"
LCSC_LEAK = "LCSC_LEAK"
BUILD_COUNT_MISMATCH = "BUILD_COUNT_MISMATCH"
R2_MASS_FAIL = "R2_MASS_FAIL"
AUDIT_FAIL = "AUDIT_FAIL"
MANIFEST_ERROR = "MANIFEST_ERROR"

CODE_SEVERITY = {
    DUPLICATE_SKIP: AUTO_SKIP,
    DATASHEET_404: AUTO_SKIP,
    DATASHEET_INVALID: AUTO_SKIP,
    DATASHEET_TIMEOUT: AUTO_SKIP,

    R2_OBJECT_EXISTS: AUTO_PASS,

    SYNTHETIC_MPN: AUTO_SKIP,
    SOURCE_MISSING: AUTO_SKIP,
    R2_UPLOAD_ERROR: AUTO_SKIP,
    R2_TIMEOUT: AUTO_SKIP,
    R2_NETWORK: AUTO_SKIP,

    R2_AUTH_FAILED: STOP,
    LOCAL_HASH_UNSTABLE: STOP,
    REMOTE_HASH_MISMATCH: STOP,
    REMOTE_VERIFY_FAILED: STOP,

    BRAND_UNMAPPED: WARNING,
    SPEC_THIN: WARNING,
    ATTR_UNKNOWN: WARNING,
    EXEMPT_100MHZ: WARNING,
    RELATED_DRIFT: WARNING,
    DATASHEET_THIN: WARNING,
    UNMAPPED_CATEGORY: WARNING,

    NO_BACKUP: STOP,
    BACKUP_VERIFY_FAIL: STOP,
    DUPLICATE_ABORT: STOP,
    BATCH_SELF_DUPLICATE: STOP,
    MASS_DUPLICATE: STOP,
    MASTER_CORRUPT: STOP,
    CJK_LEAK: STOP,
    LCSC_LEAK: STOP,
    BUILD_COUNT_MISMATCH: STOP,
    R2_MASS_FAIL: STOP,
    POOL_WRITE_FAIL: STOP,
    AUDIT_FAIL: STOP,
    MANIFEST_ERROR: STOP,
}

# Process exit codes (stable, machine-readable)
EXIT_OK = 0
EXIT_STOP = 10
EXIT_INTERNAL = 11


class Gate:
    """Collects exceptions and answers: may the pipeline continue?"""

    def __init__(self):
        self.items = []

    def add(self, code, mpn=None, message="", severity=None):
        sev = severity or CODE_SEVERITY.get(code)
        if sev is None:
            raise KeyError(f"unknown exception code: {code}")
        item = {"code": code, "severity": sev, "mpn": mpn,
                "message": message, "at": datetime.now().isoformat(timespec="seconds")}
        self.items.append(item)
        return item

    def extend(self, items):
        for it in items:
            self.add(it["code"], it.get("mpn"), it.get("message", ""), it.get("severity"))

    def by_severity(self, sev):
        return [i for i in self.items if i["severity"] == sev]

    @property
    def stops(self):
        return self.by_severity(STOP)

    @property
    def warnings(self):
        return self.by_severity(WARNING)

    @property
    def skipped(self):
        return self.by_severity(AUTO_SKIP)

    def must_stop(self):
        return bool(self.stops)

    def evaluate(self):
        """Return a decision dict; exit_code is what the CLI should use."""
        return {
            "continue": not self.must_stop(),
            "exit_code": EXIT_STOP if self.must_stop() else EXIT_OK,
            "total": len(self.items),
            "stop": len(self.stops),
            "warning": len(self.warnings),
            "skip": len(self.skipped),
            "stops_detail": self.stops,
        }

    def print_report(self, stream=None):
        import sys
        out = stream or sys.stdout
        dec = self.evaluate()
        out.write("=== EXCEPTION GATE ===\n")
        out.write(f"  total={dec['total']}  stop={dec['stop']}  "
                  f"warning={dec['warning']}  skip={dec['skip']}\n")
        for i in self.items:
            mpn = f" [{i['mpn']}]" if i.get("mpn") else ""
            out.write(f"  {i['severity']:<10} {i['code']}{mpn}: {i['message']}\n")
        out.write(f"  DECISION: {'STOP' if dec['stop'] else 'CONTINUE'}"
                  f" (exit {dec['exit_code']})\n")
        return dec


def severity_of(code):
    return CODE_SEVERITY.get(code)
