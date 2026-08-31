"""
#6 Sitemap sharding regression test (Phase 1 hardening).

Drives gen_parts.py's REAL sitemap writer via a full (sandboxed) build using a
synthetic-but-valid Master that passes the production-source guards, then asserts:

  (a) total URL count <= SITEMAP_BATCH  -> a SINGLE sitemap_parts.xml (no numeric
      suffix) and sitemap_parts_index.xml points to exactly that one shard;
  (b) total URL count  > SITEMAP_BATCH  -> auto-sharded sitemap_parts_{1..k}.xml,
      every shard's <url> count <= SITEMAP_BATCH, and the index lists ALL shards;
  (c) the SITEMAP_BATCH constant is still 45000 (no accidental change).

gen_parts.py is NOT modified by this test (frozen layer). The synthetic master is
written under data/production/ (gitignored) with a guard-friendly name and removed
in a finally block.
"""
import csv
import os
import re
import subprocess
import sys
import tempfile

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
GEN = os.path.join(REPO, "gen_parts.py")
WRAPPER = os.path.join(REPO, "tests", "_shard_build_helper.py")
MASTER_COLS = ["mpn", "clean_mpn", "manufacturer", "brand", "url_slug", "category",
               "subcategory", "description", "applications", "keywords",
               "attributes_json", "availability", "alternative_parts", "datasheet_url",
               "faq", "image", "source", "source_url", "supplier_reference"]

# Import gen_parts only to read the constant (no side effects; main() is guarded).
import importlib.util
_spec = importlib.util.spec_from_file_location("gen_parts_const", GEN)
_genmod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_genmod)
SITEMAP_BATCH = _genmod.SITEMAP_BATCH


def _write_synth_master(n_products, path):
    rows = []
    for i in range(1, n_products + 1):
        mpn = f"WIDGET-{i:05d}"                # avoids SYNTHETIC_MPN_PATTERNS
        # Production MASTER url_slug is already slugified; replicate that so the
        # #1 regression guard (base == built slug) holds, exactly as on the 550 site.
        url_slug = _genmod.slugify(mpn)
        rows.append({
            "mpn": mpn,
            "clean_mpn": "",
            "manufacturer": "Widgets Inc",     # not in FAKE_BRAND_TOKENS
            "brand": "Widgets Inc",
            "url_slug": url_slug,               # unique -> no slug collision
            "category": "Integrated Circuits",
            "subcategory": "",
            "description": "", "applications": "", "keywords": "",
            "attributes_json": "", "availability": "", "alternative_parts": "",
            "datasheet_url": "", "faq": "", "image": "",
            "source": "probe", "source_url": "", "supplier_reference": "",
        })
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_COLS)
        w.writeheader()
        w.writerows(rows)


def _run_build(out_root, csv_path):
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "0"
    subprocess.run(
        [PY, WRAPPER, "--csv", csv_path, "--out", out_root],
        check=True, capture_output=True, text=True, env=env)


def _count_url_entries(path):
    return len(re.findall(r"<loc>", open(path, encoding="utf-8").read()))


def _index_loc_targets(path):
    txt = open(path, encoding="utf-8").read()
    return re.findall(r"<loc>(.*?sitemap_parts[^<]*\.xml)</loc>", txt)


def test_sitemap_batch_constant_unchanged():
    # (c) the contract must stay at 45000
    assert SITEMAP_BATCH == 45000


def test_single_sitemap_when_under_batch():
    # (a) small catalog -> one sitemap_parts.xml, index points to exactly it.
    out = tempfile.mkdtemp(prefix="sz_shard_single_")
    fd, csv_path = tempfile.mkstemp(prefix="sz_shard_probe_", suffix=".csv",
                                    dir=tempfile.gettempdir())
    os.close(fd)
    try:
        _write_synth_master(200, csv_path)
        _run_build(out, csv_path)
        assert os.path.exists(os.path.join(out, "sitemap_parts.xml")), \
            "expected single sitemap_parts.xml when under batch size"
        assert not os.path.exists(os.path.join(out, "sitemap_parts_1.xml")), \
            "no numeric-suffix shard should exist under batch size"
        idx = os.path.join(out, "sitemap_parts_index.xml")
        targets = _index_loc_targets(idx)
        assert targets == [f"{_genmod.DOMAIN}/sitemap_parts.xml"], \
            f"index must reference the single shard only, got {targets}"
        n = _count_url_entries(os.path.join(out, "sitemap_parts.xml"))
        assert n <= SITEMAP_BATCH, f"sitemap url count {n} exceeds batch {SITEMAP_BATCH}"
        # sanity: product urls were emitted
        assert n > 200, f"expected >200 urls (products+structural), got {n}"
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass


def test_auto_shard_when_over_batch():
    # (b) catalog crossing the threshold auto-shards; every shard <= batch;
    #     index references ALL shards.
    out = tempfile.mkdtemp(prefix="sz_shard_big_")
    fd, csv_path = tempfile.mkstemp(prefix="sz_shard_probe_", suffix=".csv",
                                    dir=tempfile.gettempdir())
    os.close(fd)
    try:
        _write_synth_master(45000, csv_path)   # 45000 products + ~5 structural -> > 45000 total
        _run_build(out, csv_path)
        shards = sorted(
            fn for fn in os.listdir(out)
            if re.fullmatch(r"sitemap_parts_\d+\.xml", fn)
        )
        assert len(shards) >= 2, f"expected >=2 shards when over batch, got {shards}"
        assert not os.path.exists(os.path.join(out, "sitemap_parts.xml")), \
            "single sitemap_parts.xml must NOT exist once sharded"
        total = 0
        for fn in shards:
            cnt = _count_url_entries(os.path.join(out, fn))
            assert cnt <= SITEMAP_BATCH, f"shard {fn} has {cnt} urls (> batch)"
            total += cnt
        idx = os.path.join(out, "sitemap_parts_index.xml")
        targets = _index_loc_targets(idx)
        expected = [f"{_genmod.DOMAIN}/{fn}" for fn in shards]
        assert sorted(targets) == sorted(expected), \
            f"index must list all shards: got {targets}, expected {expected}"
        # structural urls (manufacturers/components/hubs) are few; products dominate
        assert total > SITEMAP_BATCH, \
            f"total sharded urls {total} should exceed batch {SITEMAP_BATCH}"
    finally:
        try:
            os.remove(csv_path)
        except OSError:
            pass
