#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SZ Procure — Part / Manufacturer / Category Page Generator (data-driven, static output)
========================================================================================
Reads a parts CSV and generates static pages under SEMANTIC URL paths:

  /products/{slug}/        one page per part (the core "knowledge node")
  /manufacturers/{slug}/   one page per manufacturer (captures "X distributor China")
  /categories/{slug}/      one page per category (big top/mid-funnel traffic hub)

Why semantic paths (not /part/ or ?id=):
 - Must be designed ONCE before launch. Changing URLs after indexing requires 301s.
 - AI/search engines parse path semantics; /products/stm32f103c8t6 is self-describing.

Why static + templates:
 - 200k pages must be pre-generated, not JS-rendered (Google can't crawl JS at scale,
   and thin/duplicate content would be penalized).
 - Every page carries REAL value (specs, alternates, sourcing notes, related links).

Structured data (schema.org) on every page:
 - Product JSON-LD      (on part pages)
 - Organization JSON-LD (site-wide identity)
 - Breadcrumb JSON-LD   (hierarchy: Home > Category > Part / Home > Manufacturers > Mfr)

CSV columns (from 料号库.csv):
  PN, Mfr, Category, KeySpecs, Applications, TargetCustomers,
  AltParts, DemandRegion, Notes, Status, Image, Source

  Image  : path to a locally-hosted SVG/PNG symbol image (self-owned, zero copyright risk).
           e.g. /assets/img/mcu.svg  (falls back to hero.svg if empty)
  Source : optional external reference URL (e.g. distributor page) rendered as a
           nofollow "View on <site>" link — legitimate citation, NOT image hotlinking.

