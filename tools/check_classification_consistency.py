#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SZProcure 金水流自动上架系统 V1.0
分类链路一致性验收脚本 (read-only)

用途：
  在 MASTER.native_l1 已被确立为唯一分类源之后，作为防回归检查，
  只读校验以下链路的一致性：
    MASTER (data/production/master_parts_v2.1.csv)
      -> final_subcategory_map.json
      -> parts.json
      -> components/components-data.js
      -> page_manifest.json

特性：
  - 完全只读，不修改任何文件，不调用任何生成器。
  - 退出码 0 = 全部 PASS；非 0 = 至少有一项 FAIL。

运行：
  python tools/check_classification_consistency.py
  (在 site 仓库根目录，或任意目录均可，路径相对仓库根自动解析)
"""

import csv
import json
import os
import re
import sys
from datetime import datetime

csv.field_size_limit(10 ** 9)

FORBIDDEN = ["office-supplies", "fasteners-hardware", "industrial-control-electrical"]

# ---- 路径解析 -------------------------------------------------------------
def repo_root():
    """定位仓库根目录：优先用本脚本位置上溯。"""
    here = os.path.dirname(os.path.abspath(__file__))
    # 本脚本位于 <root>/tools/，根目录是上一级
    cand = os.path.dirname(here)
    if os.path.isfile(os.path.join(cand, "gen_parts.py")) or os.path.isdir(os.path.join(cand, "data")):
        return cand
    # 兜底：当前工作目录
    return os.getcwd()


ROOT = repo_root()
MASTER = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
FINAL_MAP = os.path.join(ROOT, "final_subcategory_map.json")
PARTS = os.path.join(ROOT, "parts.json")
COMPONENTS_JS = os.path.join(ROOT, "components", "components-data.js")
PAGE_MANIFEST = os.path.join(ROOT, "page_manifest.json")
TAXONOMY = os.path.join(ROOT, "data", "category_taxonomy.json")

EXPECT_UNIQUE_MPN = 8591
EXPECT_NATIVE_L1_EMPTY = 0


def load_master():
    rows = 0
    mpn_set = set()
    mpn_empty = 0
    native_l1_empty = 0
    native_l1_distinct = set()
    m = {}  # mpn -> native_l1
    with open(MASTER, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            rows += 1
            mpn = (row.get("mpn") or "").strip()
            if not mpn:
                mpn_empty += 1
                continue
            mpn_set.add(mpn)
            nl = (row.get("native_l1") or "").strip()
            if not nl:
                native_l1_empty += 1
            else:
                native_l1_distinct.add(nl)
            m[mpn] = nl
    return dict(
        rows=rows, mpn_set=mpn_set, mpn_empty=mpn_empty,
        native_l1_empty=native_l1_empty, native_l1_distinct=native_l1_distinct, m=m,
    )


def load_final_map():
    with open(FINAL_MAP, encoding="utf-8") as f:
        fm = json.load(f)
    return fm  # dict: mpn -> {final_subcategory, final_native_l1, final_slug}


def load_parts():
    with open(PARTS, encoding="utf-8") as f:
        data = json.load(f)
    pdict = {}
    for it in data:
        mpn = (it.get("mpn") or "").strip()
        if mpn:
            pdict[mpn] = it
    return data, pdict


def load_components_js_raw():
    with open(COMPONENTS_JS, encoding="utf-8") as f:
        return f.read()


def load_page_manifest():
    with open(PAGE_MANIFEST, encoding="utf-8") as f:
        return json.load(f)


def load_taxonomy_slugs():
    with open(TAXONOMY, encoding="utf-8") as f:
        tx = json.load(f)
    slugs = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "slug" and isinstance(v, str):
                    slugs.add(v)
                walk(v)
        elif isinstance(o, (list, tuple)):
            for x in o:
                walk(x)
    walk(tx)
    top_scopes = [t.get("slug") for t in tx.get("top_scopes", []) if isinstance(t, dict) and t.get("slug")]
    return slugs, top_scopes


# ---- 检查 -------------------------------------------------------------
def check_a(master):
    """MASTER 基准。"""
    ok = (len(master["mpn_set"]) == EXPECT_UNIQUE_MPN and
          master["native_l1_empty"] == EXPECT_NATIVE_L1_EMPTY)
    print("[A] MASTER 基准")
    print("    unique MPN        : %d  (期望 %d) %s" % (
        len(master["mpn_set"]), EXPECT_UNIQUE_MPN, "OK" if len(master["mpn_set"]) == EXPECT_UNIQUE_MPN else "FAIL"))
    print("    native_l1 empty  : %d  (期望 %d) %s" % (
        master["native_l1_empty"], EXPECT_NATIVE_L1_EMPTY, "OK" if master["native_l1_empty"] == EXPECT_NATIVE_L1_EMPTY else "FAIL"))
    print("    native_l1 distinct: %d" % len(master["native_l1_distinct"]))
    print("    total rows        : %d" % master["rows"])
    return ok


def check_b(master, fm):
    """MASTER -> final_map。"""
    print("[B] MASTER -> final_subcategory_map")
    checked = 0
    mismatch = 0
    examples = []
    for mpn, nl in master["m"].items():
        if mpn not in fm:
            continue
        checked += 1
        fnl = (fm[mpn].get("final_native_l1") or "").strip()
        if fnl != nl:
            mismatch += 1
            if len(examples) < 10:
                examples.append((mpn, nl, fnl))
    ok = mismatch == 0
    print("    checked   : %d" % checked)
    print("    mismatch  : %d  %s" % (mismatch, "OK" if ok else "FAIL"))
    for mpn, nl, fnl in examples:
        print("      MISMATCH %s master=%r map=%r" % (mpn, nl, fnl))
    return ok


def check_c(master, pdict):
    """MASTER -> parts.json。"""
    print("[C] MASTER -> parts.json")
    M = master["mpn_set"]
    P = set(pdict.keys())
    checked = len(M & P)
    missing_in_parts = len(M - P)
    orphan_in_parts = len(P - M)
    mismatch = 0
    examples = []
    for mpn in (M & P):
        nl = master["m"][mpn]
        fnl = (pdict[mpn].get("final_native_l1") or "").strip()
        if fnl != nl:
            mismatch += 1
            if len(examples) < 10:
                examples.append((mpn, nl, fnl))
    ok = mismatch == 0
    print("    checked            : %d" % checked)
    print("    missing (in MASTER not in parts): %d" % missing_in_parts)
    print("    orphan  (in parts not in MASTER): %d" % orphan_in_parts)
    print("    mismatch           : %d  %s" % (mismatch, "OK" if ok else "FAIL"))
    for mpn, nl, fnl in examples:
        print("      MISMATCH %s master=%r parts=%r" % (mpn, nl, fnl))
    return ok


def check_d(parts_data, comp_js_raw, page_manifest):
    """Legacy 分类污染检查。"""
    print("[D] Legacy 分类污染检查 (forbidden: %s)" % ", ".join(FORBIDDEN))
    counts = {}

    # parts.json: scan final_native_l1 values + raw text
    p_text = json.dumps(parts_data)
    counts["parts.json"] = sum(p_text.count(t) for t in FORBIDDEN)

    # components-data.js: raw text
    counts["components-data.js"] = sum(comp_js_raw.count(t) for t in FORBIDDEN)

    # page_manifest.json: scan fine page slugs + raw text
    pm_text = json.dumps(page_manifest)
    counts["page_manifest.json"] = sum(pm_text.count(t) for t in FORBIDDEN)

    total = sum(counts.values())
    ok = total == 0
    for k, v in counts.items():
        print("    %-22s : %d %s" % (k, v, "OK" if v == 0 else "FAIL"))
    print("    TOTAL               : %d  %s" % (total, "OK" if ok else "FAIL"))
    return ok


def check_e(page_manifest, fm, taxonomy_slugs, taxonomy_top_scopes):
    """页面链路检查：fine slug 必须能在 final_map / taxonomy 找到，或属于合法的 0-SKU SEO 覆盖页。

    判定规则（严格但不误报）：
      - slug 命中 final_slug / taxonomy slug / 'search'  -> 真实页（含有料与 0-SKU taxonomy 节点）
      - slug 未命中，但 top_slug 是合法 taxonomy top_scope -> 视为 0-SKU SEO 覆盖页，允许（不算错误）
      - slug 未命中 且 top_slug 也非法                                  -> 真实悬空页，FAIL
    """
    print("[E] 页面链路检查 (page_manifest fine slugs)")
    pages = page_manifest.get("pages", [])
    fine = [p for p in pages if p.get("level") == "fine"]
    top = [p for p in pages if p.get("level") == "top"]

    valid_slug = set(taxonomy_slugs) | {v.get("final_slug") for v in fm.values() if (v.get("final_slug") or "").strip()}
    valid_slug.add("search")  # 特殊搜索页，允许
    valid_top = set(taxonomy_top_scopes) | {"search"}

    real_ok = 0
    allowed_seo = []   # 0-SKU SEO 覆盖页（允许）
    dangling = []      # 真实悬空页（FAIL）
    for p in fine:
        slug = p.get("slug", "")
        top_slug = p.get("top_slug", "")
        if slug in valid_slug:
            real_ok += 1
        elif top_slug in valid_top:
            allowed_seo.append(slug)
        else:
            dangling.append(slug)

    ok = len(dangling) == 0
    print("    top pages              : %d" % len(top))
    print("    fine pages             : %d" % len(fine))
    print("    real (slug matched)    : %d" % real_ok)
    print("    allowed 0-SKU SEO      : %d %s" % (len(allowed_seo), "(不判定为错误)" if allowed_seo else ""))
    for s in allowed_seo:
        print("      ALLOWED SEO: %s (top_slug=%s)" % (s, dict((p.get("slug"), p.get("top_slug")) for p in fine).get(s)))
    print("    dangling (FAIL if >0)  : %d  %s" % (len(dangling), "OK" if ok else "FAIL"))
    for s in dangling[:20]:
        print("      DANGLING fine slug: %s" % s)
    return ok


def main():
    print("=" * 72)
    print("Classification Consistency Check")
    print("Time: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("Root: %s" % ROOT)
    print("=" * 72)

    # 预加载
    master = load_master()
    fm = load_final_map()
    parts_data, pdict = load_parts()
    comp_js_raw = load_components_js_raw()
    page_manifest = load_page_manifest()
    taxonomy_slugs, taxonomy_top_scopes = load_taxonomy_slugs()

    print("")

    a = check_a(master)
    print("")
    b = check_b(master, fm)
    print("")
    c = check_c(master, pdict)
    print("")
    d = check_d(parts_data, comp_js_raw, page_manifest)
    print("")
    e = check_e(page_manifest, fm, taxonomy_slugs, taxonomy_top_scopes)
    print("")

    print("-" * 72)
    print("Classification Consistency Check")
    print("Time: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("MASTER       : %s" % ("OK" if a else "FAIL"))
    print("final_map    : %s" % ("OK" if b else "FAIL"))
    print("parts        : %s" % ("OK" if c else "FAIL"))
    print("pages        : %s" % ("OK" if e else "FAIL"))
    print("legacy       : %s" % ("OK" if d else "FAIL"))
    print("-" * 72)

    all_ok = a and b and c and d and e
    if all_ok:
        print("Classification pipeline healthy.")
        return 0
    else:
        print("Classification pipeline UNHEALTHY -- see FAIL above.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
