"""Datasheet Factory — batch PDF acquisition into the local pool (Phase P1-B).

Acquisition and publication are fully decoupled:

    candidate pool -> DISCOVERED -> download+verify -> LOCAL PDF POOL
                                                    -> (P1-C) R2 upload
                                                    -> (later) release

Nothing here uploads, builds, or touches production MASTER. The local PDF pool
is a durable asset: it is never deleted after upload, because it will feed the
future Content Factory (PDF -> technical summary / FAQ / social posts).

Concurrency, retry, resume
--------------------------
* ThreadPoolExecutor; the worker count is a parameter (default 8), never a
  constant baked into the logic — 4 / 8 / 16 / 32 all work.
* retry with exponential backoff, HTTP status check, Content-Type check,
  size floor/ceiling, %PDF magic, %%EOF tail check, then SHA256.
* Every download goes to a temp file IN THE SAME DIRECTORY as the final path
  and is only promoted by os.replace after validation, so a failed download
  can never leave a half-written PDF behind.
* Resume is ledger-driven: VERIFIED / DUPLICATE / UPLOADED are skipped,
  FAILED is retried, and a stale DOWNLOADING is reset to DISCOVERED on start
  (a crash can never strand an item in a non-resumable state).
* Results are appended to a JSONL report after EVERY task, so a crash, Ctrl+C
  or network drop loses nothing already done.

Hash de-duplication
-------------------
MPN dedup != PDF dedup. Physical files are stored content-addressed as
datasheets/pdf/<aa>/<sha256>.pdf; two MPNs resolving to the same datasheet
share one physical file while both keep their own ledger entry (the second is
marked DUPLICATE — recorded, never a failure).
"""
import hashlib
import json
import os
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from . import pool

# ---------------------------------------------------------------- tunables --
DEFAULT_WORKERS = 8          # default only; pass workers=4/8/16/32
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 60         # seconds per attempt
DEFAULT_BACKOFF = 1.5        # exponential base
MIN_PDF_BYTES = 1024         # catches empty files and HTML error pages
MAX_PDF_BYTES = 100 * 1024 * 1024
ALLOWED_CONTENT_TYPES = ("application/pdf", "application/octet-stream",
                         "binary/octet-stream", "")
REQUIRE_EOF_MARKER = True    # a PDF without %%EOF was truncated in transfer
LEDGER_CHECKPOINT_EVERY = 25

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# ---------------------------------------------------------------- statuses --
DISCOVERED = "DISCOVERED"
DOWNLOADING = "DOWNLOADING"
DOWNLOADED = "DOWNLOADED"
VERIFIED = "VERIFIED"
DUPLICATE = "DUPLICATE"
FAILED = "FAILED"
SKIPPED = "SKIPPED"
UPLOADED = "UPLOADED"
UPLOAD_FAILED = "UPLOAD_FAILED"

ALL_STATUSES = (DISCOVERED, DOWNLOADING, DOWNLOADED, VERIFIED, DUPLICATE,
                FAILED, SKIPPED, UPLOADED, UPLOAD_FAILED)
# states that mean "work already done, do not re-download"
DONE_STATES = {VERIFIED, DUPLICATE, UPLOADED}
# states that may be retried
RETRYABLE_STATES = {DISCOVERED, FAILED, DOWNLOADING, DOWNLOADED}

# error codes
E_NO_URL = "NO_URL"
E_HTTP = "HTTP_ERROR"
E_TIMEOUT = "TIMEOUT"
E_NETWORK = "NETWORK"
E_CONTENT_TYPE = "CONTENT_TYPE"
E_NOT_PDF = "NOT_PDF"
E_EMPTY = "EMPTY"
E_TOO_SMALL = "TOO_SMALL"
E_TOO_LARGE = "TOO_LARGE"
E_TRUNCATED = "TRUNCATED_PDF"

PDF_MAGIC = b"%PDF"
EOF_MARKER = b"%%EOF"


class DatasheetError(Exception):
    pass


def _now():
    return datetime.now().isoformat(timespec="seconds")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# =====================================================================