Usage:
  python gen_parts.py --csv "path/to/料号库.csv" --out "."
  (defaults: csv = ../芯片/料号库/料号库.csv relative to this script's dir)
"""
import csv, os, re, argparse, html, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
DOMAIN = "https://www.szprocure.com"
SITEMAP_BATCH = 45000  # urls per sitemap file (Google soft cap 50k)

STATUS_LABEL = {
    "scarce": ("Long lead-time / hard to source", "scarce"),
    "active": ("Active production", "active"),
    "eol": ("End of life / discontinued", "eol"),
}

# ---- slug helpers -------------------------------------------------------------
def slugify(pn):
    # AD7606BSTZ -> ad7606bstz ; keep alnum only
    return re.sub(r"[^a-z0-9]", "", pn.lower())

def pn_search_keys(pn):
    """Return search-key variants for a part number so users find it whether they
    type the original PN (AMS1117-3.3), the URL slug (ams111733), or a stripped
    version (ams11173)."""
    s = pn.strip().lower()
    variants = {s, slugify(pn)}
    variants.add(re.sub(r"[^a-z0-9]", "", s))
    return sorted(variants)

def slugify_name(name):
    # "STMicroelectronics" -> "stmicroelectronics" ; "Power Management" -> "power-management"
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")

def split_specs(s):
    return [x.strip() for x in s.split(",") if x.strip()]

def split_multi(s):
    return [x.strip() for x in re.split(r"[;]", s) if x.strip()]

def esc(s):
    return html.escape(str(s), quote=True)

# ---- reusable JSON-LD blocks --------------------------------------------------
def org_jsonld():
    return f"""
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Organization",
    "name": "SZ Procure",
    "url": "{DOMAIN}/",
    "description": "China electronics & AI hardware sourcing — connect global buyers to Shenzhen supply chain.",
    "email": "sales@szprocure.com",
    "address": {{ "@type": "PostalAddress", "addressLocality": "Shenzhen", "addressCountry": "CN" }}
  }}
  </script>"""

def breadcrumb_jsonld(items):
    # items: list of (name, url)
    ld_items = []
    for i, (name, url) in enumerate(items, 1):
        ld_items.append(
            f'    {{ "@type": "ListItem", "position": {i}, "name": "{esc(name)}", "item": "{url}" }}'
        )
    body = ",\n".join(ld_items)
    return f"""
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "itemListElement": [
{body}
    ]
  }}
  </script>"""

# ---- SEO head builder (consistent across all generated pages) -----------------
def seo_head(title, desc, url, img=None):
    og_img = img or f"{DOMAIN}/assets/img/hero.svg"
    return f"""  <title>{title}</title>
  <meta name="description" content="{desc}" />
  <meta name="robots" content="index, follow" />
  <link rel="canonical" href="{url}" />
  <link rel="alternate" hreflang="x-default" href="{url}" />
  <meta property="og:type" content="website" />
  <meta property="og:site_name" content="SZ Procure" />
  <meta property="og:title" content="{title}" />
  <meta property="og:description" content="{desc}" />
  <meta property="og:url" content="{url}" />
  <meta property="og:image" content="{og_img}" />"""

# ==============================================================================
# PART PAGE
# ==============================================================================
def gen_part_page(row, cat_slug, mfr_slug):
    pn = row["PN"].strip()
    mfr = row["Mfr"].strip()
    cat = row["Category"].strip()
    specs_raw = row["KeySpecs"].strip()
    apps = row["Applications"].strip()
    cust = row["TargetCustomers"].strip()
    alt_raw = row["AltParts"].strip()
    region = row["DemandRegion"].strip()
    notes = row["Notes"].strip()
    status = row["Status"].strip().lower()
    img = (row.get("Image") or "").strip()
    source = (row.get("Source") or "").strip()

    slug = slugify(pn)
    url = f"{DOMAIN}/products/{slug}/"
    img_url = img if img else "/assets/img/hero.svg"
    og_img = f"{DOMAIN}{img_url}" if img_url.startswith("/") else img_url
    title = f"{esc(pn)} {esc(mfr)} {esc(cat)} — Datasheet, Alternatives & Sourcing | SZ Procure"
    desc = (f"Source {pn} ({mfr} {cat}) from Shenzhen. Specifications, alternate parts "
            f"({esc(alt_raw) or 'N/A'}), applications and sourcing support for overseas buyers.")

    specs = split_specs(specs_raw)
    alts = split_multi(alt_raw)
    apps_list = split_multi(apps)
    cust_list = split_multi(cust)
    region_list = split_multi(region)

    st_label, st_cls = STATUS_LABEL.get(status, ("Status unknown", "active"))

    specs_html = "".join(f"<li><span>{esc(x)}</span></li>" for x in specs) or "<li><span>—</span></li>"
    alts_html = "".join(
        f'<li><a href="{DOMAIN}/products/{slugify(a)}/" class="alt-link">{esc(a)}</a></li>'
        for a in alts
    ) or "<li>No direct alternates listed — contact us for cross-reference.</li>"
    apps_html = "".join(f"<li>{esc(x)}</li>" for x in apps_list) or "<li>—</li>"
    cust_html = "".join(f"<li>{esc(x)}</li>" for x in cust_list) or "<li>—</li>"
    region_html = "".join(f'<span class="tag">{esc(x)}</span>' for x in region_list) or '<span class="tag">Global</span>'

    # scarcity callout — unique value content, avoids thin pages for scarce parts
    scarcity_block = ""
    if status == "scarce":
        scarcity_block = f"""
    <div class="callout scarce">
      <h2>Why {esc(pn)} is hard to source</h2>
      <p>This part shows <strong>long lead times or limited availability</strong> in the open market
      ({esc(notes) if notes else 'scarce supply'}). Many overseas factories struggle to secure stable
      volume. We maintain Shenzhen distributor and alternative-channel relationships to help you
      confirm real availability and lead time before you commit.</p>
    </div>"""
    elif status == "eol":
        scarcity_block = f"""
    <div class="callout eol">
      <h2>{esc(pn)} is end-of-life</h2>
      <p>This part is discontinued by the manufacturer. If your design depends on it, we can help
      locate last-time-buy stock or qualify a drop-in alternative ({esc(alt_raw) or 'contact us'}).</p>
    </div>"""

    notes_block = f"<p>{esc(notes)}</p>" if notes else "<p>—</p>"

    # External reference link (legitimate citation, nofollow — never image hotlinking)
    source_link = ""
    if source:
        source_link = (f'<p class="source-link">'
                       f'<a href="{esc(source)}" target="_blank" rel="nofollow noopener">'
                       f'View on 云汉芯城 ↗</a></p>'
                       f'<p class="muted small">Reference only — image &amp; data © respective owners.</p>')

    # breadcrumb: Home > Category > Part
    crumb = breadcrumb_jsonld([
        ("Home", f"{DOMAIN}/"),
        (cat, f"{DOMAIN}/categories/{cat_slug}/"),
        (pn, url),
    ])
    # Product JSON-LD
    alt_ld = ", ".join(f'"{esc(a)}"' for a in alts)
    product_jsonld = f"""
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "{esc(pn)}",
    "category": "{esc(cat)}",
    "brand": {{ "@type": "Brand", "name": "{esc(mfr)}" }},
    "description": "{esc(desc)}",
    "url": "{url}",
    "offers": {{
      "@type": "Offer",
      "priceCurrency": "USD",
      "availability": "https://schema.org/InStock",
      "seller": {{ "@type": "Organization", "name": "SZ Procure" }}
    }}{(", \"alternatePart\": [" + alt_ld + "]") if alt_ld else ""}
  }}
  </script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
{seo_head(title, desc, url, og_img)}
  <link rel="stylesheet" href="/assets/styles.css" />
{crumb}
{product_jsonld}
{org_jsonld()}
</head>
<body>
  <div id="site-header"></div>
  <main>
    <nav class="breadcrumb"><div class="container">
      <a href="/">Home</a> ›
      <a href="/categories/{cat_slug}/">{esc(cat)}</a> ›
      <span>{esc(pn)}</span>
    </div></nav>
    <section class="page-head">
      <div class="container">
        <div class="eyebrow"><a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a> · {esc(cat)}</div>
        <h1>{esc(pn)} <small>{esc(mfr)}</small></h1>
        <p class="lead">{esc(cat)} — sourced from Shenzhen's electronics supply chain.</p>
        <div class="status-row"><span class="status {st_cls}">{st_label}</span>
          <span class="region">Demand regions: {region_html}</span></div>
      </div>
    </section>

    <section class="section">
      <div class="container two-col">
        <div>
          <figure class="part-figure">
            <img src="{img_url}" alt="{esc(pn)} {esc(cat)} symbol / package illustration" width="320" height="240" loading="lazy" />
            <figcaption>Illustrative package symbol — SZ Procure original artwork.</figcaption>
          </figure>

          <h2>Key Specifications</h2>
          <h2>Key Specifications</h2>
          <ul class="spec-list">{specs_html}</ul>

          <h2>Applications</h2>
          <ul class="bullet-list">{apps_html}</ul>

          <h2>Typical Buyers</h2>
          <ul class="bullet-list">{cust_html}</ul>

          <h2>Sourcing Notes</h2>
          {notes_block}
        </div>
        <aside class="part-aside">
          <div class="card">
            <h3>Alternate Parts</h3>
            <ul class="alt-list">{alts_html}</ul>
            <h3>Manufacturer</h3>
            <p><a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a> — view all parts we source.</p>
            <h3>Need a quote?</h3>
            <p>Send us the part number and quantity — we'll check Shenzhen availability.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/?pn={esc(pn)}">Request a Quote</a>
            {source_link}
          </div>
        </aside>
      </div>
    </section>
{scarcity_block}
    <section class="section cta-band">
      <div class="container">
        <h2>Can't find the exact variant?</h2>
        <p>We cross-reference packages, grades and end-of-life parts daily. Tell us what you need.</p>
        <a class="btn btn-primary btn-lg" href="/request-a-quote/">Request a Quote</a>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
