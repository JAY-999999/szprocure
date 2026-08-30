"""Local staging pool — the Factory's warehouse (Phase P1-A).

Why a pool exists
-----------------
The old workflow was: fetch 1 SKU -> process 1 SKU -> upload -> publish.
That cannot scale and it couples intake to release.

The Factory instead stages everything locally FIRST:

    raw source -> raw pool      (verbatim intake, never edited)
               -> candidates    (cleaned, normalised, de-duplicated)
               -> ready         (assets attached; filled by P1-B/C)

Pool layout (root = D:\\SZ Procure\\03_MASTER\\pool)
----------------------------------------------------
    products/raw/<batch_id>.jsonl         verbatim intake, one JSON per line
    products/candidates/<batch_id>.json   cleaned + de-duplicated rows
    products/ready/<batch_id>.json        rows with assets resolved (P1-C)
    datasheets/pdf/<aa>/<sha256>.pdf      CONTENT-ADDRESSED physical PDFs (P1-B)
    datasheets/index/<batch_id>.json      per-batch asset ledger (P1-B)
    reports/<batch_id>/                   incremental JSONL + summary (P1-B)
    content/<slug>.json                   RESERVED for the future Content Factory
    INDEX.json                            cross-batch index

Physical PDFs are stored by SHA256, so two MPNs sharing one datasheet occupy a
single file while both keep their own ledger entry: MPN dedup != PDF dedup.

Durability rules
----------------
* JSONL is appended with flush + fsync per record, so a crash during a
  10,000-SKU intake keeps every record already written. A torn final line is
  tolerated on read (and reported, never silently dropped).
* JSON is written atomically (temp in the SAME directory -> os.replace), so a
  reader never sees a half-written pool file.
* INDEX.json is read-modify-written atomically.
* Nothing here ever touches production MASTER.
"""
import json
import os
import tempfile
from datetime import datetime

DEFAULT_POOL_ROOT = r"D:\SZ Procure\03_MASTER\pool"

RAW = "products/raw"
CANDIDATES = "products/candidates"
READY = "products/ready"
DATASHEETS = "datasheets"
PDF = "datasheets/pdf"
DS_INDEX = "datasheets/index"
REPORTS = "reports"
# RESERVED: the future Content Factory writes content/<slug>.json side-cars.
# Nothing in P1-A/P1-B reads or writes it; it exists so the path stays stable.
CONTENT = "content"
SUBDIRS = (RAW, CANDIDATES, READY, PDF, DS_INDEX, REPORTS, CONTENT)


class PoolError(Exception):
    pass


# ------------------------------------------------------------------ paths --
def pool_root(root=None):
    return root or os.environ.get("SZ_POOL_ROOT") or DEFAULT_POOL_ROOT


def ensure(root=None):
    r = pool_root(root)
    for d in SUBDIRS:
        os.makedirs(os.path.join(r, d), exist_ok=True)
    return r


def raw_path(batch_id, root=None):
    return os.path.join(pool_root(root), RAW, f"{batch_id}.jsonl")


def candidates_path(batch_id, root=None):
    return os.path.join(pool_root(root), CANDIDATES, f"{batch_id}.json")


def ready_path(batch_id, root=None):
    return os.path.join(pool_root(root), READY, f"{batch_id}.json")


def asset_dir(root=None, kind="pdf"):
    return os.path.join(pool_root(root), ASSETS, kind)


def index_path(root=None):
    return os.path.join(pool_root(root), "INDEX.json")


# --------------------------------------------------- datasheets / reports --
def pdf_path(sha256_hex, root=None):
    """Content-addressed physical PDF path: pdf/<first 2 hex>/<sha256>.pdf."""
    h = (sha256_hex or "").lower()
    return os.path.join(pool_root(root), PDF, h[:2], f"{h}.pdf")


def datasheet_index_path(batch_id, root=None):
    return os.path.join(pool_root(root), DS_INDEX, f"{batch_id}.json")


def report_dir(batch_id, root=None):
    return os.path.join(pool_root(root), REPORTS, batch_id)


def download_report_path(batch_id, root=None):
    """Incremental JSONL — appended after every single task completes."""
    return os.path.join(report_dir(batch_id, root),
                        "datasheet_download_report.jsonl")


def summary_path(batch_id, root=None):
    """Final batch summary, written atomically at the end."""
    return os.path.join(report_dir(batch_id, root), "datasheet_batch_summary.json")


def content_dir(root=None):
    """RESERVED for the future Content Factory (side-car, never a blocker)."""
    return os.path.join(pool_root(root), CONTENT)


def content_path(slug, root=None):
    return os.path.join(content_dir(root), f"{slug}.json")


def ensure_report_dir(batch_id, root=None):
    d = report_dir(batch_id, root)
    os.makedirs(d, exist_ok=True)
    return d


# ------------------------------------------------------------ atomic json --
def atomic_write_json(path, obj):
    """Write JSON atomically: temp in the SAME directory -> os.replace."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = json.dumps(obj, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=".pool_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        with open(tmp, encoding="utf-8") as f:
            json.load(f)          # must parse back before it is swapped in
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ jsonl --
def append_jsonl(path, records):
    """Stream records to a JSONL file, fsync-ing after each one.

    Crash-safe at record granularity: a crash loses at most the record being
    written, and read_jsonl() tolerates a torn last line.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n = 0
    with open(path, "a", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            n += 1
    return n


def write_jsonl(path, records):
    """Rewrite a JSONL file atomically (used to compact / re-normalise)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix=".pool_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in records:
                line = json.dumps(rec, ensure_ascii=False)
                json.loads(line)          # per-record round-trip guard
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def read_jsonl(path, tolerant=True):
    """Read a JSONL file.

    tolerant=True skips (and reports) a torn trailing line left by a crash
    instead of blowing up — the damaged line count is returned to the caller
    so the Gate can decide what to do. Nothing is ever silently dropped.
    """
    if not os.path.exists(path):
        return [], 0
    out, torn = [], 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                torn += 1
                if not tolerant:
                    raise
    return out, torn


# ------------------------------------------------------------------ index --
def read_index(root=None):
    return read_json(index_path(root), default={"batches": {}}) or {"batches": {}}


def update_index(batch_id, entry, root=None):
    """Atomically upsert one batch into INDEX.json (cross-batch querying)."""
    idx = read_index(root)
    prev = idx.setdefault("batches", {}).get(batch_id, {})
    prev.update(entry)
    prev["updated_at"] = datetime.now().isoformat(timespec="seconds")
    idx["batches"][batch_id] = prev
    idx["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(index_path(root), idx)
    return prev


def pool_stats(root=None):
    """Cheap overview used by reports / tests."""
    r = pool_root(root)
    out = {"root": r, "raw_batches": 0, "candidate_batches": 0,
           "ready_batches": 0, "raw_records": 0}
    for sub, key in ((RAW, "raw_batches"), (CANDIDATES, "candidate_batches"),
                     (READY, "ready_batches")):
        d = os.path.join(r, sub)
        out[key] = len([f for f in os.listdir(d) if not f.startswith(".")]) \
            if os.path.isdir(d) else 0
    d = os.path.join(r, RAW)
    if os.path.isdir(d):
        for f in os.listdir(d):
            if f.endswith(".jsonl"):
                recs, _ = read_jsonl(os.path.join(d, f))
                out["raw_records"] += len(recs)
    return out
