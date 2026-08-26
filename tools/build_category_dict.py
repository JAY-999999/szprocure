# -*- coding: utf-8 -*-
"""Build Category raw capture + Category_Dictionary_v1 from LCSC catalog HTML.
Pure data asset. Does NOT touch gen_parts.py / CATEGORY_MAP / frontend.
Read-only input: tools/_lcsc_catalog.html (captured by explore_catalog.py).

NOTE: LCSC reuses the same catalog id across multiple tree branches (a node can
appear under several parents). We therefore stamp each row's L1 + parent during
the document-order pass (branch-local context) instead of resolving by id lookup,
which would collapse duplicates to the wrong branch.
"""
import re, html, csv, os
from collections import Counter

HTML = r"C:/Users/Administrator.SC-202105071542/Desktop/szprocure-site/tools/_lcsc_catalog.html"
OUT_RAW = r"D:/SZ Procure/03_Category/category_raw_lcsc.csv"
OUT_DICT = r"D:/SZ Procure/03_Category/Category_Dictionary_v1.csv"

# L1 lcsc_id -> (curated canonical L1 English name, legacy_l1_slug bridging to frozen slug)
L1_CANON = {
    312: ("Passive Components", "passive-components"),
    308: ("Passive Components", "passive-components"),
    316: ("Passive Components", "passive-components"),
    10991: ("Passive Components", "passive-components"),
    348: ("Passive Components", "passive-components"),
    319: ("Semiconductors & Discrete", "semiconductor-components"),
    320: ("Semiconductors & Discrete", "semiconductor-components"),
    395: ("Semiconductors & Discrete", "semiconductor-components"),
    13436: ("Semiconductors & Discrete", "semiconductor-components"),
    13437: ("Semiconductors & Discrete", "semiconductor-components"),
    13434: ("Semiconductors & Discrete", "semiconductor-components"),
    13433: ("Semiconductors & Discrete", "semiconductor-components"),
    380: ("Power Management", "integrated-circuits"),
    470: ("Integrated Circuits", "integrated-circuits"),
    487: ("Integrated Circuits", "integrated-circuits"),
    493: ("Integrated Circuits", "integrated-circuits"),
    601: ("Integrated Circuits", "integrated-circuits"),
    986: ("Integrated Circuits", "integrated-circuits"),
    515: ("Integrated Circuits", "integrated-circuits"),
    575: ("Integrated Circuits", "integrated-circuits"),
    582: ("Integrated Circuits", "integrated-circuits"),
    13504: ("Integrated Circuits", "integrated-circuits"),
    500: ("Memory", "integrated-circuits"),
    450: ("Optoelectronics", ""),
    13435: ("Optoelectronics", ""),
    513: ("Sensors", "sensors"),
    13511: ("Sensors", "sensors"),
    365: ("Connectors & Electromechanical", "connectors"),
    11304: ("Connectors & Electromechanical", "connectors"),
    423: ("Connectors & Electromechanical", "connectors"),
    13644: ("Connectors & Electromechanical", "connectors"),
    11220: ("Modules & Communication", "modules"),
    938: ("Modules & Communication", "modules"),
    13485: ("Modules & Communication", "modules"),
    953: ("Development Tools & Boards", ""),
    11232: ("Development Tools & Boards", ""),
    385: ("Audio & Signal", ""),
    570: ("Test & Measurement", ""),
    440: ("Test & Measurement", ""),
    11432: ("Industrial & Mechanical", ""),
    11440: ("Industrial & Mechanical", ""),
    11461: ("Industrial & Mechanical", ""),
    11475: ("Industrial & Mechanical", ""),
    11516: ("Industrial & Mechanical", ""),
    12805: ("Industrial & Mechanical", ""),
    13158: ("Industrial & Mechanical", ""),
    11315: ("Industrial & Mechanical", ""),
    11358: ("Industrial & Mechanical", ""),
    11209: ("Industrial & Mechanical", ""),
    11337: ("Industrial & Mechanical", ""),
    11370: ("Industrial & Mechanical", ""),
    11385: ("Industrial & Mechanical", ""),
    11395: ("Industrial & Mechanical", ""),
    11410: ("Industrial & Mechanical", ""),
    11497: ("Industrial & Mechanical", ""),
    13108: ("Industrial & Mechanical", ""),
}

def slugify(s):
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s