</body>
</html>"""

# ==============================================================================
# MANUFACTURER PAGE
# ==============================================================================
def gen_manufacturer_page(mfr, parts, cat_slugs):
    mfr_slug = slugify_name(mfr)
    url = f"{DOMAIN}/manufacturers/{mfr_slug}/"
    title = f"{esc(mfr)} Components & ICs Sourcing China — Distributor Alternative | SZ Procure"
    desc = (f"Source {esc(mfr)} parts from Shenzhen. Browse {len(parts)} {esc(mfr)} components and ICs "
            f"we help global buyers procurement — alternates, lead-time and quote support.")
    # list of parts linking back to product pages
    part_links = "".join(
        f'<li><a href="/products/{slugify(p["PN"])}/">{esc(p["PN"])}</a> '
        f'<span class="muted">— {esc(p["Category"])}</span></li>'
        for p in sorted(parts, key=lambda x: x["PN"])
    )
    # related categories for this manufacturer
    cats = sorted({p["Category"] for p in parts})
    cat_links = "".join(
        f'<li><a href="/categories/{slugify_name(c)}/">{esc(c)}</a></li>' for c in cats
    )
    crumb = breadcrumb_jsonld([
        ("Home", f"{DOMAIN}/"),
        ("Manufacturers", f"{DOMAIN}/manufacturers/"),
        (mfr, url),
    ])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
{seo_head(title, desc, url)}
  <link rel="stylesheet" href="/assets/styles.css" />
{crumb}
{org_jsonld()}
</head>
<body>
  <div id="site-header"></div>
  <main>
    <nav class="breadcrumb"><div class="container">
      <a href="/">Home</a> ›
      <a href="/manufacturers/">Manufacturers</a> ›
      <span>{esc(mfr)}</span>
    </div></nav>
    <section class="page-head">
      <div class="container">
        <div class="eyebrow">Manufacturer</div>
        <h1>{esc(mfr)} Sourcing from China</h1>
        <p class="lead">{len(parts)} {esc(mfr)} components &amp; ICs in our sourcing catalog.</p>
      </div>
    </section>
    <section class="section">
      <div class="container two-col">
        <div>
          <h2>{esc(mfr)} Parts We Source</h2>
          <ul class="bullet-list part-index">{part_links}</ul>
        </div>
        <aside class="part-aside">
          <div class="card">
            <h3>Related Categories</h3>
            <ul class="alt-list">{cat_links}</ul>
            <h3>Need a {esc(mfr)} part not listed?</h3>
            <p>Send us the exact part number — we'll check Shenzhen availability.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/">Request a Quote</a>
          </div>
        </aside>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
</body>
</html>"""

