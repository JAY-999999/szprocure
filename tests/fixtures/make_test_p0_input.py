#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SZ Procure — Phase 2.1 P0 回归测试数据集生成器
==============================================
产出 tests/fixtures/test_p0_input.csv —— 覆盖 5 大类的确定性夹具：
  P0-1 slug 碰撞        : nRF24L01 / nRF24L01+（norm_mpn 不同 → 不合并，但 slug 基相同 → 碰撞 -2）
  P0-2 品牌归一         : TI / Texas Instr. → Texas Instruments；SomeUnknownBrand → 未知
  P0-3 属性规范化        : frequency_hz / voltage_v（命中字典）；legacy_speed（未知）
  P0-4 重复 MPN 合并     : STM32F103C8T6（ST + STMicroelectronics 别名合并）；TWOSUB-A（字段回填 + alt 并集）
  lifecycle 异常        : 空 mpn 跳过 / 畸形 JSON 不崩 / 缺 source / 空 manufacturer 静默

字段顺序严格对齐真实 data/sample_parts.csv 16 列 + `source`（采集层来源标识，生成器已支持可选读取）。

运行：python tests/fixtures/make_test_p0_input.py
"""
import csv
import os

COLUMNS = [
    "mpn", "clean_mpn", "manufacturer", "brand", "url_slug",
    "category", "subcategory", "description", "applications", "keywords",
    "attributes_json", "availability", "alternative_parts", "datasheet_url",
    "faq", "image", "source",
]

# 每行是一个 dict；缺失列留空字符串。
ROWS = [
    # ---- P0-1 slug 碰撞（两件不同零件，slug 基相同）----
    dict(mpn="nRF24L01", manufacturer="Nordic", category="RF & Wireless",
         subcategory="Transceiver", description="2.4GHz transceiver",
         attributes_json='{"frequency_hz":2400000000,"voltage_v":3.3}',
         availability="In Stock", source="LCSC"),
    dict(mpn="nRF24L01+", manufacturer="Nordic", category="RF & Wireless",
         subcategory="Transceiver", description="2.4GHz transceiver with PA",
         attributes_json='{"frequency_hz":2400000000,"output_power_dbm":0}',
         availability="In Stock", source="Mouser"),

    # ---- P0-2 品牌归一（别名 → canonical；未知 → 待审）----
    dict(mpn="TLV320ADC3101", manufacturer="TI", category="Audio",
         subcategory="ADC", description="stereo audio ADC",
         attributes_json='{"voltage_v":3.3}', availability="In Stock", source="LCSC"),
    dict(mpn="OPA2345", manufacturer="Texas Instr.", category="Amplifiers",
         subcategory="OpAmp", description="dual operational amplifier",
         attributes_json='{"voltage_v":5}', availability="In Stock", source="Mouser"),
    dict(mpn="WEIRDCHIP-1", manufacturer="SomeUnknownBrand", category="Logic",
         subcategory="Gate", description="weird logic gate",
         attributes_json='{"frequency_hz":100}', availability="In Stock", source="LCSC"),

    # ---- P0-3 属性规范化（命中 / 未知）----
    dict(mpn="CC0402KRX7R7BB103", manufacturer="Yageo", category="Passive",
         subcategory="Capacitor", description="10nF 0402 X7R capacitor",
         attributes_json='{"capacitance_pf":100000,"voltage_v":50}',
         availability="In Stock", source="LCSC"),
    dict(mpn="LM358", manufacturer="TI", category="Amplifiers",
         subcategory="OpAmp", description="dual operational amplifier",
         attributes_json='{"legacy_speed":100}', availability="In Stock", source="LCSC"),
    dict(mpn="BADJSON-1", manufacturer="TI", category="Amplifiers",
         subcategory="OpAmp", description="row with malformed attributes json",
         attributes_json='{not valid json', availability="In Stock", source="LCSC"),
    # ---- F1 验证：无下划线文本 key（package/core/interface/mounting/modulation）必须命中字典 ----
    dict(mpn="F1TEXT-KEYS-1", manufacturer="ST", category="Microcontrollers",
         subcategory="ARM", description="F1 test: text keys without underscore",
         attributes_json='{"core":"ARM Cortex-M4","package":"LQFP-48","interface":"SPI","mounting":"SMD","modulation":"GFSK"}',
         availability="In Stock", source="LCSC"),

    # ---- P0-4 重复 MPN 合并（跨别名合并 + 字段回填 + alt 并集）----
    dict(mpn="STM32F103C8T6", manufacturer="ST", category="Microcontrollers",
         subcategory="ARM", description="ARM Cortex-M3 MCU",
         attributes_json='{"flash_bytes":131072,"frequency_hz":72000000,"voltage_v":3.3}',
         availability="In Stock", source="LCSC"),
    dict(mpn="STM32F103C8T6", manufacturer="STMicroelectronics", category="Microcontrollers",
         subcategory="ARM", description="",
         attributes_json='{"flash_bytes":131072}', availability="In Stock", source="Huaqiang"),
    # 空 mpn 行（lifecycle：应被跳过，不进 groups）
    dict(mpn="", manufacturer="TI", category="Amplifiers",
         subcategory="OpAmp", description="empty mpn should be skipped",
         attributes_json='{"voltage_v":3.3}', availability="In Stock", source="LCSC"),
    dict(mpn="AMS1117-3.3", manufacturer="Advanced Monolithic Systems", category="Power Management",
         subcategory="LDO", description="3.3V low-dropout regulator",
         attributes_json='{"voltage_out_v":3.3,"dropout_v":1.1}',
         availability="In Stock", source="LCSC"),
    dict(mpn="TWOSUB-A", manufacturer="ST", category="Power Management",
         subcategory="", description="",
         attributes_json='{"voltage_v":5}', availability="In Stock",
         alternative_parts="X1;Y2", source="LCSC"),
    dict(mpn="TWOSUB-A", manufacturer="ST", category="Power Management",
         subcategory="Regulator", description="",
         attributes_json='{"voltage_v":5}', availability="In Stock",
         alternative_parts="Z3", source="Mouser"),
    # 缺 source（lifecycle：sources 应为空列表，不崩）
    dict(mpn="NOSOURCE", manufacturer="TI", category="Amplifiers",
         subcategory="OpAmp", description="row without source",
         attributes_json='{"voltage_v":3.3}', availability="In Stock", source=""),
    # 空 manufacturer（lifecycle：canonicalize_brand 对空值静默返回空，不标审）
    dict(mpn="NOBRAND-1", manufacturer="", category="Logic",
         subcategory="Gate", description="row without manufacturer",
         attributes_json='{"frequency_hz":10}', availability="In Stock", source="LCSC"),
]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "test_p0_input.csv")
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in ROWS:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    print(f"wrote {out}  ({len(ROWS)} rows)")


if __name__ == "__main__":
    main()