# discovery
# =====================================================================
def new_record(mpn, source_url, batch_id, status=DISCOVERED):
    return {
        "mpn": mpn,
        "source_url": source_url or "",
        "local_path": None,
        "file_size": 0,
        "sha256": None,
        "http_status": None,
        "content_type": None,
        "fetched_at": None,
        "retry_count": 0,
        "status": status,
        "error_code": None,
        "batch_id": batch_id,
    }


def discover(batch_id, root=None, rows=None):
    """Build (or refresh) the per-batch asset ledger from the candidate pool.

    rows: optional explicit list of dicts with 'mpn' and '_source_datasheet_url'.
          Defaults to the batch's candidate pool file.
    Existing ledger entries keep their status, so re-running discovery never
    discards work already completed.
    """
    if rows is None:
        payload = pool.read_json(pool.candidates_path(batch_id, root))
        if not payload:
            raise DatasheetError(
                f"candidate pool not found for batch {batch_id}; run product_data.normalize() first")
        rows = payload.get("rows", [])

    ledger = load_ledger(batch_id, root)
    by_mpn = {r["mpn"]: r for r in ledger}

    out = []
    for row in rows:
        mpn = row.get("mpn")
        if not mpn:
            continue
        url = (row.get("_source_datasheet_url") or "").strip()
        prev = by_mpn.get(mpn)
        if prev:
            # keep completed work; only refresh the URL if it was unknown
            if not prev.get("source_url") and url:
                prev["source_url"] = url
            out.append(prev)
            continue
        out.append(new_record(mpn, url, batch_id,
                              DISCOVERED if url else SKIPPED))
    save_ledger(batch_id, out, root)
    return out


def load_ledger(batch_id, root=None):
    """Return the list of asset records.

    The ledger file is a wrapper object ({batch_id, updated_at, records:[...]});
    a bare list is also accepted so hand-written ledgers keep working.
    """
    data = pool.read_json(pool.datasheet_index_path(batch_id, root), default=None)
    if data is None:
        return []
    if isinstance(data, dict):
        return data.get("records", []) or []
    return list(data)


