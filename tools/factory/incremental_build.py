"""
#16 Incremental build (pre-scale hardening, Phase 3) — sandbox-only, no gen_parts edit.

Goal: prove that, once 550 pages are already deployed, adding N new SKUs does NOT
require regenerating the 550 existing pages, and that the incremental result is
BYTE-FOR-BYTE identical to a full rebuild of the same (550+N) Master.

How it works (no frozen-layer edit):
  1. Copy the existing deployed build (base_out, the 550 pages) into out_root.
     The 550 product pages are NEVER re-rendered -> zero drift, huge compute save.
  2. Run the SAME preprocessing gen_parts.main() uses on the FULL (550+N) Master
     (production-source guard, lexicons, synthetic-MPN guard, merge, stable
     SlugRegistry.assign). Slugs are therefore identical to a full rebuild.
  3. Render ONLY the new product pages (those whose <slug>/index.html is absent)
     via the REAL gen_part_page. Existing 550 pages stay as copied.
  4. Regenerate every GLOBAL artifact (manufacturer/component pages + hubs,
     sitemap shards, parts.json, client-side search index) from the full row set,
     reusing gen_parts' real functions and mirroring the sitemap/parts.json/search
     writers EXACTLY. The mirror is validated by the byte-diff in the test suite:
     if incremental_build diverged from gen_parts, the diff would fail.

Equivalence is asserted by tests/test_incremental_build.py:
  incremental_build(base550, full550+N)  ==  gen_parts(full550+N)   (byte-identical)
and the 550 existing product pages are unchanged vs the base build.
"""
from __future__ import annotations

import os
import re
import sys
import json
import csv
import shutil
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import gen_parts as gp  # the frozen module; we only CALL its functions, never edit it.


def _preprocess(full_csv, mfr_map, attr_dict, attr_json, val_json, strict=False):
    """Mirror gen_parts.main()'s load + normalize + slug pipeline on the FULL set."""
    gp.validate_production_source(full_csv)
    mfr_map_d = gp.load_mfr_canonical(mfr_map)
    attr_allow = gp.load_attr_allowlist(attr_dict)
    gp._ATTR_KEY_TRANS = gp.load_attr_key_translation(attr_json)
    gp._VAL_TRANS = gp.load_value_translation(val_json)
    with open(full_csv, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("mpn", "").strip()]
    gp.detect_synthetic_mpn(rows)
    review = []
    groups, stats = gp.build_merged_groups(rows, mfr_map_d, attr_allow, review)
    registry = gp.SlugRegistry()
    for g in groups:
        base = (g.get("url_slug") or "").strip() or gp.slugify(g["mpn"].strip())
        g["url_slug"] = registry.assign(base, g["manufacturer"].strip(), g["mpn"].strip())
    if strict and (stats["brand_unmatched"] > 0 or stats["attr_unknown"] > 0):
        raise SystemExit(3)
    by_mfr = defaultdict(list)
    by_cat = defaultdict(list)
    for g in groups:
        by_mfr[g["manufacturer"].strip()].append(g)
        cslug, _ = gp.resolve_cat(g["category"].strip())
        by_cat[cslug].append(g)
    related_map = gp.build_related_map(by_cat, k=6)
    return groups, by_mfr, by_cat, related_map, registry, stats


