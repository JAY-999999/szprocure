"""P1-E — Category-Aware Product Data Factory tests (sandbox, no real I/O).

Pure-function layer test: exercises ``category.detect_category``,
``category.build_category_row`` and the wired ``product_data.build_row`` /
``product_data.qualify`` path. NO real batch, NO MASTER write, NO R2, NO PDF,
NO Build / Commit / Push / Deploy.

What it proves
--------------
 * All 11 adapters classify the right family and extract the right ENGLISH
   spec keys from the real Chinese-keyed RAW ``attributes_json``.
 * The 4-level detection pipeline works (L1 catalogName / L2 category /
   L3 description / L4 attributes fingerprint) and NEVER falls back to
   Microcontroller.
 * Unmapped rows become UNKNOWN_CATEGORY (needs_review=True) and are held
   by ``qualify`` as a WARNING (UNMAPPED_CATEGORY) — never auto-released.
 * MCU adapter is byte-equivalent to the legacy ``build_mcu_fields``.
 * A non-ASCII attribute value that cannot be translated is DROPPED (CJK
   guard) — no Chinese key/value survives into attributes_json / description.
 * Numeric values are normalised to SI base units.

Run:  python tests/test_p1e_category.py
Exit 0 = all pass.
"""
import os
import sys
import json

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

from factory import product_data, category, gate

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  -- {detail}")
    return cond


def rec(mpn, brand, catalogName="", category="", description="", aj=None,
        ds_url=""):
    return {
        "mpn": mpn,
        "manufacturer_raw": brand,
        "catalogName": catalogName,
        "category": category,
        "description": description,
        "attributes_json": json.dumps(aj or {}, ensure_ascii=False),
        "source_datasheet_url": ds_url,
        "supplier_sku": "",
    }


def is_ascii(s):
    return all(ord(ch) < 128 for ch in (s or ""))


def aj_of(fields_or_row):
    blob = fields_or_row.get("attributes_json") or "{}"
    return json.loads(blob)


def assert_fields_ascii(name, fields):
    ok = is_ascii(fields.get("category")) and is_ascii(fields.get("subcategory")) \
        and is_ascii(fields.get("description")) and is_ascii(fields.get("keywords")) \
        and is_ascii(fields.get("applications")) and is_ascii(fields.get("faq"))
    aj = aj_of(fields)
    for k, v in aj.items():
        if not is_ascii(k):
            ok = False
        if isinstance(v, str) and not is_ascii(v):
            ok = False
    check(f"{name}: all category fields ascii", ok,
          f"category={fields.get('category')!r} desc={fields.get('description')!r} aj={aj}")


