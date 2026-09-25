"""2026-09-25 (R8) — regression tests for the FORMAL 02 mapping rules.

These pin the rules the release chain must keep obeying for EVERY SKU:

  1. RANGE never collapses into a single value ("4V~80V" stays a range).
  2. Sibling currents never share one attribute key
     (forward vs non-repetitive peak forward surge stay separate).
  3. Reverse leakage is a CURRENT and is never pushed through voltage normalisation.
  4. Unmapped specs are forwarded, never silently dropped.
  5. A datasheet that is shared across manufacturers, or bound twice to one
     (mpn, manufacturer), BLOCKS the release (datasheet identity gate).
  6. Unit re-writing may change the format, never the semantics
     (150mA -> 0.15A yes, 4V~80V -> 42V never; integer 1 never renders as 1.0).
  7. MASTER stays append-only: an in-place row correction is possible ONLY for
     MPNs a human explicitly authored (allow_row_update_mpns), and BOM-bearing
     MASTER still reads/writes with its BOM intact.

All tests are read-only w.r.t. the production MASTER (they only ever construct
throw-away rows / temp files). Run with pytest or directly as a script.
"""
import csv
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, r"D:/SZ Procure/site/tools")
sys.path.insert(0, r"D:/SZ Procure/site")

from factory import category, master_io, product_data, release_pipeline as rp
from factory import has_illegal_text
from factory.lcsc_http_adapter import flatten_envelope, HTTP_ATTR_MAP
from factory import lcsc_http_adapter

RAWDIR = r"D:/SZ Procure/采集流水线/基础数据"
MASTER = r"D:/SZ Procure/site/data/production/master_parts_v2.1.csv"
ATTR_DICT = r"D:/SZ Procure/site/data/production/attributes_dictionary.md"

# the 10 validation SKUs of this formalisation round (C# -> MPN)
VALIDATION = {
    "C15127": ("AO3401A", "Alpha & Omega Semiconductor"),
    "C20416655": ("BAV99S", "Diodes Incorporated"),
    "C118131": ("LT4363IMS-2#TRPBF", "Analog Devices"),
    "C1521784": ("XC7A100T-1FGG484I", "AMD Xilinx"),
    "C2931360": ("62684-402100ALF", "Amphenol"),
    "C282519": ("TCC0603X7R104K500CT", "Sunlord"),
    "C118954": ("3296W-1-103", "Bourns"),
    "C496549": ("BWSMA-KE-Z001", "TE Connectivity"),
    "C22367830": ("HGC0805R5106K500NSLJ", "UNI Royal"),
    "C34846": ("3296W-1-103LF", "Bourns"),
}

RESULTS = []


def _record(c_number):
    with io.open(os.path.join(RAWDIR, c_number + ".json"), encoding="utf-8") as f:
        rec = flatten_envelope(json.load(f))
    rec["_source_kind"] = "lcsc_http_json"
    return rec


def _row(c_number):
    c, (mpn, brand) = c_number, VALIDATION[c_number]
    rec = _record(c)
    row, _meta = product_data.build_row(rec, mpn, brand)
    row["supplier_reference"] = c
    return row


def _rec(name_en, value, mpn="SYNTH1"):
    """Minimal 02-shaped record carrying one raw paramVOList spec."""
    return {
        "mpn": mpn,
        "manufacturer_raw": "X",
        "supplier_sku": "C9999999",
        "_source_kind": "lcsc_http_json",
        "attributes_json": json.dumps({name_en: value}, ensure_ascii=False),
        "attributes_json_unmapped": json.dumps({name_en: value}, ensure_ascii=False),
        "paramVOList": [{"name": {"en": name_en, "zh": name_en},
                         "value": {"en": value, "zh": value}}],
    }


# --------------------------------------------------------------------------
# 1. range safety
# --------------------------------------------------------------------------
def test_range_never_collapses():
    # the range-safe guards must pass ranges through untouched
    assert category._r("4V~80V") == "4V~80V"
    assert category._i("4V~80V") == "4V~80V"
    # voltage normalisation must not average a range into one number
    assert "~" in str(category._norm_voltage("4V~80V")), \
        "range collapsed: %r" % (category._norm_voltage("4V~80V"),)
    # end-to-end through 02 with the REAL LT4363 RAW
    row = _row("C118131")
    wv = row["attributes_json"]
    assert '"working_voltage_v": "4V~80V"' in wv.replace(", ", ",").replace('"', '"') \
        or "4V~80V" in wv, "02 collapsed the supply-voltage range: %s" % wv[:200]
    assert "42" not in wv.split("working_voltage_v")[1][:40], \
        "42V average leaked into the range field"
    RESULTS.append(("range 4V~80V preserved (02, LT4363)", "-", "4V~80V", "PASS"))


