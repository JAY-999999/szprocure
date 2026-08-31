"""
Integration tests for the Phase 3 SEO post-processors (YELLOW-1/2/3).

Exercises apply_index_policy + sitemap_prune end-to-end against a small
synthetic build rooted in a temp dir, asserting the required policy matrix
(§11 of the implementation directive):

  test_spec_thin_stays_index
  test_empty_attr_with_mpn_stays_index
  test_unknown_category_noindex
  test_no_mpn_noindex
  test_duplicate_noindex_with_canonical
  test_never_nofollow
  test_noindex_excluded_from_sitemap
"""
from __future__ import annotations

import json
import os
import re
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(REPO, "tools"), os.path.join(REPO, "tools", "factory")):
    if _p not in __import__("sys").path:
        __import__("sys").path.insert(0, _p)

import apply_index_policy as ap  # noqa: E402
import sitemap_prune as sp       # noqa: E402
import index_policy as ip        # noqa: E402

DOMAIN = "https://www.szprocure.com"
BRAND_ID = '"@id": "#szprocure-org"'


def _page(slug: str, pn: str, mfr: str, cat: str, desc: str) -> str:
    """Byte-faithful minimal product page (only the 3 editable zones matter)."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>{pn} {mfr}</title>
  <meta name="robots" content="index, follow" />
  <link rel="canonical" href="{DOMAIN}/products/{slug}/" />
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "{pn}",
    "model": "{pn}",
    "category": "{cat}",
    "brand": {{ "@type": "Brand", "name": "{mfr}" }},
    "description": "{desc}",
    "url": "{DOMAIN}/products/{slug}/"
  }}
  </script>
</head>
<body></body>
</html>
"""


def _records():
    """Synthetic catalog exercising every classification branch."""
    return [
        # R1 normal index (also brand-native primary of a dup group)
        {"url_slug": "abc100", "mpn": "ABC-100", "clean_mpn": "ABC-100",
         "category": "Microcontroller", "brand": "ACME", "manufacturer": "ACME",
         "attributes_json": None},
        # R2 duplicate (normalized MPN == R1) -> non-primary -> noindex+canonical
        {"url_slug": "abc100-alt", "mpn": "abc 100", "clean_mpn": "abc 100",
         "category": "Microcontroller", "brand": "OtherCorp",
         "manufacturer": "OtherCorp", "attributes_json": '{"freq": "10MHz"}'},
        # R3 unknown category -> noindex,follow (canonical stays self)
        {"url_slug": "unknowncat", "mpn": "XYZ-1", "clean_mpn": "XYZ-1",
         "category": "", "brand": "XCorp", "manufacturer": "XCorp",
         "attributes_json": '{"a": 1}'},
        # R4 no real MPN -> noindex,follow
        {"url_slug": "nompn", "mpn": "", "clean_mpn": "",
         "category": "Resistor", "brand": "RCorp", "manufacturer": "RCorp",
         "attributes_json": '{"r": "1k"}'},
        # R5 spec-thin but real MPN -> index,follow (policy §三)
        {"url_slug": "specThin", "mpn": "ST-9", "clean_mpn": "ST-9",
         "category": "Capacitor", "brand": "SCorp", "manufacturer": "SCorp",
         "attributes_json": "{}"},
    ]


