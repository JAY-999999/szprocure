#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SZ Procure — Part / Manufacturer / Category Page Generator (data-driven, static output)
========================================================================================
Reads a parts CSV and generates static pages under SEMANTIC URL paths:

  /products/{slug}/        one page per part (the core "knowledge node")
  /manufacturers/{slug}/   one page per manufacturer (captures "X distributor China")
  /components/{slug}/      one page per top-level category (6 canonical categories)

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

CSV columns (data/sample_parts.csv) — structured contract schema:
  mpn, clean_mpn, manufacturer, brand, url_slug,
  category, subcategory, description, applications, keywords,
  attributes_json, availability, alternative_parts, datasheet_url,
  faq, image
  (clean_mpn / url_slug are also derived in-code if a row leaves them blank)

  Image  : path to a locally-hosted SVG/PNG symbol image (self-owned, zero copyright risk).
           e.g. /assets/img/mcu.svg  (falls back to hero.svg if empty)
  Source : internal data-curation field ONLY. May point to an external
           reference site used while compiling the catalog. It is
           NEVER rendered on generated pages — SZ Procure does not link out to
           any third-party store. Reference Resources link only to the manufacturer's own site.

Usage:
  python gen_parts.py --csv "path/to/料号库.csv" --out "."
  (defaults: csv = ../芯片/料号库/料号库.csv relative to this script's dir)
"""
import csv, os, re, argparse, html, sys, json
from collections import defaultdict
from urllib.parse import quote as urlquote

ROOT = os.path.dirname(os.path.abspath(__file__))
DOMAIN = "https://www.szprocure.com"
SITEMAP_BATCH = 45000  # urls per sitemap file (Google soft cap 50k)
SEARCH_SHARD_SIZE = 5000  # entries per search shard (keeps each /search/N.json small)

# GA4 Measurement ID — replace with the real one from your GA4 property.
# Format: G-XXXXXXXXXX.
GA4_ID = "G-ZZLJH3Q2KF"

def ga4_script():
    """Google Analytics 4 tracking snippet, injected before </body> on every page.
    Uses the standard gtag.js loader. No PII collected; respects same-origin only."""
    return f"""  <!-- Google Analytics 4 -->
  <script async src="https://www.googletagmanager.com/gtag/js?id={GA4_ID}"></script>
  <script>
    window.dataLayer = window.dataLayer || [];
    function gtag(){{ dataLayer.push(arguments); }}
    gtag('js', new Date());
    gtag('config', '{GA4_ID}');
  </script>"""

STATUS_LABEL = {
    "scarce": ("Long lead-time / hard to source", "scarce"),
    "active": ("Active production", "active"),
    "eol": ("End of life / discontinued", "eol"),
}

# ---- Category mapping: fine-grained CSV category -> 6 top-level /components/ URLs ----
# CSV keeps the fine-grained product subcategory (e.g. "Microcontroller") for on-page
# display and SEO body copy. Breadcrumbs, internal links and category-page grouping
# all resolve to the 6 canonical top-level categories below.
# To add a new part later, just add its fine subcategory here — no CSV schema change.
CATEGORY_MAP = {
    # Integrated Circuits
    "Microcontroller": "integrated-circuits",
    "Microcontrollers": "integrated-circuits",
    "MCU": "integrated-circuits",
    "Memory IC": "integrated-circuits",
    "Memory": "integrated-circuits",
    "Power Management IC": "integrated-circuits",
    "Voltage Regulator": "integrated-circuits",
    "Analog IC": "integrated-circuits",
    "Operational Amplifier": "integrated-circuits",
    "Interface IC": "integrated-circuits",
    "Logic IC": "integrated-circuits",
    # Discrete Semiconductor Components
    "Semiconductor Components": "semiconductor-components",
    "Power MOSFET": "semiconductor-components",
    "MOSFET": "semiconductor-components",
    "Diode": "semiconductor-components",
    "Rectifier Diode": "semiconductor-components",
    "Transistor": "semiconductor-components",
    "IGBT": "semiconductor-components",
    "Rectifier": "semiconductor-components",
    "Thyristor": "semiconductor-components",
    # Passive Components
    "Passive Components": "passive-components",
    "Resistor": "passive-components",
    "Resistors": "passive-components",
    "Capacitor": "passive-components",
    "Capacitors": "passive-components",
    "Electrolytic Capacitor": "passive-components",
    "Inductor": "passive-components",
    "Inductors": "passive-components",
    "Crystal Oscillator": "passive-components",
    "LED Components": "passive-components",
    # Sensors & Transducers
    "Sensors & Transducers": "sensors",
    "Sensors": "sensors",
    "MEMS Sensor": "sensors",
    "Temperature Sensors": "sensors",
    "Pressure Sensors": "sensors",
    "Motion Sensors": "sensors",
    "Optical Sensors": "sensors",
    # Connectors & Electromechanical
    "Connectors & Electromechanical": "connectors",
    "Connectors": "connectors",
    "Pin Header": "connectors",
    "USB Connectors": "connectors",
    "FFC/FPC": "connectors",
    "Board-to-Board": "connectors",
    "Wire Connectors": "connectors",
    "Switches": "connectors",
    # Modules & Communication Modules
    "Modules & Communication Modules": "modules",
    "Modules": "modules",
    "WiFi Modules": "modules",
    "Bluetooth Modules": "modules",
    "RF Modules": "modules",
    "Cellular Modules": "modules",
    "GNSS Modules": "modules",
}
# canonical top-level category slug -> display name (matches /components/ CollectionPage)
TOP_CATEGORIES = {
    "integrated-circuits": "Integrated Circuits",
    "semiconductor-components": "Semiconductor Components",
    "passive-components": "Passive Components",
    "sensors": "Sensors & Transducers",
    "connectors": "Connectors & Electromechanical",
    "modules": "Modules & Communication Modules",
}
DEFAULT_CAT_SLUG = "integrated-circuits"  # fallback for unmapped fine categories

def resolve_cat(fine_cat):
    """Return (top_slug, top_name) for a fine-grained CSV category.
    Falls back to DEFAULT_CAT_SLUG with a warning so batch never dies on a new value."""
    slug = CATEGORY_MAP.get((fine_cat or "").strip())
    if not slug:
        print(f"  [WARN] unmapped Category {fine_cat!r} -> default {DEFAULT_CAT_SLUG}")
        return DEFAULT_CAT_SLUG, TOP_CATEGORIES[DEFAULT_CAT_SLUG]
    return slug, TOP_CATEGORIES[slug]

# ---- manufacturer official websites (for Reference Resources) -----------------
# Only OFFICIAL manufacturer / vendor domains are listed here. These are used to
# link buyers to the manufacturer's own datasheet / technical documentation —
# NEVER to a third-party marketplace. SZ Procure is a sourcing partner, not a
# distributor; we keep the brand neutral and self-contained.
MFR_OFFICIAL = {
    "STMicroelectronics": "https://www.st.com",
    "Texas Instruments": "https://www.ti.com",
    "Analog Devices": "https://www.analog.com",
    "NXP": "https://www.nxp.com",
    "Infineon": "https://www.infineon.com",
    "Microchip": "https://www.microchip.com",
    "ON Semiconductor": "https://www.onsemi.com",
    "Renesas": "https://www.renesas.com",
    "Toshiba": "https://www.toshiba.com",
    "ROHM": "https://www.rohm.com",
    "Diodes Incorporated": "https://www.diodes.com",
    "Fairchild": "https://www.onsemi.com",
    "Maxim Integrated": "https://www.analog.com",
    "Vishay": "https://www.vishay.com",
    "Bourns": "https://www.bourns.com",
    "Murata": "https://www.murata.com",
    "TDK": "https://www.tdk.com",
    "Yageo": "https://www.yageo.com",
    "KEMET": "https://www.kemet.com",
    "Panasonic": "https://www.panasonic.com",
    "Samsung Electro-Mechanics": "https://www.samsungsem.com",
    "TE Connectivity": "https://www.te.com",
    "Molex": "https://www.molex.com",
    "Amphenol": "https://www.amphenol.com",
    "Omron": "https://www.omron.com",
    "Bosch": "https://www.bosch.com",
    "InvenSense": "https://www.invensense.com",
}

# ---- Popular Components map (display PN -> real product slug) -----------------
# The hub page (components/index.html) hard-codes a few "Popular Components"
# cards. Their displayed model numbers do NOT always equal the generated slug
# (e.g. "LM358" -> slug "lm358dr", "AMS1117-3.3" -> "ams111733"). This map is
# the single source of truth so we never guess the slug from the model string.
# A model with no entry (or whose slug is not generated yet) falls back to
# /request-a-quote/ — never a 404.
POPULAR_SKU_MAP = {
    "STM32F103C8T6": "stm32f103c8t6",
    "LM358":          "lm358dr",
    "AMS1117-3.3":    "ams111733",
    "LM2596":         "lm2596",   # slug reserved; falls back to /request-a-quote/ until generated
}

def popular_href(model: str, generated_slugs=None) -> str:
    """Return the correct href for a Popular Components card."""
    slug = POPULAR_SKU_MAP.get(model)
    if slug and (generated_slugs is None or slug in generated_slugs):
        return f"/products/{slug}/"
    return "/request-a-quote/"

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

def parse_faq(raw, pn=""):
    """Parse FAQ column into list of (question, answer).
    Format: Q: question?A: answer;  Q: q2?A: a2
    Falls back to a default procurement FAQ (model-aware) when empty."""
    pairs = []
    if raw:
        for chunk in re.split(r"Q\s*:", raw):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "?" in chunk and "A:" in chunk:
                q_part, a_part = chunk.split("?", 1)
                a_part = a_part.split("A:", 1)[1] if "A:" in a_part else a_part
                pairs.append((q_part.strip(), a_part.strip()))
    if not pairs:
        pn_disp = pn or "this part"
        pairs = [
            (f"Where can I buy {pn_disp} from China?",
             f"SZ Procure helps overseas buyers source {pn_disp} from Shenzhen electronics suppliers. Send the part number and quantity for a quote."),
            (f"Can SZ Procure supply hard-to-find {pn_disp}?",
             f"Yes. We support shortage and end-of-life components through our Shenzhen supplier network. Tell us your requirement."),
        ]
    return pairs

def render_faq(pairs, pn):
    """Return (html_block, FAQ JSON-LD script)."""
    items_html = ""
    ld_items = []
    for i, (q, a) in enumerate(pairs, 1):
        items_html += f'<div class="faq-item"><h3>{esc(q)}</h3><p>{esc(a)}</p></div>'
        ld_items.append(
            f'    {{ "@type": "Question", "name": "{esc(q)}", '
            f'"acceptedAnswer": {{ "@type": "Answer", "text": "{esc(a)}" }} }}'
        )
    body = ",\n".join(ld_items)
    ld = f"""
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "FAQPage",
    "mainEntity": [
{body}
    ]
  }}
  </script>"""
    return items_html, ld

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
# PART PAGE — V2 (procurement landing page, not datasheet)
# ==============================================================================
# ==============================================================================
# MANUFACTURER HUB — data-driven commercial directory (Phase D.3)
# Every brand card is a full <a> (no dead <div> wrappers). SEO head strings are
# FROZEN (Phase D.3 lock) and passed verbatim to seo_head() — do NOT alter.
# ==============================================================================
def gen_manufacturers_hub(by_mfr):
    url = f"{DOMAIN}/manufacturers/"
    # FROZEN SEO head strings — locked by Phase D.3 freeze layer. Do not change.
    title = "Manufacturers"
    desc = "Browse electronic component manufacturers we source from Shenzhen."
    # brand cards — every card fully wrapped in <a>, 100% clickable
    cards = []
    for name in sorted(by_mfr.keys()):
        slug = slugify_name(name)
        n = len(by_mfr[name])
        cards.append(f'''        <a class="card mfr-card" href="/manufacturers/{slug}/">
          <h3>{esc(name)}</h3>
          <p>{n} components in our sourcing catalog.</p>
          <span class="mfr-link">View Sourced Parts &rarr;</span>
        </a>''')
    cards_html = "\n".join(cards)
    # full directory list
    dir_items = "\n          ".join(
        f'<li><a href="/manufacturers/{slugify_name(m)}/">{esc(m)}</a></li>'
        for m in sorted(by_mfr.keys())
    )
    crumb = breadcrumb_jsonld([("Home", f"{DOMAIN}/"), ("Manufacturers", url)])
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
    <!-- HERO Type B (navy commercial hero) -->
    <section class="comp-hero">
      <div class="container">
        <div class="eyebrow" data-zh="可信元器件制造商">TRUSTED COMPONENT MANUFACTURERS</div>
        <h1 data-zh="我们采购的可靠元器件制造商">Trusted Component Manufacturers We Source From</h1>
        <p class="lead" data-zh="我们为全球买家信赖的品牌，通过已验证的供应渠道采购原装元器件。浏览我们支持的制造商目录并发起询价。">We source original components from verified supply channels for the brands global buyers rely on. Browse our supported manufacturer catalog and request a quote for any part.</p>
        <div class="hero-actions">
          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
          <a class="btn btn-outline-light btn-lg" href="#brands" data-zh="浏览品牌">Browse Brands</a>
        </div>
        <div class="trust-bar">
          <span><b>&#10003;</b> <span data-zh="已验证供应渠道">Verified Supply Channels</span></span>
          <span><b>&#10003;</b> <span data-zh="原装元器件">Original Components</span></span>
          <span><b>&#10003;</b> <span data-zh="全球买家支持">Global Buyer Support</span></span>
          <span><b>&#10003;</b> <span data-zh="快速报价响应">Fast RFQ Response</span></span>
        </div>
      </div>
    </section>

    <!-- BRAND GRID -->
    <section class="section" id="brands">
      <div class="container">
        <div class="section-head">
          <div class="eyebrow" data-zh="支持的制造商">SUPPORTED MANUFACTURERS</div>
          <h2 data-zh="我们采购的品牌">Brands We Source</h2>
          <p class="lead" data-zh="点击任意制造商，查看我们采购的元器件并发起询价。">Click any manufacturer to view sourced components and request a quote.</p>
        </div>
        <div class="grid grid-4">
{cards_html}
        </div>
        <p class="muted small" data-zh="以上品牌为其各自所有者之商标，仅用于说明我们支持的采购范围。">Brand names are trademarks of their respective owners, listed to indicate the sourcing range we support.</p>
      </div>
    </section>

    <!-- FULL DIRECTORY -->
    <section class="section soft">
      <div class="container">
        <div class="section-head">
          <div class="eyebrow" data-zh="完整目录">FULL DIRECTORY</div>
          <h2 data-zh="全部支持的制造商">All Supported Manufacturers</h2>
        </div>
        <ul class="bullet-list part-index">
          {dir_items}
        </ul>
      </div>
    </section>

    <!-- FINAL CTA -->
    <section class="section soft">
      <div class="container">
        <div class="cta-band">
          <div>
            <h2 data-zh="需要特定品牌的料号？">Need a Part From a Specific Manufacturer?</h2>
            <p data-zh="发送准确的料号与制造商，我们将核对库存并报价。">Send us the exact part number and manufacturer — we'll check availability and quote.</p>
          </div>
          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
        </div>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
{ga4_script()}
</body>
</html>"""


