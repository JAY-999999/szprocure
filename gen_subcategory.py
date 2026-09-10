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


# V3: per-subcategory definition lead (one sentence, definition-style — mirrors the L1
# top-category hero format). Keys MUST be the current canonical L3 slugs from
# data_driven_groups(). Values are a single natural definition sentence: NO sourcing tail,
# matching the L1 "Passive Components are …" one-sentence leads. The previous seo_intro()
# appended a "As your China sourcing partner …" tail that L1 does not use; L2 now mirrors L1.
# Graceful fallback (never the old subcat.lower() casing/redundancy bug).
SUBCAT_LEAD = {
    "connectors": "Connectors are electromechanical components that join circuits and cables, carrying power and signals between boards, modules and external devices.",
    "switches": "Switches are electromechanical controls that open, close or change signal and power paths in a circuit.",
    "amplifiers-comparators": "Amplifiers and comparators are analog ICs that boost signal amplitude or compare voltage levels for sensing and conditioning.",
    "data-converters": "Data converters are mixed-signal ICs that translate between analog voltages and digital codes — ADCs and DACs.",
    "interface-ics": "Interface ICs bridge communication between chips and systems — UART, SPI, I2C, CAN, RS-485 and transceivers.",
    "logic-ics": "Logic ICs implement the basic building blocks of digital circuits — gates, flip-flops, buffers and translators.",
    "microcontrollers": "Microcontrollers are self-contained integrated circuits that combine a processor, memory and peripherals to control embedded and IoT devices.",
    "memory": "Memory ICs store program code and data — flash, EEPROM, SRAM, DRAM and serial memory.",
    "functional-modules": "Functional modules are ready-made building blocks — power, driver and interface modules — that drop into a design.",
    "iot-communication-modules": "IoT & communication modules add wireless connectivity — Wi-Fi, Bluetooth, LoRa, cellular and GNSS — to connected products.",
    "optoelectronics": "Optoelectronics combine light and electronics — LEDs, photodiodes, displays and optical sensors.",
    "capacitors": "Capacitors are passive components used for energy storage, filtering, coupling and voltage stabilization in electronic circuits.",
    "inductors-coils-transformers": "Inductors, coils and transformers store energy in magnetic fields for filtering, power conversion and signal isolation.",
    "oscillators-resonators": "Oscillators and resonators provide stable clock and timing references for digital and RF systems.",
    "resistors": "Resistors limit current and set bias, divide voltage and terminate signals across electronic circuits.",
    "power-management": "Power-management ICs regulate, convert and distribute power — LDOs, DC-DC converters, PMICs and supervisors.",
    "diodes": "Diodes allow current in one direction and are used for rectification, clamping, protection and switching.",
    "transistors": "Transistors switch and amplify signals — bipolar (BJT), MOSFET and IGBT devices for analog and power use.",
    "sensors": "Sensors convert physical quantities — temperature, pressure, motion and light — into electrical signals for measurement.",
}


