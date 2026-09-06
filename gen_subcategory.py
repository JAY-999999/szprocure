#!/usr/bin/env python3
"""
gen_subcategory.py  —  SZProcure Subcategory V2 generator (ADDITIVE / ISOLATED)
================================================================================
Generates Product Collection Landing Pages for every L3 subcategory that already
exists on disk under components/<top>/<l3>/, plus pagination pages, plus a
standalone sitemap_subcat.xml.  Designed to be re-runnable and to scale toward
20k SKUs: classification comes from the data source (parts.json `category`), not
from any hand-built taxonomy engine.

V2 redesign: merged hero+intro, single Request a Quote CTA, no local filter,
no visible SKU count, static pagination, Related Manufacturers / Subcategories.
V2.1: Related Subcategories / Manufacturers rendered as inline-CSS card grids,
capped at RELATED_CAP, Manufacturers sorted by SKU count desc and filtered to
real existing pages (slugify_mfr matches gen_parts slugify_name rule).

Freeze guarantees (MUST stay true):
  * gen_parts.py / parts.json / sitemap_parts.xml / vercel.json untouched
  * assets/styles.css / assets/site.js untouched
  * all 552 /products/ SKU pages, 6 L2 category pages, Hub, Search, 84 MFR pages untouched

Usage:
  python gen_subcategory.py --dry-run     # report only, write nothing
  python gen_subcategory.py --apply       # generate pages + sitemap
"""
import os
import sys
import re
import json
import html
import argparse
from collections import defaultdict, Counter

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = "https://www.szprocure.com"
COMP = os.path.join(ROOT, "components")
MFR_DIR = os.path.join(ROOT, "manufacturers")
ASSETS = os.path.join(ROOT, "assets")
PARTS_JSON = os.path.join(ROOT, "parts.json")

PER_PAGE = 50
GATE = 10  # SKU >= GATE -> indexable SEO page + sitemap entry
RELATED_CAP = 8  # Related Subcategories / Related Manufacturers max card count


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify(s: str) -> str:
    out = []
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch.isspace() or ch in "-_/":
            out.append("-")
    return "-".join(p for p in "".join(out).split("-") if p)


def slugify_mfr(name: str) -> str:
    """Manufacturer slug — MUST match gen_parts.slugify_name so links hit real
    /manufacturers/<slug>/ pages. Non-alphanumeric runs -> single hyphen."""
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def slugify_name(name: str) -> str:
    """EXACT mirror of gen_parts.slugify_name (gen_parts.py L229-233).

    The on-disk L3 directory name `l3_slug` IS gen_parts.slugify_name(category),
    so this is the STABLE identity key for matching SKUs to subcategory pages.
    The page <h1>/display text must NOT be used as an identity key (it may drift
    for SEO/copy reasons). Matching by slug keeps the generator robust against
    any future H1/category-label wording changes.
        "STMicroelectronics" -> "stmicroelectronics"
        "Power Management"   -> "power-management"
    """
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def jstr(s) -> str:
    """JSON-safe string (no problematic chars)."""
    return json.dumps(s, ensure_ascii=False)


