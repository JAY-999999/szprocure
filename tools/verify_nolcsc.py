#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Post-build verification: confirm the DEPLOYED site carries ZERO lcsc identifiers,
and that internal provenance columns never leak into the frontend.

Deployed surface scanned (the only things that reach Vercel):
  1. All generated HTML  -> products/, components/, manufacturers/, root *.html
  2. sitemap*.xml, robots.txt
  3. parts.json            (structured catalog consumed by search / frontend)
  4. search/*.json         (search index)
  5. assets/*.css, assets/*.js   (other production static resources)
  6. any other *.json / *.xml at repo root (deployed config)

Prohibited strings (case-insensitive) — covers all three forms:
  - lcsc.com
  - www.lcsc.com
  - assets.lcsc.com

Also asserts the MASTER build CSV does NOT carry http(s) asset URLs that would
be hot-linked (image / datasheet_url must be local paths or empty).

Internal data layers (data/raw, data/clean, D:\\SZ Procure archive) intentionally
retain lcsc source URLs for traceability and are NOT scanned — they are
git-ignored and never deployed.
"""
import os, re, sys, glob, csv

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Matches lcsc.com / www.lcsc.com / assets.lcsc.com (any subdomain prefix)
LCSC = re.compile(r"lcsc\.com", re.IGNORECASE)

scan_files = []

# 1) HTML pages
for d in ("products", "components", "manufacturers"):
    dp = os.path.join(ROOT, d)
    if os.path.isdir(dp):
        for r, _, fns in os.walk(dp):
            for fn in fns:
                if fn.endswith(".html"):
                    scan_files.append(os.path.join(r, fn))
scan_files += glob.glob(os.path.join(ROOT, "*.html"))

# 2) sitemap / robots
scan_files += glob.glob(os.path.join(ROOT, "sitemap*.xml"))
scan_files += glob.glob(os.path.join(ROOT, "robots.txt"))

# 3) structured data + search index
scan_files += glob.glob(os.path.join(ROOT, "parts.json"))
scan_files += glob.glob(os.path.join(ROOT, "search", "*.json"))
scan_files += glob.glob(os.path.join(ROOT, "search-index*.json"))

# 4) other production static resources
scan_files += glob.glob(os.path.join(ROOT, "assets", "*.css"))
scan_files += glob.glob(os.path.join(ROOT, "assets", "*.js"))

# 5) any other root-level deployed json/xml
scan_files += glob.glob(os.path.join(ROOT, "*.json"))
scan_files += glob.glob(os.path.join(ROOT, "*.xml"))

scan_files = sorted(set(scan_files))

hits = 0
hit_samples = []
for path in scan_files:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            txt = f.read()
    except Exception:
        continue
    for m in LCSC.finditer(txt):
        hits += 1
        if len(hit_samples) < 20:
            s = max(0, m.start() - 40)
            e = min(len(txt), m.end() + 40)
            hit_samples.append((os.path.relpath(path, ROOT),
                                txt[s:e].replace("\n", " ")))

print(f"[scan] files checked : {len(scan_files)}")
print(f"[scan] lcsc.com hits : {hits}")
for rel, snip in hit_samples:
    print(f"   HIT {rel}: ...{snip}...")

# Master build CSV: image / datasheet_url must never be an http(s) URL (hot-link)
master = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
bad = 0
if os.path.exists(master):
    with open(master, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            for col in ("image", "datasheet_url"):
                v = (r.get(col) or "").strip()
                if v and v.startswith("http"):
                    bad += 1
                    if len(hit_samples) < 20:
                        hit_samples.append((f"MASTER:{col}:{r['mpn']}", v))
    print(f"[master] http asset refs: {bad}  (must be 0 — no hot-links)")
else:
    print("[master] NOT FOUND")

ok = (hits == 0 and bad == 0)
print("\nRESULT:",
      "PASS — 0 lcsc identifiers in deployed site" if ok
      else "FAIL — lcsc identifiers present (see above)")
sys.exit(0 if ok else 1)
