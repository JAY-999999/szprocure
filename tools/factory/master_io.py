"""Atomic Master Write — replaces the old `open(...,'a')` + full-rewrite pattern.

The old pattern (used by the 08-29 / 08-30 batch scripts) was:
    open(path, "a")  -> append new rows
    re-read the whole file
    open(path, "w")  -> rewrite every row with a hard-coded 19-col HEADER
Any crash between the two writes left a corrupt or half-rewritten MASTER.

New contract
------------
1. read the existing MASTER (cols + rows)
2. build the complete new content in memory (old rows MUST be passed through
   untouched)
3. write `<path>.tmp` **in the same directory / same volume**
4. validate the temp file (row count, column count, header identity, MPN
   uniqueness, required fields, and that every pre-existing row is unchanged)
5. os.replace(tmp, path)  -- atomic on the same volume

If any validation fails the temp file is deleted and the real MASTER is never
touched.
"""
import csv
import os
import shutil
import tempfile

from . import MASTER_COLS, REQUIRED_FIELDS


class MasterWriteError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------
def read_master(path, expected_cols=None):
    if not os.path.exists(path):
        raise MasterWriteError(f"master not found: {path}")
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        cols = list(reader.fieldnames or [])
        rows = list(reader)
    if not cols:
        raise MasterWriteError(f"master has no header: {path}")
    if expected_cols and cols != list(expected_cols):
        raise MasterWriteError(
            f"unexpected header in {path}\n  expected={expected_cols}\n  actual ={cols}")
    return cols, rows


def detect_lineterminator(path):
    with open(path, "rb") as f:
        return "\r\n" if b"\r\n" in f.read(4096) else "\n"


def mpn_set(rows):
    return {(r.get("mpn") or "").strip() for r in rows}


def row_fingerprint(rows, cols):
    """Stable field-by-field fingerprint (order sensitive)."""
    import hashlib
    body = "\n".join("|".join((r.get(c) or "") for c in cols) for r in rows)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------
def validate_rows(cols, old_rows, new_rows, allow_extra_rows=True):
    """Return (ok, problems).

    Hard requirements:
      * header identical
      * new_rows length == old_rows length (+N if allow_extra_rows)
      * the first len(old_rows) rows must be field-for-field identical to
        old_rows  <-- this is what guarantees an existing 540 SKU can never
                      be modified by adding a batch
      * no duplicate MPN
      * required fields non-empty for every row
    """
    problems = []

    if len(new_rows) < len(old_rows):
        problems.append(f"row count shrank: {len(old_rows)} -> {len(new_rows)}")
    if not allow_extra_rows and len(new_rows) != len(old_rows):
        problems.append(f"row count changed: {len(old_rows)} -> {len(new_rows)}")

    n = min(len(old_rows), len(new_rows))
    diffs = 0
    for i in range(n):
        if {k: (v or "") for k, v in old_rows[i].items()} != \
           {k: (v or "") for k, v in new_rows[i].items()}:
            diffs += 1
            if diffs <= 5:
                problems.append(f"pre-existing row #{i} was modified (mpn="
                                f"{old_rows[i].get('mpn')!r})")
    if diffs:
        problems.append(f"total pre-existing rows modified: {diffs}")

    seen, dups = set(), []
    for r in new_rows:
        m = (r.get("mpn") or "").strip()
        if m in seen:
            dups.append(m)
        seen.add(m)
    if dups:
        problems.append(f"duplicate MPN(s): {sorted(set(dups))[:10]}")

    # Required-field check applies to NEWLY ADDED rows only.
    # Pre-existing rows are grandfathered: the audit already reports 10 legacy
    # SKUs with an empty description, and re-validating history against today's
    # bar would make every batch impossible to apply. Their safety comes from
    # the "pre-existing rows unchanged" assertion above, not from this check.
    base_n = len(old_rows)
    for idx in range(base_n, len(new_rows)):
        r = new_rows[idx]
        for f in REQUIRED_FIELDS:
            if not (r.get(f) or "").strip():
                problems.append(f"NEW row #{idx} (mpn={r.get('mpn')!r}) "
                                f"missing required field {f!r}")
                break

    return (not problems), problems


# --------------------------------------------------------------------------
# atomic write
# --------------------------------------------------------------------------
def atomic_write_master(path, cols, new_rows, old_rows=None, lineterminator=None,
                        dry_run=False):
    """Write `new_rows` to `path` atomically.

    old_rows: the rows read from the current file. When provided, the
    'pre-existing rows unchanged' assertion is enforced.

    Returns a dict describing what happened. On any validation failure raises
    MasterWriteError and leaves `path` byte-identical.
    """
    if old_rows is None:
        old_rows = []
    ok, problems = validate_rows(cols, old_rows, new_rows)
    if not ok:
        raise MasterWriteError("validation failed, MASTER not modified:\n  - "
                               + "\n  - ".join(problems))

    lt = lineterminator or (detect_lineterminator(path) if os.path.exists(path) else "\r\n")

    if dry_run:
        return {"written": False, "dry_run": True, "path": path,
                "rows": len(new_rows), "cols": len(cols)}

    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, lineterminator=lt)
            w.writeheader()
            w.writerows(new_rows)

        # re-read the temp file and validate it exactly as we validated memory
        t_cols, t_rows = read_master(tmp)
        if t_cols != cols:
            raise MasterWriteError("temp file header mismatch")
        ok2, problems2 = validate_rows(cols, old_rows, t_rows)
        if not ok2:
            raise MasterWriteError("temp file failed validation:\n  - "
                                   + "\n  - ".join(problems2))

        os.replace(tmp, path)          # atomic on the same volume
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise

    return {"written": True, "dry_run": False, "path": path,
            "rows": len(new_rows), "cols": len(cols), "lineterminator": repr(lt)}


def append_rows_atomically(path, appended_rows, expected_cols=None, dry_run=False):
    """Read -> append in memory -> atomic write. Never uses mode 'a'."""
    cols, rows = read_master(path, expected_cols)
    for r in appended_rows:
        missing = [c for c in cols if c not in r]
        extra = [c for c in r if c not in cols]
        if missing or extra:
            raise MasterWriteError(
                f"appended row {r.get('mpn')!r} column mismatch "
                f"(missing={missing}, extra={extra})")
    new_rows = rows + list(appended_rows)
    return atomic_write_master(path, cols, new_rows, old_rows=rows, dry_run=dry_run)


def sha256_of(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_pair_identical(path_a, path_b, expected_cols=None):
    """Confirm the C working copy and the D asset root are byte-identical."""
    a, b = sha256_of(path_a), sha256_of(path_b)
    return {"identical": a == b, "a_sha256": a, "b_sha256": b}