def save_ledger(batch_id, records, root=None):
    path = pool.datasheet_index_path(batch_id, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return pool.atomic_write_json(path, {
        "batch_id": batch_id,
        "updated_at": _now(),
        "records": records,
    })


# =====================================================================
# download
# =====================================================================
class _Shared:
    """Cross-thread state: the hash -> physical-path map and its lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_hash = {}


def _validate(data, content_type):
    """Return (ok, error_code). Validation happens BEFORE anything is renamed."""
    if not data:
        return False, E_EMPTY
    if len(data) < MIN_PDF_BYTES:
        return False, E_TOO_SMALL
    if len(data) > MAX_PDF_BYTES:
        return False, E_TOO_LARGE
    ct = (content_type or "").split(";")[0].strip().lower()
    if ct not in ALLOWED_CONTENT_TYPES:
        return False, E_CONTENT_TYPE
    if data[:4] != PDF_MAGIC:
        return False, E_NOT_PDF
    if REQUIRE_EOF_MARKER and EOF_MARKER not in data[-2048:]:
        return False, E_TRUNCATED
    return True, None


def _fetch_once(url, timeout):
    """Single GET attempt. Returns (data, http_status, content_type)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.lcsc.com/"})
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read(), getattr(r, "status", 200), r.headers.get("Content-Type", "")


def _store(data, batch_id, shared, root=None):
    """Write the PDF content-addressed. Returns (local_path, is_duplicate)."""
    digest = sha256_bytes(data)
    final = pool.pdf_path(digest, root)
    os.makedirs(os.path.dirname(final), exist_ok=True)

    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(final),
                               prefix=".ds_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        with shared.lock:
            if os.path.exists(final) and os.path.getsize(final) == len(data):
                os.unlink(tmp)               # identical bytes already stored
                return final, True
            os.replace(tmp, final)           # atomic promote within same dir
            shared.by_hash[digest] = final
            return final, False
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def download_one(rec, batch_id, shared, root=None, retries=DEFAULT_RETRIES,
                 timeout=DEFAULT_TIMEOUT, backoff=DEFAULT_BACKOFF):
    """Download + verify a single asset. Mutates and returns rec."""
    if rec.get("status") in DONE_STATES:
        return rec                                   # resume: already done

    url = (rec.get("source_url") or "").strip()
    if not url:
        rec.update(status=SKIPPED, error_code=E_NO_URL)
        return rec

    if rec.get("status") == DOWNLOADING:
        rec["status"] = DISCOVERED                   # recover from a crash

    last_err, last_code = "", None
    for attempt in range(1, retries + 1):
        rec["retry_count"] = attempt
        try:
            data, http_status, ctype = _fetch_once(url, timeout)
            rec["http_status"] = http_status
            rec["content_type"] = ctype
            ok, code = _validate(data, ctype)
            if not ok:
                rec.update(status=FAILED, error_code=code)
                return rec
            path, dup = _store(data, batch_id, shared, root)
            rec.update(local_path=path, file_size=len(data),
                       sha256=sha256_bytes(data), fetched_at=_now(),
                       status=DUPLICATE if dup else VERIFIED,
                       error_code=None)
            return rec
        except urllib.error.HTTPError as e:
            last_err, last_code = f"HTTP {e.code}", E_HTTP
            rec["http_status"] = e.code
        except Exception as e:                        # timeout / DNS / reset
            name = type(e).__name__
            last_err = f"{name}: {e}"
            last_code = E_TIMEOUT if "timeout" in name.lower() else E_NETWORK
        if attempt < retries:
            time.sleep(backoff ** attempt)           # exponential backoff
    rec.update(status=FAILED, error_code=last_code or E_NETWORK)
    rec.setdefault("_last_error", last_err)
    return rec


# =====================================================================
# batch orchestration
# =====================================================================
class DownloadResult:
    def __init__(self):
        self.batch_id = None
        self.workers = 0
        self.discovered = self.downloaded = self.verified = 0
        self.duplicate = self.failed = self.skipped = 0
        self.uploaded = self.upload_failed = 0
        self.bytes_downloaded = 0
        self.retry_total = 0
        self.duration = 0.0
        # Cumulative batch totals vs. what THIS run actually fetched. Both are
        # needed: the summary reports the batch state, while `new_downloads`
        # proves a resumed run did not re-download finished work.
        self.new_downloads = 0
        self.report_path = None
        self.summary_path = None
        self.ledger_path = None

    def as_dict(self):
        return {
            "discovered": self.discovered, "downloaded": self.downloaded,
            "verified": self.verified, "duplicate": self.duplicate,
            "failed": self.failed, "skipped": self.skipped,
            "uploaded": self.uploaded, "upload_failed": self.upload_failed,
            "bytes_downloaded": self.bytes_downloaded,
            "duration": round(self.duration, 2), "workers": self.workers,
            "retry_total": self.retry_total,
            "new_downloads": self.new_downloads,
        }


def download_batch(batch_id, root=None, workers=DEFAULT_WORKERS,
                   retries=DEFAULT_RETRIES, timeout=DEFAULT_TIMEOUT,
                   backoff=DEFAULT_BACKOFF, limit=None, manifest=None,
                   on_record=None):
    """Download every non-finished asset in the batch ledger, concurrently."""
    if workers < 1:
        raise DatasheetError("workers must be >= 1")

    records = load_ledger(batch_id, root)
    if not records:
        records = discover(batch_id, root=root)

    pool.ensure_report_dir(batch_id, root)
    report_path = pool.download_report_path(batch_id, root)
    if not os.path.exists(report_path):
        open(report_path, "w", encoding="utf-8").close()

    # resume: recover anything stranded mid-flight by a previous crash
    for r in records:
        if r.get("status") == DOWNLOADING:
            r["status"] = DISCOVERED

    todo = [r for r in records if r.get("status") not in DONE_STATES]
    if limit is not None:
        todo = todo[:limit]

    res = DownloadResult()
    res.batch_id = batch_id
    res.workers = workers
    res.discovered = len(records)

    shared = _Shared()
    t0 = time.time()
    done = 0

    with open(report_path, "a", encoding="utf-8") as rf, \
            ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(download_one, r, batch_id, shared, root,
                          retries, timeout, backoff): r for r in todo}
        for fut in as_completed(futs):
            rec = fut.result()
            # incremental report: one line per completed task, flushed+fsynced
            rf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rf.flush()
            os.fsync(rf.fileno())
            done += 1
            res.retry_total += rec.get("retry_count", 0)
            if rec.get("status") == VERIFIED:
                res.verified += 1
                res.downloaded += 1
                res.new_downloads += 1
                res.bytes_downloaded += rec.get("file_size", 0)
            elif rec.get("status") == DUPLICATE:
                res.duplicate += 1
                res.downloaded += 1
                res.new_downloads += 1
            elif rec.get("status") == FAILED:
                res.failed += 1
            elif rec.get("status") == SKIPPED:
                res.skipped += 1
            if on_record:
                on_record(rec)
            if done % LEDGER_CHECKPOINT_EVERY == 0:
                save_ledger(batch_id, records, root)     # crash checkpoint

    res.duration = time.time() - t0
    # recompute the definitive counts from the WHOLE ledger, so the summary
    # reflects the full batch state (this run + everything resumed from before)
    final = {VERIFIED: 0, DUPLICATE: 0, FAILED: 0, SKIPPED: 0, UPLOADED: 0,
             UPLOAD_FAILED: 0}
    for r in records:
        final[r.get("status")] = final.get(r.get("status"), 0) + 1
    res.verified, res.duplicate = final[VERIFIED], final[DUPLICATE]
    res.failed, res.skipped = final[FAILED], final[SKIPPED]
    res.uploaded, res.upload_failed = final[UPLOADED], final[UPLOAD_FAILED]
    res.downloaded = res.verified + res.duplicate + res.uploaded
    # bytes_downloaded stays RUN-scoped on purpose: it is the volume actually
    # transferred, not the sum of file_size over the ledger (with hash dedup
    # 1,000 MPNs may share 50 physical files, which would inflate it ~20x).

    res.ledger_path = save_ledger(batch_id, records, root)
    res.report_path = report_path

    summary = {"batch_id": batch_id, "finished_at": _now()}
    summary.update(res.as_dict())
    summary["paths"] = {"ledger": res.ledger_path, "report": report_path,
                        "summary": pool.summary_path(batch_id, root)}
    res.summary_path = pool.atomic_write_json(pool.summary_path(batch_id, root),
                                              summary)

    pool.update_index(batch_id, {
        "datasheet_status": "DOWNLOADED",
        "datasheet_verified": res.verified,
        "datasheet_duplicate": res.duplicate,
        "datasheet_failed": res.failed,
        "datasheet_skipped": res.skipped,
    }, root)

    if manifest is not None:
        manifest.set_counts(
            datasheet_found=res.verified + res.duplicate + res.uploaded,
            datasheet_missing=res.failed + res.skipped)
        manifest.data.setdefault("pool", {}).setdefault("datasheets", {}).update({
            "ledger": res.ledger_path,
            "report": report_path,
            "summary": res.summary_path,
            "discovered": res.discovered,
            "verified": res.verified,
            "duplicate": res.duplicate,
            "failed": res.failed,
            "skipped": res.skipped,
            "bytes_downloaded": res.bytes_downloaded,
            "duration_s": round(res.duration, 2),
            "workers": workers,
            "retry_total": res.retry_total,
        })
        manifest.record_stage("DATASHEET_DOWNLOAD", ok=res.failed == 0,
                              note=(f"{res.verified} verified / {res.duplicate} dup / "
                                    f"{res.failed} failed / {res.skipped} skipped"))
        manifest.save()
    return res


def reset_for_retry(batch_id, root=None, only_failed=True):
    """Reset FAILED (or all non-done) records so they can be retried."""
    records = load_ledger(batch_id, root)
    n = 0
    for r in records:
        st = r.get("status")
        if only_failed and st != FAILED:
            continue
        if not only_failed and st in DONE_STATES:
            continue
        r["status"] = DISCOVERED
        r["error_code"] = None
        r["retry_count"] = 0
        n += 1
    save_ledger(batch_id, records, root)
    return n