def load_parts():
    with open(PARTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def walk_existing_subcats():
    """Return list of dicts: top_slug, l3_slug, display, top_display.
    Only subcats that already have an index.html on disk are generated (V1 scope)."""
    found = []
    for top in sorted(os.listdir(COMP)):
        tp = os.path.join(COMP, top)
        if not os.path.isdir(tp) or top == "search":
            continue
        # L2 display name from its index.html <title>; L2 titles bake in an SEO
        # suffix (" Sourcing from Shenzhen, China") that we strip for a clean name.
        top_display = top.replace("-", " ").title()
        t_idx = os.path.join(tp, "index.html")
        if os.path.exists(t_idx):
            t = open(t_idx, encoding="utf-8").read()
            m = __import__("re").search(r"<title>([^<|]+)", t)
            if m:
                # L2 <title> is already HTML-escaped ("&amp;"); unescape so esc() below emits a single escape
                top_display = html.unescape(m.group(1).split("|")[0].strip())
                # L2 titles bake in an SEO suffix ("Sourcing from Shenzhen, China"); strip it so the
                # subcat hero/breadcrumb show a clean category name instead of SEO boilerplate.
                for _sfx in (" Sourcing from Shenzhen, China",
                             " — Source from Shenzhen, China",
                             " Source from Shenzhen, China"):
                    if top_display.endswith(_sfx):
                        top_display = top_display[: -len(_sfx)].strip()
                        break
        for l3 in sorted(os.listdir(tp)):
            lp = os.path.join(tp, l3)
            idx = os.path.join(lp, "index.html")
            if os.path.isdir(lp) and os.path.exists(idx):
                t = open(idx, encoding="utf-8").read()
                h1 = __import__("re").search(r"<h1[^>]*>(.*?)</h1>", t, __import__("re").S)
                display = html.unescape(h1.group(1).strip()) if h1 else l3.replace("-", " ").title()
                found.append(
                    {
                        "top_slug": top,
                        "l3_slug": l3,
                        "display": display,
                        "top_display": top_display,
                    }
                )
    return found


def valid_mfr_slugs():
    if not os.path.isdir(MFR_DIR):
        return set()
    return set(d for d in os.listdir(MFR_DIR) if os.path.isdir(os.path.join(MFR_DIR, d)))


def mfr_slug(manufacturer, known):
    s = slugify(manufacturer)
    return s if s in known else s


# V2: per-subcategory natural SEO intro phrases (plural form, used as sentence subject
# so grammar stays correct regardless of the singular display name in <h1>).
SUBCAT_INTRO_PHRASE = {
    "connectors": "connectors",
    "pin-header": "pin headers",
    "switches": "switches",
    "usb-connectors": "USB connectors",
    "analog-ic": "analog ICs",
    "interface-ic": "interface ICs",
    "logic-ic": "logic ICs",
    "memory": "memory components",
    "memory-ic": "memory ICs",
    "microcontroller": "microcontrollers (MCUs)",
    "operational-amplifier": "operational amplifiers (op-amps)",
    "power-management-ic": "power-management ICs (PMICs)",
    "voltage-regulator": "voltage regulators (LDOs and linear regulators)",
    "gnss-modules": "GNSS and GPS modules",
    "modules": "modules",
    "rf-modules": "RF modules",
    "wifi-modules": "Wi-Fi modules",
    "capacitor": "capacitors",
    "crystal-oscillator": "crystal oscillators",
    "inductor": "inductors",
    "led-components": "LED components",
    "resistor": "resistors",
    "diode": "diodes",
    "mosfet": "MOSFETs",
    "transistor": "transistors",
    "sensors": "sensors",
}


def seo_intro(subcat, top_display, l3_slug):
    """One natural ~45-60 word intro paragraph (V2). No SKU count, no keyword stuffing.
    The plural phrase is the sentence subject so grammar is correct for any display form."""
    phrase = SUBCAT_INTRO_PHRASE.get(l3_slug, subcat.lower())
    first = phrase[0].upper() + phrase[1:]
    return (
        f"{first} are core {top_display.lower()} used throughout electronic and IoT products. "
        f"As your China sourcing partner, SZ Procure helps global buyers procure {phrase} from a verified "
        f"Shenzhen supply network — including popular families and hard-to-find versions — with BOM "
        f"consolidation and consolidated quotes."
    )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def render_head(subcat, top_slug, top_display, page_url, robots, parts_page, total_n):
    title = f"{esc(subcat)} — {esc(top_display)} | SZ Procure"
    desc = (
        f"Browse {esc(subcat)} we help global buyers source from Shenzhen, China — including popular "
        f"families and hard-to-find components, with consolidated quotes and BOM support."
    )
    canon = SITE + page_url
    h = []
    h.append('<!DOCTYPE html>')
    h.append('<html lang="en">')
    h.append('<head>')
    h.append('  <meta charset="UTF-8" />')
    h.append('  <meta name="viewport" content="width=device-width, initial-scale=1" />')
    h.append(f'  <title>{title}</title>')
    h.append(f'  <meta name="description" content="{desc}" />')
    h.append(f'  <meta name="robots" content="{robots}" />')
    h.append(f'  <link rel="canonical" href="{canon}" />')
    h.append(f'  <link rel="alternate" hreflang="x-default" href="{canon}" />')
    # Open Graph
    h.append('  <meta property="og:type" content="website" />')
    h.append('  <meta property="og:site_name" content="SZ Procure" />')
    h.append(f'  <meta property="og:title" content="{title}" />')
    h.append(f'  <meta property="og:description" content="{desc}" />')
    h.append(f'  <meta property="og:url" content="{canon}" />')
    h.append('  <meta property="og:image" content="https://www.szprocure.com/assets/img/hero.svg" />')
    h.append('  <link rel="stylesheet" href="/assets/styles.css" />')
    # site.js injects the shared #site-header nav (frozen asset, linked read-only)
    h.append('  <script src="/assets/site.js"></script>')
    # BreadcrumbList
    crumbs = [
        ("Home", SITE + "/"),
        ("Components", SITE + "/components/"),
        (top_display, SITE + f"/components/{top_slug}/"),
        (subcat, canon),
    ]
    bl = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": n, "item": u}
            for i, (n, u) in enumerate(crumbs)
        ],
    }
    h.append('  <script type="application/ld+json">')
    h.append("  " + json.dumps(bl, ensure_ascii=False, indent=2))
    h.append("  </script>")
    # Organization
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "SZ Procure",
        "url": SITE + "/",
        "description": "China electronics & AI hardware sourcing — connect global buyers to Shenzhen supply chain.",
        "email": "sales@szprocure.com",
        "address": {"@type": "PostalAddress", "addressLocality": "Shenzhen", "addressCountry": "CN"},
    }
    h.append('  <script type="application/ld+json">')
    h.append("  " + json.dumps(org, ensure_ascii=False, indent=2))
    h.append("  </script>")
    # CollectionPage + ItemList (each SKU = Product, NO offers/price/availability)
    items = []
    for p in parts_page:
        items.append(
            {
                "@type": "Product",
                "name": p["mpn"],
                "url": SITE + f"/products/{p['url_slug']}/",
                "brand": {"@type": "Brand", "name": p["manufacturer"]},
            }
        )
    coll = {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": f"{subcat} — {top_display}",
        "url": canon,
        "description": desc,
        "mainEntity": {
            "@type": "ItemList",
            "numberOfItems": len(items),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1, "item": it}
                for i, it in enumerate(items)
            ],
        },
    }
    h.append('  <script type="application/ld+json">')
    h.append("  " + json.dumps(coll, ensure_ascii=False, indent=2))
    h.append("  </script>")
    h.append("</head>")
    return "\n".join(h)


