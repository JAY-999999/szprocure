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

import os
import json

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

MASTER_COLS = ["mpn", "clean_mpn", "manufacturer", "brand", "url_slug",
               "description", "applications", "keywords", "attributes_json",
               "availability", "alternative_parts", "datasheet_url", "faq", "image",
               "source", "source_url", "supplier_reference", "native_l1",
               # P0-1 fix (2026-09-19): LCSC parent-chain captured from the REAL
               # scale500 JSON RAW — previously 02 CLEAN dropped it. Order MUST
               # stay identical to clean_factory.MASTER_HEADER below.
               "lcsc_parent_chain", "lcsc_leaf_id", "lcsc_leaf_name",
               "lcsc_parent_id", "lcsc_parent_name", "lcsc_depth"]

REQUIRED_FIELDS = ("mpn", "manufacturer", "description")


# ----------------------------------------------------------------------------- #
# P0-1 fix (2026-09-19): LCSC parent-chain capture from the REAL scale500 RAW
# ----------------------------------------------------------------------------- #
# 02 CLEAN previously computed native_l1 (from parentCatalogName/catalogName)
# but DROPPED the full ancestor chain (parentCatalogList). This helper fixes
# that at the SOURCE: it reads the same scale500 JSON RAW that native_l1_mapper
# uses (L1_RAWDIR), so the parent chain is capture-driven (we map whatever the
# RAW actually contains, never a static pre-map) and stays consistent with
# native_l1. Fail-safe: returns None on any missing/empty/unreadable RAW so
# callers can leave the columns blank (never guessed).
def _extract_lcsc_chain(supplier_reference):
    """Return LCSC parent-chain metadata for a SKU's supplier_reference (C-number).

    Reads the scale500 JSON RAW via native_l1_mapper.L1_RAWDIR (single source of
    truth for the RAW dir) and returns a dict:
        parent_chain : JSON string of [{id,name}, ...] ordered root -> leaf
        leaf_id, leaf_name, parent_id, parent_name, depth
    or None when nothing can be derived (empty ref / missing file / bad JSON).
    """
    if not supplier_reference:
        return None
    # Lazy import keeps native_l1_mapper (the canonical home of L1_RAWDIR) as the
    # single source of truth for the RAW directory — no duplicate path constant.
    try:
        import native_l1_mapper as nl1_mod
    except Exception:
        return None
    f = os.path.join(nl1_mod.L1_RAWDIR, "%s.json" % supplier_reference)
    if not os.path.exists(f):
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        mp = d["source_raw"]["main_product"]
    except Exception:
        return None
    pcl = mp.get("parentCatalogList") or []
    leaf_name = mp.get("catalogName") or ""
    leaf_id = mp.get("catalogId")
    chain = []
    for node in pcl:
        cid = node.get("catalogId")
        cname = node.get("catalogNameEn") or node.get("catalogName") or ""
        if not cname:
            continue
        # NAME is the stable key: LCSC reuses the same catalog NAME with
        # different IDs across fields (e.g. direct-parent id 380 vs list 263),
        # so we keep the human name, not the id.
        # Dedup: LCSC sometimes lists the LEAF itself as the LAST parentCatalogList
        # entry (e.g. pcl=[ICs, Memory] with leaf=Memory). Skip any pcl node whose
        # name equals the leaf name so we never produce [ICs, Memory, Memory]
        # (which would double the depth). Bug-1 fix (2026-09-19).
        if cname == leaf_name:
            continue
        chain.append({"id": cid, "name": cname})
    if leaf_name:
        chain.append({"id": leaf_id, "name": leaf_name})
    if not chain:
        return None
    # Bug-2 fix (2026-09-19): parent_id/parent_name must come from the chain's
    # immediate parent (chain[-2]), NOT from the JSON envelope's separate
    # parentCatalogName/parentCatalogId fields — those describe a DIFFERENT, finer
    # "functional parent" tree (e.g. ACS712 leaf "Current Sensors" but envelope
    # parentCatalogName="Magnetic Sensors"). Keeping parent aligned to the chain
    # makes the whole lcsc_* column group internally self-consistent.
    if len(chain) >= 2:
        parent_node = chain[-2]
        parent_id = parent_node["id"]
        parent_name = parent_node["name"]
    else:
        parent_id = None
        parent_name = ""
    return {
        "parent_chain": json.dumps(chain, ensure_ascii=False),
        "leaf_id": leaf_id,
        "leaf_name": leaf_name,
        "parent_id": parent_id,
        "parent_name": parent_name,
        "depth": len(chain),
    }

# MASS_DUPLICATE batch-level STOP gate.
# BOTH conditions must hold, so small / trial batches are not tripped by a
# single stray duplicate (e.g. 1 duplicate in 3 candidates = 33% must NOT stop).
MASS_DUPLICATE_RATE = 0.20
MASS_DUPLICATE_MIN_COUNT = 5
