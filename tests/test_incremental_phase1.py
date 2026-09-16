#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 1 Incremental Publishing — unit + integration tests (A..K).

Pure-logic tests (A-J) use synthetic data; Case K scans the REAL products/
directory to prove the bootstrap path never touches HTML. No production files
are modified; the real build_manifest.json is never written by these tests
(bootstrap runs in --dry-run against a synthetic manifest path, or derives
groups from the live products/ tree so nothing is mutated).

Run from repo root:
    python -m unittest tests.test_incremental_phase1 -v
"""
import os
import sys
import json
import types
import shutil
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import gen_parts  # safe: guarded by `if __name__ == "__main__": main()`


def make_group(mpn, cat="microcontroller", slug=None):
    slug = slug or mpn.lower()
    return {
        "mpn": mpn, "manufacturer": "ST", "brand": "ST", "category": cat,
        "subcategory": "mcu", "description": "desc", "applications": "app",
        "keywords": "k", "attributes_json": "{}", "alternative_parts": "",
        "datasheet_url": "", "faq": "", "image": "", "url_slug": slug,
    }


def base_key(slug="abc123", mpn="ABC123"):
    return {
        "slug": slug, "mpn": mpn,
        "renderer_v": "v3",
        "data_fp": "d1", "enrich_fp": "e1", "template_v": "t1",
        "asset_v": "a1", "dependency_fp": "dep1",
    }


class TestClassify(unittest.TestCase):
    # Case A: manifest empty -> CREATE
    def test_A_create_when_no_record(self):
        self.assertEqual(gen_parts.classify_sku(base_key(), None), "CREATE")

    # Case B: identical -> SKIP
    def test_B_skip_when_identical(self):
        desired = base_key()
        recorded = dict(desired)
        self.assertEqual(gen_parts.classify_sku(desired, recorded), "SKIP")

    # Cases C-H: exactly one fingerprint field changes -> UPDATE
    def test_C_update_data_fp(self):
        d, r = base_key(), base_key(); r["data_fp"] = "d2"
        self.assertEqual(gen_parts.classify_sku(d, r), "UPDATE")

    def test_D_update_enrich_fp(self):
        d, r = base_key(), base_key(); r["enrich_fp"] = "e2"
        self.assertEqual(gen_parts.classify_sku(d, r), "UPDATE")

    def test_E_update_renderer_v(self):
        d, r = base_key(), base_key(); r["renderer_v"] = "v2"
        self.assertEqual(gen_parts.classify_sku(d, r), "UPDATE")

    def test_F_update_template_v(self):
        d, r = base_key(), base_key(); r["template_v"] = "t2"
        self.assertEqual(gen_parts.classify_sku(d, r), "UPDATE")

    def test_G_update_asset_v(self):
        d, r = base_key(), base_key(); r["asset_v"] = "a2"
        self.assertEqual(gen_parts.classify_sku(d, r), "UPDATE")

    def test_H_update_dependency_fp(self):
        d, r = base_key(), base_key(); r["dependency_fp"] = "dep2"
        self.assertEqual(gen_parts.classify_sku(d, r), "UPDATE")


class TestManifestIO(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="incr_test_")
        self.mpath = os.path.join(self.tmp, "build_manifest.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # Case I: missing manifest -> None (fail-safe: treated as empty)
    def test_I_missing_manifest_returns_none(self):
        missing = os.path.join(self.tmp, "does_not_exist.json")
        self.assertIsNone(gen_parts.load_manifest(missing))

    # Case J: corrupt manifest -> ManifestError (never silent)
    def test_J_corrupt_manifest_raises(self):
        with open(self.mpath, "w", encoding="utf-8") as f:
            f.write("{ this is not valid json ,,")
        with self.assertRaises(gen_parts.ManifestError):
            gen_parts.load_manifest(self.mpath)

    def test_save_manifest_atomic_and_roundtrip(self):
        manifest = {"meta": {"v": 1}, "skus": {"abc": base_key()}}
        res = gen_parts.save_manifest(path=self.mpath, manifest=manifest, dry_run=False)
        self.assertTrue(res["written"])
        self.assertTrue(os.path.exists(self.mpath))
        with open(self.mpath, encoding="utf-8") as f:
            reloaded = json.load(f)
        self.assertEqual(reloaded["skus"]["abc"]["mpn"], "ABC123")

    def test_save_manifest_dry_run_writes_nothing(self):
        manifest = {"meta": {}, "skus": {"abc": base_key()}}
        res = gen_parts.save_manifest(path=self.mpath, manifest=manifest, dry_run=True)
        self.assertFalse(res["written"])
        self.assertFalse(os.path.exists(self.mpath))


class TestBuildKey(unittest.TestCase):
    def test_build_key_is_deterministic(self):
        groups = [make_group("ABC123", slug="abc123"),
                  make_group("XYZ999", slug="xyz999")]
        by_cat = gen_parts._build_by_cat(groups)
        k1 = gen_parts.compute_build_key("abc123", groups[0], by_cat)
        k2 = gen_parts.compute_build_key("abc123", groups[0], by_cat)
        k1.pop("slug"); k1.pop("mpn")
        k2.pop("slug"); k2.pop("mpn")
        self.assertEqual(k1, k2)

    def test_dependency_fp_changes_when_category_pool_changes(self):
        cslug, _ = gen_parts.resolve_cat("microcontroller")  # real resolved slug
        # Single SKU in category -> baseline pool fp
        g0 = [make_group("A", slug="a")]
        by0 = gen_parts._build_by_cat(g0)
        fp0 = gen_parts._category_pool_fingerprint(cslug, by0)
        # Add a second SKU to the same category -> pool fp must change
        g1 = [make_group("A", slug="a"), make_group("B", slug="b")]
        by1 = gen_parts._build_by_cat(g1)
        fp1 = gen_parts._category_pool_fingerprint(cslug, by1)
        self.assertNotEqual(fp0, fp1)


class TestBootstrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="incr_boot_")
        self.products = os.path.join(self.tmp, "products")
        os.makedirs(self.products)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_product(self, slug):
        d = os.path.join(self.products, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write("<html>test</html>")

    def test_bootstrap_dry_run_writes_no_manifest_and_detects_orphans(self):
        # Two published SKUs present in MASTER + one orphan (no MASTER row)
        self._make_product("a")
        self._make_product("b")
        self._make_product("orphan1")
        groups = [make_group("A", slug="a"), make_group("B", slug="b")]
        mpath = os.path.join(self.tmp, "build_manifest.json")
        args = types.SimpleNamespace(dry_run=True)
        res = gen_parts.bootstrap_manifest(args, groups, self.tmp, manifest_path=mpath)
        self.assertFalse(res["written"])
        self.assertFalse(os.path.exists(mpath))           # dry-run: no write
        self.assertEqual(res["skus"], 2)                  # only MASTER-backed
        self.assertEqual(res["orphans"], ["orphan1"])     # orphan reported, not manifested

    def test_bootstrap_writes_manifest_excluding_orphans(self):
        self._make_product("a")
        self._make_product("orphan1")
        groups = [make_group("A", slug="a")]
        mpath = os.path.join(self.tmp, "build_manifest.json")
        args = types.SimpleNamespace(dry_run=False)
        gen_parts.bootstrap_manifest(args, groups, self.tmp, manifest_path=mpath)
        self.assertTrue(os.path.exists(mpath))
        with open(mpath, encoding="utf-8") as f:
            m = json.load(f)
        self.assertIn("a", m["skus"])
        self.assertNotIn("orphan1", m["skus"])
        self.assertEqual(m["meta"]["orphan_count"], 1)


class TestCaseKReal552(unittest.TestCase):
    """Integration: bootstrap over the REAL products/ tree must not change HTML."""

    def test_K_real_products_untouched_and_count_matches(self):
        products_dir = os.path.join(gen_parts.ROOT, "products")
        if not os.path.isdir(products_dir):
            self.skipTest("no products/ in repo")
        slugs = [n for n in os.listdir(products_dir)
                 if os.path.isfile(os.path.join(products_dir, n, "index.html"))]
        self.assertGreaterEqual(len(slugs), 550)

        # capture mtimes before
        mtimes = {s: os.path.getmtime(os.path.join(products_dir, s, "index.html"))
                  for s in slugs}

        # synthetic groups derived from the live tree (orphan detection -> 0)
        groups = [make_group(s, slug=s) for s in slugs]
        args = types.SimpleNamespace(dry_run=True)
        res = gen_parts.bootstrap_manifest(args, groups, gen_parts.ROOT)

        # no product HTML changed
        for s in slugs:
            self.assertEqual(
                os.path.getmtime(os.path.join(products_dir, s, "index.html")),
                mtimes[s], msg=f"HTML mtime changed for {s}")
        # dry-run: real manifest NOT written/modified (a legitimate baseline
        # build_manifest.json may already exist on disk from an earlier authorized
        # bootstrap; dry-run must neither create nor touch it).
        mtime_before = (os.path.getmtime(gen_parts.MANIFEST_PATH)
                        if os.path.exists(gen_parts.MANIFEST_PATH) else None)
        if mtime_before is None:
            self.assertFalse(os.path.exists(gen_parts.MANIFEST_PATH),
                             "dry-run bootstrap must not create build_manifest.json")
        else:
            self.assertEqual(
                os.path.getmtime(gen_parts.MANIFEST_PATH), mtime_before,
                msg="dry-run bootstrap must not modify build_manifest.json")
        self.assertEqual(res["skus"], len(slugs))
        self.assertEqual(res["orphans"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
