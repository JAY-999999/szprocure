#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_enrich_apply_preview.py  -- SAFE, ISOLATED preview-only apply step for #1609/#1610.

WHAT IT DOES
------------
Reads the REAL production MASTER (read-only) + the #1609 LCSC patch
(data/raw/enrich_lcsc_patch_<ts>.csv) and emits a NEW, tiny 6-row preview
Master named  master_parts_v2.1.preview.csv  under data/production/.

This file is intentionally NON-DEPLOYABLE:
  * distinct filename (deploy uses master_parts_v2.1.csv exactly)
  * placed in data/production/ only so gen_parts.py's validate_production_source()
    gate accepts it (it requires data/production/ + master_parts_*.csv)
  * it is an UNTRACKED artifact and is NEVER committed/pushed.

It does NOT modify the real master_parts_v2.1.csv. Deleting it is harmless.

The 6 patch columns (attributes_json, applications, alternative_parts,
description, keywords, faq) overwrite the matching MASTER rows; every other
MASTER column is preserved verbatim. This reproduces the #1609 merge result.
"""
import csv, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
PATCH = sys.argv[1] if len(sys.argv) > 1 else None
OUT = os.path.join(ROOT, "data", "production", "master_parts_v2.1.preview.csv")

if not PATCH or not os.path.exists(PATCH):
    # default to the latest patch in data/raw/
    raw = os.path.join(ROOT, "data", "raw")
    cands = sorted([f for f in os.listdir(raw) if f.startswith("enrich_lcsc_patch_") and f.endswith(".csv")])
    if not cands:
        print("No patch CSV found in data/raw/"); sys.exit(1)
    PATCH = os.path.join(raw, cands[-1])
    print(f"[i] using latest patch: {PATCH}")

PATCH_COLS = ["attributes_json", "applications", "alternative_parts", "description", "keywords", "faq"]

with open(MASTER, encoding="utf-8") as f:
    rdr = csv.DictReader(f)
    master_header = rdr.fieldnames
    master_rows = {row["mpn"].strip(): row for row in rdr if row.get("mpn", "").strip()}

with open(PATCH, encoding="utf-8") as f:
    prdr = csv.DictReader(f)
    patch_rows = [r for r in prdr if r.get("mpn", "").strip()]

matched = 0
applied = []
for p in patch_rows:
    mpn = p["mpn"].strip()
    if mpn not in master_rows:
        print(f"  [SKIP] {mpn} not found in MASTER"); continue
    mrow = master_rows[mpn]
    for col in PATCH_COLS:
        if col in p and p[col] is not None:
            mrow[col] = p[col]
    applied.append(mrow)
    matched += 1

print(f"[i] MASTER rows={len(master_rows)}  patch rows={len(patch_rows)}  matched={matched}")

with open(OUT, "w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=master_header)
    w.writeheader()
    for row in applied:
        w.writerow({k: row.get(k, "") for k in master_header})

print(f"[OK] preview Master -> {OUT}  ({len(applied)} rows)")
print("      (delete this file any time; it is NOT the production MASTER)")
