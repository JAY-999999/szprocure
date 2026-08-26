#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SZ Procure — Phase 2.1 P0 回归测试（自动化验收）
================================================
把 P0-1~P0-4 + lifecycle 异常规则转化为可自动验收的断言。
直接 import gen_parts（无 import 期副作用），喂入 tests/fixtures/test_p0_input.csv，
对 7 个新增函数/类做单元 + 集成断言。

运行：python tests/test_p0_regression.py
退出码：0 = 全部通过；1 = 有失败项（可作 CI 闸门）。
不写任何站点文件、不改 gen_parts.py、不改冻结 Schema。
"""
import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
sys.path.insert(0, REPO_ROOT)

import gen_parts as gp  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "test_p0_input.csv")
MFR_CSV = os.path.join(REPO_ROOT, "data", "mfr_canonical.csv")
ATTR_MD = os.path.join(REPO_ROOT, "data", "attributes_dictionary.md")

failures = []
findings = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {detail}")
        failures.append(name)


def main():
    if not os.path.exists(FIXTURE):
        print(f"[WARN] fixture missing: {FIXTURE} — run tests/fixtures/make_test_p0_input.py first")
        sys.exit(2)

    # ---- 载入夹具 ----
    rows = []
    with open(FIXTURE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    print(f"fixture rows loaded: {len(rows)}")

    mfr_map = gp.load_mfr_canonical(MFR_CSV)
    allow = gp.load_attr_allowlist(ATTR_MD)
    review = []
    groups, stats = gp.build_merged_groups(rows, mfr_map, allow, review)
    print(f"groups_out={stats['groups_out']} merged_dups={stats['merged_dups']} "
          f"brand_unmatched={stats['brand_unmatched']} attr_unknown={stats['attr_unknown']}")

    # ===== A. P0-1 SlugRegistry（单元）=====
    print("\n[A] P0-1 slug 防碰撞 (SlugRegistry)")
    reg = gp.SlugRegistry()
    check("首个占用者保留原 slug", reg.assign("nrf24l01", "a") == "nrf24l01")
    check("碰撞自动 -2", reg.assign("nrf24l01", "b") == "nrf24l01-2")
    check("碰撞自动 -3", reg.assign("nrf24l01", "c") == "nrf24l01-3")
    check("空基 slug 返回空串（不崩）", reg.assign("+++", "d") == "")
    # 集成：在真实 groups 上跑 main 同款分配循环
    reg2 = gp.SlugRegistry()
    for g in groups:
        reg2.assign(g["mpn"], g["mpn"])
    check("nRF24L01+ 在组流中碰撞为 nrf24l01-2",
          reg2.renamed.get("nrf24l01") == "nrf24l01-2",
          f"renamed={reg2.renamed}")

    # ===== B. P0-2 品牌归一（单元）=====
    print("\n[B] P0-2 品牌归一 (canonicalize_brand)")
    check("TI -> Texas Instruments", gp.canonicalize_brand("TI", mfr_map) == ("Texas Instruments", True))
    check("Texas Instr. -> Texas Instruments", gp.canonicalize_brand("Texas Instr.", mfr_map) == ("Texas Instruments", True))
    check("未知品牌原值透传 + matched=False",
          gp.canonicalize_brand("SomeUnknownBrand", mfr_map) == ("SomeUnknownBrand", False))

    # ===== C. P0-3 属性规范化（单元）=====
    print("\n[C] P0-3 属性规范化 (validate_attributes / allowlist)")
    check("frequency_hz 在允许字典", "frequency_hz" in allow)
    check("voltage_v 在允许字典", "voltage_v" in allow)
    check("legacy_speed 不在允许字典", "legacy_speed" not in allow)
    uk, ok = gp.validate_attributes({"frequency_hz": 1, "voltage_v": 3.3}, allow)
    check("已知属性 -> ok 且无未知键", ok and uk == set(), f"uk={uk} ok={ok}")
    uk2, ok2 = gp.validate_attributes({"legacy_speed": 100}, allow)
    check("未知属性 -> 不 ok 且 unknown={legacy_speed}",
          (not ok2) and uk2 == {"legacy_speed"}, f"uk={uk2} ok={ok2}")
    uk3, ok3 = gp.validate_attributes(None, allow)
    check("非 dict 属性 -> ok（由上游容错处理）", ok3)

    # ===== D. P0-4 重复 MPN 合并（集成）=====
    print("\n[D] P0-4 重复 MPN 合并 (build_merged_groups)")
    stm = [g for g in groups if g["mpn"] == "STM32F103C8T6"]
    check("STM32 跨别名合并为 1 组", len(stm) == 1, f"got {len(stm)}")
    if stm:
        g = stm[0]
        check("STM32 品牌归一为 STMicroelectronics", g["manufacturer"] == "STMicroelectronics")
        check("STM32 sources 并集 [LCSC, Huaqiang]", g["sources"] == ["LCSC", "Huaqiang"], f"got {g['sources']}")
        check("STM32 不进待审", g["needs_review"] is False)
        check("STM32 缺描述被回填", bool(g.get("description")))
    two = [g for g in groups if g["mpn"] == "TWOSUB-A"]
    check("TWOSUB-A 合并为 1 组", len(two) == 1, f"got {len(two)}")
    if two:
        g = two[0]
        check("TWOSUB 子类别回填 = Regulator", g.get("subcategory") == "Regulator", f"got {g.get('subcategory')}")
        alt = set(x for x in g.get("alternative_parts", "").split(";") if x)
        check("TWOSUB alt 并集含 X1/Y2/Z3", alt >= {"X1", "Y2", "Z3"}, f"got {alt}")
        check("TWOSUB sources [LCSC, Mouser]", g["sources"] == ["LCSC", "Mouser"], f"got {g['sources']}")

    # ===== E. lifecycle 异常（集成）=====
    print("\n[E] lifecycle 异常")
    check("空 mpn 被跳过 -> groups_out==14（17 行 - 1 空mpn - 2 合并）",
          stats["groups_out"] == 14, f"got {stats['groups_out']}")
    check("merged_dups==2", stats["merged_dups"] == 2, f"got {stats['merged_dups']}")
    check("brand_unmatched==1（仅 WEIRDCHIP；AMS 已归一不再未知）",
          stats["brand_unmatched"] == 1, f"got {stats['brand_unmatched']}")
    check("brand_missing==1（NOBRAND 空 manufacturer）",
          stats.get("brand_missing") == 1, f"got {stats.get('brand_missing')}")
    check("attr_unknown==1", stats["attr_unknown"] == 1, f"got {stats['attr_unknown']}")
    weird = [g for g in groups if g["mpn"] == "WEIRDCHIP-1"]
    check("未知品牌进待审 unknown_manufacturer",
          weird and weird[0]["needs_review"] and "unknown_manufacturer" in weird[0]["review_reasons"])
    ams = [g for g in groups if g["mpn"] == "AMS1117-3.3"]
    check("AMS 品牌已归一 -> 不再 unknown_manufacturer",
          ams and "unknown_manufacturer" not in ams[0]["review_reasons"])
    check("AMS 属性电压合法(voltage_out_v/dropout_v) -> 不进待审",
          ams and ams[0]["needs_review"] is False,
          f"reasons={ams[0]['review_reasons'] if ams else None}")
    lm = [g for g in groups if g["mpn"] == "LM358"]
    check("未知属性进待审 unknown_attr_key=legacy_speed",
          lm and lm[0]["needs_review"] and "unknown_attr_key=legacy_speed" in lm[0]["review_reasons"])
    bad = [g for g in groups if g["mpn"] == "BADJSON-1"]
    check("畸形 JSON 不崩且不错标待审",
          bad and bad[0]["needs_review"] is False, "malformed json should not crash or false-flag")
    no = [g for g in groups if g["mpn"] == "NOSOURCE"]
    check("缺 source -> sources==[]", no and no[0]["sources"] == [], f"got {no[0]['sources'] if no else None}")
    check("review_queue 至少含 3 条（2 未知品牌 + 1 未知属性）",
          len(review) >= 3, f"got {len(review)}")

    # ===== F1. 修复验证：无下划线文本 key 必须命中字典（硬断言）=====
    print("\n[F1] F1 修复验证：文本 key 无下划线也能命中 allowlist")
    for k in ("package", "core", "interface", "mounting", "modulation"):
        check(f"F1: {k} 在 allowlist", k in allow)
    f1 = [g for g in groups if g["mpn"] == "F1TEXT-KEYS-1"]
    check("F1: 文本 key 行不进待审",
          f1 and f1[0]["needs_review"] is False,
          f"reasons={f1[0]['review_reasons'] if f1 else None}")
    check("F1: 文本 key 行无 unknown_attr_key",
          f1 and not any(r.startswith("unknown_attr_key") for r in f1[0]["review_reasons"]))

    # ===== F2. 修复验证：空 manufacturer 必填 + 进待审（硬断言）=====
    print("\n[F2] F2 修复验证：空 manufacturer 不允许静默入库")
    nb = [g for g in groups if g["mpn"] == "NOBRAND-1"]
    check("F2: 空 manufacturer -> missing_manufacturer 标记",
          nb and "missing_manufacturer" in nb[0]["review_reasons"],
          f"reasons={nb[0]['review_reasons'] if nb else None}")
    check("F2: 空 manufacturer -> needs_review=True",
          nb and nb[0]["needs_review"] is True)
    check("F2: 空 manufacturer -> 进入 review_queue",
          "missing_manufacturer" in [r[2] for r in review])
    check("F2: 空 manufacturer 不再被静默赋空品牌入库",
          nb and nb[0]["manufacturer"] == "")

    # ===== 汇总 =====
    print("\n================ 汇总 ================")
    print(f"groups_out={stats['groups_out']}  merged_dups={stats['merged_dups']}  "
          f"brand_unmatched={stats['brand_unmatched']}  attr_unknown={stats['attr_unknown']}")
    print(f"review_queue 条数={len(review)}")
    if findings:
        print("FINDINGS:")
        for i, fnd in enumerate(findings, 1):
            print(f"  [{i}] {fnd}")
    print(f"FAILURES: {len(failures)}")
    if failures:
        for f in failures:
            print(f"   - {f}")
        sys.exit(1)
    print("ALL PASS ✅")
    sys.exit(0)


if __name__ == "__main__":
    main()