def gen_part_page(row, cat_slug, mfr_slug, related=None, generated_slugs=None):
    pn = row["mpn"].strip()
    mfr = row["manufacturer"].strip()
    cat = row["category"].strip()
    subcat = (row.get("subcategory") or "").strip()
    specs_raw = (row.get("attributes_json") or "").strip()
    apps = (row.get("applications") or "").strip()
    alt_raw = (row.get("alternative_parts") or "").strip()
    supply = (row.get("availability") or "").strip()
    faq_raw = (row.get("faq") or "").strip()
    img = (row.get("image") or "").strip()
    dsheet = (row.get("datasheet_url") or "").strip()
    # Derived contract fields (kept explicit in CSV, but safe to recompute)
    clean_mpn = (row.get("clean_mpn") or "").strip() or re.sub(r"[^A-Z0-9]", "", pn.upper())
    url_slug = (row.get("url_slug") or "").strip() or slugify(pn)
    # NOTE: `Source` column (CSV) is for internal data curation only — it may
    # point to an external reference site. We NEVER render it on the page.
    # SZ Procure is a sourcing partner, not a distributor, so SKU pages must
    # not link out to any third-party store.

    slug = url_slug
    # ---- same-category cross-links (product spider-web) ----
    # `related` is precomputed upstream by build_related_map() in O(n) total
    # (replaces the old O(n^2) per-page scan over all_rows).
    related = related or []
    url = f"{DOMAIN}/products/{slug}/"
    img_url = img if img else "/assets/img/hero.svg"
    og_img = f"{DOMAIN}{img_url}" if img_url.startswith("/") else img_url

    # Resolve fine category -> 6 top-level /components/ URL (breadcrumbs & links)
    cat_slug, cat_top = resolve_cat(cat)

    # ---- SEO copy: procurement language, Shenzhen/China sourcing keywords ----
    # Lead / overview emphasizes the BUYING scenario (global procurement from
    # Shenzhen supply chain), not just a spec description of the part.
    # P0-3: the VISIBLE Product Overview now prefers the REAL description from the
    # CSV. Only when it is blank do we fall back to the procurement template.
    # The meta `desc` and the Product JSON-LD `description` below stay unchanged
    # (URL / Title / Meta / Schema / H1 are frozen).
    fallback_overview = (f"{esc(pn)} is a {esc(subcat or cat).lower()} from {esc(mfr)}. "
                         f"SZ Procure helps global buyers source this part through verified suppliers, "
                         f"with flexible quantity, hard-to-find support and competitive quotes.")
    desc_csv = (row.get("description") or "").strip()
    overview = esc(desc_csv) if desc_csv else fallback_overview
    # Structured-data description is kept frozen (Schema unchanged).
    schema_overview = fallback_overview
    title = f"{esc(pn)} {esc(mfr)} — Source from Shenzhen, China | SZ Procure"
    desc = (f"Source {esc(pn)} ({esc(mfr)} {esc(cat).lower()}) from Shenzhen, China. "
            f"Shenzhen supplier network, hard-to-find support and BOM procurement for global buyers.")

    # ---- parse repeatable fields ----
    # Filter alternates: keep only tokens that yield a non-empty slug (real part
    # numbers). Drops junk like "-" so we never emit alternatePart:["-"] in schema.
    alts = [a for a in split_multi(alt_raw) if slugify(a)]
    apps_list = split_multi(apps)
    faq_pairs = parse_faq(faq_raw, pn)

    # ---- structured attribute extraction ----
    # attributes_json (object or array) is the canonical spec source. Falls back
    # to infer_spec_key() when a row ships a plain comma string instead of JSON.
    def infer_spec_key(val: str) -> str:
        """Map a bare descriptive spec value to a real attribute name.
        Never invents values — only derives the field label from known
        semiconductor phrasing. Unmatched values fall back to 'Specification'."""
        s = val.strip()
        low = s.lower()
        # Processor core
        if any(t in low for t in ("cortex", "arm", "-bit", "mcu", "risc-v", "riscv", "dsp")):
            return "Core"
        # Clock speed
        if "hz" in low and any(t in low for t in ("mhz", "ghz", "khz", ".")):
            return "Clock Speed"
        # Program memory
        if "flash" in low or "eeprom" in low or "rom" in low:
            return "Program Memory"
        # RAM / data memory
        if "ram" in low or ("kb" in low and "flash" not in low):
            return "RAM"
        # Package / footprint
        if any(t in low for t in ("lqfp", "qfp", "sot", "soic", "tssop", "to-", "qfn",
                                   "hc-", "0805", "0603", "1206", "radial", "sod",
                                   "dip", "pitch", "qfp", "bga", "dfn", "sop")):
            return "Package"
        # Channel type (MOSFET / transistor)
        if "channel" in low or "n-channel" in low or "p-channel" in low:
            return "Channel"
        # Output current / current rating
        if "a" in low and any(t in low for t in ("output", "ma", "a ", "amp", "33a", "1a")):
            return "Output Current"
        # Current rating (bare number + A)
        if "a" in low and any(ch.isdigit() for ch in low):
            return "Current Rating"
        # Voltage (bare number + V, or explicit supply/dropout/voltage)
        if "v" in low and any(t in low for t in ("v", "voltage", "supply", "dropout", "v fixed", " ldo")):
            return "Voltage"
        if any(ch.isdigit() for ch in low) and "v" in low:
            return "Voltage"
        # Tolerance (resistor / capacitor %)
        if "%" in low:
            return "Tolerance"
        # Resistance
        if "ohm" in low or "ω" in low or ("k" in low and "o" in low):
            return "Resistance"
        # Capacitance (must check before Voltage — "100uF" contains 'u' not 'v')
        if "uf" in low or "pf" in low or "nf" in low or "capacitor" in low or "farad" in low:
            return "Capacitance"
        # Power rating (W)
        if "w" in low and any(t in low for t in ("0.", "w", "watt")):
            return "Power Rating"
        # Interface (communication bus)
        if any(t in low for t in ("i2c", "spi", "uart", "can bus", "usb", "interface")):
            return "Interface"
        # Configuration / pin layout
        if any(t in low for t in ("x", "pin", "male", "female", "position", "2x4", "pitch")):
            return "Configuration"
        # Technology / construction
        if any(t in low for t in ("electrolytic", "ceramic", "film", "tantalum", "thick-film",
                                   "switching", "ldo", "regulator", "op-amp", "gyro", "accel")):
            return "Type"
        # Frequency (crystal / oscillator)
        if "ppm" in low or "load" in low or ("mhz" in low and "ghz" not in low and "khz" not in low):
            return "Frequency"
        # Generic amplifier / sensor type
        if any(t in low for t in ("op-amp", "op amp", "gyro", "accel", "sensor", "ldo", "regulator")):
            return "Type"
        return "Specification"

    spec_pairs = []
    if specs_raw:
        try:
            obj = json.loads(specs_raw)
            if isinstance(obj, dict):
                spec_pairs = [[k, str(v)] for k, v in obj.items()]
            elif isinstance(obj, list):
                spec_pairs = [[str((a.get("k") if isinstance(a, dict) else (a[0] if isinstance(a, (list, tuple)) else a))),
                               str((a.get("v") if isinstance(a, dict) else (a[1] if isinstance(a, (list, tuple)) and len(a) > 1 else "")))] for a in obj]
        except Exception:
            for token in split_specs(specs_raw):
                if ":" in token:
                    k, v = token.split(":", 1)
                    spec_pairs.append([k.strip(), v.strip()])
                else:
                    spec_pairs.append([infer_spec_key(token), token.strip()])

    # ---- render blocks ----
    # 3. Technical Specifications table (Item | Value) — for Google entity
    # understanding. Guarantee at least 8 core fields by backfilling standard
    # semiconductor attributes (we never invent values — unknown → "See datasheet").
    # This block lives BELOW the fold (section 3), never in the first screen.
    STANDARD_FIELDS = [
        "Package", "Mounting Type", "Operating Temperature",
        "Supply Voltage", "Operating Current", "Pin Count",
        "RoHS", "Status",
    ]
    if not spec_pairs:
        # No structured attributes from source — show an honest empty-state
        # note instead of 8 placeholder "See datasheet" rows (P1-3).
        specs_html = (
            '<div class="spec-empty">'
            '<p>Detailed specifications and the official datasheet are available on request. '
            'Send the part number and our team will provide the full parameter table and documentation.</p>'
            '</div>'
        )
    else:
        filled_keys = {k for k, _ in spec_pairs}
        backfill = [(k, "See datasheet") for k in STANDARD_FIELDS if k not in filled_keys]
        all_spec_pairs = spec_pairs + backfill
        all_spec_pairs = all_spec_pairs[:12]  # cap, real specs first
        specs_table = "".join(
            f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in all_spec_pairs
        )
        specs_html = f'<table class="spec-table">\n<tbody>\n{specs_table}</tbody>\n</table>'

    # 1. Key Information table — lean, no stock/inventory wording
    qi_rows = []
    qi_rows.append(("Manufacturer", f'<a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a> <span class="muted small">Verified sourcing partner</span>'))
    qi_rows.append(("Category", f'<a href="/components/{cat_slug}/">{esc(cat_top)}</a>'))
    if subcat and subcat.lower() != cat.lower():
        qi_rows.append(("Type", esc(subcat)))
    for k, v in spec_pairs:
        if k in ("Package", "Core"):
            qi_rows.append((k, esc(v)))
    # Supply field uses procurement language, never "stock"
    qi_rows.append(("Sourcing Availability", "Verified supplier network"))
    qi_rows.append(("Procurement Support", "Quote by quantity &amp; target price"))
    quick_info = "".join(f"<tr><th>{esc(k)}</th><td>{v}</td></tr>" for k, v in qi_rows)

    # Alternative Parts links: point to the real SKU page when it exists,
    # otherwise fall back to Request-a-Quote (never a 404 dead link).
    alts_html_items = []
    for a in alts:
        aslug = slugify(a)
        if generated_slugs and aslug in generated_slugs:
            alts_html_items.append(
                f'<li><a href="/products/{aslug}/" class="alt-link">{esc(a)}</a></li>')
        else:
            alts_html_items.append(
                f'<li><a href="/request-a-quote/?pn={esc(a)}" class="alt-link">{esc(a)} '
                f'<span class="muted">(request quote)</span></a></li>')
    alts_html = "".join(alts_html_items) or \
        "<li>No direct alternates listed — contact us for cross-reference.</li>"
    apps_html = "".join(f"<li>{esc(x)}</li>" for x in apps_list) or "<li>—</li>"

    # 4. Sourcing Information — FIXED template (our sourcing moat, no Shenzhen drift)
    sourcing_html = f"""<p>SZ Procure is a sourcing partner for <strong>{esc(pn)}</strong> — not a stock catalog. We help global buyers access China's electronics supply chain for original components.</p>
      <dl class="sourcing-facts">
        <div><dt>Availability</dt><dd>Sourced through our verified supply network</dd></div>
        <div><dt>MOQ</dt><dd>Flexible MOQ depending on part availability</dd></div>
        <div><dt>Lead Time</dt><dd>Typical 3-15 working days</dd></div>
        <div><dt>Authenticity</dt><dd>Original components from verified supply channels</dd></div>
        <div><dt>Support</dt><dd>Global procurement and shipping support</dd></div>
      </dl>
      <p>Send your quantity and target price for a quotation.</p>"""

    # FAQ block + FAQ schema
    faq_html, faq_jsonld = render_faq(faq_pairs, pn)

    # Related Products (same top-category) — internal links form a product web.
    if related:
        rel_items = "".join(
            f'<li><a href="/products/{oslug}/" class="alt-link">{esc(opn)}</a></li>'
            for opn, oslug in related
        )
        related_html = (f'<h2>Related {esc(cat_top)}</h2>'
                        f'<p>Other {esc(cat_top).lower()} we help global buyers source:</p>'
                        f'<ul class="alt-list">{rel_items}</ul>')
    else:
        related_html = ""

    # Reference Resources — links ONLY to the manufacturer's OWN official
    # documentation (datasheet / technical resources). We never link to a
    # third-party marketplace. If we don't have the manufacturer's official
    # site mapped, we show a neutral note instead of a store link.
    # Datasheet — show ONLY when a real datasheet URL exists (Phase B: honest
    # degradation, never an empty link). When present, surface it both as a
    # sticky-card button and inside Reference Resources.
    datasheet_html = ""
    if dsheet:
        datasheet_html = (
            f'<a class="btn btn-ghost btn-block" href="{esc(dsheet)}" '
            f'target="_blank" rel="nofollow noopener">Download Datasheet ↧</a>'
        )
    dsheet_li = ""
    if dsheet:
        dsheet_li = (
            f'<li><a href="{esc(dsheet)}" target="_blank" rel="nofollow noopener">'
            f'{esc(pn)} Datasheet (PDF) ↧</a></li>'
        )

    ref_block = ""
    mfr_official = MFR_OFFICIAL.get(mfr)
    if mfr_official:
        ref_block = (
            f'<div class="reference-resources">'
            f'<h3>Reference Resources</h3>'
            f'<ul class="alt-list">'
            f'<li><a href="{esc(mfr_official)}" target="_blank" rel="nofollow noopener">'
            f'{esc(mfr)} Official Website ↗</a></li>'
            f'{dsheet_li}'
            f'</ul>'
            f'<p class="muted small">Reference only — specifications &amp; images '
            f'© {esc(mfr)}. SZ Procure is an independent sourcing partner, not the distributor.</p>'
            f'</div>')
    else:
        ref_block = (
            f'<div class="reference-resources">'
            f'<h3>Reference Resources</h3>'
            f'{dsheet_li}'
            f'<p>For the official {esc(mfr)} datasheet and technical documentation, '
            f'visit the manufacturer\'s website. SZ Procure sources this part through '
            f'our supply chain — we are an independent sourcing partner, not a distributor.</p>'
            f'</div>')

    # breadcrumb: Home > Components > Category > Sub Category > MPN
    # Adds the fine/sub-category (L3) level. The L3 page /components/<top>/<fine>/
    # is generated for every fine category that has >=1 SKU (main loop), so the
    # link always resolves (never a dead link). Uses `cat` (the `category` field)
    # which is the authoritative L3 key — NOT `subcat`.
    fine_slug = slugify_name(cat) if cat else ""
    sub_crumb = f'<a href="/components/{cat_slug}/{fine_slug}/">{esc(cat)}</a> › ' if cat else ""
    crumb_items = [
        ("Home", f"{DOMAIN}/"),
        ("Components", f"{DOMAIN}/components/"),
        (cat_top, f"{DOMAIN}/components/{cat_slug}/"),
    ]
    if cat:
        crumb_items.append((cat, f"{DOMAIN}/components/{cat_slug}/{fine_slug}/"))
    crumb_items.append((pn, url))
    crumb = breadcrumb_jsonld(crumb_items)
    # Product JSON-LD — core fields only. NO price / NO availability / NO offers:
    # we are a sourcing partner, not a stock catalog — inventory fields would mislead Google.
    alt_ld = ", ".join(f'"{esc(a)}"' for a in alts)
    product_jsonld = f"""
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "{esc(pn)}",
    "model": "{esc(pn)}",
    "category": "{esc(cat_top)}",
    "brand": {{ "@type": "Brand", "name": "{esc(mfr)}" }},
    "description": "{esc(schema_overview)}",
    "url": "{url}"{(", \"alternatePart\": [" + alt_ld + "]") if alt_ld else ""}
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
{faq_jsonld}
{org_jsonld()}
</head>
<body>
  <div id="site-header"></div>
  <main>
    <nav class="breadcrumb"><div class="container">
      <a href="/">Home</a> ›
      <a href="/components/">Components</a> ›
      <a href="/components/{cat_slug}/">{esc(cat_top)}</a> ›
      {sub_crumb}<span>{esc(pn)}</span>
    </div></nav>

    <!-- 1. Product Header (procurement landing — above the fold, lean) -->
    <section class="page-head part-head">
      <div class="container part-head-grid">
        <div class="part-head-main">
          <div class="sku-badge">Sourcing Partner</div>
          <div class="eyebrow"><a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a> · {esc(subcat or cat)}</div>
          <h1>{esc(pn)}</h1>
          <p class="lead-sub">{esc(mfr)} {esc(subcat or cat)}</p>
          <p class="lead">Source {esc(pn)} — we help global buyers access this part through verified suppliers with flexible quantity and competitive pricing.</p>
          <div class="part-head-actions">
            <a class="btn btn-primary btn-lg" href="/request-a-quote/?pn={esc(pn)}&mfr={esc(mfr)}&cat={esc(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
            <a class="btn btn-outline" href="https://wa.me/8613530888389?text=Hi%20SZ%20Procure,%20I%20need%20{esc(pn)}">WhatsApp</a>
            <a class="link-cta" href="mailto:sales@szprocure.com?subject=Quote%20for%20{esc(pn)}">Email</a>
          </div>
          <div class="trust-bar sku-trust">
            <span><b>✓</b> Verified sourcing network</span>
            <span><b>✓</b> Hard-to-find &amp; EOL support</span>
            <span><b>✓</b> Small-batch sourcing</span>
            <span><b>✓</b> China supply ecosystem</span>
          </div>
        </div>
        <aside class="part-head-aside">
          <div class="card key-info">
            <h2 class="quick-info-title">Key Information</h2>
            <table class="spec-table compact"><tbody>
{quick_info}
            </tbody></table>
          </div>
        </aside>
      </div>
    </section>

    <!-- Why source from SZ Procure (above the fold, procurement-first) -->
    <section class="section sku-why">
      <div class="container">
        <div class="section-head">
          <div class="eyebrow">Why source from SZ Procure</div>
          <h2>More than a parts catalog</h2>
        </div>
        <div class="cap-grid">
          <div class="cap-card">
            <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg></div>
            <h3>Hard-to-Find &amp; EOL</h3>
            <p>Obsolete and hard-to-source parts other distributors can't supply — sourced through China's electronics ecosystem.</p>
          </div>
          <div class="cap-card">
            <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l9 5-9 5-9-5 9-5z"/><path d="M3 13l9 5 9-5"/></svg></div>
            <h3>Flexible MOQ</h3>
            <p>Small-batch and prototype quantities supported, not just volume orders.</p>
          </div>
          <div class="cap-card">
            <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3z"/><path d="M9 12l2 2 4-4"/></svg></div>
            <h3>Verified Network</h3>
            <p>Original components from verified supply channels, with documentation on request.</p>
          </div>
        </div>
      </div>
    </section>

    <!-- Mobile-only quote card (after first screen, no fixed overlay) -->
    <section class="section mobile-quote-only">
      <div class="container">
        <div class="card sticky-card">
          <h3>Need this component?</h3>
          <p>Send the part number and quantity.</p>
          <a class="btn btn-primary btn-block" href="/request-a-quote/?pn={esc(pn)}&mfr={esc(mfr)}&cat={esc(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
          {datasheet_html}
          <p class="muted small">sales@szprocure.com · WhatsApp</p>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container two-col">
        <div class="part-main">
          <!-- 2. Product Overview (SEO, not encyclopedia) -->
          <h2>Product Overview</h2>
          <p>{overview}</p>

          <!-- 3. Technical Specifications -->
          <h2>Technical Specifications</h2>
          {specs_html}

          <!-- 4. Sourcing Information (the moat) -->
          <h2>Sourcing Information</h2>
          {sourcing_html}

          <!-- 5. Alternative Parts (SEO long-tail) -->
          <h2>Alternative Parts</h2>
          <p>Common <strong>{esc(pn)} alternatives</strong> overseas buyers search for:</p>
          <ul class="alt-list">{alts_html}</ul>
          <p class="muted small">Looking for "{esc(pn)} alternative"? Tell us your requirement in the quote form.</p>

          <!-- 5b. Related Products (same-category spider-web) -->
          {related_html}

          <!-- 6. FAQ (SEO + conversion) -->
          <h2>Frequently Asked Questions</h2>
          {faq_html}
          {ref_block}
        </div>

        <aside class="part-aside">
          <!-- Sticky Quote Card (desktop) -->
          <div class="card sticky-card desk-sticky">
            <h3>Need this component?</h3>
            <p>Send the part number and quantity.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/?pn={esc(pn)}&mfr={esc(mfr)}&cat={esc(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
            {datasheet_html}
            <p class="muted small">sales@szprocure.com<br/>WhatsApp</p>
          </div>

          <!-- Related Categories -->
          <div class="card">
            <h3>Related</h3>
            <ul class="alt-list">
              <li><a href="/components/{cat_slug}/">{esc(cat_top)}</a></li>
              <li><a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a></li>
            </ul>
          </div>
        </aside>
      </div>
    </section>

    <!-- Bottom conversion CTA -->
    <section class="section cta-band">
      <div class="container">
        <h2>Request a Quote for {esc(pn)}</h2>
        <p>Send your quantity and target price — our sourcing team will check availability, pricing and lead time.</p>
        <a class="btn btn-primary btn-lg" href="/request-a-quote/?pn={esc(pn)}&mfr={esc(mfr)}&cat={esc(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
{ga4_script()}
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
        f'<li><a href="/products/{p.get("url_slug") or slugify(p["mpn"])}/">{esc(p["mpn"])}</a> '
        f'<span class="muted">— {esc(p["category"])}</span></li>'
        for p in sorted(parts, key=lambda x: x["mpn"])
    )
    # related categories for this manufacturer (resolve fine -> top slug)
    cat_links = "".join(
        f'<li><a href="/components/{resolve_cat(c)[0]}/">{esc(c)}</a></li>'
        for c in sorted({p["category"] for p in parts})
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
        <p class="lead">{len(parts)} {esc(mfr)} components &amp; ICs in our sourcing catalog. Send us a part number and we'll check availability across verified supply channels.</p>
        <div class="sku-badge" data-zh="认证供应伙伴">Verified Sourcing Partner</div>
        <div class="hero-cta">
          <a class="btn btn-primary btn-lg" href="/request-a-quote/?mfr={urlquote(mfr)}&source=manufacturer&rfq_type=mfr_quote" data-zh="获取报价">Request a Quote</a>
          <a class="btn btn-outline btn-lg" href="https://wa.me/8613530888389" target="_blank" rel="noopener" data-zh="WhatsApp">WhatsApp</a>
          <a class="link-cta" href="mailto:sales@szprocure.com" data-zh="发邮件">Email</a>
        </div>
        <div class="trust-bar">
          <span><b>&#10003;</b> <span data-zh="原装元器件">Original Components</span></span>
          <span><b>&#10003;</b> <span data-zh="已验证供应渠道">Verified Supply Channels</span></span>
          <span><b>&#10003;</b> <span data-zh="难找料与停产料支持">Hard-to-Find &amp; EOL Support</span></span>
          <span><b>&#10003;</b> <span data-zh="快速报价响应">Fast RFQ Response</span></span>
        </div>
      </div>
    </section>

    <!-- WHY SOURCE THIS MANUFACTURER -->
    <section class="section soft">
      <div class="container">
        <div class="section-head">
          <div class="eyebrow" data-zh="为何采购此品牌">WHY SOURCE THIS MANUFACTURER</div>
          <h2 data-zh="为何从我们采购该品牌">Why Source {esc(mfr)} From Us</h2>
          <p class="lead" data-zh="我们帮助全球买家通过已验证的供应渠道获取 {esc(mfr)} 元器件。">We help global buyers access {esc(mfr)} components through verified supply channels.</p>
        </div>
        <div class="cap-grid">
          <div class="cap-card">
            <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2l8 3v6c0 5-3.5 8.5-8 11-4.5-2.5-8-6-8-11V5z"/><path d="M9 12l2 2 4-4"/></svg></div>
            <h3 data-zh="原装元器件">Original Components</h3>
            <p data-zh="我们通过已验证的供应渠道采购原装器件，而非翻新或假冒库存。">We source genuine parts from verified supply channels — not refurbished or counterfeit stock.</p>
          </div>
          <div class="cap-card">
            <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M5.6 5.6l12.8 12.8"/></svg></div>
            <h3 data-zh="难找料与停产料">Hard-to-Find &amp; EOL</h3>
            <p data-zh="传统分销商难以提供的 {esc(mfr)} 停产与稀缺器件。">Discontinued and scarce {esc(mfr)} parts that traditional distributors struggle to supply.</p>
          </div>
          <div class="cap-card">
            <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M7 7h7l4 5-4 5H7z"/><path d="M14 12h7"/></svg></div>
            <h3 data-zh="替代料匹配">Replacement Matching</h3>
            <p data-zh="当原型号缺货时，我们提供合格替代与交叉参考。">When the original is unavailable, we identify qualified alternates and cross-references.</p>
          </div>
          <div class="cap-card">
            <div class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13 2L3 14h7l-1 8 10-12h-7z"/></svg></div>
            <h3 data-zh="快速报价响应">Fast RFQ Response</h3>
            <p data-zh="一个工作日内回复报价，让项目持续推进。">Quote response within one business day to keep your projects moving.</p>
          </div>
        </div>
      </div>
    </section>

    <!-- SOURCED COMPONENTS -->
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
            <p>Send us the exact part number and we'll check availability across our supply channels.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/?mfr={urlquote(mfr)}&source=manufacturer&rfq_type=mfr_quote" data-zh="获取报价">Request a Quote</a>
          </div>
        </aside>
      </div>
    </section>

    <!-- FINAL CTA -->
    <section class="section soft">
      <div class="container">
        <div class="cta-band">
          <div>
            <h2 data-zh="有未列出的该品牌料号？">Need a {esc(mfr)} Part Not Listed?</h2>
            <p data-zh="发送准确的料号，我们将通过已验证的供应渠道为您采购。">Send us the exact part number and we'll source it through verified supply channels.</p>
          </div>
          <a class="btn btn-primary btn-lg" href="/request-a-quote/?mfr={urlquote(mfr)}&source=manufacturer&rfq_type=mfr_quote" data-zh="获取报价">Request a Quote</a>
        </div>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
{ga4_script()}
</body>
</html>"""

