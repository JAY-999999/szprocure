"""R2 Asset Factory — local PDF pool -> Cloudflare R2 (Phase P1-C).

Scope of this phase: local PDF -> R2 -> remote verification -> reconcile.
It does NOT touch the CSV, publish, MASTER, datasheet_map, the build or the
site. The local PDF pool is the source repository; R2 is the publication
asset store; the two must correspond one-to-one.

R2 key
------
    datasheets/pdf/<first 2 hex of sha256>/<sha256>.pdf

Content-addressed and immutable. The MPN is NEVER part of the physical key,
because one datasheet can serve many MPNs and a repeated PDF across batches
must resolve to the same single object. MPN is stored only as metadata for
traceability: sha256 is the object identity, MPN is not.

Why ETag is not trusted
-----------------------
For an S3-compatible API the ETag is an MD5 for a simple PUT but is explicitly
NOT a content hash for multipart uploads, and R2 may differ again. Therefore
verification is:
  1. HEAD the object (existence + size + x-amz-meta-sha256).
  2. If the sha256 metadata is present and equals the local digest -> match.
  3. Otherwise fall back to GET + SHA256 over the actual remote bytes.
The ETag is recorded for diagnostics only and never used as proof of content.

Durability
----------
* One JSONL line appended and fsynced after every completed task.
* Ledger checkpointed atomically every 25 tasks and at the end.
* UPLOADING is reset to its pre-upload state at start, so a crash can never
  strand a record in a non-resumable state.
* UPLOADED is skipped; UPLOAD_FAILED can be re-armed with
  reset_upload_for_retry().
* Local PDFs are never deleted — the pool is a durable asset (re-upload, and
  the future Content Factory).
"""
import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from . import datasheet as ds
from . import gate
from . import pool

# ---------------------------------------------------------------- tunables --
DEFAULT_WORKERS = 8           # default only; 4 / 8 / 16 all supported
DEFAULT_RETRIES = 3
DEFAULT_TIMEOUT = 120         # seconds, per attempt
DEFAULT_BACKOFF = 1.5
CHECKPOINT_EVERY = 25
R2_PREFIX = "datasheets/pdf"
MASS_FAIL_RATE = 0.10         # per the approved Exception Gate (R2_MASS_FAIL)

REG_KEY = r"Environment"
CRED_NAMES = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
              "R2_SECRET_ACCESS_KEY", "R2_BUCKET")

# upload lifecycle state (the download lifecycle states live in datasheet.py)
UPLOADING = "UPLOADING"
# records that are candidates for upload
UPLOADABLE = (ds.VERIFIED, ds.DUPLICATE, ds.UPLOAD_FAILED)
# pool-only key: remembers whether the record was VERIFIED or DUPLICATE
F_PRE_UPLOAD = "_pre_upload_status"

# Per-content-key serialisation. Two records sharing the same sha256 (a VERIFIED
# twin and a DUPLICATE) map to the same R2 key; without a key lock the second
# thread can PUT before the first's object is visible, producing two physical
# objects instead of one. The lock serialises same-key work (one PUT, the rest
# observe already_exists) while different keys still run in parallel.
_GLOBAL_KEY_LOCKS = {}
_GLOBAL_KEY_LOCKS_GUARD = threading.Lock()


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _key_lock(key, registry, guard):
    if registry is None:
        return _NoLock()
    with guard:
        lk = registry.get(key)
        if lk is None:
            lk = threading.Lock()
            registry[key] = lk
    return lk


class R2Error(Exception):
    pass


class R2CredentialsError(R2Error):
    pass


def _now():
    return datetime.now().isoformat(timespec="seconds")


def r2_key_for(sha256_hex):
    """Immutable, content-addressed object key. Never keyed by MPN."""
    h = (sha256_hex or "").lower()
    if len(h) != 64:
        raise R2Error(f"not a sha256 hex digest: {sha256_hex!r}")
    return f"{R2_PREFIX}/{h[:2]}/{h}.pdf"


def local_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


# =====================================================================
# credentials (winreg bridge, mirrors the proven _r2_apply.py pattern)
# =====================================================================
def load_credentials(require=True):
    """Read R2 creds from the environment, falling back to HKCU\\Environment.

    Secrets are never written to disk and never printed. Returns (creds, missing).
    """
    creds, missing = {}, []
    for name in CRED_NAMES:
        val = os.environ.get(name)
        if not val:
            try:
                import winreg
                h = winreg.OpenKey(winreg.HKEY_CURRENT_USER, REG_KEY)
                val, _ = winreg.QueryValueEx(h, name)
                val = str(val)
            except Exception:
                val = None
        if val:
            creds[name] = val
        else:
            missing.append(name)
    if missing and require:
        raise R2CredentialsError(
            "R2 credentials unavailable: " + ", ".join(missing))
    return creds, missing


