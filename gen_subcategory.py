#!/usr/bin/env python3
"""
gen_subcategory.py  —  SZProcure Subcategory V2 generator (ADDITIVE / ISOLATED)
================================================================================
Generates Product Collection Landing Pages for every L3 subcategory that appears
in parts.json, keyed EXACTLY like gen_parts.py's L3 sitemap loop
(slugify_name(category) -> top_slug, slugify_name(subcategory, paren-stripped) ->
l3_slug) so the rendered HTML lands at the same URL the sitemap references. Plus
pagination pages and a standalone sitemap_subcat.xml.  Data-driven and re-runnable;
classification comes from parts.json, I3 publish-status gating comes from the
(read-only) taxonomy file.

V2 redesign: merged hero+intro, single Request a Quote CTA, no local filter,
no visible SKU count, static pagination, Related Manufacturers / Subcategories.
V2.1: Related Subcategories / Manufacturers rendered as inline-CSS card grids,
capped at RELATED_CAP, Manufacturers sorted by SKU count desc and filtered to
real existing pages (slugify_mfr matches gen_parts slugify_name rule).

Freeze guarantees (MUST stay true):
  * gen_parts.py / sitemap_parts.xml / vercel.json UNTOUCHED (we read parts.json +
    data/category_taxonomy.json READ-ONLY; we write only L3 HTML + sitemap_subcat.xml)
  * assets/styles.css / assets/site.js untouched
  * all /products/ SKU pages, L2 category top pages, Hub, Search, MFR pages untouched

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


def load_taxonomy():
    """I3/I4 (READ-ONLY): read data/category_taxonomy.json to replicate gen_parts.py's
    L3 publish-status gating. Returns (TOP_PS, L1_PS):
      TOP_PS: {top_slug: publish_status}     (active/hidden/review)
      L1_PS:  {l1_slug:  publish_status}     (slug == slugify_name(en_display))
    gen_subcategory.py never writes this file (freeze guarantee)."""
    path = os.path.join(ROOT, "data", "category_taxonomy.json")
    if not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8") as f:
        tax = json.load(f)
    tops = {t["slug"]: t.get("publish_status", "active") for t in tax.get("top_scopes", [])}
    l1 = {}
    for c in tax.get("l1_categories", []):
        l1[c.get("slug")] = c.get("publish_status", "active")
    return tops, l1


def data_driven_groups(parts):
    """Group parts EXACTLY like gen_parts.py's L3 sitemap loop (gen_parts.py L4424-4446).

    gen_parts.py keys by (native_top_slug, native_l1) where:
      native_top_slug = slugify_name(parts.json `category`)
      native_l1       = slugify_name(parts.json `subcategory`, paren-alias stripped)
    Verified across all 746 parts: slugify_name(subcategory.split('(')[0])
    == slugify_name(native_l1) (0 mismatch), so both slugs derive from parts.json
    alone (parts.json has no native_l1 column). This REMOVES the old dual-source bug
    where grouping was keyed by `category` (the TOP) but looked up by `l3_slug`
    (the L3) -- silently dropping every SKU from its L3 page and leaving the
    sitemap's 19 L3 URLs pointing at empty directories.

    Returns dict: (top_slug, l3_slug) -> {parts, top_display, l3_display}."""
    groups = {}
    for p in parts:
        cat = (p.get("category") or "").strip()
        sub = (p.get("subcategory") or "")
        if not cat or not sub:
            continue
        top_slug = slugify_name(cat)
        l3_display = sub.split("(")[0].strip()
        l3_slug = slugify_name(l3_display)
        key = (top_slug, l3_slug)
        g = groups.get(key)
        if g is None:
            g = {"parts": [], "top_display": cat, "l3_display": l3_display}
            groups[key] = g
        g["parts"].append(p)
    return groups


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
    TOP_PS, L1_PS = load_taxonomy()
    raw_groups = data_driven_groups(parts)

    # ---- I3 gating: mirror gen_parts.py L3 loop (skip non-active top / hidden|review l1) ----
    groups = {}
    skipped = []
    for key, g in raw_groups.items():
        top_slug, l3_slug = key
        if TOP_PS.get(top_slug, "active") != "active":
            skipped.append((key, "top_not_active"))
            continue
        if L1_PS.get(l3_slug, "active") in ("hidden", "review"):
            skipped.append((key, "l1_hidden_review"))
            continue
        groups[key] = g

    known_mfr = valid_mfr_slugs()
    # index groups by top for sibling (Related Subcategories) lookup
    by_top = defaultdict(list)
    for key in groups:
        by_top[key[0]].append(key)

    plan = []
    for key in sorted(groups):
        top_slug, l3_slug = key
        g = groups[key]
        all_parts = g["parts"]
        total_n = len(all_parts)
        total_pages = max(1, (total_n + PER_PAGE - 1) // PER_PAGE)
        # Related Manufacturers: by SKU count desc, only existing pages, capped
        mfr_counts = Counter(p["manufacturer"] for p in all_parts)
        mfr_options = [m for m, _ in mfr_counts.most_common()
                       if slugify_mfr(m) in known_mfr][:RELATED_CAP]
        # Related Subcategories: same L2, other L3, capped
        siblings = [(groups[s]["l3_display"], f"/components/{s[0]}/{s[1]}/")
                    for s in by_top[top_slug] if s != key][:RELATED_CAP]
        # All I3-passing groups are listed in sitemap_parts.xml (gen_parts.py applies no
        # GATE in its L3 loop), so render them indexable to stay consistent with the sitemap.
        indexable = True
        robots = "index, follow"
        plan.append({
            "top": top_slug, "l3": l3_slug, "display": g["l3_display"],
            "top_display": g["top_display"],
            "n": total_n, "pages": total_pages, "robots": robots,
            "in_sitemap": indexable, "siblings": len(siblings),
            "mfrs": len(mfr_options),
        })

    if args.dry_run:
        print("=== SUBCATEGORY V2 DRY-RUN REPORT (data-driven, I3-gated) ===")
        print(f"Data-driven (top,l3) groups: {len(raw_groups)}")
        print(f"After I3 gating (rendered):  {len(groups)}")
        if skipped:
            print(f"Skipped by I3 gating:        {len(skipped)}")
            for k, why in skipped:
                print(f"   - {k[0]}/{k[1]}  ({why})")
        pag = [r for r in plan if r["pages"] > 1]
        print(f"Subcats needing pagination (> {PER_PAGE}/page): {len(pag)} -> "
              + ", ".join(f"{r['display']}({r['pages']})" for r in pag))
        print(f"Sitemap entries (all rendered): {len(plan)}")
        print("\n-- Per subcat --")
        for r in sorted(plan, key=lambda x: -x["n"]):
            print(f"  {r['display']:26} n={r['n']:3} pages={r['pages']} robots={r['robots']:14} "
                  f"sitemap={'Y' if r['in_sitemap'] else 'N'} siblings={r['siblings']} mfrs={r['mfrs']}")
        # parts that failed to group (missing category/subcategory)
        n_ungrouped = len(parts) - sum(len(g["parts"]) for g in raw_groups.values())
        print(f"\nParts not grouped (missing category/subcategory): {n_ungrouped}")
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
    for key in sorted(groups):
        top_slug, l3_slug = key
        g = groups[key]
        all_parts = g["parts"]
        total_n = len(all_parts)
        if total_n == 0:
            continue
        total_pages = max(1, (total_n + PER_PAGE - 1) // PER_PAGE)
        # Related Manufacturers: by SKU count desc, only existing pages, capped
        mfr_counts = Counter(p["manufacturer"] for p in all_parts)
        mfr_options = [m for m, _ in mfr_counts.most_common()
                       if slugify_mfr(m) in known_mfr][:RELATED_CAP]
        # Related Subcategories: same L2, other L3, capped
        siblings = [(groups[s]["l3_display"], f"/components/{s[0]}/{s[1]}/")
                    for s in by_top[top_slug] if s != key][:RELATED_CAP]
        base_dir = os.path.join(COMP, top_slug, l3_slug)
        for pg in range(1, total_pages + 1):
            doc = build_page(g["l3_display"], top_slug, g["top_display"], l3_slug,
                             all_parts, pg, total_pages, mfr_options, siblings)
            if pg == 1:
                out = os.path.join(base_dir, "index.html")
                page_url = f"/components/{top_slug}/{l3_slug}/"
            else:
                out = os.path.join(base_dir, "page", str(pg), "index.html")
                page_url = f"/components/{top_slug}/{l3_slug}/page/{pg}/"
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                f.write(doc)
            written += 1
            # sitemap: every rendered (I3-passing) subcat — mirrors sitemap_parts.xml
            if pg == 1:
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