def render_body(subcat, top_slug, top_display, l3_slug, parts_page, page_num,
                total_pages, total_n, mfr_options, siblings, prev_url, next_url):
    b = []
    base = f"/components/{top_slug}/{l3_slug}/"
    b.append("<body>")
    b.append('  <div id="site-header"></div>')
    b.append("  <main>")
    # Breadcrumb nav
    b.append('    <nav class="breadcrumb"><div class="container">')
    b.append('      <a href="/">Home</a> ›')
    b.append('      <a href="/components/">Components</a> ›')
    b.append(f'      <a href="/components/{top_slug}/">{esc(top_display)}</a> ›')
    b.append(f'      <span>{esc(subcat)}</span>')
    b.append("    </div></nav>")
    # SEO Hero / Intro (V2: merged Hero + About — single focused intro, one CTA)
    b.append('    <section class="page-head">')
    b.append('      <div class="container">')
    b.append('        <div class="eyebrow">Component Subcategory</div>')
    b.append(f"        <h1>{esc(subcat)}</h1>")
    _lead = seo_intro(subcat, top_display, l3_slug)
    b.append(f'        <p class="lead">{esc(_lead)}</p>')
    b.append('        <div class="part-head-actions">')
    b.append('          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>')
    b.append('        </div>')
    b.append("      </div>")
    b.append("    </section>")
    # SKU Directory (V2: no local filter, no SKU count)
    b.append('    <section class="section">')
    b.append('      <div class="container">')
    b.append(f"        <h2>{esc(subcat)} We Source</h2>")
    b.append('        <ul class="bullet-list part-index" id="part-index">')
    for p in parts_page:
        b.append(
            f'          <li>'
            f'<a href="/products/{esc(p["url_slug"])}/">{esc(p["mpn"])}</a> '
            f'<span class="muted">— {esc(p["manufacturer"])}</span></li>'
        )
    b.append("        </ul>")
    # Pagination (static; only when more than one page) — Prev | 1 | 2 | Next
    if total_pages > 1:
        b.append('        <nav class="pagination" aria-label="Pagination">')
        if prev_url:
            b.append(f'          <a rel="prev" class="btn btn-ghost" href="{prev_url}">‹ Prev</a>')
        for pg in range(1, total_pages + 1):
            if pg == page_num:
                b.append(f'          <span class="page-state" aria-current="page">{pg}</span>')
            elif pg == 1:
                b.append(f'          <a class="btn btn-ghost" href="{base}">{pg}</a>')
            else:
                b.append(f'          <a class="btn btn-ghost" href="{base}page/{pg}/">{pg}</a>')
        if next_url:
            b.append(f'          <a rel="next" class="btn btn-ghost" href="{next_url}">Next ›</a>')
        b.append("        </nav>")
    b.append("      </div>")
    b.append("    </section>")
    # Related Subcategories (V2.1: inline-CSS card grid, capped at RELATED_CAP)
    if siblings:
        b.append('    <section class="section">')
        b.append('      <div class="container">')
        b.append("        <h2>Related Subcategories</h2>")
        b.append('        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;">')
        for sname, surl in siblings:
            b.append(f'          <a href="{surl}" style="display:block;padding:14px 16px;border:1px solid #e2e2e7;border-radius:10px;text-decoration:none;color:#1d1d1f;background:#fff;font-size:14px;font-weight:500;">{esc(sname)}</a>')
        b.append("        </div>")
        b.append("      </div>")
        b.append("    </section>")
    # Related Manufacturers (V2.1: inline-CSS card grid, by SKU count desc, real pages only)
    if mfr_options:
        b.append('    <section class="section">')
        b.append('      <div class="container">')
        b.append("        <h2>Related Manufacturers</h2>")
        b.append('        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px;">')
        for m in mfr_options:
            b.append(f'          <a href="/manufacturers/{esc(slugify_mfr(m))}/" style="display:block;padding:14px 16px;border:1px solid #e2e2e7;border-radius:10px;text-decoration:none;color:#1d1d1f;background:#fff;font-size:14px;font-weight:500;">{esc(m)}</a>')
        b.append("        </div>")
        b.append("      </div>")
        b.append("    </section>")
    # RFQ CTA (V2: single, focused — no duplicate sibling navigation)
    b.append('    <section class="section">')
    b.append('      <div class="container">')
    b.append('        <h2>Need a Quote?</h2>')
    b.append('        <p>Send us your BOM or a list of parts. We return a consolidated quote with lead-time and alternates.</p>')
    b.append('        <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>')
    b.append("      </div>")
    b.append("    </section>")
    b.append("  </main>")
    b.append("</body>")
    b.append("</html>")
    return "\n".join(b)


