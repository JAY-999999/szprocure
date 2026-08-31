"""
SZ Procure — sitemap coverage post-processor (Phase 3, YELLOW-2).

Adds the Homepage + 6 static pages to sitemap_parts.xml, and ENSURES no
noindex URL ever enters the sitemap (gate: NOINDEX ∩ SITEMAP = ∅). The single
source of truth is index_policy.classify_parts(); the same map also drives the
robots <meta>, so the two can never disagree.

Output stays a single sitemap_parts.xml (675 <= SITEMAP_BATCH 45000) plus the
unchanged sitemap_parts_index.xml wrapper.
"""
from __future__ import annotations

import json
import os
import re
import sys

DOMAIN = "https://www.szprocure.com"
SITEMAP_BATCH = 45000

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import index_policy as ip

STATIC_PATHS = [
    "/about/", "/sourcing-services/", "/how-it-works/",
    "/request-a-quote/", "/contact/", "/ai-hardware/",
]

_XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>\n'
_URLSET_OPEN = '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
_URLSET_CLOSE = "</urlset>\n"

# One <url> per line, 2-space indent, matching gen_parts.py's exact layout.
_URL_TMPL = '  <url><loc>{u}</loc><changefreq>weekly</changefreq><priority>0.5</priority></url>'


def _product_slug_of(url: str) -> "str | None":
    m = re.search(r"/products/([^/]+)/$", url)
    return m.group(1) if m else None


def rebuild_sitemap(site_root: str, parts_json_path: str,
                    dry_run: bool = False) -> dict:
    with open(parts_json_path, encoding="utf-8") as fh:
        records = json.load(fh)
    classified = ip.classify_parts(records)

    sm_path = os.path.join(site_root, "sitemap_parts.xml")
    with open(sm_path, encoding="utf-8") as fh:
        existing_xml = fh.read()
    existing = re.findall(r"<loc>(.*?)</loc>", existing_xml)
    existing_set = set(existing)

    # 1) drop any noindex product URL already present (defensive; current build
    #    has none, but the gate must hold for every future build too).
    noindex_slugs = {s for s, c in classified.items() if not c.indexable}
    kept: list[str] = []
    dropped: list[str] = []
    for u in existing:
        slug = _product_slug_of(u)
        if slug in noindex_slugs:
            dropped.append(u)
        else:
            kept.append(u)

    # 2) add homepage + 6 static (idempotent: skip if already present).
    additions: list[str] = []
    homepage = DOMAIN + "/"
    if homepage not in existing_set and homepage not in kept:
        additions.append(homepage)
    for p in STATIC_PATHS:
        u = DOMAIN + p
        if u not in existing_set and u not in kept:
            additions.append(u)

    final_urls = kept + additions
    final_set = set(final_urls)

    # 3) hard gate: NOINDEX ∩ SITEMAP = ∅
    violation = [f"{DOMAIN}/products/{s}/" for s in noindex_slugs
                 if f"{DOMAIN}/products/{s}/" in final_set]
    if violation:
        raise AssertionError(f"NOINDEX ∩ SITEMAP violation: {violation}")

    # 4) single-file guard (675 <= SITEMAP_BATCH).
    if len(final_urls) > SITEMAP_BATCH:
        raise AssertionError(
            f"sitemap exceeds batch size: {len(final_urls)} > {SITEMAP_BATCH}")

    new_xml = (_XML_HEADER + _URLSET_OPEN
               + "\n".join(_URL_TMPL.format(u=u) for u in final_urls)
               + "\n" + _URLSET_CLOSE)

    if not dry_run:
        with open(sm_path + ".bak", "w", encoding="utf-8") as fh:
            fh.write(existing_xml)           # reversible backup
        with open(sm_path, "w", encoding="utf-8") as fh:
            fh.write(new_xml)

    return {
        "existing": len(existing),
        "kept": len(kept),
        "dropped_noindex": dropped,
        "added": additions,
        "total": len(final_urls),
    }


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Rebuild sitemap with coverage + noindex exclusion")
    p.add_argument("--site-root", default=".")
    p.add_argument("--parts-json", default="parts.json")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    report = rebuild_sitemap(args.site_root, args.parts_json, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
