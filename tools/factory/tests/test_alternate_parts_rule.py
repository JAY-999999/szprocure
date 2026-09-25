"""2026-09-25 (R17) — regression tests for the FORMAL Alternative Parts rule.

The rule, in one line: **the RAW `alternatePartList` is replayed AS-IS**, in
RAW order, with NO filter layer on top (no brand gate, no LCSC stock gate, no
`hasAlternatePart` gate, no self-exclusion, no de-duplication). Only non-dict
entries and entries with an empty `productModel` are skipped.

This file exists so the rule cannot quietly rot. R14 briefly added an LCSC
stock gate and R11/R12 carried three other filters; every one of them was
reverted, and these tests are what keeps them out for good.

What is pinned here:

  1. AS-IS replay — content AND order come straight from RAW.
  2. NO LCSC stock gate — a zero-stock alternate is still an alternate.
  3. NO brand gate — cross-brand entries stay; the part listing itself stays.
  4. Identity rule (supplier_reference, never MPN alone) — the AOS AO3401A
     (C15127) and the UMW AO3401A (C347476) own DIFFERENT alternate lists, and
     neither row may ever serve the other's list.
  5. The ONLY skip is an entry with no `productModel`.
  6. The NEXT-BATCH path: RAW -> 02 `flatten_envelope` -> `master_row`
     carries the same AS-IS list byte-identically, so a freshly released SKU
     does not need a per-SKU patch.
  7. The release gate `alternates.audit_release_rows` stops a batch whose
     candidate disagrees with RAW, and does not block on a missing RAW file.

All tests are read-only w.r.t. production data. Run with pytest or directly.
"""
import glob
import io
import json
import os
import sys

sys.path.insert(0, r"D:/SZ Procure/site/tools")
sys.path.insert(0, r"D:/SZ Procure/site")

from factory import alternates, product_data
from factory.lcsc_http_adapter import flatten_envelope

RAWDIR = r"D:/SZ Procure/采集流水线/基础数据"
SITE = r"D:/SZ Procure/site"

# the 10 validation SKUs (C# -> MPN); the ones worth pinning are the ones that
# actually carry an alternate list.
VALIDATION = {
    "C15127": "AO3401A",
    "C2931360": "62684-402100ALF",
    "C282519": "TCC0603X7R104K500CT",
    "C118954": "3296W-1-103",
    "C34846": "3296W-1-103LF",
    "C20416655": "BAV99S",
    "C118131": "LT4363IMS-2#TRPBF",
    "C1521784": "XC7A100T-1FGG484I",
    "C496549": "BWSMA-KE-Z001",
    "C22367830": "HGC0805R5106K500NSLJ",
}

RESULTS = []


def _load(cid):
    p = os.path.join(RAWDIR, cid + ".json")
    if not os.path.exists(p):
        return None
    with io.open(p, encoding="utf-8") as f:
        d = json.load(f)
    sr = d.get("source_raw", d)
    return sr, (sr.get("main_product") or {})


def _asis(cid):
    sr, mp = _load(cid)
    assert sr is not None, "RAW missing for %s" % cid
    return alternates.real_alternates(sr, mp)


# --------------------------------------------------------------------------
# 1. AS-IS replay: content AND order
# --------------------------------------------------------------------------
def test_asis_replay_keeps_order():
    for cid in ("C34846", "C2931360", "C15127", "C282519"):
        sr, mp = _load(cid)
        raw = sr.get("alternatePartList") or (mp.get("alternatePartList") or [])
        expected = [str(a.get("productModel")).strip() for a in raw]
        got = [d["mpn"] for d in alternates.real_alternates(sr, mp)]
        assert got == expected, "%s: order/content drift: %r vs %r" % (cid, got, expected)
    RESULTS.append(("AS-IS replay keeps RAW order", "-", "4 SKUs 1:1", "PASS"))


# --------------------------------------------------------------------------
# 2. NO LCSC stock gate
# --------------------------------------------------------------------------
def test_no_stock_gate():
    # synthetic zero-stock + out-of-stock-but-real entries must survive
    src = {"alternatePartList": [
        {"productModel": "ZERO-STOCK-PART", "brandNameEn": "X",
         "productCode": "C1", "stockNumber": 0},
        {"productModel": "IN-STOCK-PART", "brandNameEn": "X",
         "productCode": "C2", "stockNumber": 500},
    ]}
    got = [d["mpn"] for d in alternates.real_alternates(src, {})]
    assert got == ["ZERO-STOCK-PART", "IN-STOCK-PART"], got

    # ... and the same on a REAL SKU: TCC0603 (C282519) has 3 zero-stock
    # alternates that the R14 stock gate used to hide.
    c282519 = [d["mpn"] for d in _asis("C282519")]
    assert len(c282519) == 11, "TCC0603 alternate count moved: %d" % len(c282519)

    sr, mp = _load("C2931360")
    sr.setdefault("alternatePartList", []).append(
        {"productModel": "SHOULD-NOT-BE-GATED", "stockNumber": 0})
    gated = [d["mpn"] for d in alternates.real_alternates(sr, mp)]
    assert "SHOULD-NOT-BE-GATED" in gated
    RESULTS.append(("no LCSC stock gate (0-stock shown)", "-", "11 / 11 kept", "PASS"))