# ==============================================================================
# CATEGORY PAGE
# ==============================================================================
def gen_category_page(cat, parts, mfr_slugs):
    cat_slug = slugify_name(cat)
    url = f"{DOMAIN}/categories/{cat_slug}/"
    title = f"{esc(cat)} — Electronic Components Sourcing China | SZ Procure"
    desc = (f"Source {esc(cat)} from Shenzhen. Browse {len(parts)} {esc(cat)} parts we help global "
            f"buyers procure — alternates, lead-time and quote support.")
    part_links = "".join(
        f'<li><a href="/products/{slugify(p["PN"])}/">{esc(p["PN"])}</a> '
        f'<span class="muted">— {esc(p["Mfr"])}</span></li>'
        for p in sorted(parts, key=lambda x: x["PN"])
    )
    mfrs = sorted({p["Mfr"] for p in parts})
    mfr_links = "".join(
        f'<li><a href="/manufacturers/{slugify_name(m)}/">{esc(m)}</a></li>' for m in mfrs
    )
    crumb = breadcrumb_jsonld([
        ("Home", f"{DOMAIN}/"),
        ("Categories", f"{DOMAIN}/categories/"),
        (cat, url),
    ])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
{seo_head(title, desc, url)}
  <link rel="stylesheet" href="/assets/styles.css" />
{crumb}
{org_jsonld()}
</head>
<body>
  <div id="site-header"></div>
  <main>
    <nav class="breadcrumb"><div class="container">
      <a href="/">Home</a> ›
      <a href="/categories/">Categories</a> ›
      <span>{esc(cat)}</span>
    </div></nav>
    <section class="page-head">
      <div class="container">
        <div class="eyebrow">Category</div>
        <h1>{esc(cat)} Sourcing from China</h1>
        <p class="lead">{len(parts)} {esc(cat)} parts in our sourcing catalog.</p>
      </div>
    </section>
    <section class="section">
      <div class="container two-col">
        <div>
          <h2>{esc(cat)} Parts We Source</h2>
          <ul class="bullet-list part-index">{part_links}</ul>
        </div>
        <aside class="part-aside">
          <div class="card">
            <h3>Manufacturers in this category</h3>
            <ul class="alt-list">{mfr_links}</ul>
            <h3>Need a {esc(cat)} part not listed?</h3>
            <p>Send us the exact part number — we'll check Shenzhen availability.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/">Request a Quote</a>
          </div>
        </aside>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