# --------------------------------------------------------------------------
# T1..T11 — per-adapter: classify + extract + ascii
# --------------------------------------------------------------------------
def t_per_adapter():
    print("\n[T1-T11] per-adapter classify + extract + ascii")
    # (label, mpn, brand, expected_canon, attributes, expected_english_keys)
    specimens = [
        ("MCU", "STM32F103C8T6", "STMicroelectronics", "Microcontroller",
         {"CPU内核": "Cortex-M4", "CPU位数": "32", "CPU最大主频": "72"},
         ["core", "frequency_hz"]),
        ("VoltageRegulator", "LM7805", "Texas Instruments", "Voltage Regulator",
         {"输出类型": "Linear", "输出电压": "5V", "输出电流": "1A"},
         ["output_type", "output_voltage_v"]),
        ("Diode", "1N4148WS", "Diodes Inc", "Diode",
         {"二极管配置": "Single", "整流电流": "0.3A", "正向压降(Vf)": "1V"},
         ["config", "forward_current_a"]),
        ("Capacitor", "CC0402KRX7R7BB103", "Yageo", "Capacitor",
         {"容值": "10nF", "精度": "10%", "额定电压": "16V"},
         ["capacitance", "rated_voltage_v"]),
        ("InterfaceIC", "MAX3232", "Maxim", "Interface IC",
         {"类型": "RS-232", "数据速率": "1Mbps", "工作电压": "3.3V"},
         ["interface", "data_rate"]),
        ("OpAmp", "LM358", "Texas Instruments", "Operational Amplifier",
         {"放大器数": "2", "增益带宽积(GBW)": "1MHz", "轨到轨": "Yes"},
         ["num_amps", "gbw_hz"]),
        ("MOSFET", "AO3400A", "Alpha & Omega", "MOSFET",
         {"类型": "N-Channel", "漏源电压(Vdss)": "30V", "连续漏极电流(Id)": "5A"},
         ["chan_type", "vdss_v"]),
        ("Transistor", "BC847", "Nexperia", "Transistor",
         {"晶体管类型": "NPN", "集电极电流(Ic)": "0.1A", "集射极击穿电压(Vceo)": "45V"},
         ["tran_type", "ic_a"]),
        ("LogicIC", "74HC00", "Texas Instruments", "Logic IC",
         {"功能": "NAND Gate", "灌电流(IOL)": "5.2mA", "工作电压": "5V"},
         ["function", "iol_a"]),
        ("Resistor", "RC0402FR-0710KL", "Yageo", "Resistor",
         {"阻值": "10kΩ", "精度": "1%", "功率": "0.063W"},
         ["resistance_ohm", "power_w"]),
        ("Inductor", "LQG15HN4N7S02D", "Murata", "Inductor",
         {"电感值": "4.7µH", "额定电流": "0.6A", "直流电阻(DCR)": "0.3Ω"},
         ["inductance_h", "rated_current_a"]),
    ]
    for label, mpn, brand, canon, aj, keys in specimens:
        r = rec(mpn, brand, aj=aj)
        got_canon, signals, conf = category.detect_category(r)
        check(f"T[{label}] detect -> {canon}", got_canon == canon,
              f"got {got_canon!r} signals={signals}")
        fields, meta = category.build_category_row(r, mpn, brand)
        check(f"T[{label}] fields.category == {canon}",
              fields["category"] == canon, fields["category"])
        ajp = aj_of(fields)
        for k in keys:
            check(f"T[{label}] extracts '{k}'", k in ajp, f"aj={ajp}")
        check(f"T[{label}] needs_review False",
              meta.get("needs_review") is False, meta)
        assert_fields_ascii(f"T[{label}]", fields)


# --------------------------------------------------------------------------
# T12 — L4 isolation (all empty except attributes_json fingerprint)
# --------------------------------------------------------------------------
def t_l4_isolation():
    print("\n[T12] L4 attributes fingerprint isolation")
    r = rec("GRM155R71C104KA88D", "Murata", aj={"容值": "100nF"})
    canon, sig, conf = category.detect_category(r)
    check("T12 empty catalogName/category/desc -> Capacitor via L4",
          canon == "Capacitor" and sig.get("level") == "L4_attributes",
          f"canon={canon} sig={sig}")


# --------------------------------------------------------------------------
# T13 — L1 catalogName ordering (Logic IC must beat Interface IC)
# --------------------------------------------------------------------------
def t_l1_catalogname():
    print("\n[T13] L1 catalogName ordering (Logic IC vs Interface IC)")
    r1 = rec("74HC00", "Texas Instruments", catalogName="Logic Gate 74HC00")
    c1, s1, _ = category.detect_category(r1)
    check("T13 'Logic Gate' -> Logic IC (not Interface IC)",
          c1 == "Logic IC", f"got {c1!r} sig={s1}")
    r2 = rec("MAX3485", "Maxim", catalogName="RS-485 Transceiver")
    c2, s2, _ = category.detect_category(r2)
    check("T13 'RS-485 Transceiver' -> Interface IC",
          c2 == "Interface IC", f"got {c2!r} sig={s2}")


# --------------------------------------------------------------------------
# T14 — L2 category column
# --------------------------------------------------------------------------
def t_l2_category():
    print("\n[T14] L2 category column mapping")
    r = rec("RC0402FR-0710KL", "Yageo", category="resistor")
    c, s, _ = category.detect_category(r)
    check("T14 category 'resistor' -> Resistor",
          c == "Resistor" and s.get("level") == "L2_category", f"c={c} s={s}")


# --------------------------------------------------------------------------
# T15 — NO fallback to Microcontroller
# --------------------------------------------------------------------------
def t_no_mcu_fallback():
    print("\n[T15] unmapped families NEVER fall back to Microcontroller")
    r = rec("1N4148WS", "Diodes Inc", aj={"二极管配置": "Single", "整流电流": "0.3A"})
    canon, _, _ = category.detect_category(r)
    check("T15 Diode record classified as Diode (not Microcontroller)",
          canon == "Diode", canon)
    fields, meta = category.build_category_row(r, "1N4148WS", "Diodes Inc")
    check("T15 subcategory is diode-shaped",
          "Microcontroller" not in fields["subcategory"]
          and fields["category"] == "Diode", fields["subcategory"])