def make_client(creds=None, endpoint_url=None, timeout=DEFAULT_TIMEOUT):
    """S3-compatible R2 client. Retries are handled by us, so botocore gets 1."""
    creds = creds or load_credentials()[0]
    endpoint = endpoint_url or \
        f"https://{creds['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=creds["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=creds["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": "path"},
                      connect_timeout=timeout, read_timeout=timeout,
                      retries={"max_attempts": 1}),
    )


# =====================================================================
# remote verification — the core safety gate
# =====================================================================
def verify_remote(client, bucket, key, local_sha, local_size,
                  allow_content_fallback=True, mode="metadata"):
    """Return (ok, info). Never trusts ETag as a content hash.

    mode="metadata" (cheap, for idempotence checks on pre-existing objects)
        HEAD the object: existence + size + x-amz-meta-sha256. If the metadata
        is absent or unusable, fall back to hashing the remote body.
    mode="content" (strict, for anything we just wrote)
        GET the object and hash the actual bytes. This is the only check that
        can detect a corrupted body whose metadata still looks correct.

    ETag is recorded for diagnostics only and is never treated as proof.
    """
    info = {"r2_key": key, "exists": False, "r2_size": None,
            "r2_etag": None, "r2_meta_sha256": None,
            "verify_method": None, "matched": False}
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            return False, info
        raise
    info["exists"] = True
    info["r2_size"] = head.get("ContentLength")
    info["r2_etag"] = (head.get("ETag") or "").strip('"')
    meta = {str(k).lower(): v for k, v in (head.get("Metadata") or {}).items()}
    info["r2_meta_sha256"] = meta.get("sha256")

    if mode == "content":
        # strict: prove it from the actual remote bytes
        body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        digest = hashlib.sha256(body).hexdigest()
        info["r2_size"] = len(body)
        info["verify_method"] = "remote_content_sha256"
        info["matched"] = (len(body) == local_size
                           and digest.lower() == local_sha.lower())
        return info["matched"], info

    if info["r2_size"] != local_size:
        return False, info
    if info["r2_meta_sha256"] and \
            info["r2_meta_sha256"].lower() == local_sha.lower():
        info["verify_method"] = "metadata_sha256"
        info["matched"] = True
        return True, info
    if not allow_content_fallback:
        return False, info
    body = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    digest = hashlib.sha256(body).hexdigest()
    info["verify_method"] = "remote_content_sha256"
    info["matched"] = digest.lower() == local_sha.lower()
    return info["matched"], info


def _is_missing(e):
    code = ""
    try:
        code = e.response.get("Error", {}).get("Code") or ""
    except Exception:
        pass
    return code in ("404", "NoSuchKey", "NotFound")


def _error_code(e):
    if isinstance(e, ClientError):
        r = e.response.get("Error", {}) or {}
        code = r.get("Code") or ""
        http = (e.response.get("ResponseMetadata", {}) or {}).get("HTTPStatusCode")
        if code in ("403", "AccessDenied", "InvalidAccessKeyId",
                    "SignatureDoesNotMatch"):
            return gate.R2_AUTH_FAILED, f"{code} (HTTP {http})"
        return gate.R2_UPLOAD_ERROR, f"{code} (HTTP {http})"
    name = type(e).__name__
    if "timeout" in name.lower() or "Timeout" in name:
        return gate.R2_TIMEOUT, f"{name}: {e}"
    return gate.R2_NETWORK, f"{name}: {e}"


# =====================================================================
# upload
# =====================================================================
class UploadResult:
    def __init__(self):
        self.batch_id = None
        self.workers = 0
        self.total = self.uploaded = self.already_exists = 0
        self.verified = self.failed = self.retry_count = 0
        self.bytes_uploaded = self.bytes_skipped = 0
        self.remote_verify_failed = 0
        self.new_objects = self.duplicate_objects = 0
        self.started_at = self.finished_at = None
        self.duration = 0.0
        self.stop = False
        self.exceptions = []
        self.report_path = self.summary_path = self.ledger_path = None

    def as_dict(self):
        return {
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "workers": self.workers,
            "total": self.total,
            "uploaded": self.uploaded,
            "already_exists": self.already_exists,
            "verified": self.verified,
            "failed": self.failed,
            "retry_count": self.retry_count,
            "bytes_uploaded": self.bytes_uploaded,
            "bytes_skipped": self.bytes_skipped,
            "remote_verify_failed": self.remote_verify_failed,
            "new_objects": self.new_objects,
            "duplicate_objects": self.duplicate_objects,
            "duration": round(self.duration, 2),
            "stop": self.stop,
        }