def build_page(subcat, top_slug, top_display, l3_slug, all_parts, page_num,
               total_pages, mfr_options, siblings):
    total_n = len(all_parts)
    start = (page_num - 1) * PER_PAGE
    parts_page = all_parts[start:start + PER_PAGE]
    base = f"/components/{top_slug}/{l3_slug}/"
    if page_num == 1:
        page_url = base
    else:
        page_url = f"{base}page/{page_num}/"
    # robots: Gate on the WHOLE subcat (page1 decides indexability)
    robots = "index, follow" if total_n >= GATE else "noindex, follow"
    prev_url = base if page_num > 1 else None
    if page_num < total_pages:
        next_url = f"{base}page/{page_num+1}/" if page_num + 1 > 1 else base
        if page_num + 1 == 2:
            next_url = f"{base}page/2/"
    else:
        next_url = None
    head = render_head(subcat, top_slug, top_display, page_url, robots, parts_page, total_n)
    body = render_body(subcat, top_slug, top_display, l3_slug, parts_page, page_num,
                       total_pages, total_n, mfr_options, siblings, prev_url, next_url)
    return head + "\n" + body


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--apply", action="store_true", help="generate files")
    args = ap.parse_args()
    if not (args.dry_run or args.apply):
        ap.error("specify --dry-run or --apply")

    parts = load_parts()
    # subcat -> list of parts
    # KEY BY STABLE SLUG: slugify_name(p["category"]) == on-disk l3_slug.
    # Do NOT key by the raw category string or by the page display/H1 text —
    # those can drift for SEO/copy reasons and would silently drop SKUs.
    by_cat = defaultdict(list)
    for p in parts:
        by_cat[slugify_name(p.get("category", ""))].append(p)
    existing = walk_existing_subcats()
    known_mfr = valid_mfr_slugs()

    # index existing by (top,l3) for sibling lookup
    by_top = defaultdict(list)
    for e in existing:
        by_top[e["top_slug"]].append(e)

    plan = []  # rows for report
    for e in existing:
        cat = e["display"]                         # render-only: H1 / title text
        all_parts = by_cat.get(e["l3_slug"], [])  # STABLE match: slug == on-disk dir
        total_n = len(all_parts)
        total_pages = max(1, (total_n + PER_PAGE - 1) // PER_PAGE) if total_n else 0
        # Related Manufacturers: by SKU count desc, only existing pages, capped
        mfr_counts = Counter(p["manufacturer"] for p in all_parts)
        mfr_options = [m for m, _ in mfr_counts.most_common()
                       if slugify_mfr(m) in known_mfr][:RELATED_CAP]
        # Related Subcategories: same L2, other L3, alphabetical, capped
        siblings = [(s["display"], f"/components/{s['top_slug']}/{s['l3_slug']}/")
                    for s in by_top[e["top_slug"]]
                    if s["l3_slug"] != e["l3_slug"]][:RELATED_CAP]
        indexable = total_n >= GATE
        robots = "index, follow" if indexable else "noindex, follow"
        plan.append({
            "top": e["top_slug"], "l3": e["l3_slug"], "display": cat,
            "n": total_n, "pages": total_pages, "robots": robots,
            "in_sitemap": indexable, "siblings": len(siblings),
            "mfrs": len(mfr_options),
        })

    if args.dry_run:
        print("=== SUBCATEGORY V1 DRY-RUN REPORT ===")
        print(f"Existing subcats on disk: {len(existing)}")
        idx = [r for r in plan if r["in_sitemap"]]
        noidx = [r for r in plan if not r["in_sitemap"]]
        print(f"Indexable (>= {GATE} SKU): {len(idx)}")
        print(f"Noindex (< {GATE} SKU):   {len(noidx)}")
        pag = [r for r in plan if r["pages"] > 1]
        print(f"Subcats needing pagination (> {PER_PAGE}/page): {len(pag)} -> "
              + ", ".join(f"{r['display']}({r['pages']})" for r in pag))
        print(f"Sitemap entries (page-1 only): {len(idx)}")
        print("\n-- Per subcat --")
        for r in sorted(plan, key=lambda x: -x["n"]):
            print(f"  {r['display']:24} n={r['n']:3} pages={r['pages']} robots={r['robots']:14} "
                  f"sitemap={'Y' if r['in_sitemap'] else 'N'} siblings={r['siblings']} mfrs={r['mfrs']}")
        # orphan check: parts whose category slug has no existing subcat dir
        # (stable key = slugify_name(category); display/H1 text must not be used)
        mapped_slugs = {e["l3_slug"] for e in existing}
        orphans = [p for p in parts if slugify_name(p.get("category", "")) not in mapped_slugs]
        print(f"\nOrphan SKUs (category w/ no subcat page): {len(orphans)}")
        for o in orphans:
            print(f"  {o['url_slug']} cat={o['category']}")
        # duplicate MPN across subcats
        dup = [m for m, c in Counter(p["mpn"] for p in parts).items() if c > 1]
        print(f"\nDuplicate MPN values in parts.json: {len(dup)} (cross-subcat dup would be a bug)")
        # URL uniqueness among generated
        urls = set()
        for r in plan:
            urls.add(f"/components/{r['top']}/{r['l3']}/")
            for pg in range(2, r["pages"] + 1):
                urls.add(f"/components/{r['top']}/{r['l3']}/page/{pg}/")
        print(f"Unique generated URLs: {len(urls)}")
        print("DRY-RUN OK — no files written.")
        return

    # ---- APPLY ----
    written = 0
    sitemap_urls = []
    for e in existing:
        cat = e["display"]                          # render-only: H1 / title text
        all_parts = by_cat.get(e["l3_slug"], [])   # STABLE match: slug == on-disk dir
        total_n = len(all_parts)
        if total_n == 0:
            continue
        total_pages = max(1, (total_n + PER_PAGE - 1) // PER_PAGE)
        # Related Manufacturers: by SKU count desc, only existing pages, capped
        mfr_counts = Counter(p["manufacturer"] for p in all_parts)
        mfr_options = [m for m, _ in mfr_counts.most_common()
                       if slugify_mfr(m) in known_mfr][:RELATED_CAP]
        # Related Subcategories: same L2, other L3, alphabetical, capped
        siblings = [(s["display"], f"/components/{s['top_slug']}/{s['l3_slug']}/")
                    for s in by_top[e["top_slug"]]
                    if s["l3_slug"] != e["l3_slug"]][:RELATED_CAP]
        base_dir = os.path.join(COMP, e["top_slug"], e["l3_slug"])
        for pg in range(1, total_pages + 1):
            doc = build_page(cat, e["top_slug"], e["top_display"], e["l3_slug"],
                             all_parts, pg, total_pages, mfr_options, siblings)
            if pg == 1:
                out = os.path.join(base_dir, "index.html")
                page_url = f"/components/{e['top_slug']}/{e['l3_slug']}/"
            else:
                out = os.path.join(base_dir, "page", str(pg), "index.html")
                page_url = f"/components/{e['top_slug']}/{e['l3_slug']}/page/{pg}/"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(doc)
            written += 1
            # sitemap: only page-1 of indexable subcats
            if total_n >= GATE and pg == 1:
                sitemap_urls.append(SITE + page_url)
    # sitemap_subcat.xml
    sm = ['<?xml version="1.0" encoding="UTF-8"?>']
    sm.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for u in sorted(sitemap_urls):
        sm.append("  <url>")
        sm.append(f"    <loc>{u}</loc>")
        sm.append("    <changefreq>weekly</changefreq>")
        sm.append("    <priority>0.6</priority>")
        sm.append("  </url>")
    sm.append("</urlset>")
    with open(os.path.join(ROOT, "sitemap_subcat.xml"), "w", encoding="utf-8") as f:
        f.write("\n".join(sm) + "\n")
    print(f"APPLY done. Subcat HTML pages written: {written}")
    print(f"Sitemap entries: {len(sitemap_urls)}")


if __name__ == "__main__":
    main()
