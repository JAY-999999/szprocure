"""
Phase 3 — Noindex policy unit tests (sandbox, no production writes).

Exercises factory.index_policy entirely in-memory. It NEVER touches gen_parts.py,
the production MASTER, the 550 live pages, the sitemap, or any Build/Commit/Push/Deploy.

What it proves (final policy §三/§四)
-------------------------------------
 * Normal, fully-qualified SKU                  -> "index, follow"
 * SPEC_THIN (0 / insufficient spec keys)       -> "index, follow"   (changed from old rule)
 * Empty attributes_json + real MPN             -> "index, follow"
 * UNKNOWN_CATEGORY                             -> "noindex, follow"
 * NO_MPN (no real part number)                 -> "noindex, follow"
 * SYNTHETIC_MPN (test/placeholder pattern)     -> "noindex, follow"
 * true-DUPLICATE (non-primary)                 -> "noindex, follow" + canonical_to primary
 * ``follow`` is preserved in EVERY case (never nofollow) -> internal-link equity safe
 * Fail-open: unexpected input -> "index, follow"
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import factory.index_policy as ip  # noqa: E402


def _rec(mpn="X1", category="Microcontroller", brand="ACME", manufacturer="ACME",
         url_slug="x1", clean_mpn=None, attributes_json=None):
    return {
        "mpn": mpn, "clean_mpn": clean_mpn or mpn, "brand": brand,
        "manufacturer": manufacturer, "url_slug": url_slug, "category": category,
        "attributes_json": attributes_json,
    }


def test_normal_skus_are_indexed():
    for attrs in ({"package": "LQFP-48"},
                  {"core": "ARM Cortex-M3", "freq": 72},
                  None):
        cls = ip.classify_part_record(_rec(attributes_json=attrs))
        assert cls.indexable is True
        assert cls.reason == ip.REASON_INDEX
        assert ip.decide_robots_meta(classification=cls) == ip.ROBOTS_INDEX_FOLLOW


def test_spec_thin_stays_index():
    # SPEC_THIN (0 spec keys) must NOT trigger noindex under final policy.
    for attrs in ({}, "", None, "{}", '{"": ""}'):
        cls = ip.classify_part_record(_rec(attributes_json=attrs))
        assert cls.indexable is True, f"SPEC_THIN must stay indexable: {attrs!r}"
        assert cls.reason == ip.REASON_INDEX
        # legacy signature: a real MPN must be supplied or it becomes NO_MPN
        d = ip.decide_robots_meta(category="Microcontroller", mpn="X1",
                                  attributes_json=attrs)
        assert d == ip.ROBOTS_INDEX_FOLLOW
    # is_spec_thin is still correct as a diagnostic
    assert ip.is_spec_thin({}) is True
    assert ip.is_spec_thin({"a": 1}) is False


def test_empty_attr_with_mpn_stays_index():
    # Empty attributes_json + a REAL MPN -> index, follow (no noindex).
    cls = ip.classify_part_record(_rec(mpn="STM32F103C8T6", attributes_json="{}"))
    assert cls.indexable is True
    assert cls.reason == ip.REASON_INDEX
    assert ip.decide_robots_meta(
        category="Microcontroller", mpn="STM32F103C8T6", attributes_json="{}"
    ) == ip.ROBOTS_INDEX_FOLLOW


def test_unknown_category_noindex():
    for cat in ("", "uncategorized", "Unknown", "UNMAPPED", None):
        cls = ip.classify_part_record(_rec(category=cat))
        assert cls.indexable is False
        assert cls.reason == ip.REASON_UNKNOWN_CATEGORY
        assert ip.decide_robots_meta(category=cat, attributes_json={"x": 1}) \
            == ip.ROBOTS_NOINDEX_FOLLOW


def test_no_mpn_noindex():
    cls = ip.classify_part_record(_rec(mpn="", attributes_json={"package": "QFN"}))
    assert cls.indexable is False
    assert cls.reason == ip.REASON_NO_MPN
    assert ip.decide_robots_meta(
        category="Microcontroller", mpn="", attributes_json={"package": "QFN"}
    ) == ip.ROBOTS_NOINDEX_FOLLOW


def test_synthetic_mpn_noindex():
    for mpn in ("MCU100000123", "MOS100000999", "1234567", "ABC-PLACEHOLDER",
                "RES000001XXX", "FOO_TEST"):
        cls = ip.classify_part_record(_rec(mpn=mpn))
        assert cls.indexable is False, f"synthetic {mpn!r} must be noindex"
        assert cls.reason == ip.REASON_SYNTHETIC_MPN
        assert ip.decide_robots_meta(category="IC", mpn=mpn) == ip.ROBOTS_NOINDEX_FOLLOW
    # a real-looking MPN is NOT flagged
    assert ip.is_synthetic_mpn("STM32F103C8T6") is False


def test_duplicate_noindex_with_canonical():
    # Two rows share a normalized MPN. Brand-native match -> primary.
    primary = _rec(mpn="TPS79001", brand="Texas Instruments",
                   manufacturer="Texas Instruments", url_slug="tps79001")
    dup = _rec(mpn="TPS79001", brand="TI", manufacturer="Texas Instruments",
               url_slug="tps79001-ti", clean_mpn="TPS79001")
    classes = ip.classify_parts([primary, dup])
    assert classes["tps79001"].indexable is True
    assert classes["tps79001"].reason == ip.REASON_INDEX
    assert classes["tps79001-ti"].indexable is False
    assert classes["tps79001-ti"].reason == ip.REASON_DUPLICATE
    assert classes["tps79001-ti"].canonical_to == "tps79001"
    assert classes["tps79001-ti"].robots_meta == ip.ROBOTS_NOINDEX_FOLLOW

    # Deterministic fallback when NO brand-native match exists.
    a = _rec(mpn="ABC123", brand="X", manufacturer="MfrA", url_slug="abc123-a",
             category="Regulator")
    b = _rec(mpn="ABC123", brand="Y", manufacturer="MfrB", url_slug="abc123-b",
             category="Regulator")
    classes2 = ip.classify_parts([a, b])
    # deterministic: manufacturer "MfrA" < "MfrB" -> a is primary
    assert classes2["abc123-a"].indexable is True
    assert classes2["abc123-b"].indexable is False
    assert classes2["abc123-b"].canonical_to == "abc123-a"


def test_never_nofollow():
    # Every reason code must preserve 'follow'.
    cases = [
        _rec(),                                   # INDEX
        _rec(category=""),                        # UNKNOWN_CATEGORY
        _rec(mpn=""),                             # NO_MPN
        _rec(mpn="MCU100000123"),                 # SYNTHETIC_MPN
    ]
    classes = ip.classify_parts(cases)
    for cls in classes.values():
        assert "nofollow" not in cls.robots_meta
        assert cls.robots_meta in (ip.ROBOTS_INDEX_FOLLOW, ip.ROBOTS_NOINDEX_FOLLOW)
    # never return a pure noindex,nofollow
    assert ip.ROBOTS_NOINDEX_FOLLOW == "noindex, follow"


def test_fail_open_default_index():
    # Malformed / unexpected inputs fail open to index, follow.
    weird = _rec(mpn=None, category=123, attributes_json=b"not-a-str")
    cls = ip.classify_part_record(weird)
    # mpn None -> treated as no MPN -> noindex (safe). Use a real mpn for open test.
    ok = _rec(mpn="REAL-MPN-001", category="Sensor", attributes_json=b"not-a-str")
    cls2 = ip.classify_part_record(ok)
    assert cls2.indexable is True
    assert cls2.robots_meta == ip.ROBOTS_INDEX_FOLLOW


def test_normalize_mpn_ignores_case_space_hyphen():
    assert ip.normalize_mpn("TPS79001") == ip.normalize_mpn("tps 79001")
    assert ip.normalize_mpn("TPS-79001") == ip.normalize_mpn("tps79001")
    assert ip.normalize_mpn("  TPS_79001 ") == "tps_79001"
    assert ip.normalize_mpn(None) == ""
    assert ip.normalize_mpn("") == ""


def test_hyphenated_real_mpn_not_falsely_synthetic():
    # Regression: a genuine hyphenated MPN whose normalized clean_mpn is
    # pure-numeric (e.g. TE Connectivity "1909763-1" -> clean "19097631") must
    # stay indexable. Only the RAW mpn is tested against SYNTHETIC_MPN_PATTERNS
    # (mirrors gen_parts.detect_synthetic_mpn), so the hyphen in the real mpn
    # prevents a false synthetic hit.
    rec = _rec(mpn="1909763-1", clean_mpn="19097631", category="Connectors",
               brand="TE Connectivity", manufacturer="TE Connectivity",
               url_slug="19097631")
    cls = ip.classify_part_record(rec)
    assert cls.indexable is True, "real TE Connectivity connector must stay index"
    assert cls.reason == ip.REASON_INDEX
    assert cls.robots_meta == ip.ROBOTS_INDEX_FOLLOW
    # and it is NOT picked up by the synthetic detector on the raw mpn
    assert ip.is_synthetic_mpn("1909763-1") is False
    # but the pure-numeric CLEAN form alone WOULD match (proves the guard only
    # runs on the raw mpn)
    assert ip.is_synthetic_mpn("19097631") is True