# ==============================================================================
# CATEGORY PAGE
# ==============================================================================
# ==============================================================================
# COMPONENT CATEGORY PAGE  (/components/<top-slug>/)
# SEO entry + category navigation + procurement conversion. Groups SKUs that
# resolve (via CATEGORY_MAP) to this top-level category.
# ==============================================================================
def gen_component_category_page(cat_slug, cat_name, parts, all_rows=None, by_cat=None):
    url = f"{DOMAIN}/components/{cat_slug}/"
    n = len(parts)
    title = f"{esc(cat_name)} Sourcing from Shenzhen, China | SZ Procure"
    desc = (f"Source {esc(cat_name)} from Shenzhen. Browse {n} "
            f"{esc(cat_name).lower()} we help global buyers procure — alternates, "
            f"lead-time and quote support.")
    # ---- 1. Category Introduction (procurement framing) ----
    intro = (f"<p>{esc(cat_name)} are core building blocks for electronics manufacturing. "
             f"SZ Procure helps global buyers source {esc(cat_name).lower()} from the "
             f"Shenzhen supply chain — covering popular families, hard-to-find versions and "
             f"BOM consolidation with verified suppliers and competitive quotes.</p>")
    # ---- 2. Subcategories (fine categories actually present in this top category) ----
    # Phase 2.7 (A): data-driven from the SKUs in `parts` (not CATEGORY_MAP) and links
    # to the precise L3 page /components/<l2>/<l3>/ — no RFQ dead-end.
    sub_fine = sorted({p.get("category", "").strip() for p in parts if p.get("category", "").strip()})
    sub_links = "".join(
        f'<li><a href="/components/{esc(cat_slug)}/{esc(slugify_name(fine))}/">{esc(fine)}</a></li>'
        for fine in sub_fine
    ) or f'<li><a href="/request-a-quote/" data-zh="获取报价">Request a Quote</a></li>'
    # ---- 3. Popular Components (first up to 12 SKUs in this category) ----
    popular = sorted(parts, key=lambda x: x["mpn"])[:12]
    pop_links = "".join(
        f'<li><a href="/products/{p.get("url_slug") or slugify(p["mpn"])}/">{esc(p["mpn"])}</a> '
        f'<span class="muted">— {esc(p.get("manufacturer","").strip())}</span></li>'
        for p in popular
    )
    # ---- 4. Manufacturers in this category ----
    mfrs = sorted({p.get("manufacturer", "").strip() for p in parts if p.get("manufacturer", "").strip()})
    mfr_links = "".join(
        f'<li><a href="/manufacturers/{slugify_name(m)}/">{esc(m)}</a></li>' for m in mfrs
    ) or f'<li><a href="/request-a-quote/" data-zh="获取报价">Request a Quote</a></li>'
    # ---- 3b. Featured Parts (Phase D.4: SKU internal-linking boost) ----
    # Data-driven grid of up to 16 real SKUs in this category, each linking to
    # its product page. Capped to keep pages lean at 200k scale.
    featured = sorted(parts, key=lambda x: x["mpn"])[:16]
    feat_cards = "".join(
        f'<a class="card sku-card" href="/products/{p.get("url_slug") or slugify(p["mpn"])}/">'
        f'<div class="sku-mpn">{esc(p["mpn"])}</div>'
        f'<div class="sku-mfr">{esc(p.get("manufacturer","").strip())}</div></a>'
        for p in featured
    )
    # ---- 3c. Related Products by Category (Phase D.4: cross-category linking) ----
    # Surfaces a few SKUs from sibling top categories so products are not
    # isolated within one category. Data-driven via by_cat (no hand-written pages).
    rel_html = ""
    if by_cat:
        sibs = [(s2, n2) for s2, n2 in TOP_CATEGORIES.items() if s2 != cat_slug]
        rel_cards = []
        for s2, n2 in sibs:
            sample = sorted(by_cat.get(s2, []), key=lambda x: x["mpn"])[:3]
            if not sample:
                continue
            skus = "".join(
                f'<li><a href="/products/{p.get("url_slug") or slugify(p["mpn"])}/">{esc(p["mpn"])}</a></li>'
                for p in sample
            )
            rel_cards.append(
                f'<div class="card rel-card"><h4><a href="/components/{esc(s2)}/">{esc(n2)}</a></h4>'
                f'<ul class="bullet-list small">{skus}</ul></div>'
            )
        rel_html = (f'<section class="section"><div class="container">'
                    f'<h2>Related Products by Category</h2>'
                    f'<div class="grid grid-3">{ "".join(rel_cards) }</div>'
                    f'</div></section>')

    # ---- Full SKU list (all parts in this top category) ----
    part_links = "".join(
        f'<li><a href="/products/{p.get("url_slug") or slugify(p["mpn"])}/">{esc(p["mpn"])}</a> '
        f'<span class="muted">— {esc(p.get("manufacturer","").strip())} · '
        f'{esc(p.get("subcategory") or p.get("category","").strip())}</span></li>'
        for p in sorted(parts, key=lambda x: x["mpn"])
    )
    crumb = breadcrumb_jsonld([
        ("Home", f"{DOMAIN}/"),
        ("Components", f"{DOMAIN}/components/"),
        (cat_name, url),
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
      <a href="/components/">Components</a> ›
      <span>{esc(cat_name)}</span>
    </div></nav>
    <section class="page-head">
      <div class="container">
        <div class="eyebrow">Component Category</div>
        <h1>{esc(cat_name)}</h1>
        <p class="lead">{n} {esc(cat_name).lower()} we help global buyers source — from Shenzhen's electronics supply chain.</p>
        <div class="part-head-actions">
          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
          <a class="btn btn-ghost" href="https://wa.me/8613530888389">WhatsApp</a>
          <a class="btn btn-ghost" href="mailto:sales@szprocure.com">Email</a>
        </div>
      </div>
    </section>

    <!-- 1. Category Introduction -->
    <section class="section">
      <div class="container">
        <h2>About {esc(cat_name)} Sourcing</h2>
        {intro}
        <p class="muted small">Not sure which variant you need? Send the part number — our Shenzhen team cross-references and quotes.</p>
      </div>
    </section>

    <!-- 2. Subcategories -->
    <section class="section">
      <div class="container">
        <h2>{esc(cat_name)} Subcategories</h2>
        <ul class="bullet-list part-index">{sub_links}</ul>
      </div>
    </section>

    <!-- 2b. Featured Parts (Phase D.4) -->
    <section class="section">
      <div class="container">
        <h2>Featured {esc(cat_name)}</h2>
        <div class="grid grid-4">{feat_cards}</div>
      </div>
    </section>

    <!-- 3. Popular Components -->
    <section class="section">
      <div class="container two-col">
        <div>
          <h2>Popular {esc(cat_name)}</h2>
          <ul class="bullet-list part-index">{pop_links}</ul>
        </div>
        <aside class="part-aside">
          <div class="card sticky-card desk-sticky">
            <h3>Need a {esc(cat_name).lower()} part?</h3>
            <p>Send the part number and quantity for a quote.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
            <p class="muted small">sales@szprocure.com<br/>WhatsApp</p>
          </div>
        </aside>
      </div>
    </section>

    <!-- 4. Manufacturers -->
    <section class="section">
      <div class="container">
        <h2>Manufacturers in {esc(cat_name)}</h2>
        <ul class="alt-list">{mfr_links}</ul>
      </div>
    </section>

    {rel_html}

    <!-- Full SKU index -->
    <section class="section">
      <div class="container">
        <h2>All {esc(cat_name)} We Source ({n})</h2>
        <ul class="bullet-list part-index">{part_links}</ul>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
{ga4_script()}
</body>
</html>"""

# ==============================================================================
# COMPONENT SUBCATEGORY PAGE  (/components/<l2>/<l3>/)
# Phase 2.7 (A): precise L3 entry point. Data-driven from the Master CSV — only
# fine categories that have >=1 SKU generate a page (no empty pages). New fine
# categories need NO code change. Reuses the L2 breadcrumb + Organization JSON-LD
# (no new Schema type). No frozen layer (URL/RFQ/Schema/Data Factory) is touched.
# ==============================================================================
def gen_component_subcategory_page(l2_slug, l2_name, l3_name, l3_slug, parts, all_rows=None):
    url = f"{DOMAIN}/components/{l2_slug}/{l3_slug}/"
    n = len(parts)
    title = f"{esc(l3_name)} — {esc(l2_name)} | SZ Procure"
    desc = (f"Source {esc(l3_name)} ({esc(l2_name)}). Browse {n} "
            f"{esc(l3_name).lower()} we help global buyers procure — quotes, "
            f"lead-time and alternates.")
    # ---- 1. Subcategory Introduction (natural procurement framing) ----
    intro = (f"<p>{esc(l3_name)} are part of our {esc(l2_name).lower()} sourcing program. "
             f"SZ Procure helps global buyers source {esc(l3_name).lower()} from a "
             f"verified supplier network — covering popular families, hard-to-find "
             f"versions and BOM consolidation with competitive quotes.</p>")
    # ---- 2. SKU list (all parts in this fine category) ----
    part_links = "".join(
        f'<li><a href="/products/{p.get("url_slug") or slugify(p["mpn"])}/">{esc(p["mpn"])}</a> '
        f'<span class="muted">— {esc(p.get("manufacturer","").strip())}</span></li>'
        for p in sorted(parts, key=lambda x: x["mpn"])
    )
    # ---- 3. Manufacturers in this subcategory ----
    mfrs = sorted({p.get("manufacturer", "").strip() for p in parts if p.get("manufacturer", "").strip()})
    mfr_links = "".join(
        f'<li><a href="/manufacturers/{slugify_name(m)}/">{esc(m)}</a></li>' for m in mfrs
    ) or f'<li><a href="/request-a-quote/" data-zh="获取报价">Request a Quote</a></li>'
    crumb = breadcrumb_jsonld([
        ("Home", f"{DOMAIN}/"),
        ("Components", f"{DOMAIN}/components/"),
        (l2_name, f"{DOMAIN}/components/{l2_slug}/"),
        (l3_name, url),
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
      <a href="/components/">Components</a> ›
      <a href="/components/{esc(l2_slug)}/">{esc(l2_name)}</a> ›
      <span>{esc(l3_name)}</span>
    </div></nav>
    <section class="page-head">
      <div class="container">
        <div class="eyebrow">Component Subcategory</div>
        <h1>{esc(l3_name)}</h1>
        <p class="lead">{n} {esc(l3_name).lower()} we help global buyers source — from a verified supply network.</p>
        <div class="part-head-actions">
          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
          <a class="btn btn-ghost" href="https://wa.me/8613530888389">WhatsApp</a>
          <a class="btn btn-ghost" href="mailto:sales@szprocure.com">Email</a>
        </div>
      </div>
    </section>

    <!-- 1. Subcategory Introduction -->
    <section class="section">
      <div class="container">
        <h2>About {esc(l3_name)} Sourcing</h2>
        {intro}
      </div>
    </section>

    <!-- 2. SKU list -->
    <section class="section">
      <div class="container">
        <h2>All {esc(l3_name)} We Source ({n})</h2>
        <ul class="bullet-list part-index">{part_links}</ul>
      </div>
    </section>

    <!-- 3. Manufacturers -->
    <section class="section">
      <div class="container">
        <h2>Manufacturers in {esc(l3_name)}</h2>
        <ul class="alt-list">{mfr_links}</ul>
      </div>
    </section>

    <!-- 4. Back to category + RFQ -->
    <section class="section">
      <div class="container two-col">
        <div>
          <h2>More in {esc(l2_name)}</h2>
          <p><a class="link-cta" href="/components/{esc(l2_slug)}/" data-zh="查看分类 →">View all {esc(l2_name)} <span class="arrow">&rarr;</span></a></p>
        </div>
        <aside class="part-aside">
          <div class="card sticky-card desk-sticky">
            <h3>Need a {esc(l3_name).lower()} part?</h3>
            <p>Send the part number and quantity for a quote.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
          </div>
        </aside>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
{ga4_script()}
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
            slug = resolve_cat(name)[0]
            rows_html += (f'<li><a href="/components/{slug}/">{esc(name)}</a> '
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
{ga4_script()}
</body>
</html>"""

# ==============================================================================
# P0 SCALABILITY HELPERS  (O(n) related-products + slug-collision guard)
# ==============================================================================
def build_related_map(by_cat, k=6):
    """Build slug -> list[(pn, slug)] of up to k same-category neighbours.

    Replaces the old O(n^2) per-page scan. `by_cat` maps a top-level category
    slug to the list of source rows in that category. We rotate a fixed window
    over each category's pool so every part gets k deterministic neighbours in
    O(1) amortised time — total cost is O(total parts), not O(n^2).

    Colliding slugs (multiple PNs -> same slug) share one entry; collisions are
    reported separately by detect_collisions().
    """
    related_map = {}
    for cslug, rows_in_cat in by_cat.items():
        pool = [(r["mpn"].strip(), (r.get("url_slug") or "").strip() or slugify(r["mpn"].strip()))
                for r in rows_in_cat if r["mpn"].strip()]
        n = len(pool)
        if n == 0:
            continue
        for i, (pn, s) in enumerate(pool):
            if s in related_map:
                continue
            related = []
            j = i + 1
            guard = 0
            while len(related) < k and guard < n + k:
                cpn, cs = pool[j % n]
                j += 1
                guard += 1
                if cs != s:
                    related.append((cpn, cs))
            related_map[s] = related
    return related_map


def detect_collisions(rows):
    """Detect slug <-> MPN collisions BEFORE writing any page.

    Returns (slug_groups, empty_slugs):
      - slug_groups: colliding slug -> list of DISTINCT MPNs that resolve to it
      - empty_slugs: MPNs that slugify to nothing (silently dropped by generator)

    The generator MUST print + record these; we NEVER silently overwrite.
    """
    slug_to_mpns = defaultdict(list)
    empty = []
    for r in rows:
        pn = (r.get("mpn") or "").strip()
        if not pn:
            continue
        slug = slugify(pn)
        if not slug:
            empty.append(pn)
            continue
        slug_to_mpns[slug].append(pn)
    groups = {}
    for slug, mpns in slug_to_mpns.items():
        uniq = []
        for m in mpns:
            if m not in uniq:
                uniq.append(m)
        if len(uniq) > 1:
            groups[slug] = uniq
    return groups, empty


def detect_duplicate_mpns(rows):
    """Count EXACTLY-identical MPN rows (case/space-insensitive match).

    Data sources (LCSC, Huaqiang, DigiKey, vendor feeds) frequently re-emit the
    same part. Identical MPNs are NOT slug collisions (they resolve to the same
    page on purpose), so this NEVER blocks generation — it only records how many
    duplicate rows were collapsed, for data-hygiene review.

    Returns (dup_groups, dup_count) where:
      - dup_groups: normalized-mpn -> list of (raw_mpn, source_line) for groups > 1
      - dup_count : total number of rows that are duplicates of an earlier row
    """
    norm_to_rows = defaultdict(list)
    for idx, r in enumerate(rows, start=2):  # +2: header + 1-based data row
        pn = (r.get("mpn") or "").strip()
        if not pn:
            continue
        norm = re.sub(r"\s+", "", pn.lower())
        norm_to_rows[norm].append((pn, idx))
    dup_groups = {}
    dup_count = 0
    for norm, occ in norm_to_rows.items():
        if len(occ) > 1:
            # first occurrence is the canonical retained row; the rest are dups
            dup_groups[norm] = occ
            dup_count += len(occ) - 1
    return dup_groups, dup_count


# ==============================================================================
# MAIN
# ==============================================================================
# ==============================================================================
# PHASE 2.1 — DATA FACTORY P0 MECHANISMS
# P0-1  slug de-collision (resolution, not just detection — no silent overwrite)
# P0-2  brand canonicalization via mfr_canonical.csv
# P0-3  attributes_json validation against attributes_dictionary.md (single source)
# P0-4  duplicate-MPN merge by (canonical_brand, normalized_mpn) -> sources[]
# All four are NON-BLOCKING by default (report + flag into review_queue).
# Use --strict to hard-abort on unknown manufacturer / unknown attribute key.
# ==============================================================================

def norm_mpn(pn):
    """Stable part-identity key for merge (P0-4).
    Lower-cases and strips whitespace ONLY — preserves structural chars
    (+, -, ., /) that are part of real PNs (e.g. nRF24L01+ != nRF24L01).
    Over-stripping (like clean_mpn) would wrongly merge distinct parts."""
    return re.sub(r"\s+", "", (pn or "").strip().lower())


class SlugRegistry:
    """Deterministic, collision-free slug assignment (P0-1).
    First owner of a base slug keeps it; later collisions get -2, -3, ...
    Guarantees every /products/<slug>/ is unique -> no silent page overwrite.
    Stable for identical input order, so existing SEO URLs are preserved."""
    def __init__(self):
        self.used = {}
        self.renamed = {}
        self.first_mpn = {}
        self.extra_mpns = defaultdict(list)

    def assign(self, base, owner, mpn=""):
        base = re.sub(r"[^a-z0-9]", "", (base or "").lower())
        if not base:
            return ""
        if base not in self.used:
            self.used[base] = owner
            self.first_mpn[base] = mpn
            return base
        n = 2
        while True:
            cand = f"{base}-{n}"
            if cand not in self.used:
                self.used[cand] = owner
                self.renamed[base] = cand
                self.extra_mpns[base].append(mpn)
                return cand
            n += 1


def load_mfr_canonical(path):
    """raw brand/alias (lower) -> canonical brand. Tab-separated raw\tcanonical.
    Returns dict; missing file -> empty (caller passthrough + needs_review)."""
    m = {}
    if not os.path.exists(path):
        print(f"  [WARN] mfr_canonical.csv missing: {path} — brand passthrough + needs_review")
        return m
    with open(path, encoding="utf-8") as f:
        for row in csv.reader(f, delimiter="\t"):
            if len(row) < 2 or not row[0].strip():
                continue
            raw, canon = row[0].strip(), row[1].strip()
            if canon:
                m[raw.lower()] = canon
    return m


def canonicalize_brand(raw, mfr_map):
    """Return (canonical_or_raw, matched_bool). Unknown -> (raw, False)."""
    raw = (raw or "").strip()
    if not raw:
        return ("", False)
    canon = mfr_map.get(raw.lower())
    return (canon, True) if canon else (raw, False)


# Metasyntax tokens that are NOT real attribute keys even if ever surfaced
# (defensive; the §4 table parser below already excludes prose / unit-suffixes).
# NOTE: `speed_hz` was removed — it IS a real key listed in §4 of the frozen
# doc, so it must be ALLOWED, not denied.
_ATTR_DENY = {"attributes_json", "snake_case", "needs_review"}


def load_attr_allowlist(path):
    """Extract allowed attribute keys from attributes_dictionary.md (P0-3).

    The frozen doc's §4 '属性字典（key 清单）' table is the SINGLE SOURCE OF
    TRUTH. We isolate that section and take the FIRST backtick-wrapped token of
    each table row — that cell is always the attribute key. This captures BOTH:
      • snake_case keys with a unit suffix  -> `frequency_hz`, `flash_bytes`
      • plain-text keys WITHOUT an underscore -> `package`, `core`, `interface`,
        `mounting`, `modulation`, `hfe`, `sensitivity`, `range`, `accuracy`, ...
    Prose, the §3 unit-suffix table, and the §2 alias examples are ignored
    because they live OUTSIDE the §4 section, so they can never pollute the
    allowlist. (Fixes Phase 2.1 finding F1.)"""
    import re as _re
    allow = set()
    if not os.path.exists(path):
        print(f"  [WARN] attributes_dictionary.md missing: {path} — attr validation off")
        return allow
    txt = open(path, encoding="utf-8").read()
    in_section = False
    for line in txt.splitlines():
        if line.startswith("## 4."):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section or not line.startswith("|"):
            continue
        m = _re.search(r'`([^`]+)`', line)
        if not m:
            continue
        k = m.group(1).strip()
        if k and k not in _ATTR_DENY:
            allow.add(k)
    return allow


def validate_attributes(attrs_obj, allow):
    """Return (unknown_keys_set, ok). attrs_obj: dict or None."""
    if not isinstance(attrs_obj, dict):
        return set(), True
    unknown = {k for k in attrs_obj if k not in allow}
    return unknown, (len(unknown) == 0)


# ---------------------------------------------------------------------------
# Legacy attribute alias map (Phase 2.1.5) — mirrors §2 of
# attributes_dictionary.md ("禁止的同概念多字段 -> canonical key"). Raw/old
# attribute keys found in scraped data are auto-normalized to the canonical key
# so they don't pollute the review_queue. Only keys that are STILL unknown after
# this map AND the allowlist are flagged. Kept in CODE (not the frozen doc) so
# the frozen dictionary stays the authoritative KEY LIST while aliases evolve
# independently. Voltage/current families are intentionally omitted — §2 maps
# them to "use a specific *_{v,a,ma,ua}" which needs context, so they stay
# flagged for human review rather than guess-wrong.
# ---------------------------------------------------------------------------
LEGACY_ATTR_MAP = {
    # ---- frequency family ----
    "frequency": "frequency_hz", "freq": "frequency_hz",
    "clock": "frequency_hz", "speed": "frequency_hz",
    "clock speed": "frequency_hz", "max clock speed": "frequency_hz",
    "clock frequency": "frequency_hz", "operating frequency": "frequency_hz",
    # ---- flash / ram (multi-word supplier labels) ----
    "64kb": "flash_bytes", "64k flash": "flash_bytes",
    "65536 bytes": "flash_bytes", "64k": "flash_bytes",
    "program memory": "flash_bytes", "flash memory": "flash_bytes",
    "flash size": "flash_bytes", "program memory size": "flash_bytes",
    "flash memory size": "flash_bytes", "program flash": "flash_bytes",
    "ram 64k": "ram_bytes", "65536": "ram_bytes",
    "ram size": "ram_bytes", "sram size": "ram_bytes", "static ram": "ram_bytes",
    # ---- resistance / capacitance / inductance ----
    "resistance": "resistance_ohm", "ohm": "resistance_ohm",
    "r": "resistance_ohm", "res": "resistance_ohm",
    "resistor value": "resistance_ohm",
    "capacitance": "capacitance_pf", "cap": "capacitance_pf",
    "100n": "capacitance_pf", "0.1u": "capacitance_pf",
    "capacitor value": "capacitance_pf",
    "inductance": "inductance_uh", "ind": "inductance_uh",
    "inductor value": "inductance_uh",
    # ---- voltage (generic + specific) ----
    "vcc": "voltage_v", "vdd": "voltage_v", "vin": "voltage_v", "vout": "voltage_v",
    "operating voltage": "voltage_v", "supply voltage": "voltage_v",
    "nominal voltage": "voltage_v", "operating voltage range": "voltage_v",
    "input voltage": "voltage_in_max_v", "output voltage": "voltage_out_v",
    # ---- core / package / interface / mounting (canonical already, add phrasings) ----
    "core": "core", "cpu core": "core",
    "package": "package", "package type": "package", "case": "package", "case package": "package",
    "interface": "interface", "bus interface": "interface", "communication interface": "interface",
    "mounting": "mounting", "mounting type": "mounting", "mounting style": "mounting",
    "modulation": "modulation", "modulation type": "modulation",
    # ---- mosfet (multi-word) ----
    "drain source voltage": "vds_v", "vds": "vds_v", "drain-source voltage": "vds_v",
    "gate threshold voltage": "vgs_th_v", "vgs th": "vgs_th_v", "gate-source threshold": "vgs_th_v",
    "continuous drain current": "id_a", "drain current": "id_a", "continuous current": "id_a",
    "on resistance": "rds_on_mohm", "rds on": "rds_on_mohm", "drain source resistance": "rds_on_mohm",
    "gate charge": "qg_nc", "total gate charge": "qg_nc",
    # ---- passive (multi-word) ----
    "tolerance": "tolerance", "power rating": "power_rating_w", "rated power": "power_rating_w",
    "voltage rating": "voltage_rating_v", "rated voltage": "voltage_rating_v",
    "temperature coefficient": "temperature_coeff", "temp coefficient": "temperature_coeff",
    # ---- connector / module / rf (multi-word) ----
    "number of positions": "positions", "pin count": "positions", "number of pins": "positions",
    "pitch": "pitch_mm", "pin pitch": "pitch_mm",
    "current rating": "current_rating_a",
    "data rate": "data_rate_bps", "baud rate": "data_rate_bps",
    "output power": "output_power_dbm", "transmit power": "output_power_dbm",
    "sensitivity": "sensitivity_dbm", "receiver sensitivity": "sensitivity_dbm",
}


def build_merged_groups(rows, mfr_map, attr_allow, review):
    """P0-4 + P0-2 + P0-3: collapse rows by (canonical_brand, norm_mpn).
    Returns (groups, stats). `review` receives (mpn, brand, reason, detail)."""
    bucket = {}
    order = []
    stats = {"rows_in": len(rows), "groups_out": 0, "merged_dups": 0,
             "brand_unmatched": 0, "brand_missing": 0, "attr_unknown": 0,
             "attr_normalized": 0}
    seen_review = set()

    def add_review(mpn, brand, reason, detail):
        key = (mpn, reason, detail)
        if key in seen_review:
            return
        seen_review.add(key)
        review.append((mpn, brand, reason, detail))

    for r in rows:
        mpn = (r.get("mpn") or "").strip()
        if not mpn:
            continue
        raw_brand = (r.get("manufacturer") or r.get("brand") or "").strip()
        canon_mfr, matched = canonicalize_brand(raw_brand, mfr_map)
        if not raw_brand:
            # F2: manufacturer is a MANDATORY product-identity field. An empty
            # brand must NOT be silently ingested — flag it for human review.
            stats["brand_missing"] += 1
        elif not matched:
            stats["brand_unmatched"] += 1
        clean = (r.get("clean_mpn") or "").strip() or re.sub(r"[^A-Z0-9]", "", mpn.upper())
        key = (canon_mfr, norm_mpn(mpn))
        src = (r.get("source") or r.get("source_platform") or "").strip()
        if key not in bucket:
            g = dict(r)
            g["manufacturer"] = canon_mfr
            g["brand"] = canon_mfr
            g["_sources"] = []
            g["_attr_unknown"] = set()
            g["_review_reasons"] = []
            if not raw_brand:
                g["_review_reasons"].append("missing_manufacturer")
                add_review(mpn, canon_mfr, "missing_manufacturer", "empty manufacturer")
            elif not matched:
                g["_review_reasons"].append("unknown_manufacturer")
                add_review(mpn, canon_mfr, "unknown_manufacturer", f"raw={raw_brand}")
            bucket[key] = g
            order.append(key)
        else:
            g = bucket[key]
            stats["merged_dups"] += 1
            for fld in ("description", "subcategory", "applications", "keywords",
                        "faq", "image", "datasheet_url", "availability"):
                if not (g.get(fld) or "").strip() and (r.get(fld) or "").strip():
                    g[fld] = r[fld]
            if (r.get("alternative_parts") or "").strip():
                exist = set(split_multi(g.get("alternative_parts") or ""))
                newones = [x for x in split_multi(r["alternative_parts"]) if x not in exist]
                if newones:
                    g["alternative_parts"] = (g.get("alternative_parts") or "").strip() \
                        + ";" + ";".join(newones)
        if src and src not in g["_sources"]:
            g["_sources"].append(src)
        raw = (r.get("attributes_json") or "").strip()
        if raw:
            try:
                obj = json.loads(raw)
            except Exception:
                obj = None
            if isinstance(obj, dict):
                # Phase 2.1.5: normalize legacy alias keys to canonical BEFORE
                # validation so scraped data using old names is accepted.
                normalized = {}
                for k, v in obj.items():
                    nk = LEGACY_ATTR_MAP.get(k.lower(), k)
                    if nk != k:
                        stats["attr_normalized"] += 1
                    normalized[nk] = v
                unk, _ = validate_attributes(normalized, attr_allow)
                if unk:
                    g["_attr_unknown"] |= unk
                    for k in sorted(unk):
                        g["_review_reasons"].append(f"unknown_attr_key={k}")
                        stats["attr_unknown"] += 1
                        add_review(mpn, canon_mfr, "unknown_attribute_key", f"key={k}")
            elif obj is not None:
                # valid JSON but not an object (array/number) -> malformed
                g["_review_reasons"].append("malformed_attributes")
                add_review(mpn, canon_mfr, "malformed_attributes",
                           "attributes_json is not an object")

    groups = []
    for key in order:
        g = bucket[key]
        g["sources"] = g.pop("_sources")
        g["unknown_attr"] = g.pop("_attr_unknown")
        reasons = g.pop("_review_reasons")
        g["needs_review"] = bool(reasons)
        g["review_reasons"] = reasons
        groups.append(g)
    stats["groups_out"] = len(groups)
    return groups, stats


def main():
    ap = argparse.ArgumentParser()
    default_csv = os.path.join(ROOT, "data", "sample_parts.csv")
    ap.add_argument("--csv", default=default_csv)
    ap.add_argument("--out", default=ROOT)
    ap.add_argument("--strict", action="store_true",
                    help="Abort generation if ANY slug collision or empty-slug is detected "
                         "(no silent overwrite). Off by default: collisions are reported but "
                         "generation continues with last-wins behaviour.")
    args = ap.parse_args()

    csv_path = os.path.abspath(args.csv)
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)  # ensure output root exists before any report is written
    if not os.path.exists(csv_path):
        print("CSV not found:", csv_path); sys.exit(1)

    with open(csv_path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("mpn", "").strip()]

    print(f"Loaded {len(rows)} parts from {csv_path}")

    # group by manufacturer / top-level component category
    by_mfr = defaultdict(list)
    by_cat = defaultdict(list)  # key = top-level cat_slug (via CATEGORY_MAP)
    skipped_alt = 0
    for r in rows:
        by_mfr[r["manufacturer"].strip()].append(r)
        cslug, _ = resolve_cat(r["category"].strip())
        by_cat[cslug].append(r)
        # alt-part sanity: non-empty + each token slugifiable to a non-empty slug
        alt_raw = (r.get("alternative_parts") or "").strip()
        if alt_raw:
            for a in split_multi(alt_raw):
                if not slugify(a):
                    print(f"  [WARN] bad alt token {a!r} for PN {r['PN']} — skipped")
                    skipped_alt += 1

    # ---- P0-2: slug collision guard (run BEFORE any page is written) ----
    # Build slug <-> MPN mapping and surface every conflict. We never silently
    # overwrite: collisions are printed AND written to gen_collision_report.log.
    collision_groups, empty_slugs = detect_collisions(rows)
    if collision_groups or empty_slugs:
        lines = []
        lines.append("=" * 72)
        lines.append("SLUG COLLISION REPORT  —  generated by gen_parts.py")
        lines.append("Conflicting slugs would SILENTLY overwrite each other; only the LAST")
        lines.append("row for each slug wins the /products/<slug>/ page. Review and fix the CSV.")
        lines.append(f"Colliding slugs : {len(collision_groups)}")
        lines.append(f"Empty-slug drops: {len(empty_slugs)}")
        lines.append("=" * 72)
        for slug, mpns in sorted(collision_groups.items()):
            lines.append(f"  SLUG {slug!r} <- conflicting MPNs: {', '.join(mpns)}")
        for pn in empty_slugs:
            lines.append(f"  EMPTY-SLUG (dropped): {pn!r}")
        report_text = "\n".join(lines) + "\n"
        report_path = os.path.join(out_root, "gen_collision_report.log")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
        print(report_text)
        print(f"  [!] Collision report written to {report_path}")
        if args.strict:
            print("\n  [STRICT MODE] Slug collision(s) / empty-slug(s) detected — "
                  "ABORTING generation to prevent silent page overwrite.")
            print("  Fix the CSV (de-duplicate or re-slug the conflicting MPNs), then re-run.")
            sys.exit(2)
    else:
        print("  [OK] No slug collisions or empty slugs detected.")

    # ---- P0-2b: duplicate-MPN report (informational, NEVER blocks) ----
    # The same part emitted by multiple sources (LCSC/DigiKey/Huaqiang/vendor)
    # is expected; these are NOT slug collisions, so we record but keep going.
    dup_groups, dup_count = detect_duplicate_mpns(rows)
    if dup_groups:
        dlines = []
        dlines.append("=" * 72)
        dlines.append("DUPLICATE MPN REPORT  —  generated by gen_parts.py")
        dlines.append("Identical MPN rows collapse to the SAME /products/<slug>/ page (not a")
        dlines.append("collision). Recorded for data-hygiene review only; generation continues.")
        dlines.append(f"Duplicate MPN groups : {len(dup_groups)}")
        dlines.append(f"Extra duplicate rows : {dup_count}")
        dlines.append("=" * 72)
        for norm, occ in sorted(dup_groups.items()):
            dlines.append(f"  MPN {occ[0][0]!r} (normalized {norm!r}) x{len(occ)} "
                          f"at CSV lines: {', '.join(str(l) for _, l in occ)}")
        dup_text = "\n".join(dlines) + "\n"
        dup_path = os.path.join(out_root, "duplicate_mpn_report.log")
        with open(dup_path, "w", encoding="utf-8") as f:
            f.write(dup_text)
        print(dup_text)
        print(f"  [i] Duplicate-MPN report written to {dup_path}")
    else:
        print("  [OK] No duplicate MPN rows detected.")

    # ---- P0-1: O(n) related-products pre-index (replaces per-page O(n^2) scan) ----
    related_map = build_related_map(by_cat, k=6)

    # ---- generate part pages ----
    written = 0
    urls = []
    # slugs that will actually get a product page (for alt-link fallback)
    generated_slugs = {slugify(r["mpn"].strip()) for r in rows if r.get("mpn", "").strip()}
    for r in rows:
        pn = r["mpn"].strip()
        slug = slugify(pn)
        if not slug:
            continue
        cslug, _ = resolve_cat(r["category"].strip())
        mfr_slug = slugify_name(r["manufacturer"].strip())
        d = os.path.join(out_root, "products", slug)
        os.makedirs(d, exist_ok=True)
        page = gen_part_page(r, cslug, mfr_slug, related=related_map.get(slug, []), generated_slugs=generated_slugs)
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

    # ---- generate /components/<top-slug>/ category pages (all 6 canonical categories) ----
    # Iterate over TOP_CATEGORIES so every canonical category gets a page even if
    # no SKU currently maps to it (avoids dead breadcrumb links).
    for cslug, cname in TOP_CATEGORIES.items():
        parts = by_cat.get(cslug, [])
        d = os.path.join(out_root, "components", cslug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(gen_component_category_page(cslug, cname, parts, all_rows=rows, by_cat=by_cat))
        urls.append(f"{DOMAIN}/components/{cslug}/")

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
    # Front-end (search.html) loads shards on demand and does prefix/substring matching.
    #
    # Scalability: at 200k SKU this index would be 60-120MB as a single file and
    # would freeze the browser if loaded wholesale. Instead we shard it into
    # /search/0.json, /search/1.json, ... (SEARCH_SHARD_SIZE entries each) plus a
    # /search/manifest.json that records the shard count. The front-end loads only
    # the shard(s) relevant to the typed query prefix.
    search_entries = []
    seen = set()
    for r in rows:
        pn = r["mpn"].strip()
        mfr = r["manufacturer"].strip()
        cat = r["category"].strip()
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
            c_top = resolve_cat(cat)[0]
            search_entries.append({"t": cat, "k": cat.lower(), "ty": "Category",
                                   "u": f"/components/{c_top}/", "sub": "Browse category"})
            seen.add(key_c)

    # Shard the index so no single file is huge.
    # Sort by lowercase key first so each shard covers a contiguous key range;
    # the manifest records each shard's [from,to] key bounds so the front-end
    # can load only the shard(s) matching a typed query prefix.
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
            f.write(__import__("json").dumps(chunk, ensure_ascii=False))
            f.write('}')
        shards.append({"file": f"/search/{shard_idx}.json",
                       "n": len(chunk),
                       "from": chunk[0]["k"], "to": chunk[-1]["k"]})
        shard_idx += 1
    # Manifest: shard count + per-shard entry counts + total.
    manifest = {"version": 1, "shardSize": SEARCH_SHARD_SIZE,
                "shardCount": len(shards), "total": len(search_entries),
                "shards": shards}
    with open(os.path.join(search_dir, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(__import__("json").dumps(manifest, ensure_ascii=False))
    print(f"Search index: {len(search_entries)} entries -> {len(shards)} shards under /search/ "
          f"(+ manifest.json). Avg shard ~{len(search_entries)//max(len(shards),1)} entries.")

    # ---- parts.json (machine-readable structured catalog) ----
    # Contract layer for future /api/parts, AI agents and internal search.
    # Static now; at 200k scale this file is replaced by a real query API.
    parts_json = []
    for r in rows:
        mpn = (r.get("mpn") or "").strip()
        if not mpn:
            continue
        clean = (r.get("clean_mpn") or "").strip() or re.sub(r"[^A-Z0-9]", "", mpn.upper())
        uslug = (r.get("url_slug") or "").strip() or slugify(mpn)
        attrs = {}
        raw = (r.get("attributes_json") or "").strip()
        if raw:
            try:
                attrs = json.loads(raw)
            except Exception:
                attrs = {}
        parts_json.append({
            "mpn": mpn,
            "clean_mpn": clean,
            "manufacturer": (r.get("manufacturer") or "").strip(),
            "brand": (r.get("brand") or "").strip(),
            "url_slug": uslug,
            "category": (r.get("category") or "").strip(),
            "subcategory": (r.get("subcategory") or "").strip(),
            "description": (r.get("description") or "").strip(),
            "applications": (r.get("applications") or "").strip(),
            "keywords": (r.get("keywords") or "").strip(),
            "attributes": attrs,
            "availability": (r.get("availability") or "").strip(),
            "alternative_parts": (r.get("alternative_parts") or "").strip(),
            "datasheet_url": (r.get("datasheet_url") or "").strip(),
            "product_url": f"/products/{uslug}/",
        })
    with open(os.path.join(out_root, "parts.json"), "w", encoding="utf-8") as f:
        f.write(json.dumps(parts_json, ensure_ascii=False, indent=2))
    print(f"parts.json: {len(parts_json)} structured records written.")

    print(f"Generated {written} product pages under /products/")
    print(f"Manufacturer pages: {len(by_mfr)} under /manufacturers/")
    print(f"Component category pages: {len(by_cat)} under /components/<top-slug>/")
    print(f"Alt tokens skipped (bad): {skipped_alt}")
    print(f"Slug collisions (recorded): {len(collision_groups)}")
    print(f"Empty-slug rows dropped: {len(empty_slugs)}")
    print(f"Duplicate MPN rows (recorded, non-blocking): {dup_count}")
    print(f"Sitemaps: {sm_paths} (+ sitemap_parts_index.xml)")
    print(f"Total indexed URLs this run: {len(urls)}")
    print(f"At 200k scale: ~{(200000+SITEMAP_BATCH-1)//SITEMAP_BATCH} sitemap files, all auto-split.")

def main():
    ap = argparse.ArgumentParser()
    default_csv = os.path.join(ROOT, "data", "sample_parts.csv")
    ap.add_argument("--csv", default=default_csv)
    ap.add_argument("--out", default=ROOT)
    ap.add_argument("--mfr-map", default=os.path.join(ROOT, "data", "mfr_canonical.csv"))
    ap.add_argument("--attr-dict", default=os.path.join(ROOT, "data", "attributes_dictionary.md"))
    ap.add_argument("--dry-run", action="store_true",
                    help="Process + validate + report only. Writes test_p0_processed.csv and "
                         "review_queue.csv under --out, but does NOT generate HTML/sitemap/search.")
    ap.add_argument("--strict", action="store_true",
                    help="Hard gate: abort if any unknown manufacturer or unknown attribute key "
                         "is found (200k data-hygiene gate).")
    args = ap.parse_args()

    csv_path = os.path.abspath(args.csv)
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    if not os.path.exists(csv_path):
        print("CSV not found:", csv_path); sys.exit(1)

    # ---- load reference dictionaries (Phase 2.1) ----
    mfr_map = load_mfr_canonical(args.mfr_map)
    attr_allow = load_attr_allowlist(args.attr_dict)
    print(f"Loaded mfr_canonical ({len(mfr_map)} aliases) + attributes allowlist "
          f"({len(attr_allow)} keys)")

    with open(csv_path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("mpn", "").strip()]

    print(f"Loaded {len(rows)} parts from {csv_path}")

    # ---- P0-4 + P0-2 + P0-3 : merge / canonicalize / validate ----
    review = []   # (mpn, canonical_brand, reason, detail)
    groups, stats = build_merged_groups(rows, mfr_map, attr_allow, review)

    # ---- P0-1 : deterministic, collision-free slug assignment ----
    registry = SlugRegistry()
    for g in groups:
        base = (g.get("url_slug") or "").strip() or slugify(g["mpn"].strip())
        g["url_slug"] = registry.assign(base, g["manufacturer"].strip(), g["mpn"].strip())

    # ---- P0-1 report: collisions auto-resolved (no overwrite) ----
    if registry.renamed:
        lines = ["=" * 72, "SLUG COLLISION RESOLUTION REPORT — gen_parts.py (P0-1)",
                 "Colliding base slugs are auto-suffixed (-2, -3...) so NO page is overwritten.",
                 f"Resolved collisions : {len(registry.renamed)}", "=" * 72]
        for base, final in sorted(registry.renamed.items()):
            mpns = [registry.first_mpn.get(base, "")] + registry.extra_mpns.get(base, [])
            lines.append(f"  {base} -> {final}  (MPNs: {', '.join(mpns)})")
        txt = "\n".join(lines) + "\n"
        with open(os.path.join(out_root, "slug_resolution_report.log"), "w", encoding="utf-8") as f:
            f.write(txt)
        print(txt)
        print(f"  [i] Slug resolution report -> {os.path.join(out_root, 'slug_resolution_report.log')}")
    else:
        print("  [OK] No slug collisions — all base slugs unique; no URL overwrite risk.")

    # ---- P0-4 / P0-2 / P0-3 summary ----
    print(f"  [P0-4] rows in: {stats['rows_in']} -> groups out: {stats['groups_out']} "
          f"(merged duplicate rows: {stats['merged_dups']})")
    print(f"  [P0-2] rows with unmapped manufacturer (needs_review): {stats['brand_unmatched']}")
    print(f"  [P0-3] rows with unknown attribute key (needs_review): {stats['attr_unknown']}")

    # ---- DRY RUN: processed + review outputs, stop before HTML ----
    if args.dry_run:
        proc_path = os.path.join(out_root, "test_p0_processed.csv")
        with open(proc_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["mpn", "canonical_brand", "clean_mpn", "url_slug", "category",
                        "subcategory", "sources", "needs_review", "review_reasons",
                        "unknown_attr", "attributes_json"])
            for g in groups:
                w.writerow([
                    g.get("mpn", "").strip(),
                    g.get("manufacturer", "").strip(),
                    (g.get("clean_mpn") or "").strip() or re.sub(r"[^A-Z0-9]", "", (g.get("mpn") or "").upper()),
                    g.get("url_slug", ""),
                    g.get("category", "").strip(),
                    g.get("subcategory", "").strip(),
                    ";".join(g.get("sources", [])),
                    "yes" if g.get("needs_review") else "no",
                    ";".join(g.get("review_reasons", [])),
                    ";".join(sorted(g.get("unknown_attr", set()))),
                    (g.get("attributes_json") or "").strip(),
                ])
        rq_path = os.path.join(out_root, "review_queue.csv")
        with open(rq_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["mpn", "canonical_brand", "reason", "detail"])
            for mpn, brand, reason, detail in review:
                w.writerow([mpn, brand, reason, detail])
        print(f"\n  [DRY-RUN] Processed catalog -> {proc_path}")
        print(f"  [DRY-RUN] Review queue     -> {rq_path}  ({len(review)} items)")
        print(f"  [DRY-RUN] No HTML/sitemap/search written. Phase 2.1 logic verified.")
        return

    # ---- hard gate (--strict) ----
    if args.strict and (stats["brand_unmatched"] > 0 or stats["attr_unknown"] > 0):
        print("\n  [STRICT MODE] Unknown manufacturer and/or unknown attribute key detected.")
        print("  Aborting generation to protect 200k data hygiene. Fix the review_queue, then re-run.")
        sys.exit(3)

    # ---- group for page generation ----
    by_mfr = defaultdict(list)
    by_cat = defaultdict(list)
    for g in groups:
        by_mfr[g["manufacturer"].strip()].append(g)
        cslug, _ = resolve_cat(g["category"].strip())
        by_cat[cslug].append(g)

    # ---- P0-1 related-products pre-index (final slugs) ----
    related_map = build_related_map(by_cat, k=6)

    # ---- generate part pages ----
    written = 0
    urls = []
    generated_slugs = {g["url_slug"] for g in groups if g.get("url_slug")}
    for g in groups:
        pn = g["mpn"].strip()
        slug = g["url_slug"]
        if not slug:
            continue
        cslug, _ = resolve_cat(g["category"].strip())
        mfr_slug = slugify_name(g["manufacturer"].strip())
        d = os.path.join(out_root, "products", slug)
        os.makedirs(d, exist_ok=True)
        page = gen_part_page(g, cslug, mfr_slug, related=related_map.get(slug, []), generated_slugs=generated_slugs)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(page)
        urls.append(f"{DOMAIN}/products/{slug}/")
        written += 1

    # ---- manufacturer pages ----
    for mfr, parts in by_mfr.items():
        mslug = slugify_name(mfr)
        d = os.path.join(out_root, "manufacturers", mslug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(gen_manufacturer_page(mfr, parts, {}))
        urls.append(f"{DOMAIN}/manufacturers/{mslug}/")

    # ---- manufacturer hub (data-driven, Phase D.3) ----
    hub_dir = os.path.join(out_root, "manufacturers")
    os.makedirs(hub_dir, exist_ok=True)
    with open(os.path.join(hub_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(gen_manufacturers_hub(by_mfr))
    urls.append(f"{DOMAIN}/manufacturers/")

    # ---- component category pages (6 canonical categories) ----
    for cslug, cname in TOP_CATEGORIES.items():
        parts = by_cat.get(cslug, [])
        d = os.path.join(out_root, "components", cslug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(gen_component_category_page(cslug, cname, parts, all_rows=rows, by_cat=by_cat))
        urls.append(f"{DOMAIN}/components/{cslug}/")

    # ---- component L3 subcategory pages (/components/<l2>/<l3>/) ----
    # Phase 2.7 (A): data-driven. Only fine categories with >=1 SKU generate a
    # page (no empty pages). New fine categories in the Master need NO code change.
    for cslug, cname in TOP_CATEGORIES.items():
        l3_groups = defaultdict(list)
        for p in by_cat.get(cslug, []):
            fine = (p.get("category") or "").strip()
            if fine:
                l3_groups[fine].append(p)
        for fine, l3_parts in sorted(l3_groups.items()):
            l3_slug = slugify_name(fine)
            d = os.path.join(out_root, "components", cslug, l3_slug)
            os.makedirs(d, exist_ok=True)
            page = gen_component_subcategory_page(cslug, cname, fine, l3_slug, l3_parts, all_rows=rows)
            with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
                f.write(page)
            urls.append(f"{DOMAIN}/components/{cslug}/{l3_slug}/")

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
    with open(os.path.join(out_root, "sitemap_parts_index.xml"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n')
        for fn in sm_paths:
            f.write(f"  <sitemap><loc>{DOMAIN}/{fn}</loc></sitemap>\n")
        f.write('</sitemapindex>\n')

    # ---- search index (uses final slugs) ----
    search_entries = []
    seen = set()
    for g in groups:
        pn = g["mpn"].strip()
        mfr = g["manufacturer"].strip()
        cat = g["category"].strip()
        p_slug = g["url_slug"]
        m_slug = slugify_name(mfr)
        c_slug = slugify_name(cat)
        key_p = ("p", pn.lower())
        if key_p not in seen:
            search_entries.append({"t": pn, "k": pn.lower(), "keys": pn_search_keys(pn),
                                   "ty": "Part", "u": f"/products/{p_slug}/", "sub": f"{mfr} \u00b7 {cat}"})
            seen.add(key_p)
        key_m = ("m", mfr.lower())
        if key_m not in seen:
            search_entries.append({"t": mfr, "k": mfr.lower(), "ty": "Manufacturer",
                                   "u": f"/manufacturers/{m_slug}/", "sub": "View all sourced parts"})
            seen.add(key_m)
        key_c = ("c", cat.lower())
        if key_c not in seen:
            c_top = resolve_cat(cat)[0]
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
    print(f"Search index: {len(search_entries)} entries -> {len(shards)} shards under /search/.")

    # ---- parts.json (machine-readable; now carries sources + needs_review) ----
    parts_json = []
    for g in groups:
        mpn = g["mpn"].strip()
        if not mpn:
            continue
        clean = (g.get("clean_mpn") or "").strip() or re.sub(r"[^A-Z0-9]", "", mpn.upper())
        uslug = g["url_slug"]
        attrs = {}
        raw = (g.get("attributes_json") or "").strip()
        if raw:
            try:
                attrs = json.loads(raw)
            except Exception:
                attrs = {}
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
    print(f"parts.json: {len(parts_json)} structured records written.")

    print(f"Generated {written} product pages under /products/")
    print(f"Manufacturer pages: {len(by_mfr)} under /manufacturers/")
    print(f"Component category pages: {len(by_cat)} under /components/<top-slug>/")
    print(f"Slug collisions resolved (no overwrite): {len(registry.renamed)}")
    print(f"Duplicate MPN rows merged: {stats['merged_dups']}")
    print(f"Sitemaps: {sm_paths} (+ sitemap_parts_index.xml)")
    print(f"Total indexed URLs this run: {len(urls)}")

if __name__ == "__main__":
    main()