def clean_name(text):
    t = re.sub(r'\s*[（(][\d,]+[)）]\s*$', '', text).strip()
    return t

def parse():
    h = open(HTML, encoding="utf-8").read()
    pat = re.compile(r'<a[^>]*href="(https?://list\.szlcsc\.com/catalog/(\d+)\.html[^"]*)"[^>]*>(.*?)</a>', re.S)
    items = []
    for m in pat.finditer(h):
        href, cid, inner = m.group(1), int(m.group(2)), m.group(3)
        text = html.unescape(re.sub(r'<[^>]+>', '', inner)).strip()
        sp = re.search(r'spm=([^&\s"]+)', href); spm = sp.group(1) if sp else ''
        if '.ls.ca.' in spm: lvl = 1
        elif '.ca.cal.' in spm: lvl = 2
        elif '.cal.ke.' in spm: lvl = 3
        else: lvl = 0
        if lvl == 0: continue
        items.append((lvl, cid, text, href))
    # document-order pass: stamp L1 + parent (branch-local, duplicate-id safe)
    cur1 = cur2 = None
    cur1_clean = cur2_clean = ""
    rows = []
    for lvl, cid, text, href in items:
        if lvl == 1:
            cur1, cur2 = cid, None
            cur1_clean, cur2_clean = clean_name(text), ""
            l1_id, parent_clean = cid, ""
        elif lvl == 2:
            parent_clean = cur1_clean
            cur2, cur2_clean = cid, clean_name(text)
            l1_id = cur1
        else:
            parent_clean = cur2_clean
            l1_id = cur1
        rows.append({"lcsc_id": cid, "level": lvl, "parent_id": (cur1 if lvl == 2 else (cur2 if lvl == 3 else None)),
                     "raw_full": text, "clean_name": clean_name(text),
                     "source_url": href.split('?')[0], "l1_id": l1_id, "parent_clean": parent_clean})
    return rows

def main():
    rows = parse()
    # 1) raw capture
    with open(OUT_RAW, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["lcsc_id", "level", "parent_id", "raw_name", "clean_name", "source_url"])
        for r in rows:
            w.writerow([r["lcsc_id"], r["level"], r["parent_id"] or "",
                        r["raw_full"], r["clean_name"], r["source_url"]])
    print("category_raw_lcsc.csv rows:", len(rows))

    # 2) dictionary (deterministic CAT id in tree order)
    out = []
    seen_slug = set()
    for i, r in enumerate(rows, 1):
        cid_str = f"CAT-{i:04d}"
        canon_name, legacy = L1_CANON.get(r["l1_id"], ("Uncategorized", ""))
        if r["level"] == 1:
            canonical_name = canon_name
            parent_canon = ""
            base_slug = slugify(canonical_name)
        else:
            canonical_name = r["clean_name"]
            parent_canon = r["parent_clean"]
            l1slug = slugify(canon_name)
            if re.search(r'[a-zA-Z]', r["clean_name"]):
                base_slug = f"{l1slug}-{slugify(r['clean_name'])}"
            else:
                base_slug = f"{l1slug}-lcsc-{r['lcsc_id']}"
        # unique slug
        seo = base_slug
        k = 1
        while seo in seen_slug:
            k += 1
            seo = f"{base_slug}-{r['lcsc_id']}" if k == 2 else f"{base_slug}-{k}"
        seen_slug.add(seo)
        status = "active"
        tier = "A" if r["level"] == 1 else "B"
        out.append([cid_str, r["clean_name"], canonical_name, parent_canon,
                    r["level"], seo, legacy, status, tier])

    with open(OUT_DICT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["category_id", "raw_name", "canonical_name", "parent",
                    "level", "seo_slug", "legacy_l1_slug", "status", "tier"])
        w.writerows(out)
    print("Category_Dictionary_v1.csv rows:", len(out))
    print("L1 raw:", sum(1 for r in rows if r["level"] == 1))
    print("L2 raw:", sum(1 for r in rows if r["level"] == 2))
    print("L3 raw:", sum(1 for r in rows if r["level"] == 3))
    print("curated canonical... L1 buckets used:", len(set(c for c, _ in L1_CANON.values())))
    uncat = sum(1 for r in rows if L1_CANON.get(r["l1_id"], ("x",""))[0] == "Uncategorized")
    print("rows resolving to Uncategorized:", uncat)
    print("unique seo_slugs:", len(seen_slug))

if __name__ == "__main__":
    main()
