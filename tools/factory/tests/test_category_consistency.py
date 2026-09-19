"""B3 (2026-09-19) regression tests for the 010203 classification chain.

Covers:
  A. 3 known misfires are no longer mis-classified (and land correctly / blocked)
  B. 3 known-good SKUs keep their classification and are NOT caught by the gate
  C. 3 keyword false-positive counterexamples (resistor / interface / buffer)
  D. category <-> native_l1 consistency gate (conflict -> UNKNOWN_CATEGORY block;
     consistent -> pass-through, never over-blocks)
  E. plan_release stops an UNKNOWN_CATEGORY row (reuses existing stop mechanism)

Design notes:
  * native_l1 is computed via the PUBLIC api compute_native_l1(parent, leaf),
    never via the scale500 file lookup (Problem A is deferred / out of scope).
  * The 3 misfires/3 normals load REAL RAW from 01_RAW/lcsc_http (no scale500).
  * No MASTER / HTML / release_pipeline writes happen here.
"""
import os
import sys
import json

sys.path.insert(0, r"D:/SZ Procure/site/tools")
sys.path.insert(0, r"D:/SZ Procure/site")

from factory import product_data as pd
from factory import category
from factory import lcsc_http_adapter
import native_l1_mapper as nl1

RAW_DIR = r"D:/SZ Procure/01_RAW/lcsc_http"
MASTER = r"D:/SZ Procure/site/data/production/master_parts_v2.1.csv"

RESULTS = []  # (mpn, native_l1, category, final_status)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _flatten(c_number):
    env = json.load(open(os.path.join(RAW_DIR, "%s.json" % c_number),
                        encoding="utf-8"))
    return lcsc_http_adapter.flatten_envelope(env)


def _native(rec):
    return nl1.compute_native_l1(rec.get("parentCatalogName") or "",
                                 rec.get("catalogName") or "")


def _classify(rec, mpn="SYNTH"):
    fields, _meta = lcsc_http_adapter.http_build_category_row(rec, mpn, "X")
    return fields["category"]


def _synth(parent, catalog, desc="", attrs=None):
    return {
        "mpn": "SYNTH", "manufacturer_raw": "X",
        "catalogName": catalog, "parentCatalogName": parent, "category": "",
        "description": desc,
        "attributes_json": json.dumps(attrs or {}),
        "attributes_json_unmapped": "{}", "supplier_sku": "C99999999",
        "_source_kind": "lcsc_http_json", "_applications_en": "",
    }


class _LocalPatch:
    """Minimal monkeypatch stand-in so the suite also runs as a plain script."""
    def __init__(self, _module):
        self._saved = {}
    def setattr(self, obj, name, val):
        self._saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, val)
    def restore(self):
        for (obj, name), val in self._saved.items():
            setattr(obj, name, val)


# --------------------------------------------------------------------------
# A. known misfires
# --------------------------------------------------------------------------
def test_known_misfires_fixed():
    cases = {
        "74HC165D": ("C52140407", "Logic IC", "logic-ics"),
        "INA180A2IDBVR": ("C47021222", "Operational Amplifier",
                          "amplifiers-comparators"),
        "LTC6811HG-1#3ZZTRPBF": ("C459704", category.UNKNOWN_CATEGORY,
                                "power-management"),
    }
    for mpn, (c, exp_cat, exp_nl1) in cases.items():
        rec = _flatten(c)
        cat = _classify(rec, mpn)
        nl = _native(rec)
        # generic-rule proof: must NOT be the two wrong classes
        assert cat != "Resistor", "%s still Resistor (description resistor leak)" % mpn
        assert cat != "Interface IC", "%s still Interface IC (buffer/interface leak)" % mpn
        assert nl == exp_nl1, "%s native_l1 %r != %r" % (mpn, nl, exp_nl1)
        if exp_cat == category.UNKNOWN_CATEGORY:
            assert cat == category.UNKNOWN_CATEGORY, \
                "%s should be Uncategorized (blocked), got %r" % (mpn, cat)
        else:
            assert cat == exp_cat, "%s cat %r != %r" % (mpn, cat, exp_cat)
        RESULTS.append((mpn, nl, cat, "FIXED_OK"))


# --------------------------------------------------------------------------
# B. known-good SKUs stay correct and are NOT over-blocked
# --------------------------------------------------------------------------
def test_known_normals_unchanged():
    cases = {
        "LM339DR2G": ("C63821", "Operational Amplifier"),
        "SN65HVD75DRBR": ("C98708", "Interface IC"),
        "BSS138LT3G": ("C83790", "MOSFET"),
    }
    for mpn, (c, exp_cat) in cases.items():
        rec = _flatten(c)
        cat = _classify(rec, mpn)
        nl = _native(rec)
        assert cat == exp_cat, "%s REGRESSION cat %r != %r" % (mpn, cat, exp_cat)
        assert pd._cat_nl1_conflict(cat, nl) is False, \
            "%s wrongly flagged by consistency gate" % mpn
        RESULTS.append((mpn, nl, cat, "NORMAL_OK"))


