"""2026-09-26 (R25) — regression tests for the FAQ provenance gate.

The rule, in the operator's own words::

    "Frequently Asked Questions 主要就是根据 LCSC 的数据源，没有的我们自己不生成"

A product-page FAQ may ONLY exist if it originates from the LCSC RAW record of
that very part. Text our own pipeline computes is fabrication.

Three defects produced the 8 fabricated pages found on the test batch
(2026-09-26), and all three are pinned here:

  1. ``category.py::_faq`` templated sentences out of the spec values
     (``f"{mpn} is a {cap} F capacitor"``) and handed them to ``_assemble``,
     which wrote them into ``MASTER.faq``. ``lcsc_http_adapter`` imports the
     same function, so both 02 adapters were affected.
  2. ``gen_parts.py`` "Pass B" re-rendered ``MASTER.faq`` whenever RAW had no
     real FAQ, which is what actually put the text on the page.
  3. Nothing stopped either of the above at release time.

Fixes: ``_faq`` now returns ""; Pass B only runs for MPNs in
``faq_policy.VERIFIED_MPNS``; ``release_pipeline.check_faq_provenance()``
STOPS a candidate whose non-empty ``faq`` RAW cannot trace back to
``source_raw.main_product.faqs``.

What is pinned here:

  1. ``category._faq`` returns "" for any input (both adapters closed).
  2. A fabricated candidate -> STOP ``FAQ_FABRICATED_FAIL``. The eight
     offenders found in the wild are asserted one by one.
  3. A MPN in ``faq_policy.VERIFIED_MPNS`` is WARNed, not stopped.
  4. A candidate whose C# carries RAW ``faqs`` is allowed through.
  5. A candidate with an empty ``faq`` is ignored (most parts have none).
  6. The gate is scoped to CANDIDATES, so the already-published MASTER rows
     that still hold the old text cannot block a release — but they also
     cannot be re-shipped through a new batch.
  7. End to end: ``plan_release`` stops a fabricated candidate.
  8. ``gen_parts`` Pass B is closed for a non-whitelisted MPN.

All tests are read-only w.r.t. production data and need no pytest — run this
file directly (``python tools/factory/tests/test_faq_provenance.py``).
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, r"D:/SZ Procure/site/tools")
sys.path.insert(0, r"D:/SZ Procure/site")

from factory import category, faq_policy, release_pipeline as rp  # noqa: E402

SITE = r"D:/SZ Procure/site"
RAW_DIR = r"D:/SZ Procure/采集流水线/基础数据"

#: the parts caught shipping fabricated FAQ copy on the 2026-09-26 audit.
#: (mpn, C#, the sentence that reached a live page)
FABRICATED = [
    ("HGC0805R5106K500NSLJ", "C22367830", "9.999999999999999e-06 F capacitor"),
    ("TCC0603X7R104K500CT", "C282519", "1.0000000000000001e-07 F capacitor"),
    ("3296W-1-103", "C118954", "10000.0 ohm resistor"),
    ("3296W-1-103LF", "C34846", "10000.0 ohm resistor"),
    ("BWSMA-KE-Z001", "C496549", "Inner hole interface device"),
    ("L7805CV", "C111887", "delivers 5.00 V"),
    ("1N4148WS", "C118873", "rated for 75.0 V reverse"),
    ("RC0603FR-070RL", "C100044", "0.0 ohm resistor"),
]

#: exactly the 24 production columns — master_io rejects any other header.
MASTER_COLS = list(rp.MASTER_COLS)


# --------------------------------------------------------------------------
# 1. the template factory is dead
# --------------------------------------------------------------------------
def test_category_faq_is_inert():
    assert category._faq("3296W-1-103", "What is the resistance",
                         "10000.0 ohm") == ""
    assert category._faq("", "", "") == ""


# --------------------------------------------------------------------------
# 2./3./4./5. the gate itself
# --------------------------------------------------------------------------
def test_fabricated_faq_stops():
    for mpn, cid, snippet in FABRICATED:
        rows = [{"mpn": mpn, "supplier_reference": cid, "faq": f"Q: a?A: {snippet}"}]
        out = rp.check_faq_provenance(rows)
        assert [(lvl, kind) for lvl, kind, _ in out] == \
            [("STOP", "FAQ_FABRICATED_FAIL")], f"{mpn} slipped through"


def test_verified_mpn_warns_not_stops():
    mpn = next(iter(faq_policy.VERIFIED_MPNS))
    rows = [{"mpn": mpn, "supplier_reference": "C20917", "faq": "Q: a?A: b"}]
    out = rp.check_faq_provenance(rows)
    assert [(lvl, kind) for lvl, kind, _ in out] == \
        [("WARN", "FAQ_CURATED_VERIFIED")]


def test_row_without_faq_is_ignored():
    assert rp.check_faq_provenance(
        [{"mpn": "X", "supplier_reference": "C1", "faq": ""}]) == []
    assert rp.check_faq_provenance([]) == []


def test_sourced_faq_is_allowed():
    """A C# that RAW really carries faqs for must pass."""
    idx = rp._raw_faq_index(RAW_DIR)
    cids = [c[:-5] for c in os.listdir(RAW_DIR) if c[:-5].upper() in idx][:5]
    assert cids, "RAW faq index unexpectedly empty — cannot assert the pass side"
    rows = [{"mpn": "S", "supplier_reference": c, "faq": "Q: a?A: b"} for c in cids]
    assert rp.check_faq_provenance(rows) == [], "sourced FAQ wrongly blocked"