# =====================================================================
# metric invariants — prevent double-counting / distortion at scale
# =====================================================================
def validate_r2_counts(c):
    """Return None if the R2 outcome counts are self-consistent, else a
    human-readable violation string.

    Every record ends in exactly one terminal outcome, so:

      * uploaded == new_objects + already_exists
        (all non-failed records become UPLOADED; ``already_exists`` is the
         idempotent skip, ``new_objects`` the fresh PUT — they are mutually
         exclusive and together exhaust the non-failed set)
      * total    == new_objects + already_exists + failed
        (no other terminal outcome exists)

    These invariants make it impossible for ``uploaded`` (the all-UPLOADED
    total) to be reported as "new" — the exact distortion that hit the
    P1-D-C smoke batch when a re-run's idempotent 44/44 was mislabeled.
    """
    up = c.get("uploaded", 0)
    new = c.get("new_objects", 0)
    ae = c.get("already_exists", 0)
    tot = c.get("total", 0)
    fl = c.get("failed", 0)
    if up != new + ae:
        return (f"uploaded({up}) must equal new_objects({new}) "
                f"+ already_exists({ae})")
    if tot != new + ae + fl:
        return (f"total({tot}) must equal new_objects({new}) "
                f"+ already_exists({ae}) + failed({fl})")
    return None


def summarize_r2_counts(c):
    """Return a metrics-friendly, non-overlapping view of R2 counts.

    ``uploaded_total`` is the count of records that ended UPLOADED
    (= new_objects + already_exists). ``new_objects`` is the only "new"
    figure and is what ``r2_uploaded_new`` in the batch metrics must use.
    """
    inv = validate_r2_counts(c)
    return {
        "total": c.get("total", 0),
        "uploaded_total": c.get("uploaded", 0),
        "new_objects": c.get("new_objects", 0),
        "already_exists": c.get("already_exists", 0),
        "failed": c.get("failed", 0),
        "bytes_uploaded": c.get("bytes_uploaded", 0),
        "invariant_ok": inv is None,
        "invariant_violation": inv,
    }


