#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data Factory v1 — Cleaning Pipeline (Raw Supplier CSV -> Master Parts DB).

Reads realistic supplier export CSVs (LCSC / HQEW / edge cases), parses the
supplier-native `attributes` free-text field ("Key: value; ...") into a
canonical attributes_json, and emits a 16-column Master Parts CSV that
gen_parts.py consumes directly.

Reuses gen_parts.py's FROZEN normalizers as the single source of truth:
  - LEGACY_ATTR_MAP       (legacy alias -> canonical key)
  - load_attr_allowlist   (§4 allowlist, single source of truth)
  - load_mfr_canonical / canonicalize_brand
  - resolve_cat           (fine cat -> 6 top-level)
  - slugify / norm_mpn
"""
import csv, os, re, sys, json, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gen_parts as gp

MASTER_HEADER = ["mpn", "clean_mpn", "manufacturer", "brand", "url_slug",
                 "category", "subcategory", "description", "applications",
                 "keywords", "attributes_json", "availability",
                 "alternative_parts", "datasheet_url", "faq", "image"]


def parse_attrs_free(raw):
    """Parse 'Key: value; Key: value' into dict. Returns (dict, malformed).

    If the raw string looks like JSON ({ or [):
      - valid dict  -> return as dict, malformed=False
      - valid other -> return {'__raw__': raw}, malformed=True  (gen stage re-flags)
      - parse error -> return {'__raw__': raw}, malformed=True
    """
    if not raw or not raw.strip():
        return {}, False
    s = raw.strip()
    if s[0] in "{[":
        try:
            obj = json.loads(s)
        except Exception:
            return {"__raw__": s}, True
        if isinstance(obj, dict):
            return obj, False
        return {"__raw__": s}, True
    out = {}
    for chunk in s.split(";"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        k, v = chunk.split(":", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out, False


def normalize_attr_keys(d):
    """Apply LEGACY_ATTR_MAP (lower-cased key) -> canonical key. Returns (new, n_changed)."""
    out, n = {}, 0
    for k, v in d.items():
        nk = gp.LEGACY_ATTR_MAP.get(k.lower(), k)
        if nk != k:
            n += 1
        out[nk] = v
    return out, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", nargs="+", required=True, help="Raw supplier CSV(s)")
    ap.add_argument("--out", required=True, help="Output master CSV path")
    ap.add_argument("--report", required=True, help="Cleaning report .md path")
    ap.add_argument("--mfr-map", default=os.path.join(ROOT, "data", "mfr_canonical.csv"))
    ap.add_argument("--attr-dict", default=os.path.join(ROOT, "data", "attributes_dictionary.md"))
    args = ap.parse_args()

    mfr_map = gp.load_mfr_canonical(args.mfr_map)
    attr_allow = gp.load_attr_allowlist(args.attr_dict)

    rows_in = 0
    master_rows = []
    review = []  # (mpn, brand, reason, detail)
    stats = {"parsed_attrs": 0, "attr_normalized": 0, "unknown_attr_keys": 0,
             "unknown_brands": 0, "missing_mfr": 0, "malformed_attrs": 0}

    for path in args.raw:
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                mpn = (r.get("mpn") or "").strip()
                if not mpn:
                    continue
                rows_in += 1

                # ---- attributes ----
                raw_attrs = r.get("attributes") or ""
                adict, malformed = parse_attrs_free(raw_attrs)
                if malformed:
                    stats["malformed_attrs"] += 1
                else:
                    stats["parsed_attrs"] += 1
                needs_review_attrs = False
                unknown = set()
                if "__raw__" in adict:
                    raw_json = adict["__raw__"]          # keep raw string for gen stage
                    needs_review_attrs = True
                else:
                    adict, n_norm = normalize_attr_keys(adict)
                    stats["attr_normalized"] += n_norm
                    unknown = {k for k in adict if k not in attr_allow}
                    if unknown:
                        stats["unknown_attr_keys"] += len(unknown)
                    needs_review_attrs = bool(unknown)
                    raw_json = json.dumps(adict, ensure_ascii=False)

                # ---- brand ----
                raw_brand = (r.get("manufacturer") or "").strip()
                canon, matched = gp.canonicalize_brand(raw_brand, mfr_map)
                if not raw_brand:
                    stats["missing_mfr"] += 1
                    brand_reason = "missing_manufacturer"
                elif not matched:
                    stats["unknown_brands"] += 1
                    brand_reason = "unknown_manufacturer"
                else:
                    brand_reason = None

                # ---- category (fine cat, gen_parts re-resolves) ----
                fine_cat = (r.get("category") or "").strip()
                gp.resolve_cat(fine_cat)  # side-effect: prints WARN if unmapped (info only)

                # ---- mpn derivations ----
                clean_mpn = re.sub(r"[^A-Z0-9]", "", mpn.upper())
                url_slug = gp.slugify(mpn)

                # ---- assemble master row ----
                row = {
                    "mpn": mpn,
                    "clean_mpn": clean_mpn,
                    "manufacturer": canon,
                    "brand": canon,
                    "url_slug": url_slug,
                    "category": fine_cat,           # fine cat; gen_parts resolves to top
                    "subcategory": fine_cat,
                    "description": (r.get("description") or "").strip(),
                    "applications": "",
                    "keywords": "",
                    "attributes_json": raw_json,
                    "availability": "active",
                    "alternative_parts": "",
                    "datasheet_url": (r.get("datasheet_url") or "").strip(),
                    "faq": "",
                    "image": "",
                }
                if not raw_brand:
                    row["manufacturer"] = ""
                    row["brand"] = ""

                # ---- review reasons ----
                reasons = []
                if "__raw__" in adict or malformed:
                    reasons.append("malformed_attributes")
                for k in sorted(unknown):
                    reasons.append(f"unknown_attribute_key={k}")
                if brand_reason:
                    reasons.append(brand_reason)
                for rs in reasons:
                    review.append((mpn, canon or raw_brand, rs, ""))
                master_rows.append(row)

    # ---- write master CSV ----
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_HEADER)
        w.writeheader()
        for row in master_rows:
            w.writerow(row)

    # ---- write cleaning-stage review queue ----
    review_path = os.path.join(out_dir, "cleaning_review_queue.csv")
    with open(review_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mpn", "brand", "reason", "detail"])
        for mpn, b, rsn, det in review:
            w.writerow([mpn, b, rsn, det])

    # ---- cleaning report ----
    n_review = len(set((m, b, r) for m, b, r, _ in review))
    rep = []
    rep.append("# Data Factory v1 — Cleaning Report\n")
    rep.append(f"- Raw files processed : {len(args.raw)}")
    rep.append(f"- Rows in (non-empty MPN) : {rows_in}")
    rep.append(f"- Master rows out : {len(master_rows)}")
    rep.append(f"- Attributes parsed (free-text) : {stats['parsed_attrs']}")
    rep.append(f"- Attribute keys legacy-normalized : {stats['attr_normalized']}")
    rep.append(f"- Attributes malformed (kept raw) : {stats['malformed_attrs']}")
    rep.append(f"- Unknown attribute keys (needs_review) : {stats['unknown_attr_keys']}")
    rep.append(f"- Unknown brands (needs_review) : {stats['unknown_brands']}")
    rep.append(f"- Missing manufacturers (needs_review) : {stats['missing_mfr']}")
    rep.append(f"- Rows flagged needs_review : {n_review}\n")
    rep.append("## Cleaning-stage review queue\n")
    rep.append("| MPN | Brand | Reason |")
    rep.append("|-----|-------|--------|")
    for mpn, b, rsn, _ in review:
        rep.append(f"| {mpn} | {b} | {rsn} |")
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(rep) + "\n")

    print(f"Master        -> {args.out} ({len(master_rows)} rows)")
    print(f"Cleaning rep  -> {args.report}")
    print(f"Review queue  -> {review_path}")
    print(f"needs_review rows: {n_review}")


if __name__ == "__main__":
    main()
