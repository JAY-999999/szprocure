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

CSV columns (from 料号库.csv):
  PN, Mfr, Category, KeySpecs, Applications, TargetCustomers,
  AltParts, DemandRegion, Notes, Status, Image, Source

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
import csv, os, re, argparse, html, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
DOMAIN = "https://www.szprocure.com"
SITEMAP_BATCH = 45000  # urls per sitemap file (Google soft cap 50k)

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
    "email": "jay@szprocure.com",
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
def gen_part_page(row, cat_slug, mfr_slug, all_rows=None, generated_slugs=None):
    pn = row["PN"].strip()
    mfr = row["Mfr"].strip()
    cat = row["Category"].strip()
    subcat = (row.get("SubCategory") or "").strip()
    specs_raw = row["KeySpecs"].strip()
    apps = row["Applications"].strip()
    alt_raw = row["AltParts"].strip()
    supply = (row.get("SupplyInfo") or "").strip()
    faq_raw = (row.get("FAQ") or "").strip()
    region = row["DemandRegion"].strip()
    notes = row["Notes"].strip()
    img = (row.get("Image") or "").strip()
    # NOTE: `Source` column (CSV) is for internal data curation only — it may
    # point to an external reference site. We NEVER render it on the page.
    # SZ Procure is a sourcing partner, not a distributor, so SKU pages must
    # not link out to any third-party store.

    slug = slugify(pn)
    # ---- same-category cross-links (product spider-web) ----
    # all_rows: full list of part dicts. Pull up to 6 other SKUs in the same top category.
    related = []
    if all_rows:
        for r in all_rows:
            opn = r["PN"].strip()
            oslug = slugify(opn)
            if oslug == slug:
                continue
            ocslug, _ = resolve_cat(r["Category"].strip())
            if ocslug == cat_slug and len(related) < 6:
                related.append((opn, oslug))
    url = f"{DOMAIN}/products/{slug}/"
    img_url = img if img else "/assets/img/hero.svg"
    og_img = f"{DOMAIN}{img_url}" if img_url.startswith("/") else img_url

    # Resolve fine category -> 6 top-level /components/ URL (breadcrumbs & links)
    cat_slug, cat_top = resolve_cat(cat)

    # ---- SEO copy: procurement language, Shenzhen/China sourcing keywords ----
    # Lead / overview emphasizes the BUYING scenario (global procurement from
    # Shenzhen supply chain), not just a spec description of the part.
    overview = (f"SZ Procure helps global buyers source {esc(pn)} ({esc(mfr)} "
                f"{esc(subcat or cat).lower()}) from the Shenzhen electronics supply chain. "
                f"Whether you need small batches, hard-to-find versions, or BOM consolidation, "
                f"our Shenzhen team connects you with verified suppliers and competitive quotes.")
    title = f"{esc(pn)} {esc(mfr)} — Source from Shenzhen, China | SZ Procure"
    desc = (f"Source {esc(pn)} ({esc(mfr)} {esc(cat).lower()}) from Shenzhen, China. "
            f"Shenzhen supplier network, hard-to-find support and BOM procurement for global buyers.")

    # ---- parse repeatable fields ----
    specs = split_specs(specs_raw)
    # Filter alternates: keep only tokens that yield a non-empty slug (real part
    # numbers). Drops junk like "-" so we never emit alternatePart:["-"] in schema.
    alts = [a for a in split_multi(alt_raw) if slugify(a)]
    apps_list = split_multi(apps)
    region_list = split_multi(region)
    faq_pairs = parse_faq(faq_raw, pn)

    # ---- extract spec pairs (Core / Flash / Package / Voltage etc.) ----
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
    for token in specs:
        if ":" in token:
            k, v = token.split(":", 1)
            spec_pairs.append((k.strip(), v.strip()))
        else:
            # KeySpecs uses comma-separated descriptive values (not key:value),
            # e.g. "32-bit ARM Cortex-M3, 72MHz, 64KB Flash". Preserve each as a
            # real spec line with a derived attribute name so Google sees
            # concrete, labelled entity attributes (not a generic "Specification").
            spec_pairs.append((infer_spec_key(token), token.strip()))

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
    filled_keys = {k for k, _ in spec_pairs}
    backfill = [(k, "See datasheet") for k in STANDARD_FIELDS if k not in filled_keys]
    all_spec_pairs = spec_pairs + backfill
    # cap at 12 to keep it readable, prioritize real specs first
    all_spec_pairs = all_spec_pairs[:12]
    specs_table = "".join(
        f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>" for k, v in all_spec_pairs
    )
    specs_html = f'<table class="spec-table">\n<tbody>\n{specs_table}</tbody>\n</table>'

    # 1. Key Information table — lean, no stock/inventory wording
    qi_rows = []
    qi_rows.append(("Manufacturer", f'<a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a>'))
    qi_rows.append(("Category", f'<a href="/components/{cat_slug}/">{esc(cat_top)}</a>'))
    if subcat and subcat.lower() != cat.lower():
        qi_rows.append(("Type", esc(subcat)))
    for k, v in spec_pairs:
        if k in ("Package", "Core"):
            qi_rows.append((k, esc(v)))
    # Supply field uses procurement language, never "stock"
    qi_rows.append(("Sourcing Availability", "Shenzhen supplier network"))
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

    # 4. Sourcing Information — FIXED template (our Shenzhen sourcing moat)
    sourcing_html = f"""<p>SZ Procure is a Shenzhen sourcing partner for <strong>{esc(pn)}</strong> — not a stock catalog. We help global buyers access China's electronics supply chain.</p>
      <ul class="bullet-list check-list">
        <li>✔ Original component sourcing</li>
        <li>✔ Shenzhen supplier network</li>
        <li>✔ Hard-to-find parts support</li>
        <li>✔ BOM procurement service</li>
      </ul>
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
                        f'<p>Other {esc(cat_top).lower()} we help global buyers source from Shenzhen:</p>'
                        f'<ul class="alt-list">{rel_items}</ul>')
    else:
        related_html = ""

    # Reference Resources — links ONLY to the manufacturer's OWN official
    # documentation (datasheet / technical resources). We never link to a
    # third-party marketplace. If we don't have the manufacturer's official
    # site mapped, we show a neutral note instead of a store link.
    ref_block = ""
    mfr_official = MFR_OFFICIAL.get(mfr)
    if mfr_official:
        ref_block = (
            f'<div class="reference-resources">'
            f'<h3>Reference Resources</h3>'
            f'<ul class="alt-list">'
            f'<li><a href="{esc(mfr_official)}" target="_blank" rel="nofollow noopener">'
            f'{esc(mfr)} Official Website ↗</a></li>'
            f'<li><a href="{esc(mfr_official)}" target="_blank" rel="nofollow noopener">'
            f'{esc(mfr)} Datasheet &amp; Technical Documentation ↗</a></li>'
            f'</ul>'
            f'<p class="muted small">Reference only — specifications &amp; images '
            f'© {esc(mfr)}. SZ Procure is an independent sourcing partner, not the distributor.</p>'
            f'</div>')
    else:
        ref_block = (
            f'<div class="reference-resources">'
            f'<h3>Reference Resources</h3>'
            f'<p>For the official {esc(mfr)} datasheet and technical documentation, '
            f'visit the manufacturer\'s website. SZ Procure sources this part through '
            f'the Shenzhen supply chain — we are an independent sourcing partner, not a distributor.</p>'
            f'</div>')

    # breadcrumb: Home > Components > TopCategory > Part (matches /components/ final structure)
    crumb = breadcrumb_jsonld([
        ("Home", f"{DOMAIN}/"),
        ("Components", f"{DOMAIN}/components/"),
        (cat_top, f"{DOMAIN}/components/{cat_slug}/"),
        (pn, url),
    ])
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
    "description": "{esc(overview)}",
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
      <span>{esc(pn)}</span>
    </div></nav>

    <!-- 1. Product Header (procurement landing — above the fold, lean) -->
    <section class="page-head part-head">
      <div class="container part-head-grid">
        <div class="part-head-main">
          <div class="eyebrow"><a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a> · {esc(subcat or cat)}</div>
          <h1>{esc(pn)}</h1>
          <p class="lead-sub">{esc(mfr)} {esc(subcat or cat)}</p>
          <p class="lead">Source {esc(pn)} from Shenzhen, China — we help global buyers access this part through verified suppliers with flexible quantity and competitive pricing.</p>
          <div class="part-head-actions">
            <a class="btn btn-primary btn-lg" href="/request-a-quote/?pn={esc(pn)}">Request a Quote</a>
            <a class="btn btn-ghost" href="https://wa.me/8613587294123?text=Hi%20SZ%20Procure,%20I%20need%20{esc(pn)}">WhatsApp</a>
            <a class="btn btn-ghost" href="mailto:jay@szprocure.com?subject=Quote%20for%20{esc(pn)}">Email</a>
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

    <!-- Mobile-only quote card (after first screen, no fixed overlay) -->
    <section class="section mobile-quote-only">
      <div class="container">
        <div class="card sticky-card">
          <h3>Need this component?</h3>
          <p>Send the part number and quantity.</p>
          <a class="btn btn-primary btn-block" href="/request-a-quote/?pn={esc(pn)}">Request a Quote</a>
          <p class="muted small">jay@szprocure.com · WhatsApp</p>
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
            <a class="btn btn-primary btn-block" href="/request-a-quote/?pn={esc(pn)}">Request a Quote</a>
            <p class="muted small">jay@szprocure.com<br/>WhatsApp</p>
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
        <p>Send your quantity and target price — our Shenzhen team will check availability, pricing and lead time.</p>
        <a class="btn btn-primary btn-lg" href="/request-a-quote/?pn={esc(pn)}">Request a Quote</a>
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
        f'<li><a href="/products/{slugify(p["PN"])}/">{esc(p["PN"])}</a> '
        f'<span class="muted">— {esc(p["Category"])}</span></li>'
        for p in sorted(parts, key=lambda x: x["PN"])
    )
    # related categories for this manufacturer (resolve fine -> top slug)
    cat_links = "".join(
        f'<li><a href="/components/{resolve_cat(c)[0]}/">{esc(c)}</a></li>'
        for c in sorted({p["Category"] for p in parts})
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
def gen_component_category_page(cat_slug, cat_name, parts, all_rows=None):
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
    # ---- 2. Subcategories (fine categories mapped into this top category) ----
    # Reverse-lookup CATEGORY_MAP to list fine categories under this top slug.
    sub_fine = sorted({fine for fine, sl in CATEGORY_MAP.items() if sl == cat_slug})
    sub_links = "".join(
        f'<li><a href="/request-a-quote/?cat={esc(fine)}">{esc(fine)}</a></li>'
        for fine in sub_fine
    ) or f'<li><a href="/request-a-quote/">Request a quote</a></li>'
    # ---- 3. Popular Components (first up to 8 SKUs in this category) ----
    popular = sorted(parts, key=lambda x: x["PN"])[:8]
    pop_links = "".join(
        f'<li><a href="/products/{slugify(p["PN"])}/">{esc(p["PN"])}</a> '
        f'<span class="muted">— {esc(p.get("Mfr","").strip())}</span></li>'
        for p in popular
    )
    # ---- 4. Manufacturers in this category ----
    mfrs = sorted({p.get("Mfr", "").strip() for p in parts if p.get("Mfr", "").strip()})
    mfr_links = "".join(
        f'<li><a href="/manufacturers/{slugify_name(m)}/">{esc(m)}</a></li>' for m in mfrs
    ) or f'<li><a href="/request-a-quote/">Request a quote</a></li>'
    # ---- Full SKU list (all parts in this top category) ----
    part_links = "".join(
        f'<li><a href="/products/{slugify(p["PN"])}/">{esc(p["PN"])}</a> '
        f'<span class="muted">— {esc(p.get("Mfr","").strip())} · '
        f'{esc(p.get("SubCategory") or p.get("Category","").strip())}</span></li>'
        for p in sorted(parts, key=lambda x: x["PN"])
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
        <h1>{esc(cat_name)} Sourcing from Shenzhen, China</h1>
        <p class="lead">{n} {esc(cat_name).lower()} we help global buyers source — from Shenzhen's electronics supply chain.</p>
        <div class="part-head-actions">
          <a class="btn btn-primary btn-lg" href="/request-a-quote/">Request a Quote</a>
          <a class="btn btn-ghost" href="https://wa.me/8613587294123">WhatsApp</a>
          <a class="btn btn-ghost" href="mailto:jay@szprocure.com">Email</a>
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
            <a class="btn btn-primary btn-block" href="/request-a-quote/">Request a Quote</a>
            <p class="muted small">jay@szprocure.com<br/>WhatsApp</p>
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

    # group by manufacturer / top-level component category
    by_mfr = defaultdict(list)
    by_cat = defaultdict(list)  # key = top-level cat_slug (via CATEGORY_MAP)
    skipped_alt = 0
    for r in rows:
        by_mfr[r["Mfr"].strip()].append(r)
        cslug, _ = resolve_cat(r["Category"].strip())
        by_cat[cslug].append(r)
        # alt-part sanity: non-empty + each token slugifiable to a non-empty slug
        alt_raw = (r.get("AltParts") or "").strip()
        if alt_raw:
            for a in split_multi(alt_raw):
                if not slugify(a):
                    print(f"  [WARN] bad alt token {a!r} for PN {r['PN']} — skipped")
                    skipped_alt += 1

    # ---- generate part pages ----
    written = 0
    urls = []
    # slugs that will actually get a product page (for alt-link fallback)
    generated_slugs = {slugify(r["PN"].strip()) for r in rows if r.get("PN", "").strip()}
    for r in rows:
        pn = r["PN"].strip()
        slug = slugify(pn)
        if not slug:
            continue
        cslug, _ = resolve_cat(r["Category"].strip())
        mfr_slug = slugify_name(r["Mfr"].strip())
        d = os.path.join(out_root, "products", slug)
        os.makedirs(d, exist_ok=True)
        page = gen_part_page(r, cslug, mfr_slug, all_rows=rows, generated_slugs=generated_slugs)
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
            f.write(gen_component_category_page(cslug, cname, parts, all_rows=rows))
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
            c_top = resolve_cat(cat)[0]
            search_entries.append({"t": cat, "k": cat.lower(), "ty": "Category",
                                   "u": f"/components/{c_top}/", "sub": "Browse category"})
            seen.add(key_c)
    with open(os.path.join(out_root, "search-index.json"), "w", encoding="utf-8") as f:
        f.write('{"entries":')
        f.write(__import__("json").dumps(search_entries, ensure_ascii=False))
        f.write('}')

    print(f"Generated {written} product pages under /products/")
    print(f"Manufacturer pages: {len(by_mfr)} under /manufacturers/")
    print(f"Component category pages: {len(by_cat)} under /components/<top-slug>/")
    print(f"Alt tokens skipped (bad): {skipped_alt}")
    print(f"Sitemaps: {sm_paths} (+ sitemap_parts_index.xml)")
    print(f"Total indexed URLs this run: {len(urls)}")
    print(f"At 200k scale: ~{(200000+SITEMAP_BATCH-1)//SITEMAP_BATCH} sitemap files, all auto-split.")

if __name__ == "__main__":
    main()