# --------------------------------------------------------------------------
# 2. sibling currents keep separate keys
# --------------------------------------------------------------------------
def test_surge_current_is_its_own_field():
    # rule at the mapping level: rectified current vs surge current are distinct
    canon_f, _ = HTTP_ATTR_MAP["Current - Rectified"]
    canon_s, _ = HTTP_ATTR_MAP["Non-Repetitive Peak Forward Surge Current"]
    canon_r, _ = HTTP_ATTR_MAP["Reverse Leakage Current (Ir)"]
    assert (canon_f, canon_s, canon_r) == ("forward_current_a", "surge_current_a",
                                           "reverse_leakage_current"), \
        "field collision / identity regression: %r" % ((canon_f, canon_s, canon_r),)
    assert canon_s != canon_f and canon_r != canon_f

    # end-to-end: BAV99S carries 150mA rectified, 2.5A surge, 1uA Ir
    row = _row("C20416655")
    a = json.loads(row["attributes_json"])
    assert a.get("forward_current_a") == 0.15, "rectified current wrong: %r" % a.get("forward_current_a")
    assert a.get("surge_current_a") == "2.5A", "surge current lost: %r" % a.get("surge_current_a")
    assert a.get("surge_current_a") != a.get("forward_current_a")
    RESULTS.append(("surge vs forward vs leakage keys distinct", "-",
                    "0.15A / 2.5A / 1uA", "PASS"))


# --------------------------------------------------------------------------
# 3. leakage is current, never voltage
# --------------------------------------------------------------------------
def test_leakage_normalised_as_current():
    row = _row("C20416655")
    a = json.loads(row["attributes_json"])
    ir = a.get("reverse_leakage_current")
    assert ir == "1uA", "reverse leakage dropped or mis-normalised: %r" % ir
    # must NOT have been folded into a voltage field as 1e-6
    assert not any(str(v) in ("1e-06", "0.000001") for v in a.values()), \
        "leakage value leaked into a voltage field: %r" % a
    RESULTS.append(("reverse leakage 1uA survives", "-", "1uA", "PASS"))


# --------------------------------------------------------------------------
# 4. unmapped specs are forwarded, not dropped
# --------------------------------------------------------------------------
def test_unmapped_specs_forwarded():
    merged = lcsc_http_adapter._forward_unmapped_specs(
        {"forward_current_a": 0.15},
        {"Output Capacitance(Coss)": "80pF"})
    assert merged.get("Output Capacitance(Coss)") == "80pF", \
        "unmapped spec silently dropped: %r" % merged
    # an existing canonical key is never overwritten by an unmapped one
    merged2 = lcsc_http_adapter._forward_unmapped_specs(
        {"forward_current_a": 0.15}, {"forward_current_a": "999"})
    assert merged2["forward_current_a"] == 0.15
    # real AO3401A RAW keeps Coss
    row = _row("C15127")
    assert "Output Capacitance(Coss)" in row["attributes_json"]
    RESULTS.append(("unmapped forwarded (Coss kept)", "-", "80pF", "PASS"))


# --------------------------------------------------------------------------
# 5. datasheet identity gate
# --------------------------------------------------------------------------
def _ds_row(mpn, mfr, url):
    return {"mpn": mpn, "manufacturer": mfr, "datasheet_url": url}


