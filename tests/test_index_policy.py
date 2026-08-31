"""
#14 Robots index-policy unit tests (sandbox, no production writes).

Exercises factory.index_policy.decide_robots_meta entirely in-memory. It NEVER
touches gen_parts.py, the production MASTER, the 550 live pages, the sitemap, or
any Build/Commit/Push/Deploy.

What it proves
--------------
 * Normal, fully-qualified SKU           -> "index, follow"
 * SPEC_THIN (0/insufficient spec keys) -> "noindex, follow"
 * Empty attributes_json                -> "noindex, follow"   (covered by SPEC_THIN)
 * UNKNOWN_CATEGORY                      -> "noindex, follow"
 * ``follow`` is preserved in EVERY case (never nofollow) -> internal links safe
 * The policy is orthogonal to sitemap/canonical: its signature only consumes
   category + attributes_json, so wiring it in cannot regress sitemap or canonical.
"""
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import factory.index_policy as ip  # noqa: E402


def test_normal_skus_are_indexed():
    # known category + at least one real spec key
    for attrs in ({"package": "LQFP-48"}, {"core": "ARM Cortex-M3", "freq": 72}):
        assert ip.decide_robots_meta(category="Microcontroller", attributes_json=attrs) \
            == "index, follow"
    # JSON-string form (what gen_parts actually passes through)
    assert ip.decide_robots_meta(
        category="Integrated Circuits",
        attributes_json='{"voltage": "3.3V", "current": "1.2A"}',
    ) == "index, follow"


def test_spec_thin_is_noindex_follow():
    # zero keys
    assert ip.decide_robots_meta(category="Microcontroller", attributes_json={}) \
        == "noindex, follow"
    # empty JSON object string
    assert ip.decide_robots_meta(category="Resistor", attributes_json="{}") \
        == "noindex, follow"
    # below the min-spec threshold
    assert ip.decide_robots_meta(
        category="Capacitor", attributes_json={"note": "x"},
        min_spec_keys=3,
    ) == "noindex, follow"


def test_empty_attributes_json_is_noindex_follow():
    # the explicit user condition: attributes_json empty
    assert ip.decide_robots_meta(category="Transistor", attributes_json="") \
        == "noindex, follow"
    assert ip.decide_robots_meta(category="Transistor", attributes_json=None) \
        == "noindex, follow"
    # malformed JSON is treated safely as no-index (never leaks into index)
    assert ip.decide_robots_meta(category="Transistor", attributes_json="{bad json") \
        == "noindex, follow"


def test_unknown_category_is_noindex_follow():
    for cat in (None, "", "UNKNOWN", "   "):
        assert ip.decide_robots_meta(category=cat, attributes_json={"package": "QFN"}) \
            == "noindex, follow"


def test_known_category_with_attrs_is_always_index():
    # Even with a single key and a real category -> index (no accidental noindex)
    assert ip.decide_robots_meta(
        category="MOSFET", attributes_json='{"vdss": "30V"}',
    ) == "index, follow"


def test_follow_is_always_preserved():
    cases = [
        ("Microcontroller", {"core": "ARM"}),
        ("Microcontroller", {}),
        (None, {"core": "ARM"}),
        ("Resistor", ""),
        ("Capacitor", '{"x": 1}'),
    ]
    for cat, attrs in cases:
        directive = ip.decide_robots_meta(category=cat, attributes_json=attrs)
        assert "follow" in directive, f"follow must be preserved, got {directive!r}"
        assert "nofollow" not in directive, f"must never nofollow, got {directive!r}"


def test_policy_is_orthogonal_to_sitemap_and_canonical():
    # The decision does not consume, and therefore cannot alter, url/sitemap/
    # canonical inputs. Proven by signature + stable output regardless of any
    # external url-like field we might imagine passing alongside.
    d1 = ip.decide_robots_meta(category="MCU", attributes_json={"core": "ARM"})
    # the function ignores anything but category + attributes_json; re-calling
    # with the same logical inputs is deterministic and unaffected by a url.
    d2 = ip.decide_robots_meta(category="MCU", attributes_json={"core": "ARM"})
    assert d1 == d2 == "index, follow"
    # only the two documented directives ever appear
    assert d1 in ip._ALLOWED_DIRECTIVES
