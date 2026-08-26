# -*- coding: utf-8 -*-
"""Build Manufacturer raw capture + Manufacturer_Dictionary_v1 + alias mapping.
Layer1: manufacturer_raw_lcsc.csv  (raw_name, source, source_url)
Layer2: Manufacturer_Dictionary_v1.csv (canonical_brand, manufacturer_id, status, tier)
Layer3: manufacturer_alias_mapping.csv (raw_name -> canonical_brand + id + method)
Seed: data/mfr_canonical.csv (human-confirmed 73 canonical / 246 aliases).
Rules: normalization -> existing canonical match; SHORT abbreviations (ST/AD/TI...)
       are NOT auto-merged -> flagged status=review, tier=A for human confirm.
canonical_brand kept as given (English legal name preferred in seed); Chinese kept as alias.
Deterministic MFR-XXXX ids (stable across re-runs).
"""
import json, csv, re, os
from collections import OrderedDict

JSON = r"C:/Users/Administrator.SC-202105071542/Desktop/szprocure-site/tools/_lcsc_brands.json"
SEED = r"C:/Users/Administrator.SC-202105071542/Desktop/szprocure-site/data/mfr_canonical.csv"
OUT_RAW = r"D:/SZ Procure/02_Product_DB/manufacturer_raw_lcsc.csv"
OUT_DICT = r"D:/SZ Procure/02_Product_DB/Manufacturer_Dictionary_v1.csv"
OUT_ALIAS = r"D:/SZ Procure/02_Product_DB/manufacturer_alias_mapping.csv"

DESC_KW = ["有限公司", "公司", "研发", "高新技术", "企业", "专注于", "是一家", "主要", "产品", "导体", "新能源"]

def norm(s):
    s = (s or "").lower().strip()
    s = re.sub(r'[®™®]', '', s)
    s = re.sub(r'[\s]+', ' ', s)
    s = s.strip()
    return s

def is_blob(name):
    if len(name) > 40:
        return True
    if any(k in name for k in DESC_KW):
        return True
    return False

def is_short_abbrev(name):
    n = name.strip()
    if len(n) <= 3:
        return True
    # all-uppercase 2-4 letter token
    if n.isupper() and len(n) <= 4 and n.isalpha():
        return True
    return False

def main():
    # ---- Layer 1: raw brands from LCSC ----
    data = json.load(open(JSON, encoding="utf-8"))
    seen = set()
    raw = []
    for d in data:
        name = (d.get("name") or "").strip()
        url = (d.get("url") or "").strip()
        if not name:
            continue
        if is_blob(name):
            continue
        nk = norm(name)
        if nk in seen:
            continue
        seen.add(nk)
        raw.append({"raw_name": name, "source": "LCSC", "source_url": url})
    # write raw
    with open(OUT_RAW, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["raw_name", "source", "source_url"])
        for r in raw:
            w.writerow([r["raw_name"], r["source"], r["source_url"]])
    print("manufacturer_raw_lcsc.csv rows:", len(raw))

    # ---- Seed: mfr_canonical.csv ----
    seed_alias = {}      # norm(raw) -> canonical
    seed_canon_norm = {} # norm(canonical) -> canonical
    seed_canon_set = set()
    with open(SEED, encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t"):
            if len(row) < 2:
                continue
            if row[0].strip() == "raw_name":
                continue
            rn, cn = row[0].strip(), row[1].strip()
            seed_canon_set.add(cn)
            seed_canon_norm[norm(cn)] = cn
            seed_alias[norm(rn)] = cn
    print("seed canonical:", len(seed_canon_set), "seed aliases:", len(seed_alias))

    # ---- Match each raw -> canonical ----
    alias_rows = []
    new_canons = set()
    for r in raw:
        nr = norm(r["raw_name"])
        if nr in seed_alias:
            canon = seed_alias[nr]
            method = "seed_alias"
        elif nr in seed_canon_norm:
            canon = seed_canon_norm[nr]
            method = "seed_canonical"
        else:
            canon = r["raw_name"].strip()
            method = "new"
            new_canons.add(canon)
        alias_rows.append({"raw": r["raw_name"].strip(), "canon": canon,
                           "method": method})

    # ---- Deterministic MFR-XXXX ids over all canonicals ----
    all_canon = sorted(set(seed_canon_set) | new_canons)
    cid_of = {c: f"MFR-{i+1:05d}" for i, c in enumerate(all_canon)}

    # ---- status / tier per canonical ----
    canon_status = {}
    for c in all_canon:
        canon_status[c] = {"status": "candidate", "tier": "B"}
    # seed canonicals = active / A (human-confirmed)
    for c in seed_canon_set:
        canon_status[c] = {"status": "active", "tier": "A"}
    # new canonicals: short abbrev -> review/A, else candidate/B
    for c in new_canons:
        if is_short_abbrev(c):
            canon_status[c] = {"status": "review", "tier": "A"}
        else:
            canon_status[c] = {"status": "candidate", "tier": "B"}

    # ---- Layer 2: dictionary (distinct canonical) ----
    with open(OUT_DICT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["canonical_brand", "manufacturer_id", "status", "tier"])
        for c in all_canon:
            st = canon_status[c]
            w.writerow([c, cid_of[c], st["status"], st["tier"]])
    print("Manufacturer_Dictionary_v1.csv canonical rows:", len(all_canon))

    # ---- Layer 3: alias mapping (per raw) ----
    with open(OUT_ALIAS, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["raw_name", "canonical_brand", "manufacturer_id", "match_method"])
        for a in alias_rows:
            w.writerow([a["raw"], a["canon"], cid_of[a["canon"]], a["method"]])
    print("manufacturer_alias_mapping.csv rows:", len(alias_rows))

    # ---- quick metrics ----
    from collections import Counter
    merged = sum(1 for a in alias_rows if a["method"] in ("seed_alias", "seed_canonical"))
    unmatched = sum(1 for a in alias_rows if a["method"] == "new")
    print("merged(raw->existing canonical):", merged)
    print("unmatched(standalone new canonical):", unmatched)
    print("alias mappings where raw!=canonical:",
          sum(1 for a in alias_rows if norm(a['raw']) != norm(a['canon'])))

if __name__ == "__main__":
    main()
