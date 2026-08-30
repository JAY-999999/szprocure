"""Pre-Batch Backup — the highest-priority risk control (NO_BACKUP == STOP).

Why this exists
---------------
`.gitignore` excludes all of `data/`, so `master_parts_v2.1.csv` and
`master_parts_publish.csv` are NOT in version control. The only copies are the
C working copy and the D asset root. Backups were previously a manual step and
the 2026-08-30 batch had none at all.

Contract
--------
* Copy every file in BACKUP_FILES to <root>/pre_<batch_id>/.
* Verify each copy by SHA256 (source vs destination).
* Write _backup_manifest.json listing every file, its sha256 and byte size.
* Any missing source / unreadable file / hash mismatch -> BackupError -> STOP.
  Partial backups are removed so a "backup" directory is always trustworthy.
"""
import csv
import hashlib
import json
import os
import shutil
from datetime import datetime

from . import BACKUP_FILES, DEFAULT_BACKUP_ROOT


class BackupError(RuntimeError):
    """Raised when a pre-batch backup cannot be fully verified -> STOP."""


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_dir(batch_id, root=None):
    return os.path.join(root or DEFAULT_BACKUP_ROOT, f"pre_{batch_id}")


def take_backup(batch_id, root=None, extra_files=()):
    """Create and verify a pre-batch backup.

    Returns a dict describing the backup. Raises BackupError on ANY problem —
    the caller must treat that as a STOP and must not modify MASTER.
    """
    from .ids import validate_batch_id
    validate_batch_id(batch_id)

    dest_root = backup_dir(batch_id, root)
    if os.path.exists(dest_root):
        raise BackupError(f"backup directory already exists (refusing to overwrite): {dest_root}")

    items = list(BACKUP_FILES) + [("extra_%d" % i, p) for i, p in enumerate(extra_files)]
    records, missing = [], []
    for key, src in items:
        if not os.path.exists(src):
            missing.append(f"{key}: {src}")
            continue
        records.append((key, src))

    if missing:
        raise BackupError("missing source file(s) for backup -> " + "; ".join(missing))

    os.makedirs(dest_root, exist_ok=True)
    manifest = {
        "batch_id": batch_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": dest_root,
        "files": [],
    }

    try:
        for key, src in records:
            dst = os.path.join(dest_root, f"{key}__{os.path.basename(src)}")
            shutil.copy2(src, dst)
            s_src, s_dst = sha256_of(src), sha256_of(dst)
            if s_src != s_dst:
                raise BackupError(
                    f"SHA256 mismatch after copy for {key}: {src}\n"
                    f"  source={s_src}\n  dest  ={s_dst}")
            if os.path.getsize(src) != os.path.getsize(dst):
                raise BackupError(f"size mismatch after copy for {key}: {src}")
            manifest["files"].append({
                "key": key, "src": src, "name": os.path.basename(dst),
                "bytes": os.path.getsize(src), "sha256": s_src,
            })
    except Exception:
        # never leave a half-written backup behind
        shutil.rmtree(dest_root, ignore_errors=True)
        raise

    manifest["verified"] = True
    with open(os.path.join(dest_root, "_backup_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, ensure_ascii=False, indent=2, fp=f)

    # final re-verification pass
    for rec in manifest["files"]:
        p = os.path.join(dest_root, rec["name"])
        if not os.path.exists(p) or sha256_of(p) != rec["sha256"]:
            shutil.rmtree(dest_root, ignore_errors=True)
            raise BackupError(f"post-write verification failed for {rec['key']}")
        if rec["key"].startswith("master") or rec["key"].startswith("publish"):
            _assert_csv_readable(p, rec["key"])

    return manifest


def verify_backup(batch_id, root=None):
    """Re-verify an existing backup directory against its recorded hashes."""
    dest_root = backup_dir(batch_id, root)
    mf = os.path.join(dest_root, "_backup_manifest.json")
    if not os.path.exists(mf):
        raise BackupError(f"no backup manifest at {mf}")
    with open(mf, encoding="utf-8") as f:
        manifest = json.load(f)
    for rec in manifest["files"]:
        p = os.path.join(dest_root, rec["name"])
        if not os.path.exists(p):
            raise BackupError(f"backup file missing: {p}")
        if sha256_of(p) != rec["sha256"]:
            raise BackupError(f"backup file corrupted: {p}")
    manifest["reverified"] = True
    return manifest


def restore_from_backup(batch_id, root=None, verify=True):
    """Restore files from a backup back to their original locations.

    Used by `batch_runner rollback`. Restores ONLY files recorded in the
    backup manifest, and always takes a safety copy of the current state first.
    """
    dest_root = backup_dir(batch_id, root)
    mf = os.path.join(dest_root, "_backup_manifest.json")
    if not os.path.exists(mf):
        raise BackupError(f"no backup manifest at {mf}")
    with open(mf, encoding="utf-8") as f:
        manifest = json.load(f)
    if verify:
        verify_backup(batch_id, root)

    restored = []
    for rec in manifest["files"]:
        src_file = os.path.join(dest_root, rec["name"])
        target = rec["src"]
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            safety = target + f".before_restore_{datetime.now():%Y%m%d_%H%M%S}"
            shutil.copy2(target, safety)
        shutil.copy2(src_file, target)
        if sha256_of(target) != rec["sha256"]:
            raise BackupError(f"restore verification failed for {target}")
        restored.append({"key": rec["key"], "target": target, "sha256": rec["sha256"]})
    return restored


def _assert_csv_readable(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if not rows:
            raise ValueError("empty")
    except Exception as e:
        raise BackupError(f"backup copy of {key} is not a readable CSV: {e}")
    return len(rows)