def upload_one(rec, client, bucket, retries=DEFAULT_RETRIES,
               timeout=DEFAULT_TIMEOUT, backoff=DEFAULT_BACKOFF,
               key_locks=None, key_locks_guard=None):
    """Upload one asset and prove it remotely. Mutates and returns rec."""
    rec.setdefault("upload_retry_count", 0)

    if rec.get("status") == ds.UPLOADED:
        rec["_outcome"] = "already_exists"
        return rec

    local_path = rec.get("local_path")
    if not local_path or not os.path.exists(local_path):
        rec["status"] = ds.UPLOAD_FAILED
        rec["upload_error"] = f"LOCAL_PDF_MISSING: {local_path}"
        rec["_outcome"] = "failed"
        return rec

    size = os.path.getsize(local_path)
    digest = local_sha256(local_path)
    if rec.get("sha256") and digest.lower() != rec["sha256"].lower():
        # The local PDF changed after it was verified -> data integrity issue.
        rec["status"] = ds.UPLOAD_FAILED
        rec["upload_error"] = (f"LOCAL_HASH_UNSTABLE: recorded "
                               f"{rec.get('sha256')} != actual {digest}")
        rec["_outcome"] = "local_hash_unstable"
        return rec

    key = r2_key_for(digest)
    rec["r2_key"] = key

    # Serialise by content key so two records sharing a hash never race to PUT
    # the same object (one uploads, the other observes already_exists).
    with _key_lock(key, key_locks, key_locks_guard):
        # 1) already present and provably identical -> skip, never re-upload
        try:
            ok, info = verify_remote(client, bucket, key, digest, size)
        except Exception as e:
            code, msg = _error_code(e)
            rec["status"] = ds.UPLOAD_FAILED
            rec["upload_error"] = f"{code}: {msg}"
            rec["_outcome"] = "failed"
            return rec
        if ok:
            rec.update(status=ds.UPLOADED, r2_size=size, r2_etag=info["r2_etag"],
                       r2_meta_sha256=info["r2_meta_sha256"],
                       remote_verified=True, verified_at=_now(),
                       upload_error=None,
                       verify_method=info["verify_method"])
            rec["_outcome"] = "already_exists"
            return rec
        if info["exists"]:
            # Object present but its content does NOT match. Never overwrite.
            rec["status"] = ds.UPLOAD_FAILED
            rec["upload_error"] = ("REMOTE_HASH_MISMATCH: object exists but "
                                   f"content differs (r2_size={info['r2_size']} "
                                   f"local_size={size} "
                                   f"r2_meta_sha256={info['r2_meta_sha256']})")
            rec["_outcome"] = "remote_hash_mismatch"
            return rec

        # 2) upload with retry + exponential backoff
        last = ""
        for attempt in range(1, retries + 1):
            rec["upload_retry_count"] = attempt
            rec["status"] = UPLOADING
            try:
                with open(local_path, "rb") as fh:
                    client.put_object(
                        Bucket=bucket, Key=key, Body=fh,
                        ContentType="application/pdf",
                        Metadata={"sha256": digest.lower(),
                                  "mpn": str(rec.get("mpn") or "")},
                    )
            except Exception as e:
                code, msg = _error_code(e)
                last = f"{code}: {msg}"
                if attempt < retries:
                    time.sleep(backoff ** attempt)
                    continue
                rec["status"] = ds.UPLOAD_FAILED
                rec["upload_error"] = last
                rec["_outcome"] = "failed"
                return rec
            break

        # 3) prove it landed correctly — STRICT content check, because we just
        #    wrote it. Metadata alone cannot detect a corrupted body whose
        #    metadata still carries the right digest.
        try:
            ok, info = verify_remote(client, bucket, key, digest, size,
                                     mode="content")
        except Exception as e:
            code, msg = _error_code(e)
            rec["status"] = ds.UPLOAD_FAILED
            rec["upload_error"] = f"REMOTE_VERIFY_FAILED: {code}: {msg}"
            rec["_outcome"] = "remote_verify_failed"
            return rec
        if not ok:
            if info["exists"]:
                # Object present but its content is wrong -> integrity breach.
                # Never retry into a bad overwrite; surface as STOP.
                rec["status"] = ds.UPLOAD_FAILED
                rec["upload_error"] = ("REMOTE_HASH_MISMATCH: uploaded but "
                                       f"remote content differs "
                                       f"(r2_size={info['r2_size']} "
                                       f"local_size={size})")
                rec["_outcome"] = "remote_hash_mismatch"
            else:
                # Object simply absent after a claimed-successful PUT.
                rec["status"] = ds.UPLOAD_FAILED
                rec["upload_error"] = ("REMOTE_VERIFY_FAILED: uploaded but "
                                       f"object absent (exists={info['exists']})")
                rec["_outcome"] = "remote_verify_failed"
            return rec

        rec.update(status=ds.UPLOADED, r2_size=size, r2_etag=info["r2_etag"],
                   r2_meta_sha256=info["r2_meta_sha256"],
                   remote_verified=True, verified_at=_now(),
                   upload_error=None, verify_method=info["verify_method"])
        rec["_outcome"] = "uploaded"
        return rec


def _resolve(creds=None, client=None, endpoint_url=None, bucket=None,
             timeout=DEFAULT_TIMEOUT):
    """Return (client, bucket). Lets callers inject a mock client + bucket."""
    if client is not None and bucket:
        return client, bucket
    creds = creds or load_credentials()[0]
    b = bucket or creds["R2_BUCKET"]
    if client is not None:
        return client, b
    return make_client(creds, endpoint_url, timeout), b


