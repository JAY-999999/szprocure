"""
SZ Procure — narrow SEO post-processor (Phase 3, YELLOW-1 & YELLOW-3).

This module runs AFTER gen_parts.py has produced the static build. It is the
ONLY writer of SEO policy into the built HTML, and it touches exactly three
zones per product page (per the Phase 3 hard gate — the "narrow" constraint):

  1. <meta name="robots" content="...">   (index,follow | noindex,follow)
  2. the Product JSON-LD block             (add "mpn"; add Brand "@id")
  3. <link rel="canonical" href="...">     (duplicate -> primary only)

gen_parts.py is NEVER modified. The policy itself lives in index_policy.py
(single source of truth), consumed here so the robots <meta> and the sitemap
can never disagree.

Fail-open: any page not found in the classification map keeps index,follow and
its self-canonical; we never invent noindex,nofollow.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys

DOMAIN = "https://www.szprocure.com"

# Import the single-source policy module (NON-FROZEN; do not modify gen_parts).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import index_policy as ip

# --- regexes (byte-safe, anchored to the exact layout gen_parts.py emits) -----
# 1) robots meta — content="..." is the only thing we rewrite.
ROBOTS_META_RE = re.compile(r'(<meta name="robots" content=")[^"]*(" />)')

# 3) canonical — href="..." is the only thing we rewrite (duplicates only).
CANONICAL_RE = re.compile(r'(<link rel="canonical" href=")[^"]*(" />)')

# 2a) Product JSON-LD is uniquely identified by the "model" key (only Product
#     has it; BreadcrumbList / FAQPage / Organization do not). The trailing \n
#     is captured as group 2 so the injected "mpn" line keeps its own line.
MODEL_LINE_RE = re.compile(r'(\s*"model":\s*"[^"]*",)(\n)')

# 2b) The Brand object inside the Product block.
BRAND_LINE_RE = re.compile(
    r'"brand":\s*\{\s*("@type":\s*"Brand",\s*"name":\s*"[^"]*")\s*\}'
)

BRAND_ID = '"@id": "#szprocure-org"'


def _esc(value: str) -> str:
    """Mirror gen_parts.esc() so injected values can never break the script."""
    return html.escape(str(value), quote=True)


def process_product_html(html_text: str,
                         cls: "ip.PartClassification | None",
                         mpn: str,
                         primary_url: "str | None") -> str:
    """Apply the three narrow edits to one product page's HTML.

    ``cls``       classification for this url_slug (None -> fail-open index).
    ``mpn``       real MPN to inject into Product JSON-LD (empty -> skip).
    ``primary_url`` canonical URL for DUPLICATE pages (else None -> keep self).
    """
    # 1) robots meta -----------------------------------------------------------
    robots = ip.ROBOTS_INDEX_FOLLOW if (cls is None or cls.indexable) \
        else ip.ROBOTS_NOINDEX_FOLLOW
    html_text = ROBOTS_META_RE.sub(rf"\1{robots}\2", html_text)

    # 2a) Product JSON-LD: add "mpn" right after the "model" line (idempotent)
    if mpn and '"mpn":' not in html_text:
        html_text = MODEL_LINE_RE.sub(
            lambda m: m.group(1) + '\n      "mpn": "' + _esc(mpn) + '",' + m.group(2),
            html_text, count=1,
        )

    # 2b) Product JSON-LD: add Brand "@id" (idempotent)
    if BRAND_ID not in html_text:
        html_text = BRAND_LINE_RE.sub(
            lambda m: '"brand": { ' + m.group(1) + ', ' + BRAND_ID + ' }',
            html_text, count=1,
        )

    # 3) duplicate canonical -> primary (non-duplicate reasons keep self)
    if primary_url:
        html_text = CANONICAL_RE.sub(
            lambda m: m.group(1) + primary_url + m.group(2), html_text, count=1,
        )

    return html_text


def apply_to_site(site_root: str, parts_json_path: str,
                  dry_run: bool = False) -> dict:
    """Process every product page under <site_root>/products/.

    Returns a report dict (counts + any surprises). Idempotent: a page already
    carrying mpn + Brand @id produces no diff and is counted as skipped.
    """
    with open(parts_json_path, encoding="utf-8") as fh:
        records = json.load(fh)
    classified = ip.classify_parts(records)
    by_slug = {r.get("url_slug"): r for r in records}

    products_dir = os.path.join(site_root, "products")
    if not os.path.isdir(products_dir):
        raise SystemExit(f"no products dir at {products_dir}")

    stats = {
        "processed": 0, "skipped": 0, "noindex": 0, "duplicate": 0,
        "mpn_added": 0, "brand_id_added": 0, "canonical_rewritten": 0,
    }

    for slug in sorted(os.listdir(products_dir)):
        page = os.path.join(products_dir, slug, "index.html")
        if not os.path.isfile(page):
            continue
        cls = classified.get(slug)
        rec = by_slug.get(slug, {}) or {}
        mpn = (rec.get("mpn") or rec.get("clean_mpn") or "").strip()
        primary_url = None
        if cls is not None and cls.reason == ip.REASON_DUPLICATE \
                and cls.canonical_to:
            primary_url = f"{DOMAIN}/products/{cls.canonical_to}/"

        with open(page, encoding="utf-8") as fh:
            text = fh.read()
        new = process_product_html(text, cls, mpn, primary_url)

        if new == text:
            stats["skipped"] += 1
            continue
        if not dry_run:
            with open(page, "w", encoding="utf-8") as fh:
                fh.write(new)
        stats["processed"] += 1
        if cls is not None and not cls.indexable:
            stats["noindex"] += 1
        if cls is not None and cls.reason == ip.REASON_DUPLICATE:
            stats["duplicate"] += 1
        if '"mpn":' in new and '"mpn":' not in text:
            stats["mpn_added"] += 1
        if BRAND_ID in new and BRAND_ID not in text:
            stats["brand_id_added"] += 1
        if primary_url:
            stats["canonical_rewritten"] += 1

    return stats


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Apply SEO index policy to build")
    p.add_argument("--site-root", default=".")
    p.add_argument("--parts-json", default="parts.json")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    report = apply_to_site(args.site_root, args.parts_json, dry_run=args.dry_run)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