def test_datasheet_identity_gate():
    DS_A = "https://example.test/a.pdf"
    DS_B = "https://example.test/b.pdf"

    # R1 — one (mpn, manufacturer) bound to two datasheets -> STOP
    hits = rp.check_datasheet_identity(
        [_ds_row("X1", "Alpha", DS_A), _ds_row("X1", "Alpha", DS_B)])
    assert any(lvl in ("STOP", "FAIL") for lvl, _kind, _m in hits), hits

    # R2 — one MPN across manufacturers, all landing on ONE datasheet ->
    #      the mpn-only binding is cross-manufacturer contaminated -> STOP
    hits2 = rp.check_datasheet_identity(
        [_ds_row("X2", "Alpha", DS_A), _ds_row("X2", "Beta", DS_A)])
    assert any(lvl in ("STOP", "FAIL") for lvl, _k, _m in hits2), hits2

    # R3 — same MPN, separate datasheets per manufacturer -> WARN only
    hits3 = rp.check_datasheet_identity(
        [_ds_row("X3", "Alpha", DS_A), _ds_row("X3", "Beta", DS_B)])
    assert not any(lvl in ("STOP", "FAIL") for lvl, _k, _m in hits3), hits3
    assert any(lvl == "WARN" for lvl, _k, _m in hits3), hits3

    # clean family -> nothing at all
    assert rp.check_datasheet_identity(
        [_ds_row("X4", "Alpha", DS_A)]) == [], "false positive on a single row"
    RESULTS.append(("datasheet gate R1/R2 STOP, R3 WARN", "-", "gate wired", "PASS"))


# --------------------------------------------------------------------------
# 6. unit rewriting keeps semantics
# --------------------------------------------------------------------------
def test_unit_rewrite_semantics():
    assert category._num_first("1") == 1 and isinstance(category._num_first("1"), int), \
        "integer must not render as 1.0"
    assert category._norm_current("150mA") == 0.15
    assert category._norm_power("200mW") == 0.2
    # legal symbols survive the ASCII gate
    assert has_illegal_text("±10%") is False
    assert has_illegal_text("±250ppm/°C") is False
    assert has_illegal_text("温度") is True
    RESULTS.append(("units / int / symbol fidelity", "-", "0.15A, 0.2W, 1", "PASS"))


# --------------------------------------------------------------------------
# 7. MASTER is append-only unless a human authorises a row
# --------------------------------------------------------------------------
def test_master_write_scopes():
    cols = list(master_io.MASTER_COLS)
    old = [dict(zip(cols, ["OLD"] * len(cols)))]
    old[0]["mpn"] = "ALPHA1"
    old[0]["attributes_json"] = "{}"
    new = dict(old[0])
    new["attributes_json"] = '{"changed": 1}'

    ok, problems = master_io.validate_rows(cols, old, [new])
    assert ok is False, "an unauthorised row update must be rejected"
    assert any("modified" in p for p in problems), problems

    ok2, problems2 = master_io.validate_rows(cols, old, [new],
                                             allow_row_update_mpns=["ALPHA1"])
    assert ok2 is True, problems2
    assert master_io._count_authorised_updates(old, [new], ["ALPHA1"]) == 1
    assert master_io._count_authorised_updates(old, [new], None) == 0
    RESULTS.append(("append-only + authorised update scope", "-", "1 allowed / 0 by default", "PASS"))


def test_read_master_handles_bom():
    tmp = os.path.join(tempfile.mkdtemp(), "master.csv")
    with io.open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["mpn", "attributes_json"], lineterminator="\r\n")
        w.writeheader()
        w.writerow({"mpn": "BOMSKU", "attributes_json": "{}"})
    cols, rows = master_io.read_master(tmp)
    assert cols[0] == "mpn", "BOM not stripped, first col = %r" % cols[0]
    assert rows[0]["mpn"] == "BOMSKU"
    # writing back must KEEP the BOM (production MASTER carries one)
    master_io.atomic_write_master(tmp, cols, rows, old_rows=rows)
    with open(tmp, "rb") as f:
        assert f.read(3) == b"\xef\xbb\xbf", "BOM lost on rewrite"
    RESULTS.append(("BOM-aware read/write", "-", "utf-8-sig preserved", "PASS"))


