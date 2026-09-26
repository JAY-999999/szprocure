"""Regression test: single classification tree (native_l1 is the ONLY tree).

After the 2026-09-27 unification, the published ``category`` column MUST be the
native_l1 slug. The legacy 11-family detection (category.py::detect_category)
is retained ONLY as an internal spec-formatting hint and is never written to the
published classification. This test proves:

  1. For every candidate row, row["category"] == native_l1 slug
     (or "Uncategorized" when native_l1 is empty). 卡B never appears.
  2. The release gate UNMAPPED_CATEGORY only STOPs rows whose native_l1 is empty
     (the true single-tree gate), NOT rows that merely lacked an 11-family hint.
  3. clone-part-key and datasheet-identity gates stay clean (regression guard).
"""
import os
import sys
import csv
import subprocess

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))  # site/
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import native_l1_mapper as nl1
from tools.factory import product_data as pd
from tools.factory import release_pipeline as rp

BATCH_CSV = r"D:\SZ Procure\batch_500_onboard.csv"
SOURCE = r"D:\SZ Procure\采集流水线\基础数据"
MASTER = r"D:\SZ Procure\site\data\production\master_parts_v2.1.csv"
TEST_ROOT = "data/raw/_clean_test_single_tree"  # isolated, not the live pool
UNKNOWN = "Uncategorized"


def _sample_mpns(limit=40):
    rows = list(csv.DictReader(open(BATCH_CSV, encoding="utf-8-sig")))
    by_slug = {}
    for r in rows:
        c = r["c_number"].strip()
        slug = nl1.compute_native_l1_for_row(c)
        by_slug.setdefault(slug or "", []).append(r["mpn"].strip().upper())
    chosen = []
    # up to 3 per slug for coverage, cap total
    for slug, mpns in by_slug.items():
        chosen.extend(mpns[:3])
        if len(chosen) >= limit:
            break
    return sorted(set(chosen))[:limit]


def _run_pipeline(mpns):
    res = pd.intake_http_json(batch_id="single_tree_test",
                              source_path=SOURCE,
                              selector=set(mpns), root=TEST_ROOT)
    norm = pd.normalize(batch_id="single_tree_test", master_csv=MASTER,
                        root=TEST_ROOT, skip_mass_duplicate_check=True)
    rows, _ = rp.collect_candidates("single_tree_test", TEST_ROOT)
    plan = rp.plan_release(MASTER, rows, subset_mpns=mpns,
                           batch_id="single_tree_test")
    return res, norm, rows, plan


def test_category_column_is_native_l1():
    mpns = _sample_mpns(40)
    res, norm, rows, plan = _run_pipeline(mpns)
    assert res.written > 0, "intake wrote nothing"
    mismatches = []
    for r in rows:
        c = (r.get("supplier_reference") or "").strip()
        expected = nl1.compute_native_l1_for_row(c) or UNKNOWN
        if r.get("category") != expected:
            mismatches.append((c, r.get("category"), expected))
    assert not mismatches, (
        "category column != native_l1 (single-tree violation): %r" % mismatches[:10])


def test_uncategorized_only_when_native_l1_empty():
    mpns = _sample_mpns(40)
    res, norm, rows, plan = _run_pipeline(mpns)
    for r in rows:
        if (r.get("native_l1") or "").strip():
            assert r.get("category") != UNKNOWN, (
                "row %s has native_l1 but category is Uncategorized"
                % r.get("supplier_reference"))


def test_unmapped_gate_driven_by_native_l1():
    mpns = _sample_mpns(40)
    res, norm, rows, plan = _run_pipeline(mpns)
    empty_nl1 = sum(1 for r in rows if not (r.get("native_l1") or "").strip())
    stops = [s for s in plan.stops if s["code"] == "UNMAPPED_CATEGORY"]
    assert len(stops) == empty_nl1, (
        "UNMAPPED_CATEGORY stops (%d) != empty-native_l1 rows (%d)"
        % (len(stops), empty_nl1))


def test_clone_and_identity_gates_clean():
    mpns = _sample_mpns(40)
    res, norm, rows, plan = _run_pipeline(mpns)
    assert len(plan.clone_part_key) == 0, plan.clone_part_key[:5]
    assert len(plan.datasheet_identity) == 0, plan.datasheet_identity[:5]


if __name__ == "__main__":
    import traceback
    failed = 0
    for fn in (test_category_column_is_native_l1,
               test_uncategorized_only_when_native_l1_empty,
               test_unmapped_gate_driven_by_native_l1,
               test_clone_and_identity_gates_clean):
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", fn.__name__)
            print("   ", str(e)[:400])
        except Exception:
            failed += 1
            print("ERROR", fn.__name__)
            traceback.print_exc()
    print("FAILED=%d" % failed)
    sys.exit(1 if failed else 0)
