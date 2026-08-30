"""SZ Procure SKU Factory — orchestration layer (Phase P0).

Design rules (from the Architecture Review / Implementation Plan):
  * This package is an ORCHESTRATOR. It does NOT re-implement any stage.
    Frozen layers (gen_parts.py, publish_normalizer.py, build_datasheet_map.py,
    pre_deploy_audit.py) are invoked as subprocesses / read-only imports only.
  * No database. Manifest is a single JSON file per batch.
  * No build, no push, no deploy here. Release stays a human gate.
  * Every dangerous write is atomic (temp + validate + os.replace) and is
    preceded by a verified backup. NO_BACKUP == STOP.

Phase P0 delivers only the safety primitives:
    ids.py       Batch ID
    manifest.py  Batch Manifest (atomic, crash-safe)
    backup.py    Pre-Batch Backup (hard gate)
    master_io.py Atomic Master Write
    dedup.py     Duplicate Guard
    gate.py      Exception Gate

Content enrichment is NOT implemented in this phase; only the reserved
`content_enrichment` manifest field exists (default NOT_AVAILABLE).
"""

__version__ = "0.1.0-p0"

# Roots are overridable via env so unit tests can run against a temp sandbox
# and never touch production data.
DEFAULT_BATCH_ROOT = r"D:\SZ Procure\03_MASTER\batches"
DEFAULT_BACKUP_ROOT = r"D:\SZ Procure\05_BACKUP"

# Files that must be preserved byte-for-byte before any MASTER mutation.
BACKUP_FILES = (
    ("master_C", r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\data\production\master_parts_v2.1.csv"),
    ("master_D", r"D:\SZ Procure\03_MASTER\product_master\master_parts_v2.1.csv"),
    ("publish_C", r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\data\production\master_parts_publish.csv"),
    ("datasheet_map", r"D:\SZ Procure\02_CLEAN\datasheet_map.csv"),
    ("datasheet_map_summary", r"D:\SZ Procure\02_CLEAN\datasheet_map_summary.json"),
    ("normalize_report", r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\tools\normalize_report.json"),
    ("audit_exemptions", r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\tools\audit_exemptions.json"),
    # --- pipeline scripts -------------------------------------------------
    # These are NOT tracked by git (`git ls-files tools/` shows only
    # publish_normalizer.py and pre_deploy_audit.py), yet the Factory modifies
    # them. Without including them here there would be no rollback copy.
    ("apply_datasheet_map_py", r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\tools\apply_datasheet_map.py"),
    ("upload_datasheets_py", r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\tools\upload_datasheets.py"),
    ("build_datasheet_map_py", r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\tools\build_datasheet_map.py"),
)

MASTER_COLS = ["mpn", "clean_mpn", "manufacturer", "brand", "url_slug", "category",
               "subcategory", "description", "applications", "keywords", "attributes_json",
               "availability", "alternative_parts", "datasheet_url", "faq", "image",
               "source", "source_url", "supplier_reference"]

REQUIRED_FIELDS = ("mpn", "manufacturer", "category", "description")

# MASS_DUPLICATE batch-level STOP gate.
# BOTH conditions must hold, so small / trial batches are not tripped by a
# single stray duplicate (e.g. 1 duplicate in 3 candidates = 33% must NOT stop).
MASS_DUPLICATE_RATE = 0.20
MASS_DUPLICATE_MIN_COUNT = 5