# --------------------------------------------------------------------------
# 8. 03 layer allowlist still covers the production keys
# --------------------------------------------------------------------------
def test_attr_allowlist_covers_production():
    sys.path.insert(0, r"D:/SZ Procure/site")
    os.chdir(r"D:/SZ Procure/site")
    import gen_parts
    allow = gen_parts.load_attr_allowlist(ATTR_DICT)
    assert allow, "allowlist empty"
    used = set()
    with io.open(MASTER, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if r.get("mpn") not in {v[0] for v in VALIDATION.values()}:
                continue
            raw = (r.get("attributes_json") or "").strip()
            if raw:
                used |= set(json.loads(raw).keys())
    missing = sorted(k for k in used if k not in allow)
    assert not missing, "03 layer would reject production keys: %s" % missing
    RESULTS.append(("03 allowlist covers the 10 SKUs", "%d keys" % len(allow),
                    "used=%d missing=%d" % (len(used), len(missing)), "PASS"))


# --------------------------------------------------------------------------
# 9. FORMAL PRODUCTION RULE (Resistance unit) — 2026-09-24
#    >= 1000 Ω -> kΩ, < 1 Ω -> mΩ, otherwise plain Ω; value glued to the unit.
# --------------------------------------------------------------------------
def test_resistance_unit_formatting():
    sys.path.insert(0, r"D:/SZ Procure/site")
    os.chdir(r"D:/SZ Procure/site")
    import gen_parts
    cases = [(10000, "10kΩ"), (1000, "1kΩ"), (50000, "50kΩ"), (3300, "3.3kΩ"),
             (999, "999Ω"), (100, "100Ω"), (50, "50Ω"), (0.028, "28mΩ")]
    for val, want in cases:
        got = gen_parts._fmt_ohm(val)
        assert got == want, "_fmt_ohm(%r) = %r, want %r" % (val, got, want)
    # the shared formatter must be what the spec table and the title use
    assert gen_parts.format_attr_value("resistance_ohm", 10000) == "10kΩ"
    assert gen_parts.format_attr_value("impedance_ohm", 50) == "50Ω"
    assert gen_parts.format_attr_value("rds_on_ohm", 0.028) == "28mΩ"
    RESULTS.append(("Resistance unit (>=1k=kΩ, <1=mΩ)", "%d cases" % len(cases),
                    "single _fmt_ohm formatter", "PASS"))


# --------------------------------------------------------------------------
# 10. FORMAL PRODUCTION RULE (Alternative Parts) — 2026-09-24
#     RAW has real alternates -> rendered; RAW has none -> NOTHING rendered.
#     No AI generation, no inference from parameters, no MPN-name guessing.
# --------------------------------------------------------------------------
def test_alternative_parts_raw_gate():
    sys.path.insert(0, r"D:/SZ Procure/site")
    os.chdir(r"D:/SZ Procure/site")
    import gen_parts

    real = {"supplier_reference": "C2931360", "mpn": "62684-402100ALF",
            "alternative_parts": "REAL-ALT; NOT-IN-RAW",
            "alternative_parts_detail": ""}
    fake = {"supplier_reference": "C99999999", "mpn": "NO-RAW-SKU",
            "alternative_parts": "SOME-ALT",
            "alternative_parts_detail": ""}

    # The index holds real 5-field alternates (list, not set): RAW entries
    # shown as-is (cross-brand included), only no-C# / self entries dropped.
    saved = gen_parts._raw_alt_index
    try:
        # (a) RAW authorises one MPN only -> the other candidate must be dropped
        gen_parts._raw_alt_index = lambda: {"C2931360": [{"mpn": "REAL-ALT",
                                                          "manufacturer": "BOURNS",
                                                          "lcsc": "C1", "type": "Similar",
                                                          "package": ""}]}
        detail, mpns = gen_parts.resolve_alternatives(real)
        assert mpns == ["REAL-ALT"], "RAW not honoured: %r" % (mpns,)
        assert len(detail) == 1 and detail[0]["mpn"] == "REAL-ALT"
        # (b) RAW authorises nothing at all -> no alternates, whatever MASTER says
        gen_parts._raw_alt_index = lambda: {}
        for row in (real, fake):
            d, m = gen_parts.resolve_alternatives(row)
            assert (d, m) == ([], []), "RAW-less SKU still returned alternates: %r" % (([d, m]),)
        # (c) empty MASTER fields can never invent an alternate
        gen_parts._raw_alt_index = lambda: {}
        d, m = gen_parts.resolve_alternatives(
            {"supplier_reference": "", "mpn": "", "alternative_parts": "X",
             "alternative_parts_detail": "[]"})
        assert (d, m) == ([], []), "MASTER string resurrected an alternate: %r" % (([d, m]),)
    finally:
        gen_parts._raw_alt_index = saved

    # (d) the extractor itself: the RAW alternatePartList is replayed AS-IS.
    #     Cross-brand entries are kept, entries without a C# are kept, the part
    #     itself is kept when LCSC lists it, and — business decision
    #     2026-09-25 — zero-stock entries are KEPT too: an out-of-stock
    #     alternate is still an alternate, and what LCSC cannot supply is
    #     exactly what we source from Huaqiangbei. RAW order is preserved.
    from factory import alternates
    mp = {"productModel": "3296W-1-103LF", "brandNameEn": "BOURNS"}
    src = {"alternatePartList": [
        {"productCode": "C111776", "productModel": "3296X-1-103LF",
         "brandNameEn": "BOURNS", "encapStandard": "SIP-3P", "stockNumber": 6400},
        {"productCode": "C48997937", "productModel": "3296W-1-103LF",
         "brandNameEn": "JIERR", "encapStandard": "Through Hole-3P",
         "stockNumber": 21385},
        {"productCode": "", "productModel": "3296W-1-103RLF",
         "brandNameEn": "BOURNS", "encapStandard": "", "stockNumber": 5},
        {"productCode": "C6084183", "productModel": "3296W-1-103RLF",
         "brandNameEn": "BOURNS", "encapStandard": "SIP-3P", "stockNumber": 0},
        {"productCode": "C2", "productModel": "3296W-1-103RLF",
         "brandNameEn": "BOURNS"},                 # absent stockNumber -> kept
        {"no": "model key"},                       # empty productModel -> skipped
        "not-a-dict",                              # non-dict -> skipped
    ]}
    got = alternates.real_alternates(src, mp)
    assert [g["mpn"] for g in got] == ["3296X-1-103LF", "3296W-1-103LF",
                                       "3296W-1-103RLF", "3296W-1-103RLF",
                                       "3296W-1-103RLF"], \
        "AS-IS replay incl. zero-stock: %r" % (got,)
    assert [g["lcsc"] for g in got] == ["C111776", "C48997937", "", "C6084183", "C2"]
    assert got[0]["type"] == "Similar"

    # (e) alignment lock on a REAL raw file: the rendered list MUST equal the
    #     RAW alternatePartList entry for entry, in RAW order, stock ignored
    #     (C34846 = 3296W-1-103LF: 6 RAW entries, all 6 shown — including the
    #     zero-stock 3296W-1-103RLF that LCSC's own page hides).
    import json as _json
    with open("D:/SZ Procure/采集流水线/基础数据/C34846.json", encoding="utf-8") as fh:
        _d = _json.load(fh)
    _mp = _d["source_raw"]["main_product"]
    _raw_list = _d["source_raw"].get("alternatePartList") or _mp.get("alternatePartList") or []
    _want = [a["productModel"] for a in _raw_list
             if isinstance(a, dict) and a.get("productModel")]
    _got = alternates.real_alternates(_d["source_raw"], _mp)
    assert [g["mpn"] for g in _got] == _want, \
        "page must mirror RAW 1:1: %r != %r" % ([g["mpn"] for g in _got], _want)
    assert len(_got) == 6, "C34846 full list must be 6 entries, got %d" % len(_got)
    RESULTS.append(("Alternative Parts = RAW alternatePartList, AS-IS, no stock gate",
                    "5 sub-cases", "RAW-less + no section/tab/meta", "PASS"))


def test_summary():
    print("\n" + "=" * 74)
    print("%-46s | %-16s | %s" % ("RULE", "scope", "result"))
    print("-" * 74)
    for rule, scope, res, st in RESULTS:
        print("%-46s | %-16s | %s [%s]" % (rule, scope, res, st))
    print("=" * 74)
    assert len(RESULTS) == 11, "not every rule was exercised: %d" % len(RESULTS)  # noqa: E501


if __name__ == "__main__":
    test_range_never_collapses()
    test_surge_current_is_its_own_field()
    test_leakage_normalised_as_current()
    test_unmapped_specs_forwarded()
    test_datasheet_identity_gate()
    test_unit_rewrite_semantics()
    test_master_write_scopes()
    test_read_master_handles_bom()
    test_attr_allowlist_covers_production()
    test_resistance_unit_formatting()
    test_alternative_parts_raw_gate()
    test_summary()
    print("ALL 11 RULE GROUPS PASS")