# --------------------------------------------------------------------------
# 3. NO brand gate, no self-exclusion
# --------------------------------------------------------------------------
def test_no_brand_or_self_gate():
    aos = _asis("C15127")
    mpns = [d["mpn"] for d in aos]
    # C15127 (AOS AO3401A) legitimately lists AO3401 from TWGMC / JSMSEMI —
    # cross-brand entries of its own family, and one entry that is the same
    # package family name. None of them may be filtered away.
    assert "AO3401" in mpns, "brand gate reintroduced: %r" % mpns
    assert any(d["manufacturer"] not in ("AOS", "Alpha & Omega", "")
               for d in aos), "brand gate collapsed the manufacturers"
    RESULTS.append(("no brand / self-exclusion gate", "-", "cross-brand kept", "PASS"))


# --------------------------------------------------------------------------
# 4. Identity rule: supplier_reference, never MPN alone
# --------------------------------------------------------------------------
def test_identity_rule_clone_parts():
    aos = _asis("C15127")
    umw = _asis("C347476")
    aos_mpns = [d["mpn"] for d in aos]
    umw_mpns = [d["mpn"] for d in umw]
    assert aos and umw, "clone-part fixture missing from RAW"
    assert set(aos_mpns) != set(umw_mpns), "clone parts now share a list"

    # the 03 gate must answer from the C#, and must never hand a row the
    # other manufacturer's list.
    sys.path.insert(0, SITE)
    os.chdir(SITE)
    import gen_parts
    for cid, mine, theirs in (("C15127", aos_mpns, umw_mpns),
                              ("C347476", umw_mpns, aos_mpns)):
        detail, plain = gen_parts.resolve_alternatives(
            {"supplier_reference": cid, "mpn": "AO3401A"})
        assert plain == mine, "%s served the wrong list: %r" % (cid, plain)
        assert not (set(plain) & set(theirs)), \
            "%s picked up the other manufacturer's alternates: %r" % (cid, plain)
    RESULTS.append(("identity rule: C# not MPN (AO3401A AOS/UMW)", "-",
                    "2 rows, 0 cross-talk", "PASS"))


# --------------------------------------------------------------------------
# 5. the only legal skip
# --------------------------------------------------------------------------
def test_only_empty_productmodel_skipped():
    src = {"alternatePartList": [
        {"productModel": "", "brandNameEn": "Ghost"},          # skipped
        "not-a-dict",                                          # skipped
        None,                                                  # skipped
        {"brandNameEn": "NoModel"},                            # skipped
        {"productModel": "KEEP-ME"},                           # kept
    ]}
    got = [d["mpn"] for d in alternates.real_alternates(src, {})]
    assert got == ["KEEP-ME"], got
    assert alternates.real_alternates({}, {}) == []
    assert alternates.real_alternates(None, None) == []
    RESULTS.append(("only empty productModel is skipped", "-", "4 skipped", "PASS"))


# --------------------------------------------------------------------------
# 6. the NEXT-BATCH path: RAW -> 02 -> MASTER row
# --------------------------------------------------------------------------
def test_next_batch_path_is_asis():
    checked = 0
    for cid in VALIDATION:
        loaded = _load(cid)
        if loaded is None:
            continue
        sr = loaded[0]
        asis = alternates.real_alternates(sr, sr.get("main_product") or {})
        want = "; ".join(s["mpn"] for s in asis)
        want_detail = json.dumps(asis, ensure_ascii=False)
        rec = flatten_envelope({"source_raw": sr})
        assert (rec.get("alternative_parts") or "") == want, \
            "%s: 02 candidate lost the AS-IS list" % cid
        row = product_data.master_row(rec)
        assert (row.get("alternative_parts") or "") == want, \
            "%s: master_row dropped alternative_parts" % cid
        assert (row.get("alternative_parts_detail") or "") == want_detail, \
            "%s: master_row dropped alternative_parts_detail" % cid
        checked += 1
    assert checked == len(VALIDATION)
    RESULTS.append(("next-batch path RAW->02->MASTER row", "-",
                    "%d SKUs byte-identical" % checked, "PASS"))


# --------------------------------------------------------------------------
# 7. the release gate
# --------------------------------------------------------------------------
def test_release_gate():
    # a genuine 02 candidate -> gate passes, no failure
    env = _load("C34846")[0]
    rec = flatten_envelope({"source_raw": env})
    fails, warns, report = alternates.audit_release_rows([rec])
    assert fails == [], fails
    assert not any(f[0] == "C34846" for f in fails)

    # a tampered candidate (someone re-added a filter in 02) -> STOP
    tampered = dict(rec, alternative_parts="; ".join(
        [m for m in (rec.get("alternative_parts") or "").split("; ") if "RLF" not in m]))
    fails2, _w2, _r2 = alternates.audit_release_rows([tampered])
    assert fails2 and fails2[0][0] == "C34846", fails2

    # a candidate with no RAW file at all -> WARN only, never a stop
    _f3, warns3, _r3 = alternates.audit_release_rows(
        [{"supplier_reference": "C0000000", "alternative_parts": "X"}])
    assert any(w[0] == "C0000000" for w in warns3), warns3
    assert not _f3
    RESULTS.append(("release gate: mismatch=STOP, missing RAW=WARN", "-",
                    "1 pass / 1 stop / 1 warn", "PASS"))


def main():
    import traceback
    for fn in (test_asis_replay_keeps_order, test_no_stock_gate,
               test_no_brand_or_self_gate, test_identity_rule_clone_parts,
               test_only_empty_productmodel_skipped, test_next_batch_path_is_asis,
               test_release_gate):
        try:
            fn()
        except Exception:
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
            RESULTS.append((fn.__name__, "-", "EXCEPTION", "FAIL"))
    print("\n" + "=" * 74)
    for group, name, detail, res in RESULTS:
        print("  %-6s %-44s %s" % (res, group, detail))
    ok = all(r == "PASS" for _g, _n, _d, r in RESULTS)
    print("=" * 74)
    print("ALL %d PASS" % len(RESULTS) if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