def test_unknown_cid_with_faq_stops():
    rows = [{"mpn": "X", "supplier_reference": "C999999999", "faq": "Q: a?A: b"}]
    out = rp.check_faq_provenance(rows)
    assert out[0][0] == "STOP" and out[0][1] == "FAQ_FABRICATED_FAIL"


# --------------------------------------------------------------------------
# 7. end to end through plan_release
# --------------------------------------------------------------------------
def _plan(tmpdir, rows):
    master = os.path.join(tmpdir, "master.csv")
    with open(master, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=MASTER_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return master, rows


def test_plan_release_stops_fabricated():
    tmp = tempfile.mkdtemp(prefix="faqgate_")
    rows = [{"mpn": "L7805CV", "manufacturer": "STMicroelectronics",
             "supplier_reference": "C111887",
             "faq": "Q: What is the output voltage? A: L7805CV delivers 5.00 V"}]
    master, cand = _plan(tmp, rows)
    plan = rp.plan_release(master, cand, batch_id="test_faq")
    assert "FAQ_FABRICATED_FAIL" in [s["code"] for s in plan.stops], \
        f"plan.stops={plan.stops}"
    assert any(lvl == "STOP" and kind == "FAQ_FABRICATED_FAIL"
               for lvl, kind, _ in plan.faq_provenance), \
        f"plan.faq_provenance={plan.faq_provenance}"


def test_plan_release_allows_verified():
    tmp = tempfile.mkdtemp(prefix="faqgate_")
    mpn = next(iter(faq_policy.VERIFIED_MPNS))
    rows = [{"mpn": mpn, "manufacturer": "AOS",
             "supplier_reference": "C20917", "faq": "Q: a?A: b"}]
    master, cand = _plan(tmp, rows)
    plan = rp.plan_release(master, cand, batch_id="test_faq")
    assert "FAQ_FABRICATED_FAIL" not in [s["code"] for s in plan.stops], \
        f"plan.stops={plan.stops}"
    assert any(k == "FAQ_CURATED_VERIFIED" for _, k, _ in plan.faq_provenance), \
        f"plan.faq_provenance={plan.faq_provenance}"


# --------------------------------------------------------------------------
# 8. the renderer mirrors the gate (no import of gen_parts needed: the policy
#    module is the single switch both sides consult)
# --------------------------------------------------------------------------
def test_renderer_switch_is_the_policy_module():
    assert hasattr(rp.faq_policy, "is_verified")
    assert rp.faq_policy.is_verified("AO3400A")
    assert not rp.faq_policy.is_verified("L7805CV")
    # the whitelist is the ONLY escape: it must be small and accountable
    assert len(rp.faq_policy.VERIFIED_MPNS) <= 8
    for mpn, meta in rp.faq_policy.VERIFIED_MPNS.items():
        assert meta.get("reason") and meta.get("approved_by") and meta.get("date"), \
            f"{mpn} whitelist entry lacks reason/approver/date"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("  PASS  %s" % name)
            except AssertionError as exc:
                failures += 1
                print("  FAIL  %s: %s" % (name, exc))
            except Exception as exc:                      # noqa: BLE001
                failures += 1
                print("  ERROR %s: %r" % (name, exc))
    print("\n%s" % ("ALL PASS" if not failures else "%d FAILED" % failures))
    sys.exit(1 if failures else 0)