</body>
</html>"""

# ==============================================================================
# INDEX PAGES (manufacturers/ and categories/ hubs)
# ==============================================================================
def gen_hub_page(kind, title, desc, items):
    # kind: "manufacturers" or "categories"
    url = f"{DOMAIN}/{kind}/"
    rows_html = ""
    if kind == "manufacturers":
        for name, parts in sorted(items.items()):
            slug = slugify_name(name)
            rows_html += (f'<li><a href="/manufacturers/{slug}/">{esc(name)}</a> '
                          f'<span class="muted">— {len(parts)} parts</span></li>')
    else:
        for name, parts in sorted(items.items()):
            slug = slugify_name(name)
            rows_html += (f'<li><a href="/categories/{slug}/">{esc(name)}</a> '
                          f'<span class="muted">— {len(parts)} parts</span></li>')
    crumb = breadcrumb_jsonld([("Home", f"{DOMAIN}/"), (title, url)])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
{seo_head(title, desc, url)}
  <link rel="stylesheet" href="/assets/styles.css" />
{crumb}
{org_jsonld()}
</head>
<body>
  <div id="site-header"></div>
  <main>
    <nav class="breadcrumb"><div class="container">
      <a href="/">Home</a> › <span>{esc(title)}</span>
    </div></nav>
    <section class="page-head">
      <div class="container">
        <div class="eyebrow">Directory</div>
        <h1>{esc(title)}</h1>
        <p class="lead">{desc}</p>
      </div>
    </section>
    <section class="section">
      <div class="container">
        <ul class="bullet-list part-index">{rows_html}</ul>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
</body>
</html>"""