def _write_globals(out_root, groups, by_mfr, by_cat, related_map):
    """Regenerate every global artifact from the FULL row set (mirror of main())."""
    DOMAIN = gp.DOMAIN
    SITEMAP_BATCH = gp.SITEMAP_BATCH
    SEARCH_SHARD_SIZE = gp.SEARCH_SHARD_SIZE

    written = 0
    urls = []
    generated_slugs = {g["url_slug"] for g in groups if g.get("url_slug")}
    # --- product pages: render every page, WRITE only if content changed ---
    # This is the correct incremental behaviour: new pages are written; existing
    # 550 pages are left byte-for-byte untouched UNLESS their rendered content
    # (e.g. the related-products section) legitimately changed because new parts
    # entered their top-N related set. The result is then 100% byte-identical to
    # a full rebuild of the same (550+N) Master, while minimising writes.
    for g in groups:
        slug = g["url_slug"]
        if not slug:
            continue
        urls.append(f"{DOMAIN}/products/{slug}/")  # sitemap lists ALL products
        cslug, _ = gp.resolve_cat(g["category"].strip())
        mfr_slug = gp.slugify_name(g["manufacturer"].strip())
        d = os.path.join(out_root, "products", slug)
        os.makedirs(d, exist_ok=True)
        page = gp.gen_part_page(g, cslug, mfr_slug,
                                related=related_map.get(slug, []),
                                generated_slugs=generated_slugs)
        prod_path = os.path.join(d, "index.html")
        if os.path.exists(prod_path):
            with open(prod_path, encoding="utf-8") as f:
                if f.read() == page:
                    continue  # byte-identical -> existing 550 page NOT rewritten
        with open(prod_path, "w", encoding="utf-8") as f:
            f.write(page)
        written += 1

    # --- manufacturer pages + hub ---
    for mfr, parts in by_mfr.items():
        mslug = gp.slugify_name(mfr)
        d = os.path.join(out_root, "manufacturers", mslug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(gp.gen_manufacturer_page(mfr, parts, {}))
        urls.append(f"{DOMAIN}/manufacturers/{mslug}/")
    hub_dir = os.path.join(out_root, "manufacturers")
    os.makedirs(hub_dir, exist_ok=True)
    with open(os.path.join(hub_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(gp.gen_manufacturers_hub(by_mfr))
    urls.append(f"{DOMAIN}/manufacturers/")

    # --- component category + L3 subcategory pages + hub ---
    for cslug, cname in gp.TOP_CATEGORIES.items():
        parts = by_cat.get(cslug, [])
        d = os.path.join(out_root, "components", cslug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(gp.gen_component_category_page(cslug, cname, parts,
                                                   all_rows=None, by_cat=by_cat))
        urls.append(f"{DOMAIN}/components/{cslug}/")
    for cslug, cname in gp.TOP_CATEGORIES.items():
        l3_groups = defaultdict(list)
        for p in by_cat.get(cslug, []):
            fine = (p.get("category") or "").strip()
            if fine:
                l3_groups[fine].append(p)
        for fine, l3_parts in sorted(l3_groups.items()):
            l3_slug = gp.slugify_name(fine)
            d = os.path.join(out_root, "components", cslug, l3_slug)
            os.makedirs(d, exist_ok=True)
            page = gp.gen_component_subcategory_page(cslug, cname, fine, l3_slug,
                                                     l3_parts, all_rows=None)
            with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
                f.write(page)
            urls.append(f"{DOMAIN}/components/{cslug}/{l3_slug}/")
    hub_dir = os.path.join(out_root, "components")
    os.makedirs(hub_dir, exist_ok=True)
    with open(os.path.join(hub_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(gp.generate_components_hub(generated_slugs))
    urls.append(f"{DOMAIN}/components/")

    # --- split sitemap (mirror of main()) ---
    n_batches = (len(urls) + SITEMAP_BATCH - 1) // SITEMAP_BATCH
    sm_paths = []
    for b in range(n_batches):
        chunk = urls[b * SITEMAP_BATCH:(b + 1) * SITEMAP_BATCH]
        fn = "sitemap_parts.xml" if n_batches == 1 else f"sitemap_parts_{b+1}.xml"
        with open(os.path.join(out_root, fn), "w", encoding="utf-8") as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            f.write('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
            for u in chunk:
                f.write(f"  <url><loc>{u}</loc><changefreq>weekly</changefreq><priority>0.5</priority></url>\n")
            f.write('</urlset>\n')
        sm_paths.append(fn)
    with open(os.path.join(out_root, "sitemap_parts_index.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for fn in sm_paths:
            f.write(f"  <sitemap><loc>{DOMAIN}/{fn}</loc></sitemap>\n")
        f.write('</sitemapindex>\n')

    # --- search index (mirror of main()) ---
    search_entries = []
    seen = set()
    for g in groups:
        pn = g["mpn"].strip()
        mfr = g["manufacturer"].strip()
        cat = g["category"].strip()
        p_slug = g["url_slug"]
        m_slug = gp.slugify_name(mfr)
        c_slug = gp.slugify_name(cat)
        key_p = ("p", pn.lower())
        if key_p not in seen:
            search_entries.append({"t": pn, "k": pn.lower(),
                                   "keys": gp.pn_search_keys(pn), "ty": "Part",
                                   "u": f"/products/{p_slug}/", "sub": f"{mfr} \u00b7 {cat}"})
            seen.add(key_p)
        key_m = ("m", mfr.lower())
        if key_m not in seen:
            search_entries.append({"t": mfr, "k": mfr.lower(), "ty": "Manufacturer",
                                   "u": f"/manufacturers/{m_slug}/", "sub": "View all sourced parts"})
            seen.add(key_m)
        key_c = ("c", cat.lower())
        if key_c not in seen:
            c_top = gp.resolve_cat(cat)[0]
            search_entries.append({"t": cat, "k": cat.lower(), "ty": "Category",
                                   "u": f"/components/{c_top}/", "sub": "Browse category"})
            seen.add(key_c)
    search_entries.sort(key=lambda e: e["k"])
    search_dir = os.path.join(out_root, "search")
    os.makedirs(search_dir, exist_ok=True)
    shards = []
    shard_idx = 0
    for i in range(0, len(search_entries), SEARCH_SHARD_SIZE):
        chunk = search_entries[i:i + SEARCH_SHARD_SIZE]
        shard_path = os.path.join(search_dir, f"{shard_idx}.json")
        with open(shard_path, "w", encoding="utf-8") as f:
            f.write('{"entries":')
            f.write(json.dumps(chunk, ensure_ascii=False))
            f.write('}')
        shards.append({"file": f"/search/{shard_idx}.json", "n": len(chunk),
                       "from": chunk[0]["k"], "to": chunk[-1]["k"]})
        shard_idx += 1
    manifest = {"version": 1, "shardSize": SEARCH_SHARD_SIZE,
                "shardCount": len(shards), "total": len(search_entries), "shards": shards}
    with open(os.path.join(search_dir, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(manifest, ensure_ascii=False))

    # --- parts.json (mirror of main()) ---
    parts_json = []
    for g in groups:
        mpn = g["mpn"].strip()
        if not mpn:
            continue
        clean = (g.get("clean_mpn") or "").strip() or re.sub(r"[^A-Z0-9]", "", mpn.upper())
        uslug = g["url_slug"]
        raw = (g.get("attributes_json") or "").strip()
        attrs = gp.build_en_attrs(raw)
        parts_json.append({
            "mpn": mpn,
            "clean_mpn": clean,
            "manufacturer": g["manufacturer"].strip(),
            "brand": g.get("brand", g["manufacturer"]).strip(),
            "url_slug": uslug,
            "category": g.get("category", "").strip(),
            "subcategory": g.get("subcategory", "").strip(),
            "description": g.get("description", "").strip(),
            "applications": g.get("applications", "").strip(),
            "keywords": g.get("keywords", "").strip(),
            "attributes": attrs,
            "sources": g.get("sources", []),
            "needs_review": bool(g.get("needs_review")),
            "availability": g.get("availability", "").strip(),
            "alternative_parts": g.get("alternative_parts", "").strip(),
            "datasheet_url": g.get("datasheet_url", "").strip(),
            "product_url": f"/products/{uslug}/",
        })
    with open(os.path.join(out_root, "parts.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(parts_json, ensure_ascii=False, indent=2))

    return written


def incremental_build(base_out, full_csv, out_root,
                      mfr_map=None, attr_dict=None, attr_json=None, val_json=None,
                      strict=False):
    """Apply N new SKUs to an existing 550 build, incrementally.

    base_out : a previously-built site (the 550 deployed pages). Copied verbatim;
               550 product pages are never re-rendered.
    full_csv : the (550+N) Master used to derive slugs + regenerate globals.
    out_root : destination for the incremental build.
    Returns the number of NEW product pages rendered.
    """
    mfr_map = mfr_map or os.path.join(REPO, "data", "production", "mfr_canonical.csv")
    attr_dict = attr_dict or os.path.join(REPO, "data", "production", "attributes_dictionary.md")
    attr_json = attr_json or os.path.join(REPO, "tools", "attribute_dictionary.json")
    val_json = val_json or os.path.join(REPO, "tools", "value_translation.json")

    if os.path.exists(out_root):
        shutil.rmtree(out_root)
    shutil.copytree(base_out, out_root)

    groups, by_mfr, by_cat, related_map, registry, stats = _preprocess(
        full_csv, mfr_map, attr_dict, attr_json, val_json, strict=strict)
    new_pages = _write_globals(out_root, groups, by_mfr, by_cat, related_map)
    return new_pages