def seo_intro(subcat, top_display, l3_slug):
    """One natural definition lead (V3). Mirrors the L1 top-category hero: a single
    definition sentence, no SKU count, no sourcing tail. Data-driven from SUBCAT_LEAD;
    falls back to a clean generated definition (never subcat.lower() casing bug)."""
    lead = SUBCAT_LEAD.get(l3_slug)
    if lead:
        return lead
    first = subcat[0].upper() + subcat[1:]
    return f"{first} are {top_display.lower()} used in a wide range of electronic circuits."


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
    # V3 Subcategory Directory — styles scoped to this page only (global styles.css is frozen
    # and untouched). Reuses the site's existing CSS variables so the design language is kept.
    h.append('  <style>')
    h.append('    .sku-heading-row{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:16px;margin:0 0 14px;}')
    h.append('    .sku-title{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-self:start;}')
    h.append('    .sku-title h2{margin:0;}')
    h.append('    .sku-count-badge{display:inline-flex;align-items:center;justify-content:center;min-width:32px;height:32px;padding:0 10px;border-radius:999px;background:var(--accent-soft);color:var(--muted);font-size:.85rem;font-weight:700;}')
    h.append('    .sku-section{padding-top:48px;padding-bottom:40px;}')
    h.append('    .sku-search{width:420px;max-width:100%;justify-self:center;}')
    h.append('    .sku-search input{width:100%;padding:11px 14px;border:1px solid var(--border-strong);border-radius:10px;font-size:.95rem;color:var(--navy);background:#fff;}')
    h.append('    .sku-search input:focus{outline:none;border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);}')
    h.append('    .sku-part-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:4px;}')
    h.append('    .sku-part-card{display:flex;flex-direction:column;gap:5px;padding:14px 16px;border:1px solid var(--border-strong);border-radius:10px;background:#fff;text-decoration:none;transition:border-color .15s,box-shadow .15s;}')
    h.append('    .sku-part-card:hover{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);}')
    h.append('    .sku-part-card .pn{font-size:1rem;font-weight:600;color:var(--navy);line-height:1.35;word-break:break-word;}')
    h.append('    .sku-part-card .mfr{font-size:.9rem;color:var(--muted);line-height:1.3;}')
    h.append('    .sku-empty{display:none;padding:18px 14px;color:var(--muted);font-style:italic;}')
    h.append('    .pagination{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:26px;}')
    h.append('    .pagination .page-state{display:inline-flex;align-items:center;justify-content:center;min-width:40px;height:40px;padding:0 12px;border-radius:9px;background:var(--accent);color:#fff;font-weight:600;}')
    h.append('    .pagination .btn-ghost{min-width:40px;height:40px;display:inline-flex;align-items:center;justify-content:center;}')
    h.append('    @media (max-width:640px){')
    h.append('      .sku-heading-row{grid-template-columns:1fr;gap:12px;}')
    h.append('      .sku-search{width:100%;grid-column:1;}')
    h.append('      .sku-part-grid{grid-template-columns:1fr;}')
    h.append('    }')
    # --- L1 parity tuning (match the 10 locked top-category pages) ---
    h.append('    .section:not(.navy) h2 { font-size: clamp(1.25rem, 2vw, 1.55rem); }')
    h.append('    .page-head h1 { font-size: clamp(1.3rem, 2vw, 1.6rem); }')
    h.append('    @media (max-width: 520px) { .page-head h1 { font-size: 1.1rem !important; } }')
    h.append('    .subcat-card { padding: 14px 18px; display: flex; align-items: center; justify-content: center; min-height: 74px; color: inherit; text-decoration: none; }')
    h.append('    .subcat-card .sku-mpn { color: var(--text, #1a2233); font-weight: 600; font-size: .95rem; text-align: center; }')
    h.append('    .subcat-card:hover .sku-mpn { color: var(--accent, #0A84FF); }')
    h.append('    .sku-mfr { white-space: nowrap; }')
    h.append('    .check-list li { white-space: nowrap; }')
    # Match L1 top-category pages: Related card grids use the global .grid gap (22px).
    # Related Manufacturers cards mirror Related Subcategories in size and centering.
    h.append('    .grid.grid-4 { gap: 22px; }')
    h.append('    .sku-card { padding: 14px 18px; display: flex; align-items: center; justify-content: center; min-height: 74px; color: inherit; text-decoration: none; }')
    h.append('    .sku-card .sku-mpn { color: var(--text, #1a2233); font-weight: 600; font-size: .95rem; text-align: center; }')
    h.append('    .sku-card:hover .sku-mpn { color: var(--accent, #0A84FF); }')
    h.append('    .cta-simple h2 { font-size: 1.3rem; }')
    h.append('    /* Pull the RFQ block down so its gray background does not overlap the cards above. */')
    h.append('    .section--cta-simple { margin-top: 0 !important; padding: 24px 0 48px; }')
    h.append('    @media (max-width: 860px) { .section--cta-simple { margin-top: 0 !important; } }')
    h.append('  </style>')
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
    # SEO Hero / Intro (V3: mirrors the L1 top-category hero — H1 + one-sentence definition
    # lead + 4 capability checkmarks (2x2, blue) + single Request a Quote CTA. Same visual
    # language as the 10 locked L1 pages. No eyebrow (L1 has none).)
    b.append('    <section class="page-head" style="padding:38px 0 32px;">')
    b.append('      <div class="container">')
    b.append(f'        <h1 style="margin-bottom:4px;">{esc(subcat)}</h1>')
    _lead = seo_intro(subcat, top_display, l3_slug)
    b.append(f'        <p class="lead" style="margin-bottom:6px;">{esc(_lead)}</p>')
    b.append('        <ul class="check-list" style="margin-top:8px; display:grid; grid-template-columns:1fr 1fr; column-gap:16px; row-gap:9px; padding-left:0; max-width:760px;">')
    b.append('          <li style="margin:0; line-height:1.45;"><span style="color:var(--accent,#0A84FF)">&#10003;</span> Hard-to-find Parts Sourcing</li>')
    b.append('          <li style="margin:0; line-height:1.45;"><span style="color:var(--accent,#0A84FF)">&#10003;</span> Alternative Parts Matching</li>')
    b.append('          <li style="margin:0; line-height:1.45;"><span style="color:var(--accent,#0A84FF)">&#10003;</span> Supplier Screening &amp; Product Verification</li>')
    b.append('          <li style="margin:0; line-height:1.45;"><span style="color:var(--accent,#0A84FF)">&#10003;</span> BOM &amp; Small Quantity Orders</li>')
    b.append('        </ul>')
    b.append('        <div class="part-head-actions" style="margin-top:12px;">')
    b.append('          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>')
    b.append('        </div>')
    b.append("      </div>")
    b.append("    </section>")
    # SKU Directory (V3: real data-driven count + client-side search + compact table.
    # Part Number is the primary visual layer (clickable, bold); Manufacturer is muted/secondary.
    # No fictional Price/Stock/MOQ columns — SZProcure is a sourcing partner, not a stockist.)
    b.append('    <section class="section sku-section">')
    b.append('      <div class="container">')
    b.append('        <div class="sku-heading-row">')
    b.append('          <div class="sku-title">')
    b.append(f"            <h2>{esc(subcat)} We Source</h2>")
    b.append(f'            <span class="sku-count-badge" aria-label="{total_n} parts">{total_n}</span>')
    b.append('          </div>')
    b.append('          <div class="sku-search">')
    b.append('            <input type="search" id="sku-filter" placeholder="Filter by part number or manufacturer…" aria-label="Filter parts" autocomplete="off" />')
    b.append('          </div>')
    b.append('        </div>')
    b.append('        <div class="sku-part-grid" id="sku-grid">')
    for p in parts_page:
        b.append(
            f'          <a class="sku-part-card" href="/products/{esc(p["url_slug"])}/">'
            f'<span class="pn">{esc(p["mpn"])}</span>'
            f'<span class="mfr">{esc(p["manufacturer"])}</span></a>'
        )
    b.append('        </div>')
    b.append('        <p class="sku-empty" id="sku-empty">No parts match your filter.</p>')
    # Pagination (V3: styled via scoped .pagination CSS above; same URLs/page logic, GATE untouched)
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
    # Related Subcategories (V3: mirrors L1 "X Subcategories" card grid — .grid.grid-4 +
    # .card.subcat-card, capped at RELATED_CAP. Same card size/font as the 10 L1 pages.)
    if siblings:
        b.append('    <section class="section" id="subcategories" style="padding:12px 0 48px;">')
        b.append('      <div class="container">')
        b.append('        <h2>Related Subcategories</h2>')
        b.append('        <div class="grid grid-4">')
        for sname, surl in siblings[:RELATED_CAP]:
            b.append(f'          <a class="card subcat-card" href="{surl}"><div class="sku-mpn">{esc(sname)}</div></a>')
        b.append("        </div>")
        b.append("      </div>")
        b.append("    </section>")
    # Related Manufacturers (V3: mirrors L1 "Featured Components" card grid — .grid.grid-4 +
    # .card.sku-card, capped at RELATED_CAP. Same card size/font as the 10 L1 pages.)
    if mfr_options:
        b.append('    <section class="section" id="manufacturers" style="padding:12px 0 24px;">')
        b.append('      <div class="container">')
        b.append('        <h2>Related Manufacturers</h2>')
        b.append('        <div class="grid grid-4">')
        for m in mfr_options[:RELATED_CAP]:
            b.append(f'          <a class="card sku-card" href="/manufacturers/{esc(slugify_mfr(m))}/"><div class="sku-mpn">{esc(m)}</div></a>')
        b.append("        </div>")
        b.append("      </div>")
        b.append("    </section>")
    # RFQ CTA (V3: mirrors L1 "Need a Part or BOM?" .cta-simple block)
    b.append('    <section class="section soft section--cta-simple">')
    b.append('      <div class="container">')
    b.append('        <div class="cta-simple">')
    b.append('          <div>')
    b.append('            <h2>Need a Part or BOM?</h2>')
    b.append('            <p style="margin:6px 0 0; font-size:1rem; color:var(--muted,#6b7280); line-height:1.4;">Send us your part numbers and quantities for a quote.</p>')
    b.append('          </div>')
    b.append('          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>')
    b.append('        </div>')
    b.append("      </div>")
    b.append("    </section>")
    # V3: client-side SKU filter (V1 scope = current page only; matches Part Number + Manufacturer)
    b.append('    <script>')
    b.append('      (function(){')
    b.append('        var inp = document.getElementById("sku-filter");')
    b.append('        var grid = document.getElementById("sku-grid");')
    b.append('        var empty = document.getElementById("sku-empty");')
    b.append('        if (inp && grid) {')
    b.append('          var cards = grid.querySelectorAll(".sku-part-card");')
    b.append('          inp.addEventListener("input", function(){')
    b.append('            var q = inp.value.trim().toLowerCase();')
    b.append('            var shown = 0;')
    b.append('            for (var i=0;i<cards.length;i++){')
    b.append('              var c = cards[i];')
    b.append('              var pn = c.querySelector(".pn");')
    b.append('              var mf = c.querySelector(".mfr");')
    b.append('              var txt = ((pn?pn.textContent:"") + " " + (mf?mf.textContent:"")).toLowerCase();')
    b.append('              var ok = q === "" || txt.indexOf(q) !== -1;')
    b.append('              c.style.display = ok ? "" : "none";')
    b.append('              if (ok) shown++;')
    b.append('            }')
    b.append('            if (empty) empty.style.display = shown ? "none" : "block";')
    b.append('          });')
    b.append('        }')
    b.append('      })();')
    b.append('    </script>')
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
    ap.add_argument("--only", nargs="*", default=None,
                    help="PROTOTYPE: restrict generation to the given L3 slugs only "
                         "(e.g. --only microcontrollers capacitors). Skips sitemap rewrite.")
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

    # --only (PROTOTYPE) restricts which pages are WRITTEN, but sibling/manufacturer
    # relations are still computed from the FULL gated set so Related Subcategories stays correct.
    only_set = set(args.only) if args.only else None
    if only_set:
        print(f"--only filter active: will generate only {len(only_set)} subcat(s): "
              + ", ".join(sorted(only_set)))

    known_mfr = valid_mfr_slugs()
    # index groups by top for sibling (Related Subcategories) lookup
    by_top = defaultdict(list)
    for key in groups:
        by_top[key[0]].append(key)

    plan = []
    for key in sorted(groups):
        if only_set and key[1] not in only_set:
            continue
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
        # robots + sitemap inclusion both honor GATE (build_page already applies GATE to the
        # page's robots tag; the sitemap loop mirrors it). Subcats with < GATE SKU stay
        # published (noindex) but are excluded from sitemap_subcat.xml.
        indexable = total_n >= GATE
        robots = "index, follow" if total_n >= GATE else "noindex, follow"
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
        print(f"Sitemap entries (GATE-passing, indexable): {sum(1 for r in plan if r['in_sitemap'])}"
              f"  (rendered L2 pages total: {len(plan)})")
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
        if only_set and key[1] not in only_set:
            continue
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
            # sitemap inclusion is gated by GATE: only indexable (>= GATE SKU) subcats are
            # submitted, so noindex pages (functional-modules 4 SKU, oscillators-resonators
            # 7 SKU) stay published as HTML but are excluded from the sitemap — consistent
            # with their robots=noindex,follow tag (fixes P0-1 GATE/sitemap contradiction).
            if pg == 1 and total_n >= GATE:
                sitemap_urls.append(SITE + page_url)
    # sitemap_subcat.xml (skipped under --only so the live sitemap is never truncated)
    if args.only:
        print("PROTOTYPE mode (--only): sitemap_subcat.xml left untouched.")
    else:
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
        print(f"Sitemap entries: {len(sitemap_urls)}")
    print(f"APPLY done. Subcat HTML pages written: {written}")


if __name__ == "__main__":
    main()