# --------------------------------------------------------------------------
# T16 — pure noise -> UNKNOWN + needs_review
# --------------------------------------------------------------------------
def t_unknown_detect():
    print("\n[T16] pure noise -> UNKNOWN_CATEGORY + needs_review")
    r = rec("WEIRD-XYZ-1", "Generic", description="some random widget abc")
    canon, sig, conf = category.detect_category(r)
    check("T16 unknown -> Uncategorized", canon == category.UNKNOWN_CATEGORY, canon)
    check("T16 confidence 'none'", conf == "none", conf)
    fields, meta = category.build_category_row(r, "WEIRD-XYZ-1", "Generic")
    check("T16 fields.category == Uncategorized",
          fields["category"] == category.UNKNOWN_CATEGORY, fields["category"])
    check("T16 meta.needs_review True",
          meta.get("needs_review") is True, meta)


# --------------------------------------------------------------------------
# T17 — UNKNOWN flows through build_row -> qualify (warn, UNMAPPED_CATEGORY)
# --------------------------------------------------------------------------
def t_unknown_qualify():
    print("\n[T17] UNKNOWN row held by qualify (warn / UNMAPPED_CATEGORY)")
    r = rec("WEIRD-XYZ-1", "Generic", description="some random widget abc")
    row, meta = product_data.build_row(r, "WEIRD-XYZ-1", "Generic")
    check("T17 row._needs_review True",
          bool(row.get(product_data.F_NEEDS_REVIEW)) is True, row)
    verdict, code, msg = product_data.qualify(row)
    check("T17 verdict == warn", verdict == "warn", verdict)
    check("T17 code == UNMAPPED_CATEGORY", code == gate.UNMAPPED_CATEGORY,
          f"code={code}")
    check("T17 NOT auto-ok (so never released)",
          verdict != "ok", verdict)


# --------------------------------------------------------------------------
# T18 — MCU adapter byte-equivalent to legacy build_mcu_fields
# --------------------------------------------------------------------------
def t_mcu_regression():
    print("\n[T18] MCU adapter == legacy build_mcu_fields (regression)")
    r = rec("STM32F103C8T6", "STMicroelectronics",
            description=("STM32F103C8T6 32-bit ARM Cortex-M4 MCU 64KB Flash "
                         "20KB SRAM 37 I/O LQFP-48 operating voltage 2.0-3.6 V"),
            aj={"CPU内核": "Cortex-M4", "CPU位数": "32", "CPU最大主频": "72"})
    fields, _ = category.build_category_row(r, "STM32F103C8T6", "STMicroelectronics")
    legacy = product_data.build_mcu_fields(r, "STM32F103C8T6", "STMicroelectronics")
    same = all(fields.get(k) == legacy.get(k)
               for k in ("category", "subcategory", "description",
                         "applications", "keywords", "attributes_json", "faq"))
    check("T18 adapter output == legacy MCU fields", same,
          f"\n  adapter={fields}\n  legacy={legacy}")


# --------------------------------------------------------------------------
# T19 — CJK value that can't be translated is dropped
# --------------------------------------------------------------------------
def t_cjk_drop():
    print("\n[T19] non-ASCII untranslatable attribute value dropped")
    r = rec("CC0402", "Yageo",
            aj={"容值": "10nF", "温度系数": "陶瓷"})  # 陶瓷 not in _ENUM
    fields, meta = category.build_category_row(r, "CC0402", "Yageo")
    ajp = aj_of(fields)
    check("T19 capacitance extracted", "capacitance" in ajp, ajp)
    check("T19 temp_coef '陶瓷' dropped", "temp_coef" not in ajp, ajp)
    assert_fields_ascii("T19", fields)


