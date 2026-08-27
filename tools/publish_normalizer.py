#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUBLISH_NORMALIZER — pre-publish processing layer (MASTER -> website).

    MASTER  (data/production/master_parts_v2.1.csv)   [untouched]
        |
        v  publish_normalizer.py  (3-tier English normalization)
        |
        v  only attributes_json is rewritten:
        |
        |  KEY translation (parameter name):
        |    tier-1  attribute_dictionary.json  -> curated human-standard (HIGHEST priority)
        |    tier-2  LCSC paramNameEn (from RAW) -> ONLY if pure ASCII
        |             (NEVER passthrough Chinese; if non-ascii, falls through to "untranslated")
        |    else    keep original (will trip the CJK gate -> signals "needs curation")
        |
        |  VALUE translation (parameter value):
        |    value_map  (longest-first substring)  -> English fragments
        |    unit_map   (℃ -> °C, full-width -> half-width, etc.)
        |    full-width ASCII folding
        |    tier-2 fallback: LCSC paramValueEn (ascii-only) for residual CJK
        |
        v
    PUBLISH (data/production/master_parts_publish.csv)  [build input only; git-ignored]

gen_parts.py then consumes master_parts_publish.csv (frozen builder, unchanged).
ALL other columns are copied verbatim, so URL / RFQ / Schema / SEO are unchanged.

Residual-CJK self-check: any remaining CJK in attributes after normalization is a
FAILURE (exit 1). The permanent launch gate (pre_deploy_audit.py) independently
re-scans the built HTML/JSON, masking the deliberate data-zh i18n metadata.

Run:
    python tools/publish_normalizer.py
    python tools/publish_normalizer.py --master ... --out ...