def _build_site(root: str, include_sitemap: bool = False):
    recs = _records()
    os.makedirs(os.path.join(root, "products"), exist_ok=True)
    for r in recs:
        d = os.path.join(root, "products", r["url_slug"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as fh:
            fh.write(_page(r["url_slug"], r["mpn"], r["brand"],
                           r["category"], "desc " + r["url_slug"]))
    with open(os.path.join(root, "parts.json"), "w", encoding="utf-8") as fh:
        json.dump(recs, fh)

    if include_sitemap:
        urls = [f"{DOMAIN}/products/{r['url_slug']}/" for r in recs]
        urls += [f"{DOMAIN}/manufacturers/acme/",
                 f"{DOMAIN}/manufacturers/othercorp/",
                 f"{DOMAIN}/components/microcontroller/",
                 f"{DOMAIN}/components/passive-components/"]
        lines = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
        for u in urls:
            lines.append(f'  <url><loc>{u}</loc>'
                         f'<changefreq>weekly</changefreq>'
                         f'<priority>0.5</priority></url>')
        lines.append("</urlset>")
        with open(os.path.join(root, "sitemap_parts.xml"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return recs


def _read(root, slug):
    with open(os.path.join(root, "products", slug, "index.html"), encoding="utf-8") as fh:
        return fh.read()


# --- required §11 tests ------------------------------------------------------

def test_spec_thin_stays_index():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root)
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        h = _read(root, "specThin")
        assert 'content="index, follow"' in h, "SPEC_THIN must stay index,follow"
        assert '"mpn": "ST-9"' in h, "mpn must be injected"
        assert BRAND_ID in h, "Brand @id must be injected"


def test_empty_attr_with_mpn_stays_index():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root)
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        h = _read(root, "abc100")
        assert 'content="index, follow"' in h
        assert '"mpn": "ABC-100"' in h
        assert BRAND_ID in h
        # canonical must remain self (not a duplicate primary target)
        assert f'<link rel="canonical" href="{DOMAIN}/products/abc100/" />' in h


def test_unknown_category_noindex():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root)
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        h = _read(root, "unknowncat")
        assert 'content="noindex, follow"' in h, "UNKNOWN_CATEGORY -> noindex,follow"
        assert "nofollow" not in h
        # canonical stays self (no primary for non-duplicate noindex)
        assert f'<link rel="canonical" href="{DOMAIN}/products/unknowncat/" />' in h
        assert '"mpn": "XYZ-1"' in h, "real MPN still injected"


def test_no_mpn_noindex():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root)
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        h = _read(root, "nompn")
        assert 'content="noindex, follow"' in h, "NO_MPN -> noindex,follow"
        assert '"mpn":' not in h, "no fake mpn when MPN is empty"
        assert f'<link rel="canonical" href="{DOMAIN}/products/nompn/" />' in h


def test_duplicate_noindex_with_canonical():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root)
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        h = _read(root, "abc100-alt")
        assert 'content="noindex, follow"' in h, "non-primary dup -> noindex,follow"
        # canonical must point to the primary slug
        assert f'<link rel="canonical" href="{DOMAIN}/products/abc100/" />' in h
        assert '"mpn": "abc 100"' in h


def test_never_nofollow():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root)
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        for r in _records():
            h = _read(root, r["url_slug"])
            assert "nofollow" not in h, f"nofollow leaked into {r['url_slug']}"


def test_noindex_excluded_from_sitemap():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root, include_sitemap=True)
        sp.rebuild_sitemap(root, os.path.join(root, "parts.json"))
        sm = open(os.path.join(root, "sitemap_parts.xml"), encoding="utf-8").read()
        locs = set(re.findall(r"<loc>(.*?)</loc>", sm))

        # the 3 noindex products must NOT be in the sitemap
        for slug in ("abc100-alt", "unknowncat", "nompn"):
            assert f"{DOMAIN}/products/{slug}/" not in locs, \
                f"noindex {slug} must be excluded from sitemap"

        # homepage + 6 static must be present
        assert f"{DOMAIN}/" in locs
        for p in sp.STATIC_PATHS:
            assert f"{DOMAIN}{p}" in locs

        # expected total: 9 existing - 3 dropped + 7 added = 13
        assert len(locs) == 13, f"expected 13 urls, got {len(locs)}"


def test_pipeline_runs_end_to_end():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root, include_sitemap=True)
        import build_seo_pipeline as bp  # noqa: F401  (import check)
        summary = bp.run(root, os.path.join(root, "parts.json"))
        assert summary["html"]["mpn_added"] == 4  # R1,R2,R3,R5 (R4 has no mpn)
        assert summary["html"]["brand_id_added"] == 5
        assert summary["sitemap"]["total"] == 13


def test_idempotent_rerun():
    with tempfile.TemporaryDirectory() as root:
        _build_site(root)
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        first = {s: _read(root, s) for s in ("abc100", "abc100-alt")}
        ap.apply_to_site(root, os.path.join(root, "parts.json"))
        second = {s: _read(root, s) for s in ("abc100", "abc100-alt")}
        assert first == second, "re-running the post-processor must be a no-op"