# ==============================================================================
# MAIN
# ==============================================================================
def main():
    ap = argparse.ArgumentParser()
    default_csv = os.path.join(ROOT, "data", "sample_parts.csv")
    ap.add_argument("--csv", default=default_csv)
    ap.add_argument("--out", default=ROOT)
    args = ap.parse_args()

    csv_path = os.path.abspath(args.csv)
    out_root = os.path.abspath(args.out)
    if not os.path.exists(csv_path):
        print("CSV not found:", csv_path); sys.exit(1)

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("PN", "").strip()]

    print(f"Loaded {len(rows)} parts from {csv_path}")

    # group by manufacturer / category
    by_mfr = defaultdict(list)
    by_cat = defaultdict(list)
    for r in rows:
        by_mfr[r["Mfr"].strip()].append(r)
        by_cat[r["Category"].strip()].append(r)

    # ---- generate part pages ----
    written = 0
    urls = []
    for r in rows:
        pn = r["PN"].strip()
        slug = slugify(pn)
        if not slug:
            continue
        cat_slug = slugify_name(r["Category"].strip())
        mfr_slug = slugify_name(r["Mfr"].strip())
        d = os.path.join(out_root, "products", slug)
        os.makedirs(d, exist_ok=True)
        page = gen_part_page(r, cat_slug, mfr_slug)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(page)
        urls.append(f"{DOMAIN}/products/{slug}/")
        written += 1

    # ---- generate manufacturer pages ----
    for mfr, parts in by_mfr.items():
        slug = slugify_name(mfr)
        d = os.path.join(out_root, "manufacturers", slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(gen_manufacturer_page(mfr, parts, {}))
        urls.append(f"{DOMAIN}/manufacturers/{slug}/")

    # ---- generate category pages ----
    for cat, parts in by_cat.items():
        slug = slugify_name(cat)
        d = os.path.join(out_root, "categories", slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(gen_category_page(cat, parts, {}))
        urls.append(f"{DOMAIN}/categories/{slug}/")

    # ---- hub index pages ----
    os.makedirs(os.path.join(out_root, "manufacturers"), exist_ok=True)
    with open(os.path.join(out_root, "manufacturers", "index.html"), "w", encoding="utf-8") as f:
        f.write(gen_hub_page("manufacturers", "Manufacturers",
                             "Browse electronic component manufacturers we source from Shenzhen.",
                             by_mfr))
    urls.append(f"{DOMAIN}/manufacturers/")
    os.makedirs(os.path.join(out_root, "categories"), exist_ok=True)
    with open(os.path.join(out_root, "categories", "index.html"), "w", encoding="utf-8") as f:
        f.write(gen_hub_page("categories", "Categories",
                             "Browse electronic component categories we source from Shenzhen.",
                             by_cat))
    urls.append(f"{DOMAIN}/categories/")

    # ---- split sitemap (all generated URLs) ----
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

    # sitemap index
    with open(os.path.join(out_root, "sitemap_parts_index.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for fn in sm_paths:
            f.write(f"  <sitemap><loc>{DOMAIN}/{fn}</loc></sitemap>\n")
        f.write('</sitemapindex>\n')

    # ---- search index (client-side search over generated pages) ----
    # Maps query tokens -> URLs. Kept small: only PN/Mfr/Category + slug URLs.
    # Front-end (search.html) loads this and does prefix/substring matching.
    search_entries = []
    seen = set()
    for r in rows:
        pn = r["PN"].strip()
        mfr = r["Mfr"].strip()
        cat = r["Category"].strip()
        p_slug = slugify(pn)
        m_slug = slugify_name(mfr)
        c_slug = slugify_name(cat)
        # product entries (exact PN match gets priority)
        key_p = ("p", pn.lower())
        if key_p not in seen:
            search_entries.append({"t": pn, "k": pn.lower(), "keys": pn_search_keys(pn),
                                   "ty": "Part", "u": f"/products/{p_slug}/",
                                   "sub": f"{mfr} · {cat}"})
            seen.add(key_p)
        key_m = ("m", mfr.lower())
        if key_m not in seen:
            search_entries.append({"t": mfr, "k": mfr.lower(), "ty": "Manufacturer",
                                   "u": f"/manufacturers/{m_slug}/", "sub": "View all sourced parts"})
            seen.add(key_m)
        key_c = ("c", cat.lower())
        if key_c not in seen:
            search_entries.append({"t": cat, "k": cat.lower(), "ty": "Category",
                                   "u": f"/categories/{c_slug}/", "sub": "Browse category"})
            seen.add(key_c)
    with open(os.path.join(out_root, "search-index.json"), "w", encoding="utf-8") as f:
        f.write('{"entries":')
        f.write(__import__("json").dumps(search_entries, ensure_ascii=False))
        f.write('}')

    print(f"Generated {written} product pages under /products/")
    print(f"Manufacturer pages: {len(by_mfr)} under /manufacturers/")
    print(f"Category pages: {len(by_cat)} under /categories/")
    print(f"Sitemaps: {sm_paths} (+ sitemap_parts_index.xml)")
    print(f"Total indexed URLs this run: {len(urls)}")
    print(f"At 200k scale: ~{(200000+SITEMAP_BATCH-1)//SITEMAP_BATCH} sitemap files, all auto-split.")

if __name__ == "__main__":
    main()