Exit 0 = zero residual CJK; 1 = residual present (do NOT deploy).
"""
import csv, json, os, re, argparse, sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MASTER   = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
DEFAULT_OUT      = os.path.join(ROOT, "data", "production", "master_parts_publish.csv")
DEFAULT_DICT     = os.path.join(ROOT, "tools", "attribute_dictionary.json")
DEFAULT_VT       = os.path.join(ROOT, "tools", "value_translation.json")
DEFAULT_FB       = os.path.join(ROOT, "data", "production", "lcsc_en_fallback.json")
DEFAULT_RAW      = os.path.join(ROOT, "data", "raw", "lcsc_api_FULL_20260827.json")
DEFAULT_REPORT   = os.path.join(ROOT, "tools", "normalize_report.json")

CJK = re.compile(r"[一-鿿]")     # CJK unified ideographs

def is_ascii(s):
    return bool(s) and all(ord(c) < 128 for c in s)

# ---- full-width ASCII -> half-width (does NOT touch CJK or ℃) ----
def fw_fold(s):
    out = []
    for c in s:
        o = ord(c)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        elif c == "\u3000":
            out.append(" ")
        else:
            out.append(c)
    return "".join(out)

def load_dictionary(path):
    d = json.load(open(path, encoding="utf-8"))
    return d["keys"]

def load_value_translation(path):
    d = json.load(open(path, encoding="utf-8"))
    vm = sorted(d["value_map"], key=lambda e: -len(e["zh"]))
    um = sorted(d.get("unit_map", []), key=lambda e: -len(e["zh"]))
    return vm, um

def build_fallback_from_raw(raw_path):
    raw = json.load(open(raw_path, encoding="utf-8"))
    key_en = defaultdict(Counter); val_en = defaultdict(Counter)
    for p in raw:
        for pv in (p.get("paramVOList") or []):
            cn = (pv.get("paramName") or "").strip()
            en = (pv.get("paramNameEn") or "").strip()
            if cn and is_ascii(en):
                key_en[cn][en] += 1
            cv = (pv.get("paramValue") or "").strip()
            ev = (pv.get("paramValueEn") or "").strip()
            if cv and CJK.search(cv) and is_ascii(ev):
                val_en[cv][ev] += 1
    def pick(counter):
        items = sorted(counter.items(), key=lambda kv: (-kv[1], len(kv[0]), kv[0]))
        return items[0][0] if items else None
    return {k: pick(c) for k, c in key_en.items() if pick(c)}, \
           {k: pick(c) for k, c in val_en.items() if pick(c)}

def load_fallback(fb_path, raw_path):
    if os.path.exists(fb_path):
        fb = json.load(open(fb_path, encoding="utf-8"))
        return fb.get("key_en", {}), fb.get("value_en", {})
    if os.path.exists(raw_path):
        print("[norm] fallback cache missing; building from RAW (one-time)...")
        k, v = build_fallback_from_raw(raw_path)
        json.dump({"key_en": k, "value_en": v},
                  open(fb_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        return k, v
    print("[norm] WARNING: no fallback cache and no RAW; tier-2 disabled.")
    return {}, {}

def translate_key(zh, dict_keys, fb_key, stats):
    # tier-1: curated dictionary (highest priority)
    if zh in dict_keys:
        stats["key_tier1"] += 1
        return dict_keys[zh]
    # tier-2: LCSC paramNameEn, only if pure ASCII (never passthrough Chinese)
    en = fb_key.get(zh)
    if en and is_ascii(en):
        stats["key_tier2"] += 1
        return en
    stats["key_untranslated"] += 1
    return zh   # untranslated -> CJK gate will flag

def translate_value(zh, value_map, unit_map, fb_val, stats):
    s = zh
    for e in value_map:
        if e["zh"] in s:
            s = s.replace(e["zh"], e["en"])
    for e in unit_map:
        if e["zh"] in s:
            s = s.replace(e["zh"], e["en"])
    s = fw_fold(s)
    s = re.sub(r"\s+", " ", s).strip()
    # tier-2 fallback for residual CJK: whole-value paramValueEn (ascii-only)
    if CJK.search(s):
        en = fb_val.get(zh)
        if en and is_ascii(en):
            stats["val_tier2"] += 1
            return en
        stats["val_untranslated"] += 1
    else:
        stats["val_ok"] += 1
    return s

def normalize_attributes(raw, dict_keys, value_map, unit_map, fb_key, fb_val, stats):
    raw = (raw or "").strip()
    if not raw:
        return raw, 0
    try:
        d = json.loads(raw)
    except Exception:
        return raw, 0
    if not isinstance(d, dict):
        return raw, 0
    out = {}
    residual = 0
    for k, val in d.items():
        nk = translate_key(k, dict_keys, fb_key, stats)
        if CJK.search(nk):
            residual += 1
        nv = val
        if isinstance(val, str):
            nv = translate_value(val, value_map, unit_map, fb_val, stats)
            if CJK.search(nv):
                residual += 1
        out[nk] = nv
    return json.dumps(out, ensure_ascii=False), residual

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--master", default=DEFAULT_MASTER)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--dict", default=DEFAULT_DICT)
    ap.add_argument("--vt", default=DEFAULT_VT)
    ap.add_argument("--fallback", default=DEFAULT_FB)
    ap.add_argument("--raw", default=DEFAULT_RAW)
    ap.add_argument("--report", default=DEFAULT_REPORT)
    args = ap.parse_args()

    dict_keys = load_dictionary(args.dict)
    value_map, unit_map = load_value_translation(args.vt)
    fb_key, fb_val = load_fallback(args.fallback, args.raw)

    stats = {"key_tier1":0,"key_tier2":0,"key_untranslated":0,
             "val_ok":0,"val_tier2":0,"val_untranslated":0}
    rows = list(csv.DictReader(open(args.master, encoding="utf-8")))
    fieldnames = list(rows[0].keys()) if rows else []

    residual_rows = []
    changed = 0
    out_rows = []
    for r in rows:
        raw = (r.get("attributes_json") or "").strip()
        if not raw:
            out_rows.append(r)
            continue
        new_json, resid = normalize_attributes(
            raw, dict_keys, value_map, unit_map, fb_key, fb_val, stats)
        if new_json != raw:
            changed += 1
        if resid:
            residual_rows.append({
                "mpn": r.get("mpn", ""),
                "residual_cjk": resid,
                "normalized": new_json[:300],
            })
        nr = dict(r)
        nr["attributes_json"] = new_json
        out_rows.append(nr)

    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    report = {
        "master": os.path.relpath(args.master, ROOT),
        "out": os.path.relpath(args.out,  ROOT),
        "rows": len(out_rows),
        "rows_changed": changed,
        "stats": stats,
        "residual_cjk_products": len(residual_rows),
        "residual_sample": residual_rows[:20],
    }
    with open(args.report, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"[norm] rows={len(out_rows)} changed={changed}")
    print(f"[norm] key tiers: tier1(dict)={stats['key_tier1']} "
          f"tier2(lcsc_en)={stats['key_tier2']} untranslated={stats['key_untranslated']}")
    print(f"[norm] val tiers: ok={stats['val_ok']} "
          f"tier2(lcsc_en)={stats['val_tier2']} untranslated={stats['val_untranslated']}")
    print(f"[norm] residual CJK products = {len(residual_rows)}")
    for s in residual_rows[:10]:
        print(f"   RESIDUAL {s['mpn']} ({s['residual_cjk']}): {s['normalized']}")
    print(f"[norm] wrote {args.out}")
    print(f"[norm] report -> {args.report}")
    sys.exit(0 if not residual_rows else 1)

if __name__ == "__main__":
    main()