# --------------------------------------------------------------------------
# T20 — numeric normalization to SI base units
# --------------------------------------------------------------------------
def t_numeric():
    print("\n[T20] numeric normalization (SI base units)")

    def spec(mpn, brand, aj):
        r = rec(mpn, brand, aj=aj)
        f, _ = category.build_category_row(r, mpn, brand)
        return aj_of(f)

    cap = spec("C1", "Yageo", {"容值": "100nF"})
    check("T20 capacitance 100nF -> 1e-7", abs(cap["capacitance"] - 100e-9) < 1e-12,
          cap)
    vr = spec("U1", "Texas Instruments", {"输出类型": "LDO", "输出电压": "3.3V"})
    check("T20 output_voltage 3.3V -> 3.3", abs(vr["output_voltage_v"] - 3.3) < 1e-9,
          vr)
    mos = spec("Q1", "Alpha & Omega", {"漏源电压(Vdss)": "30V", "连续漏极电流(Id)": "5A"})
    check("T20 vdss 30V -> 30.0", abs(mos["vdss_v"] - 30.0) < 1e-9, mos)
    check("T20 id 5A -> 5.0", abs(mos["id_a"] - 5.0) < 1e-9, mos)
    res = spec("R1", "Yageo", {"阻值": "10kΩ"})
    check("T20 resistance 10kΩ -> 10000", abs(res["resistance_ohm"] - 10000.0) < 1e-6,
          res)
    ind = spec("L1", "Murata", {"电感值": "4.7µH"})
    check("T20 inductance 4.7µH -> 4.7e-6",
          abs(ind["inductance_h"] - 4.7e-6) < 1e-12, ind)
    ifc = spec("U2", "Maxim", {"数据速率": "1Mbps"})
    check("T20 data_rate 1Mbps -> 1e6", abs(ifc["data_rate"] - 1e6) < 1, ifc)
    op = spec("U3", "Texas Instruments", {"增益带宽积(GBW)": "1MHz"})
    check("T20 gbw 1MHz -> 1e6", abs(op["gbw_hz"] - 1e6) < 1, op)


# --------------------------------------------------------------------------
# T21 — registry integrity
# --------------------------------------------------------------------------
def t_registry():
    print("\n[T21] registry integrity")
    check("T21 REGISTRY has 11 adapters", len(category.REGISTRY) == 11,
          len(category.REGISTRY))
    check("T21 supported_canonicals == 11",
          len(category.supported_canonicals()) == 11,
          len(category.supported_canonicals()))


# --------------------------------------------------------------------------
# T22 / T23 — full build_row + qualify for clean mapped parts
# --------------------------------------------------------------------------
def t_full_qualify():
    print("\n[T22-T23] full build_row + qualify for clean mapped parts")
    # MCU -> ok (min_specs=2, 7 specs)
    r = rec("STM32F103C8T6", "STMicroelectronics",
            description=("STM32F103C8T6 32-bit ARM Cortex-M4 MCU 64KB Flash "
                         "20KB SRAM 37 I/O LQFP-48 operating voltage 2.0-3.6 V"),
            aj={"CPU内核": "Cortex-M4", "CPU位数": "32", "CPU最大主频": "72"})
    row, meta = product_data.build_row(r, "STM32F103C8T6", "STMicroelectronics")
    v, code, msg = product_data.qualify(row)
    check("T22 MCU verdict ok", v == "ok", (v, code, msg))
    check("T22 MCU spec keys >= 2",
          (row.get(product_data.F_SPEC_KEYS) or 0) >= 2,
          row.get(product_data.F_SPEC_KEYS))
    # Capacitor -> ok (min_specs=1, 1 spec)
    r2 = rec("CC0402KRX7R7BB103", "Yageo", aj={"容值": "10nF", "额定电压": "16V"})
    row2, _ = product_data.build_row(r2, "CC0402KRX7R7BB103", "Yageo")
    v2, code2, msg2 = product_data.qualify(row2)
    check("T23 Capacitor verdict ok", v2 == "ok", (v2, code2, msg2))
    check("T23 Capacitor spec keys >= 1",
          (row2.get(product_data.F_SPEC_KEYS) or 0) >= 1,
          row2.get(product_data.F_SPEC_KEYS))
    # full-row ascii guarantee on the MCU row
    assert_fields_ascii("T22-row", row2)


def main():
    t_per_adapter()
    t_l4_isolation()
    t_l1_catalogname()
    t_l2_category()
    t_no_mcu_fallback()
    t_unknown_detect()
    t_unknown_qualify()
    t_mcu_regression()
    t_cjk_drop()
    t_numeric()
    t_registry()
    t_full_qualify()
    print(f"\n==== P1-E result: {len(PASS)} pass / {len(FAIL)} fail ====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("ALL P1-E SCENARIOS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