def upload_batch(batch_id, root=None, workers=DEFAULT_WORKERS,
                 retries=DEFAULT_RETRIES, timeout=DEFAULT_TIMEOUT,
                 creds=None, endpoint_url=None, client=None, bucket=None,
                 manifest=None, on_record=None):
    """Upload every not-yet-confirmed asset in the batch ledger, concurrently."""
    if workers < 1:
        raise R2Error("workers must be >= 1")
    client, bucket = _resolve(creds, client, endpoint_url, bucket, timeout)

    records = ds.load_ledger(batch_id, root)
    if not records:
        raise R2Error(f"empty ledger for batch {batch_id}")

    pool.ensure_report_dir(batch_id, root)
    report_path = os.path.join(pool.report_dir(batch_id, root),
                               "r2_upload_report.jsonl")
    if not os.path.exists(report_path):
        open(report_path, "w", encoding="utf-8").close()

    # crash recovery: UPLOADING is not a resumable state
    for r in records:
        if r.get("status") == UPLOADING:
            r["status"] = r.get(F_PRE_UPLOAD) or ds.VERIFIED

    # Records already confirmed need no work at all — they are counted, never
    # re-verified (that is reconcile_r2()'s job).
    confirmed = [r for r in records if r.get("status") == ds.UPLOADED]
    # A record whose local PDF vanished must still be processed so the loss is
    # RECORDED as a failure, not silently skipped.
    todo = [r for r in records if r.get("status") in UPLOADABLE]
    for r in todo:
        r.setdefault(F_PRE_UPLOAD, r.get("status"))

    res = UploadResult()
    res.batch_id = batch_id
    res.workers = workers
    res.total = len(todo) + len(confirmed)
    for r in confirmed:
        res.already_exists += 1
        res.verified += 1
        res.bytes_skipped += r.get("file_size") or 0
    res.started_at = _now()
    t0 = time.time()
    done = 0
    lock = threading.Lock()

    with open(report_path, "a", encoding="utf-8") as rf, \
            ThreadPoolExecutor(max_workers=workers) as ex:
        key_locks: dict = {}
        key_locks_guard = threading.Lock()
        futs = {ex.submit(upload_one, r, client, bucket, retries, timeout,
                          DEFAULT_BACKOFF, key_locks, key_locks_guard): r
                for r in todo}
        for fut in as_completed(futs):
            rec = fut.result()
            rf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            rf.flush()
            os.fsync(rf.fileno())
            done += 1
            res.retry_count += rec.get("upload_retry_count", 0)
            outcome = rec.get("_outcome")
            size = rec.get("file_size") or 0
            if outcome == "uploaded":
                res.new_objects += 1
                res.verified += 1
                res.bytes_uploaded += size
            elif outcome == "already_exists":
                res.already_exists += 1
                res.verified += 1
                res.bytes_skipped += size
            elif outcome == "remote_verify_failed":
                res.remote_verify_failed += 1
                res.failed += 1
            elif outcome == "remote_hash_mismatch":
                res.remote_verify_failed += 1
                res.failed += 1
                with lock:
                    res.exceptions.append({
                        "code": gate.REMOTE_HASH_MISMATCH, "severity": gate.STOP,
                        "mpn": rec.get("mpn"),
                        "message": rec.get("upload_error", "")})
            elif outcome == "local_hash_unstable":
                res.failed += 1
                with lock:
                    res.exceptions.append({
                        "code": gate.LOCAL_HASH_UNSTABLE, "severity": gate.STOP,
                        "mpn": rec.get("mpn"),
                        "message": rec.get("upload_error", "")})
            else:
                res.failed += 1
            if rec.get(F_PRE_UPLOAD) == ds.DUPLICATE:
                res.duplicate_objects += 1
            if on_record:
                on_record(rec)
            if done % CHECKPOINT_EVERY == 0:
                ds.save_ledger(batch_id, records, root)

    res.duration = time.time() - t0
    res.finished_at = _now()
    res.uploaded = sum(1 for r in records if r.get("status") == ds.UPLOADED)

    if res.total and (res.failed / res.total) > MASS_FAIL_RATE:
        res.stop = True
        res.exceptions.append({
            "code": gate.R2_MASS_FAIL, "severity": gate.STOP, "mpn": None,
            "message": (f"upload failure rate {res.failed / res.total:.0%} "
                        f"> {MASS_FAIL_RATE:.0%} ({res.failed}/{res.total})")})

    res.ledger_path = ds.save_ledger(batch_id, records, root)
    res.report_path = report_path
    summary = res.as_dict()
    summary["paths"] = {"ledger": res.ledger_path, "report": report_path,
                        "summary": os.path.join(pool.report_dir(batch_id, root),
                                                "r2_batch_summary.json")}
    res.summary_path = pool.atomic_write_json(
        os.path.join(pool.report_dir(batch_id, root), "r2_batch_summary.json"),
        summary)

    pool.update_index(batch_id, {
        "r2_status": "UPLOADED" if not res.stop else "STOP",
        "r2_uploaded": res.uploaded,
        "r2_failed": res.failed,
    }, root)

    if manifest is not None:
        manifest.set_counts(r2_uploaded=res.uploaded, r2_failed=res.failed)
        manifest.data.setdefault("pool", {}).setdefault("r2", {}).update({
            "report": report_path, "summary": res.summary_path,
            "total": res.total, "uploaded": res.uploaded,
            "already_exists": res.already_exists,
            "failed": res.failed,
            "remote_verify_failed": res.remote_verify_failed,
            "new_objects": res.new_objects,
            "bytes_uploaded": res.bytes_uploaded,
            "duration_s": round(res.duration, 2), "workers": workers,
        })
        manifest.record_stage("R2_UPLOAD", ok=not res.stop,
                              note=(f"{res.uploaded} uploaded / "
                                    f"{res.already_exists} already / "
                                    f"{res.failed} failed"))
        manifest.save()
    return res