# --------------------------------------------------------------------------
# C. keyword false-positive counterexamples (proves rule-level fix, not MPN patch)
# --------------------------------------------------------------------------
def test_keyword_counterexamples():
    # 1) description contains "pull-up resistor" but parent is Logic -> NOT Resistor
    r1 = _synth("Logic", "Logic Gates", "pull-up resistor required")
    c1 = _classify(r1)
    assert c1 != "Resistor", "pull-up resistor mis-hit Resistor"
    assert c1 == "Logic IC", "expected Logic IC, got %r" % c1
    RESULTS.append(("C-ex1 pull-up resistor / Logic", "-", c1, "PASS"))

    # 2) isoSPI interface attr but parent Power Management -> NOT Interface IC
    r2 = _synth("Power Management (PMIC)", "Battery Management",
                "isoSPI interface", {"interface": "isoSPI"})
    c2 = _classify(r2)
    assert c2 != "Interface IC", "isoSPI interface mis-hit Interface IC"
    RESULTS.append(("C-ex2 isoSPI / PMIC", "-", c2, "PASS"))

    # 3) catalogName "Buffer Amps" -> NOT Interface IC
    r3 = _synth("", "Instrumentation, Op Amps, Buffer Amps", "")
    c3 = _classify(r3)
    assert c3 != "Interface IC", "Buffer Amps mis-hit Interface IC"
    RESULTS.append(("C-ex3 Buffer Amps", "-", c3, "PASS"))


# --------------------------------------------------------------------------
# D. category <-> native_l1 consistency gate
# --------------------------------------------------------------------------
def test_consistency_gate(monkeypatch):
    # conflict: category Resistor, native_l1 logic-ics -> block
    monkeypatch.setattr(nl1, "compute_native_l1_for_row", lambda sku: "logic-ics")
    rec = _synth("", "Resistors", "")
    row, _meta = pd.build_row(rec, "GATECONFLICT", "X")
    assert row["category"] == category.UNKNOWN_CATEGORY, \
        "conflict must force UNKNOWN_CATEGORY"
    assert row[pd.F_NEEDS_REVIEW] is True
    assert "gate_cat_nl1_conflict" in json.loads(row[pd.F_DETECT])

    # consistent: category Resistor, native_l1 resistors -> pass-through
    monkeypatch.setattr(nl1, "compute_native_l1_for_row", lambda sku: "resistors")
    rec2 = _synth("", "Resistors", "")
    row2, _m2 = pd.build_row(rec2, "GATEOK", "X")
    assert row2["category"] == "Resistor", "consistent row must keep category"
    assert row2[pd.F_NEEDS_REVIEW] is False

    # consistent: Logic IC + logic-ics -> pass-through
    monkeypatch.setattr(nl1, "compute_native_l1_for_row", lambda sku: "logic-ics")
    rec3 = _synth("Logic", "Logic Gates", "")
    row3, _m3 = pd.build_row(rec3, "GATEOK2", "X")
    assert row3["category"] == "Logic IC"
    assert row3[pd.F_NEEDS_REVIEW] is False
    RESULTS.append(("gate-conflict Resistor/logic-ics", "logic-ics",
                    "Resistor->UNKNOWN_CATEGORY", "BLOCK_OK"))
    RESULTS.append(("gate-pass Resistor/resistors", "resistors",
                    "Resistor", "PASS_OK"))


# --------------------------------------------------------------------------
# E. plan_release stops an UNKNOWN_CATEGORY row (reuses existing stop)
# --------------------------------------------------------------------------
def test_release_block():
    from factory import release_pipeline as rp
    rec = _synth("", "Resistors", "")
    row, _meta = pd.build_row(rec, "RELEASEBLOCK", "X")
    # emulate the gate-blocked state a real conflict produces
    row["category"] = category.UNKNOWN_CATEGORY
    row["mpn"] = "RELEASEBLOCK"
    plan = rp.plan_release(MASTER, [row])
    assert plan.stops, "UNKNOWN_CATEGORY row must be stopped by plan_release"
    assert "RELEASEBLOCK" not in [r.get("mpn") for r in plan.new_rows], \
        "blocked row must not reach release"
    RESULTS.append(("release-block UNKNOWN_CATEGORY", "-",
                    "UNKNOWN_CATEGORY", "STOP_OK"))


# --------------------------------------------------------------------------
# summary printer
# --------------------------------------------------------------------------
def test_summary():
    print("\n" + "=" * 72)
    print("MPN | native_l1 | category | final_status")
    print("-" * 72)
    for mpn, nl, cat, st in RESULTS:
        print("%s | %s | %s | %s" % (mpn, nl, cat, st))
    print("=" * 72)
    assert RESULTS, "no results collected"


if __name__ == "__main__":
    # runnable without pytest (same assertions + table)
    test_known_misfires_fixed()
    test_known_normals_unchanged()
    test_keyword_counterexamples()
    patcher = _LocalPatch(nl1)
    try:
        test_consistency_gate(patcher)
    finally:
        patcher.restore()
    test_release_block()
    test_summary()