def reset_upload_for_retry(batch_id, root=None):
    """Re-arm UPLOAD_FAILED / stuck UPLOADING records so they can be retried."""
    records = ds.load_ledger(batch_id, root)
    n = 0
    for r in records:
        if r.get("status") in (ds.UPLOAD_FAILED, UPLOADING):
            r["status"] = r.get(F_PRE_UPLOAD) or ds.VERIFIED
            r["upload_error"] = None
            r["upload_retry_count"] = 0
            n += 1
    ds.save_ledger(batch_id, records, root)
    return n


# =====================================================================
# reconcile — the independent safety gate
# =====================================================================
def reconcile_r2(batch_id, root=None, creds=None, endpoint_url=None,
                 client=None, bucket=None):
    """Compare the local PDF pool against R2, object by object.

    Returns a dict with a PASS / FAIL verdict. Any of these makes it FAIL:
      * a local PDF whose recorded sha256 no longer matches the file
      * a local PDF with no R2 object
      * an R2 object whose size or content hash does not match
      * LOCAL_VERIFIED > R2_VERIFIED  (never fewer objects remotely than locally)
    """
    client, bucket = _resolve(creds, client, endpoint_url, bucket)

    records = ds.load_ledger(batch_id, root)
    rows, problems = [], []
    local_verified = r2_verified = 0

    for r in records:
        path = r.get("local_path")
        if not path:
            continue
        row = {"mpn": r.get("mpn"), "local_path": path,
               "recorded_sha256": r.get("sha256"),
               "local_sha256": None, "local_size": None,
               "r2_key": None, "r2_exists": False, "r2_size": None,
               "r2_etag": None, "r2_meta_sha256": None,
               "verify_method": None, "match": False}
        if not os.path.exists(path):
            row["status"] = "LOCAL_MISSING"
            problems.append(f"{r.get('mpn')}: local PDF missing ({path})")
            rows.append(row)
            continue

        row["local_size"] = os.path.getsize(path)
        row["local_sha256"] = local_sha256(path)
        if row["recorded_sha256"] and \
                row["local_sha256"].lower() != row["recorded_sha256"].lower():
            row["status"] = "LOCAL_HASH_UNSTABLE"
            problems.append(f"{r.get('mpn')}: local hash changed")
            rows.append(row)
            continue

        local_verified += 1
        key = r2_key_for(row["local_sha256"])
        row["r2_key"] = key
        try:
            ok, info = verify_remote(client, bucket, key,
                                     row["local_sha256"], row["local_size"])
        except Exception as e:
            code, msg = _error_code(e)
            row["status"] = f"REMOTE_ERROR:{code}"
            problems.append(f"{r.get('mpn')}: {code}: {msg}")
            rows.append(row)
            continue
        row.update(r2_exists=info["exists"], r2_size=info["r2_size"],
                   r2_etag=info["r2_etag"],
                   r2_meta_sha256=info["r2_meta_sha256"],
                   verify_method=info["verify_method"], match=ok)
        if ok:
            r2_verified += 1
            row["status"] = "MATCH"
        else:
            row["status"] = "REMOTE_MISSING" if not info["exists"] \
                else "REMOTE_HASH_MISMATCH"
            problems.append(f"{r.get('mpn')}: {row['status']} at {key}")
        rows.append(row)

    ok = (not problems) and (local_verified == r2_verified)
    if local_verified > r2_verified:
        problems.append(
            f"LOCAL_VERIFIED({local_verified}) > R2_VERIFIED({r2_verified})")
    return {
        "batch_id": batch_id,
        "checked_at": _now(),
        "local_verified": local_verified,
        "r2_verified": r2_verified,
        "rows": rows,
        "problems": problems,
        "verdict": "PASS" if ok else "FAIL",
    }
