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
import csv, os, re, argparse, html, sys, json, hashlib, tempfile, subprocess
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
# DEPRECATED (P0, 2026-09-08): the taxonomy mapping has been EXTERNALIZED to
# data/category_taxonomy.json (authoritative). Production classification now uses
# resolve_taxonomy(); this dict is a frozen legacy mirror kept only for backward-compat
# with historical audit scripts and MUST NOT be edited for production behavior.
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
# ---------------------------------------------------------------------------
# Taxonomy resolver (I3/I4 — 2026-09-10)
# v2 native taxonomy (data/category_taxonomy.json) is the SINGLE source of truth.
# The old hardcoded 6-top TOP_CATEGORIES literal is GONE: top scopes are DERIVED from
# the v2 `top_scopes` array (see TOP_CATEGORIES below, computed at import). gen_parts.py
# reads MASTER.native_l1 + publish_status directly (Scheme A: native_l1 is the sole
# 02->03 contract — no resolve_taxonomy fallback on the old 6-top taxonomy).
# ---------------------------------------------------------------------------
_TAXONOMY = None
_TAXONOMY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "data", "category_taxonomy.json")


def load_taxonomy(force=False):
    """Load data/category_taxonomy.json (v2 — the authoritative NATIVE taxonomy).
    Module-level cached. Builds lookup tables keyed by native_l1 slug and top_scope slug:
      _TAXONOMY['tops']      : top_scope slug -> {slug,name,publish_status,l1_count}
      _TAXONOMY['l1_by_slug']: native_l1 slug -> {slug,name,top_scope,top_name,publish_status}
    Raises FileNotFoundError if the config is missing (taxonomy MUST exist; no fallback)."""
    global _TAXONOMY
    if _TAXONOMY is not None and not force:
        return _TAXONOMY
    with open(_TAXONOMY_PATH, encoding="utf-8") as _f:
        _data = json.load(_f)
    _tops = {}
    for _t in _data.get("top_scopes", []):
        _tops[_t["slug"]] = {
            "slug": _t["slug"],
            "name": _t.get("name", _t["slug"]),
            "publish_status": (_t.get("publish_status") or "active").strip().lower(),
            "l1_count": _t.get("l1_count", 0),
        }
    _l1 = {}
    for _l in _data.get("l1_categories", []):
        _ts = _l.get("top_scope")
        _l1[_l["slug"]] = {
            "slug": _l["slug"],
            "name": _l.get("en_display", _l.get("raw_name", _l["slug"])),
            "top_scope": _ts,
            "top_name": _tops.get(_ts, {}).get("name", _ts) if _ts else _ts,
            "publish_status": (_l.get("publish_status") or "active").strip().lower(),
        }
    _TAXONOMY = {"raw": _data, "tops": _tops, "l1_by_slug": _l1}
    return _TAXONOMY


def _native_entry(native_l1):
    """Look up a MASTER.native_l1 slug in the v2 taxonomy (case-insensitive)."""
    if not native_l1:
        return None
    return load_taxonomy()["l1_by_slug"].get((native_l1 or "").strip().lower())


def resolve_native(native_l1):
    """Core native resolver (I3/I4). Map MASTER.native_l1 -> top scope + publish_status.
    Returns dict {status, top_slug, top_name, l1_slug, l1_name, publish_status}.
    status in {RESOLVED, UNMAPPED}. A native L1 never self-references its top scope, so
    SELF_REFERENCE / COLLISION are impossible here (they belonged to the old fine/top model)."""
    _e = _native_entry(native_l1)
    if not _e:
        return {"status": "UNMAPPED", "top_slug": None, "top_name": None,
                "l1_slug": None, "l1_name": native_l1, "publish_status": "active"}
    return {"status": "RESOLVED", "top_slug": _e["top_scope"], "top_name": _e["top_name"],
            "l1_slug": _e["slug"], "l1_name": _e["name"], "publish_status": _e["publish_status"]}


def _as_set(v):
    if isinstance(v, (list, tuple, set)):
        return set(v)
    return {v}


def native_top_scopes(publish_status=None):
    """I4: single source of truth for top-level scopes. Returns ordered list of
    (slug, name) for every top_scope, optionally filtered by publish_status
    ('active'/'hidden'/'review'). Order follows taxonomy.json top_scopes order."""
    _tops = load_taxonomy()["tops"]
    _out = []
    for _slug, _t in _tops.items():
        if publish_status is not None and _t["publish_status"] not in _as_set(publish_status):
            continue
        _out.append((_slug, _t["name"]))
    return _out


def top_scope_name(cslug):
    """Display name for a native top_scope slug (falls back to the slug)."""
    _t = load_taxonomy()["tops"].get(cslug)
    return _t["name"] if _t else cslug


def sku_publish_status(row):
    """I3: SKU-level publish_status gate. Defaults to 'active' for legacy rows missing
    the column, so nothing is hidden by accident."""
    ps = (row.get("publish_status") if isinstance(row, dict) else None)
    ps = (ps or "").strip().lower()
    return ps if ps in ("active", "hidden", "review") else "active"


def effective_publish_status(row):
    """I3: a SKU is hidden/review if EITHER its own publish_status OR its native top
    scope's publish_status is hidden/review. active+active => active."""
    _sku = sku_publish_status(row)
    if _sku == "hidden":
        return "hidden"
    _res = resolve_native(row.get("native_l1") if isinstance(row, dict) else None)
    _top = _res.get("publish_status", "active") if _res["status"] == "RESOLVED" else "active"
    if _top == "hidden" or _sku == "hidden":
        return "hidden"
    if _top == "review" or _sku == "review":
        return "review"
    return "active"


# I4: top-level scopes are DERIVED from the v2 taxonomy (NOT a hardcoded 6-list).
# Mirrors native_top_scopes() (all 13 native canonical groups) so legacy call sites /
# build wrappers that import gen_parts.TOP_CATEGORIES keep working while the single
# source of truth stays the taxonomy file.
TOP_CATEGORIES = {_s: _n for _s, _n in native_top_scopes()}

# I3: public-facing scope set = ACTIVE top scopes only (hidden/review excluded from the
# hub navigation + sitemap). The single source of truth stays the taxonomy file.
ACTIVE_TOP_CATEGORIES = {_s: _n for _s, _n in native_top_scopes("active")}


def resolve_taxonomy(raw_cat):
    """Backward-compatible alias (I3/I4): delegate to resolve_native(). The `raw_cat`
    argument is interpreted as a MASTER.native_l1 slug. Returns the legacy 4-state dict
    shape for the few legacy call sites still using it (native L1s resolve RESOLVED/UNMAPPED)."""
    _res = resolve_native(raw_cat)
    if _res["status"] == "RESOLVED":
        return {"status": "RESOLVED", "top": _res["top_slug"], "slug": _res["l1_slug"],
                "name": _res["l1_name"], "self_reference": False, "reason": None}
    return {"status": "UNMAPPED", "top": None, "slug": None, "name": raw_cat,
            "self_reference": False, "reason": "native_l1 not present in v2 taxonomy"}


def resolve_cat(fine_cat):
    """Compatibility wrapper: (top_slug, top_name) from a MASTER.native_l1 slug.
    Unknown native_l1 resolves to a sentinel ('__UNMAPPED__', 'Unmapped')."""
    _res = resolve_native(fine_cat)
    if _res["status"] == "RESOLVED":
        return _res["top_slug"], _res["top_name"]
    return "__UNMAPPED__", "Unmapped"


# ---------------------------------------------------------------------------
# Generation-phase taxonomy classifier (I3/I4 — 2026-09-10)
# Routes through resolve_native(); UNMAPPED is OBSERVED (recorded + counted) instead
# of silently collapsing onto a fake category. Returns (status, top_slug, top_name).
# For RESOLVED the returned top_slug/top_name are the NATIVE top scope (13 canonical
# groups), so every production SKU classifies to a native scope (not the old 6).
# ---------------------------------------------------------------------------
_TAXONOMY_GEN_STATS = {"RESOLVED": 0, "SELF_REFERENCE": 0, "UNMAPPED": 0, "COLLISION": 0}
_TAXONOMY_QUARANTINE = []  # raw native_l1 strings seen as UNMAPPED during generation


def reset_taxonomy_gen_state():
    """Clear generation-time counters before a (re)generation run."""
    global _TAXONOMY_GEN_STATS, _TAXONOMY_QUARANTINE
    _TAXONOMY_GEN_STATS = {"RESOLVED": 0, "SELF_REFERENCE": 0, "UNMAPPED": 0, "COLLISION": 0}
    _TAXONOMY_QUARANTINE = []


def get_taxonomy_gen_state():
    """Snapshot of generation-time taxonomy counters + quarantine list (for tests/reports)."""
    return dict(_TAXONOMY_GEN_STATS), list(_TAXONOMY_QUARANTINE)


def resolve_cat_state(raw_cat):
    """Generation-phase classifier. `raw_cat` is the MASTER native_l1 slug (I3/I4 contract).
    Returns (status, top_slug, top_name). Never raises; UNMAPPED quarantined.
    Signature preserved so call sites only need to pass row['native_l1'] instead of
    row['category']."""
    _res = resolve_native(raw_cat)
    _status = _res["status"]
    _TAXONOMY_GEN_STATS[_status] = _TAXONOMY_GEN_STATS.get(_status, 0) + 1
    if _status == "UNMAPPED":
        _TAXONOMY_QUARANTINE.append(raw_cat)
        return _status, "__UNMAPPED__", "Unmapped"
    return _status, _res["top_slug"], _res["top_name"]


def l3_page_should_skip(fine_cat):
    """I3/I4: an L3 sub-category page is SKIPPED when its native_l1 status is UNMAPPED
    (no valid L3). Native L1s never self-reference their top, so SELF_REFERENCE/COLLISION
    no longer apply. Pure helper for unit testing."""
    return resolve_cat_state(fine_cat)[0] in ("SELF_REFERENCE", "COLLISION", "UNMAPPED")

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
# The GENERATED component hub (generate_components_hub) renders a few "Popular
# Components" links from this map. Displayed model numbers do NOT always equal
# the generated slug (e.g. "LM358" -> slug "lm358dr"). This map is the single
# source of truth so we never guess the slug from the model string. A model
# with no entry (or whose slug is not generated yet) falls back to
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

def _faq_is_offbrand(answer):
    """True when a FAQ answer echoes a competitor (LCSC) or a hard price point.

    Our site is a China sourcing agent, not a price-comparison page; publishing a
    competitor's live pricing ("On LCSC... priced at $0.0008") is off-brand. Such
    entries are dropped at generation time (real pipeline content, minus the
    competitor-specific subset) so we never fabricate and never echo a rival.
    """
    return bool(re.search(r"LCSC|\$\s?\d", answer or "", re.I))


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
    # P1-2: show ONLY real SKU-specific FAQ. No generic fallback. Hide section if none.
    return pairs

def render_faq(pairs, pn):
    """Return (html_block, FAQ JSON-LD script). Empty when no real FAQ pairs."""
    if not pairs:
        return "", ""
    # Central off-brand guard: never render a FAQ that echoes a competitor (LCSC)
    # or a hard price point — our site is a sourcing agent, not a price page.
    # Applies to BOTH question and answer text.
    pairs = [(q, a) for (q, a) in pairs if not (_faq_is_offbrand(q) or _faq_is_offbrand(a))]
    if not pairs:
        return "", ""
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


# ---- FAQ source-priority merge (final rule, 2026-09-12) ------------------------
def _lcsc_faq_is_platform(q, a):
    """True when an LCSC/RAW FAQ is about LCSC platform/business (price, stock,
    buying, shipping, account, service) rather than the *product* itself.

    Rule: we may reuse LCSC's real product knowledge, but never its
    platform/commerce copy (price / stock / order / delivery / account /
    warranty-service). Conservative: only explicit commerce signals drop.
    """
    t = f"{(q or '')} {(a or '')}".lower()
    patterns = [
        r"\$\s?\d",                                         # hard price point
        r"\bprice\b|\bpricing\b|\bcost\b|\bquote\b",
        r"\bin stock\b|\bstock level\b|\binventory\b|\bmoq\b|\bminimum order",
        r"\bbuy\b|\bpurchase\b|\border online\b|\border now\b|\bplace an order\b|\badd to cart\b|\bcheckout\b",
        r"\bshipping\b|\bshipment\b|\bdeliver\b|\bdelivery\b|\blead[- ]?time\b",
        r"\baccount\b|\blogin\b|\bregister\b|\bsign[- ]?in\b",
        r"\blcsc\b|our website|the website|\bmarketplace\b",
        r"\breturn policy\b|\bwarranty claim\b|\bcustomer service\b|\btrack my order\b",
    ]
    for p in patterns:
        if re.search(p, t):
            return True
    return False


def _faq_approx_key(t):
    """Synonym-normalized question token set for light, dependency-free
    approximate dedup. e.g. 'Operational Temperature Range' ~ 'operating
    temperature range' (operational->operating), 'temp' ~ 'temperature'."""
    _SYN = {
        "operational": "operating", "operation": "operating",
        "temp": "temperature", "temps": "temperature",
        "spec": "specification", "specs": "specification",
        "params": "parameters", "param": "parameter",
        "pkg": "package", "packaging": "package",
        "ic": "chip", "mcu": "microcontroller",
        "max": "maximum", "min": "minimum",
        "vol": "voltage", "curr": "current",
        "freq": "frequency",
    }
    toks = _enrich_norm_text(t or "").split()
    toks = [_SYN.get(w, w) for w in toks]
    return frozenset(toks)


def _faq_is_dup(q1, a1, q2, a2):
    """Exact normalized OR approximate (synonym-set equal / subset) dedup."""
    if _enrich_norm_text(q1) == _enrich_norm_text(q2):
        return True
    k1, k2 = _faq_approx_key(q1), _faq_approx_key(q2)
    if not k1 or not k2:
        return False
    if k1 == k2:
        return True
    if k1 < k2 or k2 < k1:        # one is a subset of the other
        return True
    return False


def _print_faq_audit(pn, audit):
    """Human-readable FAQ merge audit for --single verification."""
    print(f"  [FAQ AUDIT] {pn}")
    print(f"    LCSC qualified (kept in full) : {audit['lcsc_qualified']}")
    print(f"    SZProcure self-gen (top-up)   : {audit['szprocure_self_gen']}")
    print(f"    enrichment adopted (capped)   : {audit['enrichment_used']}")
    print(f"    FINAL FAQ count               : {audit['final_count']}")
    if audit["filtered"]:
        print(f"    filtered (dropped):")
        for r, c in audit["filtered"].items():
            print(f"      - {r}: {c}")
    if audit["deduped"]:
        print(f"    deduped:")
        for r, c in audit["deduped"].items():
            print(f"      - {r}: {c}")


def merge_faqs(faq_raw, enrich, row):
    """Final FAQ merge: source priority + count control (rule confirmed 2026-09-12).

    Priority:
      Pass A  LCSC/RAW qualified product FAQs -> kept IN FULL (no truncation to 3).
      Pass B  SZProcure self-gen (MASTER.faq)  -> used ONLY to top up to 3 when
              LCSC qualified < 3. MASTER is never modified; only the adopted
              count is capped at the merge layer (never fabricate to pad).
      Pass C  enrichment FAQ                    -> minor supplement; only truly-new
              high-value product questions, capped, NEVER appended uncontrolled
              when LCSC>=3.
    off-brand (LCSC/price) filtered on BOTH question+answer at merge time;
    exact + approximate (synonym) dedup across all sources.
    Returns (pairs, audit_dict).
    """
    audit = {"lcsc_qualified": 0, "szprocure_self_gen": 0, "enrichment_used": 0,
             "final_count": 0, "filtered": {}, "deduped": {}}

    def _filt(reason):
        audit["filtered"][reason] = audit["filtered"].get(reason, 0) + 1

    def _ded(reason):
        audit["deduped"][reason] = audit["deduped"].get(reason, 0) + 1

    # ---- Pass A: LCSC / RAW qualified product FAQs (kept in full) ----
    lcsc_pairs = []
    raw_ext = _raw_section_extras(row)
    if raw_ext:
        seen = set()
        for q, a in (raw_ext.get("faqs") or []):
            if _faq_is_offbrand(q) or _faq_is_offbrand(a):
                _filt("offbrand"); continue
            if _lcsc_faq_is_platform(q, a):
                _filt("lcsc_platform_business"); continue
            key = _enrich_norm_text(q)
            if key in seen:
                _ded("lcsc_internal_dup"); continue
            seen.add(key)
            lcsc_pairs.append([q, a])
    audit["lcsc_qualified"] = len(lcsc_pairs)
    final = [list(p) for p in lcsc_pairs]

    # ---- Pass B: SZProcure self-gen (MASTER.faq) tops up to 3 when LCSC < 3 ----
    self_gen = []
    if len(final) < 3:
        for q, a in parse_faq(faq_raw, row.get("mpn", "")):
            if _faq_is_offbrand(q) or _faq_is_offbrand(a):
                _filt("selfgen_offbrand"); continue
            if len(final) >= 3:
                break
            if any(_faq_is_dup(q, a, fq, fa) for fq, fa in final):
                _ded("selfgen_dup_vs_lcsc"); continue
            final.append([q, a]); self_gen.append([q, a])
    audit["szprocure_self_gen"] = len(self_gen)

    # ---- Pass C: enrichment — truly-new, capped, never uncontrolled when rich ----
    enrich_used = []
    if enrich and enrich.get("faq"):
        cap = 0 if len(final) >= 3 else (3 - len(final))
        for f in enrich["faq"]:
            if len(enrich_used) >= cap:
                break
            q = f.get("question") if isinstance(f, dict) else None
            a = f.get("answer") if isinstance(f, dict) else None
            if not (q and a):
                continue
            if _faq_is_offbrand(q) or _faq_is_offbrand(a):
                _filt("enrich_offbrand"); continue
            if any(_faq_is_dup(q, a, fq, fa) for fq, fa in final):
                _ded("enrich_dup"); continue
            final.append([q, a]); enrich_used.append([q, a])
    audit["enrichment_used"] = len(enrich_used)
    audit["final_count"] = len(final)
    return final, audit


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
def seo_head(title, desc, url, img=None, noindex=False):
    _robots = "noindex, nofollow" if noindex else "index, follow"
    og_img = img or f"{DOMAIN}/assets/img/hero.svg"
    return f"""  <title>{title}</title>
  <meta name="description" content="{desc}" />
  <meta name="robots" content="{_robots}" />
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


# ==============================================================================
# COMPONENT HUB — GENERATED (P0-1). Never hand-built again.
# Every /components/<slug>/ page links "up" to this hub via its breadcrumb, so
# the hub MUST be emitted by the generator — otherwise every regen orphans it
# (the old bug: hand-built components/index.html vanished on each rebuild).
# ==============================================================================
COMPONENT_HUB_BLURB = {
    "integrated-circuits":      "Microcontrollers, memory, power-management and interface ICs.",
    "semiconductor-components": "MOSFETs, diodes, transistors and discrete power devices.",
    "passive-components":       "Resistors, capacitors, inductors and crystal oscillators.",
    "sensors":                  "Temperature, pressure, motion and optical sensors & transducers.",
    "connectors":               "Pin headers, USB, FFC/FPC and board-to-board connectors.",
    "modules":                  "WiFi, Bluetooth, GNSS and cellular communication modules.",
}

def generate_components_hub(generated_slugs=None):
    # DEPRECATED (P1-B2 / M2 — 2026-09-09): replaced by inject_hub_anchors() on the
    # normal publish path. Kept only for reference / rollback. Do NOT call from main().
    url = f"{DOMAIN}/components/"
    # FROZEN SEO head strings — locked by Phase D.3 freeze layer. Do not change.
    title = "Electronic Components — Source from Shenzhen, China | SZ Procure"
    desc = ("Browse electronic component categories we source from Shenzhen: "
            "integrated circuits, semiconductors, passives, sensors, connectors and "
            "modules. Request a quote for any part number.")
    # category cards (data-driven from TOP_CATEGORIES)
    cards = []
    for slug, name in TOP_CATEGORIES.items():
        blurb = COMPONENT_HUB_BLURB.get(slug, "")
        cards.append(f'''        <a class="card cat-card" href="/components/{slug}/">
          <h3>{esc(name)}</h3>
          <p>{esc(blurb)}</p>
          <span class="mfr-link">Browse {esc(name)} &rarr;</span>
        </a>''')
    cards_html = "\n".join(cards)
    # popular components (real, data-driven via POPULAR_SKU_MAP; falls back to RFQ)
    pop_items = "\n          ".join(
        f'<li><a href="{popular_href(model, generated_slugs)}">{esc(model)}</a></li>'
        for model in POPULAR_SKU_MAP
    )
    crumb = breadcrumb_jsonld([("Home", f"{DOMAIN}/"), ("Components", url)])
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
        <div class="eyebrow" data-zh="元器件类别">ELECTRONIC COMPONENT CATEGORIES</div>
        <h1 data-zh="我们采购的元器件类别">Component Categories We Source From Shenzhen</h1>
        <p class="lead" data-zh="从已验证的深圳供应渠道，为全球买家采购各类原装元器件。浏览类别或发送料号获取报价。">We source original electronic components from verified Shenzhen supply channels for global buyers. Browse a category or send a part number to request a quote.</p>
        <div class="hero-actions">
          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
          <a class="btn btn-outline-light btn-lg" href="#categories" data-zh="浏览类别">Browse Categories</a>
        </div>
        <div class="trust-bar">
          <span><b>&#10003;</b> <span data-zh="已验证供应渠道">Verified Supply Channels</span></span>
          <span><b>&#10003;</b> <span data-zh="原装元器件">Original Components</span></span>
          <span><b>&#10003;</b> <span data-zh="全球买家支持">Global Buyer Support</span></span>
          <span><b>&#10003;</b> <span data-zh="快速报价响应">Fast RFQ Response</span></span>
        </div>
      </div>
    </section>

    <!-- CATEGORY GRID -->
    <section class="section" id="categories">
      <div class="container">
        <div class="section-head">
          <div class="eyebrow" data-zh="按类别浏览">BROWSE BY CATEGORY</div>
          <h2 data-zh="元器件类别">Component Categories</h2>
          <p class="lead" data-zh="点击任意类别，查看我们采购的元器件并发起询价。">Click any category to view sourced components and request a quote.</p>
        </div>
        <div class="grid grid-3">
{cards_html}
        </div>
      </div>
    </section>

    <!-- POPULAR COMPONENTS -->
    <section class="section soft">
      <div class="container">
        <div class="section-head">
          <div class="eyebrow" data-zh="热门料号">POPULAR COMPONENTS</div>
          <h2 data-zh="常用料号">Popular Part Numbers</h2>
        </div>
        <ul class="bullet-list part-index">
          {pop_items}
        </ul>
      </div>
    </section>

    <!-- FINAL CTA -->
    <section class="section soft">
      <div class="container">
        <div class="cta-band">
          <div>
            <h2 data-zh="找不到需要的料号？">Can't Find the Part You Need?</h2>
            <p data-zh="发送准确的料号、制造商与数量，我们将核对库存并报价。">Send us the exact part number, manufacturer and quantity — we'll check availability and quote.</p>
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

# ===========================================================================
# Hub Injector (P1-B2 / M2 — 2026-09-09)
# Restricted, anchor-only injection into components/index.html. It NEVER rebuilds
# the file: it only replaces the content between the explicit HUB-INJECT anchors
# (NAV-START/END and SECTIONS-START/END). If an anchor is missing it FAILS (assert)
# rather than overwriting — this protects the hand-authored V2.4 shell / SEO / visuals.
#
# Self-reference subcategories (resolve_taxonomy status SELF_REFERENCE) are rendered
# as NON-LINK spans, so no /components/<top>/<top>/ 404 link is ever emitted.
# generate_components_hub() above is now DEPRECATED and must NOT be called from the
# normal publish path (main() routes to inject_hub_anchors() instead).
# ===========================================================================

def _hub_catalog_from_groups(groups):
    """Derive the data-driven catalog (counts + subcategory lists) from SKU groups.

    Only RESOLVED / SELF_REFERENCE fine categories contribute; UNMAPPED / COLLISION
    are quarantined (skipped) — consistent with l3_page_should_skip()."""
    load_taxonomy()
    cats = {top: {"name": ACTIVE_TOP_CATEGORIES[top], "count": 0, "subs": {}}
            for top in ACTIVE_TOP_CATEGORIES}
    for g in groups:
        raw = (g.get("native_l1") or "").strip()
        if not raw:
            continue
        res = resolve_taxonomy(raw)
        if res["status"] not in ("RESOLVED", "SELF_REFERENCE"):
            continue  # UNMAPPED / COLLISION -> quarantined, no contribution
        top = res["top"]
        if top not in cats:
            continue
        cats[top]["count"] += 1
        sub = cats[top]["subs"].setdefault(
            res["slug"],
            {"slug": res["slug"], "name": res["name"], "count": 0,
             "self_reference": bool(res.get("self_reference"))},
        )
        sub["count"] += 1
    out = {}
    for top in ACTIVE_TOP_CATEGORIES:
        subs = sorted(cats[top]["subs"].values(), key=lambda s: s["name"].lower())
        out[top] = {"name": cats[top]["name"], "count": cats[top]["count"], "subs": subs}
    return out


def _render_hub_nav(catalog):
    items = []
    for top in ACTIVE_TOP_CATEGORIES:
        c = catalog[top]
        items.append(
            '          <button type="button" class="catalog-nav-item" '
            'data-category="%s">%s<span class="cat-nav-count">%d</span></button>'
            % (top, esc(c["name"]), c["count"])
        )
    return "\n".join(items)


def _render_hub_sections(catalog):
    sections = []
    for top in ACTIVE_TOP_CATEGORIES:
        c = catalog[top]
        subs = []
        for s in c["subs"]:
            label = "%s<span class=\"cat-sub-count\">%d</span>" % (esc(s["name"]), s["count"])
            if s["self_reference"] or s["slug"] == top:
                # Non-link span: NEVER a /components/<top>/<top>/ URL (no 404).
                subs.append('                <span class="cat-sub">%s</span>' % label)
            else:
                href = "/components/%s/%s/" % (top, s["slug"])
                subs.append('                <a class="cat-sub" href="%s">%s</a>' % (href, label))
        subs_html = "\n".join(subs)
        sections.append(
            '        <section class="catalog-section" data-category="%s">\n'
            '          <header class="catalog-section-head">\n'
            '            <h3><a href="/components/%s/">%s</a> <span class="cat-count">%d</span></h3>\n'
            '            <button type="button" class="catalog-toggle" aria-expanded="true" '
            'aria-label="Toggle %s subcategories">&#9662;</button>\n'
            '          </header>\n'
            '          <div class="catalog-subs">\n%s\n          </div>\n'
            '        </section>' % (top, top, esc(c["name"]), c["count"], esc(c["name"]), subs_html)
        )
    return "\n".join(sections)


def _replace_hub_anchor(html, start_marker, end_marker, new_content):
    """Replace the text strictly BETWEEN start_marker and end_marker (markers preserved).
    Raises AssertionError if the anchor pair is absent — so a missing anchor can never
    silently trigger a whole-file rebuild."""
    pat = re.compile(re.escape(start_marker) + r".*?" + re.escape(end_marker), re.DOTALL)
    if not pat.search(html):
        raise AssertionError(
            "Hub injection anchor missing: %r ... %r. Refusing to rebuild "
            "components/index.html (V2.4 shell protection)." % (start_marker, end_marker)
        )
    return pat.sub("%s\n%s\n%s" % (start_marker, new_content, end_marker), html, count=1)


def inject_hub_anchors(hub_path, groups):
    """Inject the data-driven catalog into components/index.html BETWEEN the explicit
    HUB-INJECT anchors. Never rebuilds the file; fails (assert) if anchors are absent.
    Self-reference subcategories render as non-link spans (no /components/<top>/<top>/)."""
    with open(hub_path, encoding="utf-8") as _f:
        html = _f.read()
    catalog = _hub_catalog_from_groups(groups)
    nav = _render_hub_nav(catalog)
    sections = _render_hub_sections(catalog)
    html = _replace_hub_anchor(html, "<!-- HUB-INJECT:NAV-START -->",
                               "<!-- HUB-INJECT:NAV-END -->", nav)
    html = _replace_hub_anchor(html, "<!-- HUB-INJECT:SECTIONS-START -->",
                               "<!-- HUB-INJECT:SECTIONS-END -->", sections)
    with open(hub_path, "w", encoding="utf-8") as _f:
        _f.write(html)


def gen_part_page(row, cat_slug, mfr_slug, related=None, generated_slugs=None):
    pn = row["mpn"].strip()
    mfr = row["manufacturer"].strip()
    _cat_res = resolve_native(row.get("native_l1"))
    cat = _cat_res.get("l1_name") or (row.get("category") or "").strip()
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
    # P1-B1/I3/I4: generation-phase classifier over MASTER.native_l1 (UNMAPPED quarantined).
    status, cat_slug, cat_top = resolve_cat_state(row["native_l1"])
    cat_resolved = status in ("RESOLVED", "SELF_REFERENCE")
    ps = effective_publish_status(row)
    noindex = ps in ("hidden", "review")

    # ---- SEO copy: procurement language, Shenzhen/China sourcing keywords ----
    # Lead / overview emphasizes the BUYING scenario (global procurement from
    # Shenzhen supply chain), not just a spec description of the part.
    # P0-3: the VISIBLE Product Introduction now prefers the REAL description from the
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
    # understanding. Render ONLY real structured attributes from the source
    # master; never backfill with placeholder "See datasheet" rows (P1-3 cleanup).
    # This block lives BELOW the fold (section 3), never in the first screen.
    # Translate raw (often Chinese) attribute keys/values to English for the
    # public storefront. Unmappable CJK values are dropped (kept in MASTER);
    # the visible layer stays Chinese-free (permanent CJK gate).
    spec_pairs_en = translate_spec_pairs(spec_pairs)
    if not spec_pairs_en:
        # No English-renderable attributes from source — show an honest empty-state
        # note instead of placeholder rows.
        specs_html = (
            '<div class="spec-empty">'
            '<p>Detailed specifications and the official datasheet are available on request. '
            'Send the part number and our team will provide the full parameter table and documentation.</p>'
            '</div>'
        )
    else:
        # Real attributes only (English) — capped, never invented/placeholder values.
        all_spec_pairs = spec_pairs_en[:12]
        specs_table = "".join(
            f"<tr><th>{esc(human_attr_label(k))}</th><td>{esc(format_attr_value(k, v))}</td></tr>"
            for k, v in all_spec_pairs
        )
        specs_html = f'<table class="spec-table">\n<tbody>\n{specs_table}</tbody>\n</table>'

    # 1. Key Information table — lean, no stock/inventory wording
    qi_rows = []
    qi_rows.append(("Manufacturer", f'<a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a> <span class="muted small">Verified sourcing partner</span>'))
    qi_rows.append(("Part Number", esc(pn)))
    if cat_resolved:
        qi_rows.append(("Product Type", f'<a href="/components/{cat_slug}/">{esc(subcat or cat_top)}</a>'))
    else:
        qi_rows.append(("Product Type", esc(subcat or cat_top)))
    for k, v in spec_pairs_en:
        if k.lower() in ("package", "core"):
            qi_rows.append((human_attr_label(k), esc(format_attr_value(k, v))))
    if dsheet:
        qi_rows.append(("Datasheet", f'<a href="{esc(dsheet)}" target="_blank" rel="nofollow noopener">{esc(pn)} Datasheet (PDF) ↧</a>'))
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
    alts_html = "".join(alts_html_items)
    if alts_html:
        NL = chr(10)
        alt_section_html = (
            "<h2>Alternative Parts</h2>" + NL +
            f"<p>Common <strong>{esc(pn)} alternatives</strong> overseas buyers search for:</p>" + NL +
            f'<ul class="alt-list">{alts_html}</ul>' + NL +
            f'<p class="muted small">Looking for "{esc(pn)} alternative"? Tell us your requirement in the quote form.</p>'
        )
    else:
        alt_section_html = ""
    apps_html = "".join(f"<li>{esc(x)}</li>" for x in apps_list) or "<li>—</li>"
    # P0-3: Common Applications block — render ONLY when REAL applications data
    # exists in the source master. PDF/CSV has it -> show it; has nothing -> show
    # nothing (honest degradation, never a placeholder "—" / "N/A" block).
    if apps_list:
        apps_items = "".join(f"<li>{esc(x)}</li>" for x in apps_list)
        apps_section = (
            '<section class="section apps-section">\n'
            '  <div class="container">\n'
            '    <h2>Applications</h2>\n'
            f'    <ul class="alt-list">{apps_items}</ul>\n'
            '  </div>\n'
            '</section>'
        )
    else:
        apps_section = ""

    # 4. Sourcing Information — minimal, restrained service note (P0-4).
    # No long marketing copy; no invented stock / availability / authorized-agent /
    # lowest-price claims. PDF/CSV drives everything; nothing is fabricated.
    sourcing_html = (
        f"<p>SZ Procure is a sourcing partner for <strong>{esc(pn)}</strong>, not a stock "
        f"catalog. We help international buyers source original components from the China "
        f"electronics supply chain — send your quantity and target price for a quotation.</p>"
    )

    # FAQ block + FAQ schema
    faq_html, faq_jsonld = render_faq(faq_pairs, pn)
    if faq_html:
        faq_section_html = "<h2>Frequently Asked Questions</h2>" + chr(10) + faq_html
    else:
        faq_section_html = ""

    # Related Products (same top-category) — internal links form a product web.
    if related and cat_resolved:
        rel_items = "".join(
            f'<li><a href="/products/{oslug}/" class="alt-link">{esc(opn)}</a></li>'
            for opn, oslug in related
        )
        related_html = (f'<h2>Related {esc(cat_top)}</h2>'
                        f'<p>Other {esc(cat_top).lower()} we help global buyers source:</p>'
                        f'<ul class="alt-list">{rel_items}</ul>')
    elif related:
        # UNMAPPED/COLLISION: category is quarantined -> neutral "Related Parts" (no broken link)
        related_html = '<h2>Related Parts</h2>'
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
    sub_crumb = (f'<a href="/components/{cat_slug}/{fine_slug}/">{esc(cat)}</a> › '
                 if (cat and cat_resolved) else "")
    crumb_items = [
        ("Home", f"{DOMAIN}/"),
        ("Components", f"{DOMAIN}/components/"),
    ]
    if cat_resolved:
        crumb_items.append((cat_top, f"{DOMAIN}/components/{cat_slug}/"))
    if cat and cat_resolved:
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
    "mpn": "{esc(pn)}",
    "category": "{esc(cat_top)}",
    "brand": {{ "@type": "Brand", "name": "{esc(mfr)}", "@id": "https://www.szprocure.com/#szprocure-org" }},
    "description": "{esc(schema_overview)}",
    "url": "{url}"{(", \"alternatePart\": [" + alt_ld + "]") if alt_ld else ""}
  }}
  </script>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
{seo_head(title, desc, url, og_img, noindex=noindex)}
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
      {('' if not cat_resolved else f'<a href="/components/{cat_slug}/">{esc(cat_top)}</a> ›')}
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
            <a class="btn btn-primary btn-lg" href="/request-a-quote/?pn={urlquote(pn)}&mfr={urlquote(mfr)}&cat={urlquote(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
            <a class="btn btn-outline" href="https://wa.me/8613530888389?text=Hi%20SZ%20Procure,%20I%20need%20{esc(pn)}">WhatsApp</a>
            <a class="link-cta" href="mailto:sales@szprocure.com?subject=Quote%20for%20{esc(pn)}">Email</a>
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
          <a class="btn btn-primary btn-block" href="/request-a-quote/?pn={urlquote(pn)}&mfr={urlquote(mfr)}&cat={urlquote(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
          {datasheet_html}
          <p class="muted small">sales@szprocure.com · WhatsApp</p>
        </div>
      </div>
    </section>

    <section class="section">
      <div class="container two-col">
        <div class="part-main">
          <!-- 2. Product Introduction (SEO, not encyclopedia) -->
          <h2>Product Introduction</h2>
          <p>{overview}</p>

          <!-- 3. Technical Specifications -->
          <h2>Technical Specifications</h2>
          {specs_html}

          {apps_section}

          <!-- 4. Sourcing Information (the moat) -->
          <h2>Sourcing Information</h2>
          {sourcing_html}

          {alt_section_html}

          <!-- 5b. Related Products (same-category spider-web) -->
          {related_html}

          {faq_section_html}
          {ref_block}
        </div>

        <aside class="part-aside">
          <!-- Sticky Quote Card (desktop) -->
          <div class="card sticky-card desk-sticky">
            <h3>Need this component?</h3>
            <p>Send the part number and quantity.</p>
            <a class="btn btn-primary btn-block" href="/request-a-quote/?pn={urlquote(pn)}&mfr={urlquote(mfr)}&cat={urlquote(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
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
        <a class="btn btn-primary btn-lg" href="/request-a-quote/?pn={urlquote(pn)}&mfr={urlquote(mfr)}&cat={urlquote(cat)}&source=product&rfq_type=sku_quote" data-zh="获取报价">Request a Quote</a>
      </div>
    </section>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
{ga4_script()}
</body>
</html>"""


# ==============================================================================
# PART PAGE — V3 (ADDITIVE renderer; visual-only change, data/SEO unchanged)
# ==============================================================================
# V3 is an ADDITIVE renderer that reuses the SAME SEO head, schema, breadcrumb,
# global header/footer shell and RFQ business logic as V2 (gen_part_page). Only
# the SKU content area + layout differ (left hero + right sticky RFQ + sticky
# scroll-spy tabs). It renders ONLY real MASTER data — no fabricated/inferred
# specs, no price/stock/lead-time, no N/A. Route: any MPN in V3_MPNS renders via
# this function; every other SKU keeps the V2 renderer. V2 is NEVER deleted.
# V3 styles come from assets/sku-v3.css (scoped to .sku-v3), linked externally.
V3_MPNS = {"1.0-4PWB", "1909763-1", "1N4148W", "1N4148W-7-F", "1N4148WS", "1N5819HW-7-F", "1N5819WS", "2.54-1*40L=15MM", "2.54-1*40P", "2.54-1*4P", "2.54-1X6P", "2N7002", "2N7002,215", "2N7002K-T1-GE3", "2N7002LT1G", "59170-1-S-00-D", "74HC14D,653", "74HC165D,653", "74HC595D,118", "ACS712ELCTR-20A-T", "AD623ARZ-R7", "AD7192BRUZ-REEL", "AD8605ARTZ-REEL7", "ADG719BRTZ-REEL7", "ADM2582EBRWZ-REEL7", "ADM2587EBRWZ-REEL7", "ADM3251EARWZ-REEL", "ADS1015IDGSR", "ADS1115IDGSR", "ADS1115IDGST", "ADS1220IPWR", "ADUM1201ARZ-RL7", "ADUM1201BRZ-RL7", "ADUM1250ARZ-RL7", "ADUM3160BRWZ-RL", "ADUM4160BRWZ-RL", "ADXL345BCCZ-RL7", "AHT20", "AMS1117-3.3", "AMS1117-5.0", "AO3400A", "AO3401A", "AO3407A", "AO4407A", "AP2112K-3.3TRG1", "AP3012KTR-G1", "AP63203WU-7", "AP63205WU-7", "AP64350SP-13", "AS5047P-ATSM", "AS5600-ASOT", "AT24C02C-SSHM-T", "AT32F415CBT7", "AT7456E", "ATMEGA1284P-AU", "ATMEGA128A-AU", "ATMEGA2560-16AU", "ATMEGA328P-AU", "ATMEGA328P-MU", "ATMEGA328P-PU", "ATMEGA328PB-AU", "ATMEGA32A-AU", "ATMEGA64A-AU", "ATMEGA88PA-AU", "ATSAMD21G18A-AU", "ATTINY1616-MNR", "ATXMEGA64A3U-AU", "B0505S-1WR3", "B1212S-1WR3", "B2B-PH-K-S(LF)(SN)", "B2B-XH-A(LF)(SN)", "B2P-VH(LF)(SN)", "B340A-13-F", "B3B-PH-K-S(LF)(SN)", "B3B-XH-A(LF)(SN)", "B4B-PH-K-S(LF)(SN)", "B4B-XH-A(LF)(SN)", "B560C-13-F", "B5B-XH-A(LF)(SN)", "B6B-PH-K-S(LF)(SN)", "B6B-XH-A(LF)(SN)", "BAS16J,115", "BAS316,115", "BAS516,115", "BAT46WJ,115", "BAT54,215", "BAT54C,215", "BAT54S", "BAT54SLT1G", "BAV70,215", "BAV99,215", "BAV99LT1G", "BC817-40,215", "BLM15AG601SN1D", "BLM15PD121SN1D", "BLM15PX121SN1D", "BLM18AG102SN1D", "BLM18AG601SN1D", "BLM18EG221SN1D", "BLM18KG121TN1D", "BLM18KG601SN1D", "BLM18PG121SN1D", "BLM18PG471SN1D", "BLM18SG121TN1D", "BLM21AG102SN1D", "BLM21PG121SN1D", "BLM21PG221SN1D", "BLM21PG300SN1D", "BLM21PG331SN1D", "BLM31PG121SN1L", "BLM31PG601SN1L", "BM04B-SRSS-TB(LF)(SN)", "BME280", "BMI088", "BMI270", "BQ24075RGTR", "BQ25185DLHR", "BQ25798RQMR", "BSS123", "BSS138", "BSS138-7-F", "BSS138LT1G", "BSS84LT1G", "BWSMA-KWE-Z001", "CH32V103C8T6", "CH340C", "CL10B104KB8NNNC", "CLRC66303HNY", "CP2102-GMR", "CP2102N-A02-GQFN20R", "CP2102N-A02-GQFN28R", "CRCW0402100KFKED", "CRCW040210K0FKED", "CRCW06030000Z0EA", "CRCW060310K0FKEA", "CSD25402Q3A", "CUS10S30,H3F", "DFE252012F-1R0M=P2", "DFE252012P-1R0M=P2", "DLW21HN900SQ2L", "DM3AT-SF-PEJM5", "DP83848IVVX/NOPB", "DPS368XTSA1", "DRV2605LDGSR", "DRV8833PWPR", "DRV8874PWPR", "DS18B20+", "DS18B20U(UMW)", "DW01A", "ERJ2GE0R00X", "ESD5Z3.3T1G", "ESD5Z5.0T1G", "ESD9L5.0ST5G", "ESP-12F(ESP8266MOD)", "ESP32-C3-MINI-1-N4", "ESP32-C3FH4", "ESP32-S3-WROOM-1-N16R8", "ESP32-S3-WROOM-1-N8", "ESP32-S3-WROOM-1-N8R8", "ESP32-S3-WROOM-1U-N16R8", "ESP32-WROOM-32D-N4", "ESP32-WROOM-32E", "ESP32-WROOM-32E-N16", "ESP32-WROOM-32E-N4", "ESP32-WROOM-32E-N8", "ESP32-WROOM-32UE-N16", "FDV301N", "FRC0402F1002TS", "FRC0603F0000TS", "FRC0603F1000TS", "FRC0603F1001TS", "FRC0603F1002TS", "FRC0603F1003TS", "FRC0603F1004TS", "FRC0603F2002TS", "FRC0603F4701TS", "FRC0603F4702TS", "FRC0603F5101TS", "FRC0603J102 TS", "FRC0603J103 TS", "FRC0805F1001TS", "FRC0805F1002TS", "FRC0805F4701TS", "FS8205A", "FT232RL-REEL", "FT232RNQ-REEL", "FT234XD-R", "G5NB-1A-E-DC5V", "GCM155R71H104KE02D", "GCM188R71E105KA64D", "GCM21BR72A104KA37L", "GD25Q64ESIGR", "GD32F303RCT6", "GP2S+", "GRM035R60J475ME15D", "GRM1555C1H100JA01D", "GRM1555C1H101JA01D", "GRM1555C1H102JA01D", "GRM155R60J226ME11D", "GRM155R61E105KA12D", "GRM155R61H105KE05D", "GRM155R71H103KA88D", "GRM155R71H104KE14D", "GRM155Z71A105KE01D", "GRM1885C1H103JA01D", "GRM188C61E226ME01D", "GRM188R60J476ME15D", "GRM188R61A106KE69D", "GRM188R61A226ME15D", "GRM188R61E106KA73D", "GRM188R61E475KE11D", "GRM188R6YA106MA73D", "GRM188R71H104KA93D", "GRM188Z71A106KA73D", "GRM21BR60J107ME15L", "GRM21BR61A476ME15L", "GRM21BR61E106KA73L", "GRM21BR61H106KE43L", "GRM21BR6YA106KE43L", "GRM21BR71H105KA12L", "GRM21BZ71A226ME15L", "GRM21BZ71E106KE15L", "GRM31C5C1H104JA01L", "GRM31CC72A475KE11L", "GRM31CR61A107MEA8L", "GRM31CR61E476ME44L", "GRM31CR71E106KA12L", "GRM31CR71H475KA12L", "GRM32EC72A106KE05L", "GRM32ER61C476KE15L", "GRM32ER71E226KE15L", "GRM32ER71H106KA12L", "GT-USB-7010ASV", "HLK-PM01", "HR4988E", "HS96L03W2C03", "HX711", "INA180A1IDBVR", "INA180A2IDBVR", "INA219AIDCNR", "INA219AIDR", "INA226AIDGSR", "INA3221AIRGVR", "INA333AIDGKR", "IRF3205PBF", "IRF540NPBF", "IRF9540NPBF", "IRFB4110PBF", "IRFR5305TRPBF", "IRFZ44NPBF", "IRLML6344TRPBF", "IRLML6402TRPBF", "ISM330DHCXTR", "ISO1044BDR", "ISO1050DUBR", "ISO1540DR", "ISO3082DWR", "JSM6288Q", "KT-0603R", "L5973D013TR", "L7805CV", "L78L05ABUTR", "L78M05ABDT-TR", "L78M05CDT-TR", "LAN8720A-CP-TR", "LAN8720AI-CP-TR", "LAN8742A-CZ-TR", "LAN8742AI-CZ-TR", "LD1117S33CTR", "LD1117S33TR", "LDL1117S33R", "LIS2DH12TR", "LIS2DW12TR", "LIS3DHTR", "LIS3MDLTR", "LL4148-GS08", "LM1117IMPX-3.3/NOPB", "LM1117MPX-3.3/NOPB", "LM13700MX/NOPB", "LM2596SX-5.0/NOPB", "LM317AEMPX/NOPB", "LM317MDT-TR", "LM339DR", "LM358DR", "LM358DR2G", "LM358DT", "LM35DZ/NOPB", "LM393DR", "LM393DR2G", "LM5116MHX/NOPB", "LM5164DDAR", "LMV321IDBVR", "LP2985-33DBVR", "LP5907MFX-3.3/NOPB", "LPC1765FBD100K", "LPC1768FBD100K", "LPC824M201JHI33Y", "LSM303AGRTR", "LSM6DS3TR-C", "LSM6DSLTR", "LSM6DSOXTR", "LSM6DSRTR", "LSM6DSV16XTR", "LSM6DSVTR", "LT3045EDD#TRPBF", "LTC6811IG-1#3ZZTRPBF", "LTM4671EY#PBF", "MAX-M10S-00B", "MAX17048G+T10", "MAX31855KASA+T", "MAX31856MUD+T", "MAX31865AAP+T", "MAX31865ATP+T", "MAX3232CDR", "MAX3232EIPWR", "MAX6675ISA+T", "MAX98357AETE+T", "MBR0520LT1G", "MBR0540T1G", "MBRA340T3G", "MC33063ADR", "MCP1700T-3302E/TT", "MCP2551-I/SN", "MCP4725A0T-E/CH", "MCP6001T-I/OT", "MCP6002T-I/SN", "MCP73831T-2ACI/OT", "MCP9700AT-E/TT", "MFRC52202HN1,151", "MKL17Z64VFM4", "MSP430F149IPMR", "MSP430F247TPMR", "MT3608", "NCD0805G1", "NCD0805R1", "NCP1117ST33T3G", "NCP15XH103F03RC", "NCP18XH103F03RB", "NE5532DR", "NE555DR", "NE555P", "NUP2105LT1G", "OPA1612AIDR", "OPA1656IDR", "OPA197IDBVR", "P82B715DR", "PCA9306DCTR", "PCM5102APWR", "PE4312C-Z", "PESD0402-140", "PESD1CAN,215", "PESD2CAN,215", "PESD3V3S2UT,215", "PESD5Z3.3,115", "PIC16F1933-I/SS", "PMEG6010CEH,115", "PMEG6010CEJ,115", "PRTR5V0U2F,115", "PZ254V-11-02P", "PZ254V-11-03P", "PZ254V-11-04P", "Q13FC13500004", "RC0402FR-070RL", "RC0402FR-07100KL", "RC0402FR-0710KL", "RC0402FR-071KL", "RC0402FR-074K7L", "RC0402FR-075K1L", "RC0603FR-07100KL", "RC0603FR-0710KL", "RC0603FR-071KL", "RC0603FR-07330RL", "RC0603FR-074K7L", "RC0603JR-070RL", "RC0805FR-071KL", "REF3030AIDBZR", "REF3033AIDBZR", "RFX2401C", "RP2040", "RT0603BRD0710KL", "S4B-XH-A(LF)(SN)", "SC-32S32.768KHZ20PPM12.5PF", "SGT50T65FD1PN", "SI2301CDS-T1-GE3", "SI2302CDS-T1-GE3", "SK6812MINI-E", "SM02B-SRSS-TB(LF)(SN)", "SM06B-SRSS-TB(LF)(SN)", "SM08B-SRSS-TB(LF)(SN)", "SM4007PL", "SM712-02HTG", "SMAJ5.0A", "SN65HVD230DR", "SN65HVD232DR", "SN65HVD233DR", "SN65HVD75DR", "SN74HC14DR", "SN74HC595DR", "SN74LVC1G08DBVR", "SN74LVC1G14DBVR", "SN74LVC1G17DCKR", "SN74LVC1G3157DCKR", "SN74LVC1T45DBVR", "SN74LVC2G17DCKR", "SN74LVC2T45DCUR", "SN74LVC8T245PWR", "SN75176BDR", "SPX3819M5-L-3-3/TR", "SRD-05VDC-SL-C", "SRD-12VDC-SL-C", "SRV05-4.TCT", "SS14", "SS34", "SS54", "STM32F030C8T6", "STM32F030F4P6TR", "STM32F030K6T6", "STM32F072CBT6", "STM32F103C8T6", "STM32F103CBT6", "STM32F103RCT6", "STM32F103RET6", "STM32F401CCU6", "STM32F405RGT6", "STM32F407VET6", "STM32F407VGT6", "STM32F407ZGT6", "STM32F412RET6", "STM32F429IGT6", "STM32F446RCT6", "STM32F446RET6", "STM32F722RET6", "STM32G030F6P6TR", "STM32G031G8U6", "STM32G070CBT6", "STM32G070RBT6", "STM32G0B1CBT6", "STM32G431CBT6", "STM32G431KBU6", "STM32G474RET6", "STM32H723VGH6", "STM32H723VGT6", "STM32H723ZGT6", "STM32H743VIH6", "STM32H743VIT6", "STM32H743ZIT6", "STM32H750VBT6", "STM32L010F4P6", "STM8S003F3P6TR", "STM8S003K3T6CTR", "STM8S103K3T6CTR", "SWPA4030S100MT", "TCA9548APWR", "TF PUSH", "THVD1450DR", "TL072CDR", "TL431AIDBZR", "TLC555CDR", "TLP350(TP1,F)", "TLV3201AIDBVR", "TLV62569DBVR", "TLV75533PDBVR", "TLV75733PDBVR", "TLV76733DRVR", "TMC2209-LA-T", "TMC5160A-TA-T", "TMP102AIDRLR", "TMP117AIDRVR", "TMS320F28035PAGT", "TMS320F28069PZT", "TMS320F28335PGFA", "TP4056-42-ESOP8", "TPD1E10B06DPYR", "TPD4E05U06DQAR", "TPD4EUSB30DQAR", "TPL5010DDCR", "TPS2116DRLR", "TPS22810DRVR", "TPS22917DBVR", "TPS22918DBVR", "TPS22919DCKR", "TPS54202DDCR", "TPS54302DDCR", "TPS5430DDAR", "TPS54331DR", "TPS54360DDAR", "TPS54560DDAR", "TPS61023DRLR", "TPS62840DLCR", "TPS62933DRLR", "TPS63020DSJR", "TPS63070RNMR", "TPS631000DRLR", "TPS63802DLAR", "TPS63900DSKR", "TPS70933DBVR", "TPS7A2033PDBVR", "TPS7A4700RGWR", "TS-1088-AR02016", "TS-1187A-B-A-B", "TXB0102DCUR", "TXB0104PWR", "TXB0108PWR", "TXS0102DCTR", "TXS0102DCUR", "TXS0104EPWR", "TXS0108EPWR", "TYPE-C 16PIN 2MD(073)", "TYPE-C 6P(073)", "TYPE-C-31-M-12", "TYPE-C-31-M-31", "U.FL-R-SMT-1(10)", "U.FL-R-SMT-1(80)", "ULN2003ADR", "ULN2003D1013TR", "ULN2803CDWR", "USB2514BI-AEZG-TR", "USBLC6-2P6", "USBLC6-2SC6", "USBLC6-4SC6", "VL53L0CXV0DH/1", "VL53L1CXV0FY/1", "VL53L4CDV0DH/1", "W25N02KVZEIR", "W25Q128JVEIQ TR", "W25Q128JVPIM TR", "W25Q128JVSIQ", "W25Q16JVSNIQ", "W25Q16JVSSIQ", "W25Q32JVSSIQ", "W25Q64JVSSIQ", "W25Q80DVSNIG TR", "W5500", "WS2812B-B/W", "WS2812B-V5/W", "X322512MSB4SI", "X322516MLB4SI", "X322516MRB4SI", "X322525MOB4SI", "X32258MOB4SI", "XC6206P332MR-G", "XL-1608SURC-06", "XL-1608UBC-04", "XL-1608UGC-04", "XL-2012SURC", "XL-2012UGC", "YLED0603B", "YLED0603G", "YLED0603R", "ZX-PZ2.54-1-16PZZ"}


# ---------------------------------------------------------------------------
# Risk #1 (2026-09-08): V3 is now the DEFAULT renderer. The historical V3_MPNS
# elected-list above is RETAINED for reference but NO LONGER drives routing.
# A new, empty allow-list opts specific SKUs BACK to the legacy V2 renderer only
# when explicitly required (e.g. a future legacy exception). Empty by default =>
# every SKU renders via gen_part_page_v3. V2 is preserved, never deleted.
# ---------------------------------------------------------------------------
V2_LEGACY_EXCEPTIONS = set()

# ---------------------------------------------------------------------------
# RoHS compliance badge (V3 template rule)
# ---------------------------------------------------------------------------
# RoHS compliance badge — authoritative source is the 01-collected A RAW
# (data/raw/lcsc_http_scale500/C*.json, source_raw.main_product.{isRohsCert,
# rohsCertType, rohsCertList}). This ALIGNS the Hero badge with the Compliance
# section (_get_features_compliance_index), which already reads the same A RAW —
# closing the previous dual-source inconsistency where the badge used the legacy
# B snapshot (lcsc_api_FULL_*.json) while the Compliance section used A.
# Build-time only (gitignored RAW): baked into static HTML, NO runtime dep,
# MASTER/parts.json NOT touched. NEVER fabricates: a badge is emitted ONLY when
# isRohsCert is true AND real evidence (rohsCertType / non-empty rohsCertList)
# exists. Every other state (missing / unknown / unmatched / evidence
# insufficient) returns '' (no badge).
_ROHS_INDEX = None
_ROHS_SRC_GLOB = "data/raw/lcsc_http_scale500/C*.json"


def _get_rohs_index():
    """Lazy-load A scale500 RAW -> {MPN_or_CODE_upper: main_product}. Empty dict on any failure."""
    global _ROHS_INDEX
    if _ROHS_INDEX is not None:
        return _ROHS_INDEX
    _ROHS_INDEX = {}
    import glob as _glob
    here = os.path.dirname(os.path.abspath(__file__))
    for fp in _glob.glob(os.path.join(here, _ROHS_SRC_GLOB)):
        try:
            with open(fp, encoding="utf-8") as fh:
                d = json.load(fh)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        src = d.get("source_raw", d)
        if not isinstance(src, dict):
            continue
        mp = src.get("main_product", {}) or {}
        if not isinstance(mp, dict):
            continue
        pm = (mp.get("productModel") or "").strip().upper()
        pc = (mp.get("productCode") or "").strip().upper()
        if pm:
            _ROHS_INDEX[pm] = mp
        if pc:
            _ROHS_INDEX[pc] = mp
    return _ROHS_INDEX


def rohs_badge_html(row):
    """Return the green RoHS badge HTML, or '' when not compliant / no evidence.

    Conditions (site policy): rohs_status == compliant (isRohsCert truthy) AND
    reliable evidence/provenance exists (rohsCertType or non-empty rohsCertList).
    """
    idx = _get_rohs_index()
    if not idx:
        return ""
    mpn = (row.get("mpn") or "").strip().upper()
    lcsc = (row.get("supplier_reference") or "").strip().upper()
    it = idx.get(mpn) or idx.get(lcsc)
    if not it:
        return ""
    if not it.get("isRohsCert"):
        return ""
    cert_type = it.get("rohsCertType")
    cert_list = it.get("rohsCertList") or []
    has_evidence = bool(cert_type) or (isinstance(cert_list, list) and len(cert_list) > 0)
    if not has_evidence:
        return ""
    return '<span class="rohs-badge" aria-hidden="true">RoHS</span>'


# ---------------------------------------------------------------------------
# Brand classification: LCSC flags Asian-brand parts via `isAsianBrand` (bool)
# in the 01-collected scale500 RAW (data/raw/lcsc_http_scale500/C*.json).
# Loaded at generation time (no MASTER/parts.json schema change, no runtime dep)
# — mirrors the RoHS index pattern. NEVER self-classifies: emits "Asian Brands"
# ONLY when isAsianBrand is literally true; every other state returns '' (no tag).
_ASIAN_INDEX = None
_ASIAN_SRC_GLOB = "data/raw/lcsc_http_scale500/C*.json"


def _get_asian_index():
    """Lazy-load scale500 RAW -> {CODE_or_MPN_upper: isAsianBrand(bool)}. Empty on failure."""
    global _ASIAN_INDEX
    if _ASIAN_INDEX is not None:
        return _ASIAN_INDEX
    _ASIAN_INDEX = {}
    import glob as _glob
    here = os.path.dirname(os.path.abspath(__file__))
    for fp in _glob.glob(os.path.join(here, _ASIAN_SRC_GLOB)):
        try:
            with open(fp, encoding="utf-8") as fh:
                d = json.load(fh)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        src = d.get("source_raw", d)
        mp = src.get("main_product", {}) or {}
        if not isinstance(mp, dict):
            continue
        pc = (mp.get("productCode") or "").strip().upper()
        pm = (mp.get("productModel") or "").strip().upper()
        flag = mp.get("isAsianBrand")
        if pc:
            _ASIAN_INDEX[pc] = flag
        if pm:
            _ASIAN_INDEX[pm] = flag
    return _ASIAN_INDEX


# ---------------------------------------------------------------------------
# Features + Compliance & Export Codes: sourced from the 01-collected scale500
# RAW at generation time (mirrors the Asian-Brands index). NEVER fabricated —
# a section is emitted ONLY when the real field exists in the RAW.
#   Features                -> source_raw.overviewData.productFeaturesEn
#   ECCN / HTS(US) / RoHS   -> source_raw.main_product.{eccn, htsMap.US, isRohsCert}
_FEATURES_COMPLIANCE_INDEX = None
_FC_SRC_GLOB = "data/raw/lcsc_http_scale500/C*.json"


def _get_features_compliance_index():
    """Lazy-load scale500 RAW -> {CODE_or_MPN_upper: dict(features, eccn, hts_us, rohs, rohs_type)}."""
    global _FEATURES_COMPLIANCE_INDEX
    if _FEATURES_COMPLIANCE_INDEX is not None:
        return _FEATURES_COMPLIANCE_INDEX
    _FEATURES_COMPLIANCE_INDEX = {}
    import glob as _glob
    here = os.path.dirname(os.path.abspath(__file__))
    for fp in _glob.glob(os.path.join(here, _FC_SRC_GLOB)):
        try:
            with open(fp, encoding="utf-8") as fh:
                d = json.load(fh)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        src = d.get("source_raw", d)
        if not isinstance(src, dict):
            continue
        mp = src.get("main_product", {}) or {}
        if not isinstance(mp, dict):
            continue
        od = src.get("overviewData", {}) or {}
        pc = (mp.get("productCode") or "").strip().upper()
        pm = (mp.get("productModel") or "").strip().upper()
        features = (od.get("productFeaturesEn") or "").strip()
        # Product Introduction: official narrative from the 01-collected RAW. PRIMARY
        # source is overviewData.productIntroEn; main_product.productIntroEn is the
        # short variant used as a fallback when the overviewData one is empty. This is
        # the real pipeline content (铁律: all SKU copy comes from the pipeline), NOT
        # AI-generated — it wires the field the 01 adapter had been discarding.
        intro = (od.get("productIntroEn") or "").strip()
        intro_short = (mp.get("productIntroEn") or "").strip()
        eccn = (mp.get("eccn") or "").strip()
        hts_map = mp.get("htsMap") or {}
        if not isinstance(hts_map, dict):
            hts_map = {}
        # Keep the full country->HTS map so the Compliance table can render every
        # country variant present in the 01-collected RAW (US, CN, CA, BR, IN, MX, TARIC).
        hts_us = (hts_map.get("US", "") or "").strip()
        rohs = bool(mp.get("isRohsCert"))
        rohs_type = (mp.get("rohsCertType") or "").strip()
        rec = {
            "features": features,
            "intro": intro,
            "intro_short": intro_short,
            "eccn": eccn,
            "hts_map": hts_map,
            "hts_us": hts_us,
            "rohs": rohs,
            "rohs_type": rohs_type,
        }
        if pc:
            _FEATURES_COMPLIANCE_INDEX[pc] = rec
        if pm:
            _FEATURES_COMPLIANCE_INDEX[pm] = rec
    return _FEATURES_COMPLIANCE_INDEX


# ---------------------------------------------------------------------------
# Applications / FAQ / Alternative Parts: sourced from the 01-collected scale500
# RAW at generation time (mirrors the Features/Compliance index). NEVER fabricated
# — a section is emitted ONLY when the real field exists in the RAW.
#   Applications  -> source_raw.overviewData.pdfApplicationAreasEn (non-empty lines)
#   FAQ           -> source_raw.main_product.faqs[] (question+answer, real pipeline copy)
#   Alternatives  -> alternatePartList[] where hasAlternatePart is True (real MPN only)
_SECTION_EXTRAS_INDEX = None
_SEXTRA_SRC_GLOB = "data/raw/lcsc_http_scale500/C*.json"


def _get_section_extras_index():
    """Lazy-load scale500 RAW -> {CODE_or_MPN_upper: dict(apps, faqs, alts)}."""
    global _SECTION_EXTRAS_INDEX
    if _SECTION_EXTRAS_INDEX is not None:
        return _SECTION_EXTRAS_INDEX
    _SECTION_EXTRAS_INDEX = {}
    import glob as _glob
    here = os.path.dirname(os.path.abspath(__file__))
    for fp in _glob.glob(os.path.join(here, _SEXTRA_SRC_GLOB)):
        try:
            with open(fp, encoding="utf-8") as fh:
                d = json.load(fh)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        src = d.get("source_raw", d)
        if not isinstance(src, dict):
            continue
        mp = src.get("main_product", {}) or {}
        if not isinstance(mp, dict):
            continue
        od = src.get("overviewData", {}) or {}
        pc = (mp.get("productCode") or "").strip().upper()
        pm = (mp.get("productModel") or "").strip().upper()
        # Applications — one list item per non-empty line of the official area text.
        apps = (od.get("pdfApplicationAreasEn") or "").strip()
        app_list = [x.strip(" -\u2022\t") for x in apps.splitlines() if x.strip()] if apps else []
        # FAQ — real pipeline Q/A pairs (competitor/price entries dropped at render).
        faqs = []
        for f in (mp.get("faqs") or []):
            if not isinstance(f, dict):
                continue
            q = (f.get("question") or "").strip().rstrip(";").strip()
            a = (f.get("answer") or "").strip().rstrip(";").strip()
            if q and a:
                faqs.append([q, a])
        # Alternatives — ONLY verified alternates (hasAlternatePart True) with a real MPN.
        alts = []
        for al in (src.get("alternatePartList") or mp.get("alternatePartList") or []):
            if not isinstance(al, dict):
                continue
            if al.get("hasAlternatePart") is not True:
                continue
            model = (al.get("productModel") or "").strip()
            if model:
                alts.append(model)
        rec = {"apps": app_list, "faqs": faqs, "alts": alts}
        if pc:
            _SECTION_EXTRAS_INDEX[pc] = rec
        if pm:
            _SECTION_EXTRAS_INDEX[pm] = rec
    return _SECTION_EXTRAS_INDEX


def _raw_section_extras(row):
    """Return {apps, faqs, alts} from the 01-collected scale500 RAW, or None (no fabrication)."""
    idx = _get_section_extras_index()
    if not idx:
        return None
    lcsc = (row.get("supplier_reference") or "").strip().upper()
    mpn = (row.get("mpn") or "").strip().upper()
    return idx.get(lcsc) or idx.get(mpn)


def _raw_fc_rec(row):
    """Return the Features/Compliance record for this row, or None (no fabrication)."""
    idx = _get_features_compliance_index()
    if not idx:
        return None
    lcsc = (row.get("supplier_reference") or "").strip().upper()
    mpn = (row.get("mpn") or "").strip().upper()
    return idx.get(lcsc) or idx.get(mpn)


def _raw_intro_text(row):
    """Return the REAL pipeline Product Introduction for this row, or None.

    Source precedence (all from the 01-collected scale500 RAW, never AI-written):
      overviewData.productIntroEn  (official full intro)  ->  main_product.productIntroEn (short).
    Returns None when neither exists so the panel falls back to MASTER short_description.
    """
    rec = _raw_fc_rec(row)
    if not rec:
        return None
    return rec.get("intro") or rec.get("intro_short") or None


def brand_class_html(row):
    """Brand classification tag — emits LCSC 'Asian Brands' ONLY when the
    01-collected RAW flag `isAsianBrand` is literally true.

    Site policy: classification is NEVER inferred from the brand name. When the
    flag is missing / false / unmatched, returns '' (no tag, no fabrication).
    Loaded at generation time from scale500 RAW; MASTER schema is untouched.
    """
    idx = _get_asian_index()
    if not idx:
        return ""
    lcsc = (row.get("supplier_reference") or "").strip().upper()
    mpn = (row.get("mpn") or "").strip().upper()
    flag = idx.get(lcsc)
    if flag is None:
        flag = idx.get(mpn)
    if flag is not True:
        return ""
    return '<span class="brand-tag">Asian Brands</span>'


# ==============================================================================
# Risk #2 (2026-09-08): PDF enrichment loaded AT GENERATION TIME.
# This replaces the old post-hoc HTML string injection (_enrich_apply.py), so
# enrichment can never be wiped by a page regeneration. Enrichment is ALWAYS
# optional and NEVER a publish blocker.
# ==============================================================================
ENRICH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "enrich")

# Authored rich-text Introduction slot (NOT overwritten by the enrichment pipeline).
INTRO_DIR = os.path.join(ROOT, "data", "intro")

# ---- Rich-text (Product Introduction) sanitizer ---------------------------------
# The Introduction tab may carry authored rich HTML (data/intro/<mpn>.html).
# We render a SAFE SUBSET only: formatting tags, no scripts, no event handlers,
# and links restricted to http/https/mailto. All other tags/attrs are stripped.
import html as _html
from html.parser import HTMLParser

_INTRO_ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "ul", "ol", "li",
    "h3", "h4", "h5", "h6", "blockquote", "code", "pre",
    "table", "thead", "tbody", "tr", "th", "td",
    "a", "span", "div",
}
_INTRO_ALLOWED_ATTRS = {
    "a": {"href", "rel", "target"},
    "span": {"class"}, "div": {"class"},
    "td": {"class"}, "th": {"class"},
}


class _IntroSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out = []
        self._stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in _INTRO_ALLOWED_TAGS:
            return
        allowed = _INTRO_ALLOWED_ATTRS.get(tag, set())
        parts = []
        for k, v in attrs:
            kl = k.lower()
            if kl not in allowed:
                continue
            if kl == "href" and v is not None:
                v = v.strip()
                if not v.startswith(("http://", "https://", "mailto:")):
                    continue
            parts.append(f' {kl}="{_html.escape(v or "", quote=True)}"')
        self._out.append(f"<{tag}{''.join(parts)}>")
        self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag not in _INTRO_ALLOWED_TAGS:
            return
        self._out.append(f"<{tag}/>")

    def handle_endtag(self, tag):
        if tag not in _INTRO_ALLOWED_TAGS:
            return
        if tag in self._stack:
            self._out.append(f"</{tag}>")
            self._stack.remove(tag)

    def handle_data(self, data):
        self._out.append(_html.escape(data))


def render_rich_html(raw_html):
    """Sanitize authored rich HTML to a safe subset; returns '' for empty/None."""
    if not raw_html:
        return ""
    s = _IntroSanitizer()
    s.feed(raw_html)
    return "".join(s._out)


def load_intro_html(mpn, slug):
    """Authored rich-text Introduction: data/intro/<mpn>.html (one file per part).
    Resolution: slug / SLUG / mpn / MPN with .html/.htm. None when absent."""
    for c in (slug, (slug or "").upper(), mpn, (mpn or "").upper()):
        if not c:
            continue
        for ext in (".html", ".htm"):
            p = os.path.join(INTRO_DIR, f"{c}{ext}")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as fh:
                        return fh.read()
                except Exception:
                    return None
    return None


def load_enrichment(slug, mpn=None):
    """Risk #2: read a SKU's PDF enrichment JSON at generation time.

    Returns the parsed enrichment dict (with ``_file_sha256`` attached) or None.

    Resolution (files are named by MPN, not slug):
      - data/enrich/<slug>.content.json
      - data/enrich/<SLUG>.content.json
      - data/enrich/<mpn>.content.json
      - data/enrich/<MPN>.content.json
    First existing file wins.

    Failure policy (explicit, never silent):
      - file missing             -> None  (enrichment optional; publish proceeds)
      - JSON invalid / unreadable -> logged WARNING, returns None (no fabricated content)
      - missing schema_version    -> logged WARNING, returns None
    """
    candidates = []
    for c in (slug, (slug or "").upper(), mpn, (mpn or "").upper()):
        if c and c not in candidates:
            candidates.append(c)
    for name in candidates:
        path = os.path.join(ENRICH_DIR, f"{name}.content.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"  [enrichment] WARNING: invalid JSON in {path}: {e} -- skipped (NOT applied)")
            return None
        except Exception as e:
            print(f"  [enrichment] WARNING: cannot read {path}: {e} -- skipped (NOT applied)")
            return None
        if not isinstance(data, dict) or "schema_version" not in data:
            print(f"  [enrichment] WARNING: {path} missing 'schema_version' -- skipped (NOT applied)")
            return None
        data["_file_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return data
    return None


# ---------------------------------------------------------------------------
# Enrichment spec/app/faq de-duplication helpers (Risk #2).
# Ported verbatim (semantics only) from the historical _enrich_apply.py so the
# generation-time injection reproduces the SAME additive behavior the old
# post-hoc injector produced: enrichment specs whose NORMALIZED concept already
# exists in the MASTER identity spec set are skipped (no duplicate rows); apps
# and FAQ are de-duplicated by normalized text. Keeping this logic inside the
# generator makes enrichment a property of the generated page, never wiped by a
# regeneration.
# ---------------------------------------------------------------------------
_ENRICH_SPEC_RULES = [
    ("vds", ["drain source voltage", "drain source", "vds", "vdss"]),
    ("vgs", ["gate source voltage", "gate source", "vgs"]),
    ("vgsth", ["gate threshold", "threshold voltage", "vgs th", "vgs th "]),
    ("rdson", ["rds", "on resistance", "drain source on", "on resistance rds"]),
    ("qg", ["gate charge", "total gate charge", "qg"]),
    ("id", ["drain current", "continuous drain", "id"]),
    ("ifavg", ["forward current", "average forward", "if "]),
    ("vrrm", ["reverse voltage", "vrrm", "repetitive reverse"]),
    ("vf", ["forward voltage"]),
    ("voltage", ["voltage", "v", "supply voltage", "operating voltage",
                 "rated voltage", "input voltage", "output voltage",
                 "reference voltage", "adjustable output"]),
    ("current", ["current", "iq", "quiescent", "shutdown"]),
    ("io", ["gpio", "number of io", "i o", "io "]),
    ("pkg", ["package", "case"]),
    ("flash", ["flash"]),
    ("sram", ["sram"]),
    ("core", ["core"]),
    ("clockspd", ["clock speed", "clock"]),
    ("comminterfaces", ["communication"]),
    ("cap", ["capacitance"]),
    ("loadcap", ["load capacitance"]),
    ("tol", ["tolerance"]),
    ("dielectric", ["dielectric"]),
    ("freq", ["frequency", "nominal frequency"]),
    ("freqtol", ["frequency tolerance"]),
    ("op_temp", ["operating temperature", "ambient temperature",
                 "junction temperature", "operating junction"]),
    ("stg_temp", ["storage temperature"]),
    ("temp", ["temperature"]),
    ("dim", ["dimension", "overall dimension", "size code", "size"]),
    ("mount", ["mounting", "mount"]),
    ("iface", ["interface"]),
    ("density", ["density"]),
    ("eraseg", ["erase"]),
    ("pagesz", ["page size", "page"]),
    ("esr", ["esr", "motional resistance"]),
    ("drive", ["level of drive", "drive level"]),
    ("pitch", ["pitch"]),
    ("contact", ["contact material", "contact"]),
    ("housing", ["housing"]),
    ("entry", ["entry type", "entry"]),
    ("ckt", ["circuit", "positions", "number of circuits"]),
    ("devtype", ["device type"]),
    ("finish", ["finish"]),
    ("uniqueid", ["unique id", "unique serial", "serial number"]),
    ("vin", ["input voltage"]),
    ("iout", ["output current"]),
    ("swfreq", ["switching frequency", "switching"]),
    ("voutadj", ["adjustable output"]),
    ("ilim", ["current limit", "limit threshold"]),
    ("t_sd", ["thermal shutdown"]),
    ("fage", ["frequency aging", "aging"]),
]


def _enrich_concept(k):
    s = str(k).lower()
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    for label, phrases in _ENRICH_SPEC_RULES:
        for p in phrases:
            if p in s:
                return label
    return re.sub(r"\s+", " ", s).strip()


def _enrich_norm_text(t):
    s = str(t).lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _enrich_cosmetic(s):
    s = str(s)
    s = s.replace("plusminus", "+/-")
    s = re.sub(r"degc", "degC", s, flags=re.I)
    return s


def classify_product_type(subcat, native_l1_raw):
    """Classify a SKU into a sourcing-copy variant from VERIFIED text only.

    Returns (type_key, lifecycle):
      type_key in {mcu_ic, connector, module, other}
      lifecycle in {normal, scarce, eol}
    Lifecycle is flagged ONLY when explicit, verified lifecycle keywords appear in the
    subcategory / native_l1 text — never inferred from missing or dirty data.
    """
    s = (subcat or "")
    n = (native_l1_raw or "")
    s_l = s.lower()
    n_l = n.lower()
    blob = f"{s_l} {n_l}"

    # Lifecycle — explicit verified keywords only (no inference from blanks).
    lifecycle = "normal"
    if re.search(r"\b(eol|obsolete|nrnd|discontinued|end[- ]of[- ]life|last[- ]time[- ]buy|"
                 r"not recommended for new design)\b", blob):
        lifecycle = "eol"
    elif re.search(r"(scarce|shortage|hard[- ]to[- ]find|allocated|long[- ]lead|"
                   r"low[- ]stock|out[- ]of[- ]stock)", blob):
        lifecycle = "scarce"

    def _type_from(text):
        t = (text or "").lower()
        if re.search(r"connector|receptacle|header|terminal block|rj45|hdmi|usb connector", t):
            return "connector"
        if re.search(r"mcu|microcontroller|micro[- ]controller|mpu|fpga|dsp|asic|soc|processor|"
                     r"\bcpu\b|integrated circuit|\bic\b|logic|memory|flash|eeprom|voltage regulator|"
                     r"amplifier|adc|dac|op[- ]?amp|transistor|diode|mosfet|gate driver|led driver|"
                     r"power management|switching|linear regulator", t):
            return "mcu_ic"
        if re.search(r"module|modules|wifi|rf module|wireless|ethernet|lora|ble|bluetooth|"
                     r"gps|gsm|lte|nb-iot|zigbee", t):
            return "module"
        return None

    t = _type_from(s) or _type_from(n)
    if t is None:
        t = "other"
    return (t, lifecycle)


def build_sourcing_info(pn, mfr, cat, subcat, native_l1_raw, spec_pairs_en, apps_list):
    """Build the Sourcing Information block: fixed 4-part framework + SKU-driven variant.

    Rules enforced (per 2026-09-12 spec):
      - Only VERIFIED SKU data (pn, mfr, cat, subcat, specs, apps) is used.
      - No inferred use / performance / supplier / stock / price / lead time / genuine /
        certification / quality-result claims.
      - No absolute or guarantee language (genuine, best/lowest price, guaranteed stock/
        delivery, official/authorized distributor).
      - Sourcing services are kept distinct from SKU facts.
      - 2-3 dense, readable paragraphs; SEO keywords woven where relevant (no stuffing).
    Returns an HTML string of <p> blocks (the <section> wrapper is owned by the caller).
    """
    pn_e = esc(pn)
    mfr_e = esc(mfr) if mfr else ""
    cat_e = esc(cat) if cat else ""
    subcat_e = esc(subcat) if subcat else ""
    # Official RFQ page URL (same standard SKU-quote deep-link used elsewhere in the site).
    rfq_url = (f"/request-a-quote/?pn={urlquote(pn)}&mfr={urlquote(mfr)}&cat={urlquote(cat)}"
               f"&source=product&rfq_type=sku_quote")
    type_key, lifecycle = classify_product_type(subcat, native_l1_raw)

    # product noun: prefer a specific subcategory term, else category, else "component"
    if subcat_e:
        product_noun = subcat_e
    elif cat_e:
        product_noun = cat_e
    else:
        product_noun = "component"

    # ---- Paragraph 1: procurement positioning (MPN + China electronics supply chain) ----
    if mfr_e:
        p1 = (f"<p>SZ Procure is a sourcing partner for the <strong>{pn_e}</strong> "
              f"({mfr_e} {product_noun}), not a stock catalog. We help international buyers "
              f"source this electronic component through the China electronics supply chain, "
              f"with verified supplier sourcing and quality inspection.</p>")
    else:
        p1 = (f"<p>SZ Procure is a sourcing partner for the <strong>{pn_e}</strong> "
              f"({product_noun}), not a stock catalog. We help international buyers source this "
              f"electronic component through the China electronics supply chain, with verified "
              f"supplier sourcing and quality inspection.</p>")

    # Lifecycle disclosure — only when EXPLICIT verified keywords were present (no inference).
    lifecycle_note = ""
    if lifecycle == "scarce":
        lifecycle_note = (f" Because {pn_e} may be in limited supply, we perform availability "
                          f"investigation across multiple suppliers &mdash; a sourcing effort, not a "
                          f"guarantee of stock or delivery date.")
    elif lifecycle == "eol":
        lifecycle_note = (f" Because {pn_e} is listed as end-of-life or obsolete in the supplied "
                          f"data, we provide sourcing support and availability investigation across "
                          f"remaining channels &mdash; a sourcing effort, not a guarantee of stock or "
                          f"authenticity.")

    # ---- Paragraphs 2 & 3: services & QC, varied by product type ----
    if type_key == "mcu_ic":
        svc = (f"For component sourcing of {pn_e}, we run supplier sourcing across the China "
               f"electronics supply chain, supplier verification and multi-supplier quotation "
              f"comparison so you can compare offers before purchase. We coordinate purchase "
              f"orders, consolidation and international logistics dispatch from Shenzhen.")
        qc = (f"Quality control focuses on supplier qualification and screening, then product, "
              f"packaging and labeling checks with outgoing inspection before dispatch. These are "
              f"sourcing services we perform &mdash; they are not a guarantee of any supplier's stock, "
              f"price, lead time or authenticity. Tell us your required quantity, target price and "
              f"sourcing requirements, then <a href=\"{rfq_url}\">Request a Quote</a>.")
    elif type_key == "connector":
        svc = (f"For {pn_e}, supplier sourcing matches your required specifications &mdash; such as "
               f"pin count, pitch and mounting style &mdash; against verified China electronics supply "
               f"chain suppliers, with multi-supplier quotation comparison and purchase-order "
              f"coordination. We handle consolidation and international logistics dispatch from "
              f"Shenzhen.")
        qc = (f"Quality control includes supplier qualification and screening, specification "
              f"matching verification, and product, packaging and labeling checks with outgoing "
              f"inspection. These checks are part of our sourcing service and do not constitute a "
              f"guarantee of stock, price, lead time or authenticity. Tell us your required "
              f"quantity, target price and sourcing requirements, then "
              f"<a href=\"{rfq_url}\">Request a Quote</a>.")
    elif type_key == "module":
        svc = (f"For electronics sourcing of {pn_e}, we carry out availability verification across "
               f"suppliers in the China electronics supply chain, multi-supplier quotation comparison and "
              f"purchase-order coordination, then consolidation and international logistics dispatch "
              f"from Shenzhen.")
        qc = (f"Quality control covers supplier qualification and screening, availability "
              f"cross-check, product, packaging and labeling inspection and outgoing inspection. "
              f"These are sourcing services we provide; they are not a guarantee of a specific "
              f"supplier's stock, price, lead time or authenticity. Tell us your required quantity, "
              f"target price and sourcing requirements, then "
              f"<a href=\"{rfq_url}\">Request a Quote</a>.")
    else:  # other
        svc = (f"For {pn_e}, our electronics sourcing covers supplier sourcing across the China "
               f"electronics supply chain, supplier verification and multi-supplier quotation "
              f"comparison so you can compare offers before purchase. We coordinate purchase orders, "
              f"consolidation and international logistics dispatch from Shenzhen.")
        qc = (f"Quality control includes supplier qualification and screening, then product, "
              f"packaging and labeling checks with outgoing inspection before dispatch. These are "
              f"sourcing services we perform &mdash; they are not a guarantee of any supplier's stock, "
              f"price, lead time or authenticity. Tell us your required quantity, target price and "
              f"sourcing requirements, then <a href=\"{rfq_url}\">Request a Quote</a>.")

    p2 = f"<p>{svc}{lifecycle_note}</p>"
    p3 = f"<p>{qc}</p>"
    return p1 + p2 + p3


def gen_part_page_v3(row, cat_slug, mfr_slug, related=None, generated_slugs=None, verbose=False):
    pn = row["mpn"].strip()
    mfr = row["manufacturer"].strip()
    _cat_res = resolve_native(row.get("native_l1"))
    cat = _cat_res.get("l1_name") or (row.get("category") or "").strip()
    subcat = (row.get("subcategory") or "").strip()
    specs_raw = (row.get("attributes_json") or "").strip()
    apps = (row.get("applications") or "").strip()
    alt_raw = (row.get("alternative_parts") or "").strip()
    faq_raw = (row.get("faq") or "").strip()
    img = (row.get("image") or "").strip()
    dsheet = (row.get("datasheet_url") or "").strip()
    url_slug = (row.get("url_slug") or "").strip() or slugify(pn)
    slug = url_slug
    related = related or []
    url = f"{DOMAIN}/products/{slug}/"
    img_url = img if img else "/assets/img/hero.svg"
    og_img = f"{DOMAIN}{img_url}" if img_url.startswith("/") else img_url

    # P1-B1/I3/I4: generation-phase classifier over MASTER.native_l1 (UNMAPPED quarantined).
    status, cat_slug, cat_top = resolve_cat_state(row["native_l1"])
    cat_resolved = status in ("RESOLVED", "SELF_REFERENCE")
    ps = effective_publish_status(row)
    noindex = ps in ("hidden", "review")

    # ---- SEO copy: IDENTICAL formula to V2 (guarantees byte-equal SEO head/schema) ----
    fallback_overview = (f"{esc(pn)} is a {esc(subcat or cat).lower()} from {esc(mfr)}. "
                         f"SZ Procure helps global buyers source this part through verified suppliers, "
                         f"with flexible quantity, hard-to-find support and competitive quotes.")
    desc_csv = (row.get("description") or "").strip()
    overview = esc(desc_csv) if desc_csv else fallback_overview
    schema_overview = fallback_overview
    title = f"{esc(pn)} {esc(mfr)} — Source from Shenzhen, China | SZ Procure"
    desc = (f"Source {esc(pn)} ({esc(mfr)} {esc(cat).lower()}) from Shenzhen, China. "
            f"Shenzhen supplier network, hard-to-find support and BOM procurement for global buyers.")

    # ---- parse repeatable fields (same helpers as V2) ----
    alts = [a for a in split_multi(alt_raw) if slugify(a)]
    apps_list = split_multi(apps)
    # FAQ is assembled below via merge_faqs() (after enrichment load) — source priority + count control.

    # ---- structured attribute extraction: REAL MASTER attributes only ----
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
                    spec_pairs.append(["Specification", token.strip()])
    # translate to English visible layer (CJK gate); keep ONLY real keys/values
    spec_pairs_en = translate_spec_pairs(spec_pairs)

    # ---- Risk #2: load PDF enrichment at generation time (optional, never blocks) ----
    enrich = load_enrichment(slug, pn)
    enrich_spec_pairs = []   # enrichment-only specs: supplemental, NEVER fed into id_rows identity
    enrich_keywords = []
    enrich_meta = ""
    main_attrs = ""
    if enrich is not None:
        # short_description -> Introduction tab only (MASTER hero-desc preserved; not an identity field)
        sd = (enrich.get("short_description") or {}).get("value")
        introduction_tab = esc(sd) if sd else overview
        # key_specifications -> append as supplemental datasheet params.
        # Dedup by NORMALIZED concept: skip any enrichment spec whose concept already
        # exists in the MASTER identity spec set (so no duplicate rows such as "SRAM",
        # "Flash Memory", "Package" appear twice). Mirrors the historical _enrich_apply.py.
        seen_concepts = set(_enrich_concept(human_attr_label(k)) for k, _ in spec_pairs_en)
        for spec in (enrich.get("key_specifications") or []):
            k = spec.get("key") if isinstance(spec, dict) else None
            v = spec.get("value") if isinstance(spec, dict) else None
            if not (k and v not in (None, "")):
                continue
            if _enrich_concept(k) in seen_concepts:
                continue
            seen_concepts.add(_enrich_concept(k))
            enrich_spec_pairs.append([str(k), str(_enrich_cosmetic(v))])
        # applications -> append (dedup by NORMALIZED text against MASTER apps)
        seen_apps = set(_enrich_norm_text(a) for a in apps_list)
        for app in (enrich.get("applications") or []):
            val = app.get("value") if isinstance(app, dict) else app
            if not val:
                continue
            if _enrich_norm_text(val) in seen_apps:
                continue
            seen_apps.add(_enrich_norm_text(val))
            apps_list.append(val)
        # ---- RAW section extras (Applications / FAQ / Alternative Parts) ----
        # Real pipeline content from scale500; mapped ONLY when present, never forced.
        raw_ext = _raw_section_extras(row)
        if raw_ext:
            # Applications: append RAW application areas (dedup by normalized text)
            seen_apps = set(_enrich_norm_text(a) for a in apps_list)
            for app in (raw_ext.get("apps") or []):
                if _enrich_norm_text(app) in seen_apps:
                    continue
                seen_apps.add(_enrich_norm_text(app))
                apps_list.append(app)
            # FAQ already merged in via merge_faqs() (Pass A: LCSC/RAW qualified).
            # Alternatives: extend with REAL verified MPNs (dedup)
            seen_alt = {a.lower() for a in alts}
            for model in (raw_ext.get("alts") or []):
                if model.lower() in seen_alt:
                    continue
                seen_alt.add(model.lower())
                alts.append(model)
        # keywords -> data asset ONLY (NOT injected into meta/visible SEO, per Risk #2)
        enrich_keywords = [k.get("value") for k in (enrich.get("keywords") or [])
                           if isinstance(k, dict) and k.get("value")]
        # sz-enrichment marker meta: v1|mpn|content_sha256|pdf_sha256|ts
        ts = enrich.get("extracted_at") or enrich.get("generated_at") or ""
        enrich_meta = (f'<meta name="sz-enrichment" content="v1|{esc(pn)}|'
                       f'{enrich.get("_file_sha256", "")}|{enrich.get("pdf_sha256", "")}|{esc(ts)}" />')
        main_attrs = ' data-sz-enrichment="v1"'
        if enrich_keywords:
            main_attrs += f' data-enrichment-keywords="{esc(", ".join(enrich_keywords))}"'
    else:
        introduction_tab = overview

    # ---- FAQ: source-priority merge with count control (final rule; runs even with no enrichment) ----
    faq_pairs, faq_audit = merge_faqs(faq_raw, enrich, row)
    if verbose:
        _print_faq_audit(pn, faq_audit)

    # Product Introduction panel — authored rich HTML (human override) takes precedence;
    # otherwise the REAL pipeline intro from RAW overviewData.productIntroEn (never AI);
    # otherwise fall back to MASTER short_description / overview.
    _authored_intro = load_intro_html(pn, slug)
    if _authored_intro:
        introduction_panel = f'<div class="intro-body">{render_rich_html(_authored_intro)}</div>'
    else:
        _raw_intro = _raw_intro_text(row)
        if _raw_intro:
            _paras = [p.strip() for p in _raw_intro.split("\n") if p.strip()]
            _intro_html = "".join(f"<p>{esc(p)}</p>" for p in _paras)
            introduction_panel = f'<div class="intro-body">{_intro_html}</div>'
        else:
            introduction_panel = f'<p>{introduction_tab}</p>'

    # V3 Specifications tab — real attributes, honest labels, NEVER invented rows.
    # MASTER specs first; enrichment specs appended as supplemental datasheet params.
    if spec_pairs_en:
        specs_rows = "".join(
            f"<tr><th>{esc(human_attr_label(k))}</th><td>{esc(format_attr_value(k, v))}</td></tr>"
            for k, v in spec_pairs_en
        )
    else:
        specs_rows = ""
    for k, v in enrich_spec_pairs:
        specs_rows += f"<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>"
    if specs_rows:
        row_matches = re.findall(r'<tr>.*?</tr>', specs_rows, re.DOTALL)
        # Dedupe identical rows — some source attribute lists repeat the same field
        # (e.g. LCSC productAttributesList lists Antenna Type / Sensitivity twice).
        # Key by normalized (Type|Description) and keep the first occurrence only.
        _deduped, _seen = [], set()
        for _row in row_matches:
            _m = re.match(r'<tr><th>(.*?)</th><td>(.*?)</td></tr>', _row, re.DOTALL)
            if not _m:
                _deduped.append(_row)
                continue
            _k = _enrich_norm_text(_m.group(1)) + '|' + _enrich_norm_text(_m.group(2))
            if _k in _seen:
                continue
            _seen.add(_k)
            _deduped.append(_row)
        row_matches = _deduped
        n = len(row_matches)
        thead = '<thead><tr><th>Type</th><th>Description</th></tr></thead>'
        if n <= 4:
            specs_html = f'<table class="spec-table">{thead}\n<tbody>\n{specs_rows}</tbody>\n</table>'
        else:
            mid = (n + 1) // 2
            left = ''.join(row_matches[:mid])
            right = ''.join(row_matches[mid:])
            specs_html = (
                '<div class="spec-grid">\n'
                f'  <table class="spec-table">{thead}<tbody>\n{left}</tbody></table>\n'
                f'  <table class="spec-table">{thead}<tbody>\n{right}</tbody></table>\n'
                '</div>'
            )
    else:
        specs_html = (
            '<div class="spec-empty">'
            '<p>Detailed specifications and the official datasheet are available on request. '
            'Send the part number and our team will provide the full parameter table and documentation.</p>'
            '</div>'
        )

    # Hero identity fields — real, key identity only (no fabrication).
    id_rows = []
    id_rows.append(("Manufacturer", f'<a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a>{brand_class_html(row)}'))
    id_rows.append(("MPN", esc(pn)))
    if cat_resolved:
        id_rows.append(("Product Type", f'<a href="/components/{cat_slug}/">{esc(subcat or cat_top)}</a>'))
    else:
        id_rows.append(("Product Type", esc(subcat or cat_top)))
    for k, v in spec_pairs_en:
        if k.lower() in ("package", "core", "frequency_hz", "voltage_v"):
            id_rows.append((human_attr_label(k), esc(format_attr_value(k, v))))
    if dsheet:
        id_rows.append(("Datasheet",
                        f'<a class="doc-link" href="{esc(dsheet)}" target="_blank" '
                        f'rel="nofollow noopener" download>'
                        f'<span class="doc-ico">&#128196;</span> {esc(pn)} Datasheet</a>'))
    if overview:
        id_rows.append(("Key Attributes", overview))
    id_list_html = "".join(
        f'<div class="id-row"><div class="id-label">{esc(k)}</div><div class="id-value">{v}</div></div>'
        for k, v in id_rows
    )
    # RoHS badge (conditional — authoritative LCSC provenance only; '' when not compliant/evidence)
    rohs_html = rohs_badge_html(row)

    # Applications — only when real data exists
    apps_section = ""
    if apps_list:
        apps_items = "".join(f"<li>{esc(x)}</li>" for x in apps_list)
        apps_section = (
            '<section id="applications" class="tab-panel">\n'
            '  <h2 class="section-title">Applications</h2>\n'
            f'  <ul class="app-list">{apps_items}</ul>\n'
            '</section>'
        )

    # FAQ — only when real data exists
    faq_html, faq_jsonld = render_faq(faq_pairs, pn)
    faq_section = ""
    if faq_html:
        # Standalone section (NOT a TAB): removed from tab-nav + scroll-spy on 2026-09-12.
        # Keeps the same card styling as other sections; grid-column:1 keeps it in the
        # left column (same column as .tab-wrap / .sourcing) so it does not land beside the RFQ card.
        faq_section = (
            '<section id="faq" class="tab-panel" style="grid-column:1;margin-top:16px;margin-bottom:16px">\n'
            '  <h2 class="section-title">Frequently Asked Questions</h2>\n'
            f'  {faq_html}\n'
            '</section>'
        )

    # Related Parts — REMOVED site-wide (2026-09-12): user requested removal of the
    # Related tab/section from all 746 SKU pages (internal-link product web).
    related_section = ""

    # Alternative Parts — HIDDEN unless real, verified alternates exist.
    # Source of truth = MASTER `alternative_parts` (verified cross-brand cross-references,
    # e.g. STM32F103C8T6 -> CH32F103C8T6 / AO3400A -> AO3404A / RC0402FR-0710KL -> AC0402FR-1310KL).
    # LCSC-confirmed alternates (rawExt.alts where hasAlternatePart==True) are appended when present
    # — but in scale500 every hasAlternatePart is False, so the loose "also-viewed" list is excluded.
    # NEVER fabricate/infer alternates from series or package.
    alt_section = ""
    if alts:
        alt_items = []
        for a in alts:
            aslug = slugify(a)
            if generated_slugs and aslug in generated_slugs:
                alt_items.append(f'<li><a href="/products/{aslug}/" class="alt-link">{esc(a)}</a></li>')
            else:
                alt_items.append(f'<li><a href="/request-a-quote/?pn={esc(a)}" class="alt-link">{esc(a)} <span class="muted">(request quote)</span></a></li>')
        if alt_items:
            alt_section = (
                '<section id="alternative" class="tab-panel">\n'
                '  <h2 class="section-title">Alternative Parts</h2>\n'
                f"  <p>Common <strong>{esc(pn)} alternatives</strong> overseas buyers search for:</p>\n"
                f'  <ul class="alt-list">{"".join(alt_items)}</ul>\n'
                '</section>'
            )

    # Datasheet — real PDF only (never a fake URL).
    # Inline <embed> is the ONLY PDF loaded at page init; no modal / no duplicate request.
    if dsheet:
        doc_section = (
            '<section id="documentation" class="tab-panel">\n'
            '  <h2 class="section-title">Datasheet</h2>\n'
            '  <details class="doc-acc" open>\n'
            '    <summary>\n'
            '      <span class="doc-acc-title" aria-label="Datasheet"><span class="doc-ico">&#128196;</span></span>\n'
            '      <span class="doc-acc-actions">\n'
            f'        <a class="doc-btn" href="{esc(dsheet)}" target="_blank" rel="nofollow noopener">Open</a>\n'
            f'        <a class="doc-btn" href="{esc(dsheet)}" target="_blank" rel="nofollow noopener" download>Download</a>\n'
            '      </span>\n'
            '    </summary>\n'
            '    <div class="doc-acc-body">\n'
            f'      <div class="doc-preview"><embed src="{esc(dsheet)}" type="application/pdf" '
            f'title="{esc(pn)} Datasheet Preview" aria-label="{esc(pn)} Datasheet Preview" /></div>\n'
            '    </div>\n'
            '  </details>\n'
            '</section>'
        )
    else:
        doc_section = (
            '<section id="documentation" class="tab-panel">\n'
            '  <h2 class="section-title">Datasheet</h2>\n'
            '  <p class="doc-note">Datasheet is available on request. Send the part number and our team will provide the official documentation.</p>\n'
            '</section>'
        )

    # Features — official product feature bullets (01 RAW overviewData.productFeaturesEn)
    features_section = ""
    fc = _raw_fc_rec(row)
    if fc and fc.get("features"):
        feats = [ln.strip().lstrip("-•* ").strip()
                 for ln in fc["features"].splitlines() if ln.strip()]
        if feats:
            feat_items = "".join(f"<li>{esc(x)}</li>" for x in feats)
            features_section = (
                '<section id="features" class="tab-panel">\n'
                '  <h2 class="section-title">Features</h2>\n'
                f'  <ul class="features-list">{feat_items}</ul>\n'
                '</section>'
            )

    # Compliance & Export Codes — RoHS / ECCN / HTS by country (01 RAW, real only).
    # Rendered as two side-by-side Type | Details tables to match the LCSC layout.
    compliance_section = ""
    if fc:
        comp_rows = []
        if fc.get("rohs"):
            comp_rows.append(("RoHS", fc.get("rohs_type") or "Compliant"))
        if fc.get("eccn"):
            comp_rows.append(("ECCN", fc["eccn"]))
        # HTS country variants, in the LCSC order shown in the reference screenshot:
        # CN, US, TARIC, CA, BR, IN, MX. Any extra country codes fall to the end.
        hts_map = fc.get("hts_map") or {}
        hts_order = ["CN", "US", "TARIC", "CA", "BR", "IN", "MX"]
        seen = set()
        for code in hts_order:
            val = hts_map.get(code)
            if val:
                label = "TARIC" if code == "TARIC" else f"{code}HTS"
                comp_rows.append((label, str(val).strip()))
                seen.add(code)
        for code in sorted(hts_map.keys()):
            if code in seen:
                continue
            val = hts_map.get(code)
            if val:
                label = "TARIC" if code == "TARIC" else f"{code}HTS"
                comp_rows.append((label, str(val).strip()))
        if comp_rows:
            mid = (len(comp_rows) + 1) // 2
            left_rows = comp_rows[:mid]
            right_rows = comp_rows[mid:]
            def _comp_table(rows):
                body = "".join(
                    f'<tr><th>{esc(k)}</th><td>{esc(v)}</td></tr>' for k, v in rows
                )
                return (
                    '<table class="compliance-table">\n'
                    '  <thead>\n'
                    '    <tr><th>Type</th><th>Details</th></tr>\n'
                    '  </thead>\n'
                    f'  <tbody>{body}</tbody>\n'
                    '</table>'
                )
            left_html = _comp_table(left_rows)
            right_html = _comp_table(right_rows) if right_rows else ""
            grid_inner = f"{left_html}\n{right_html}".strip()
            compliance_section = (
                '<section id="compliance" class="tab-panel">\n'
                '  <h2 class="section-title">Compliance &amp; Export Codes</h2>\n'
                f'  <div class="compliance-grid">\n{grid_inner}\n  </div>\n'
                '</section>'
            )

    # Sourcing Information — fixed 4-part framework + SKU-driven dynamic copy (spec 2026-09-12)
    # Only verified SKU data (pn, mfr, cat, subcat, native_l1, specs, apps) is used; no
    # inferred use/performance/supplier/stock/price/lead-time/authenticity claims, no guarantees.
    sourcing_html = build_sourcing_info(pn, mfr, cat, subcat, row.get("native_l1"),
                                        spec_pairs_en, apps_list)

    # RFQ card — SKU Inline RFQ Standard v1 (form -> FormSubmit.co; optional attachment <=10MB)
    # No Buy Now / Add to Cart. Standalone /request-a-quote/ retained for BOM / multi-part sourcing.
    # Aligned with products/stm32f103c8t6/index.html (reference implementation, validated 72/72).
    rfq_card = """
      <aside class="rfq-card" id="rfq-card">
        <h3>Request a Quote</h3>
        <p>Tell us your quantity, target price, and delivery requirements. We source from Shenzhen and reply within 1 business day.</p>

        <div class="form-success" id="formSuccess">
          <h3>Thank you. Your RFQ has been received.</h3>
          <p>Our sourcing team will review your requirements and contact you shortly.</p>
        </div>

        <form id="quote-form" action="https://formsubmit.co/sales@szprocure.com" method="POST" enctype="multipart/form-data" novalidate>
          <input type="hidden" name="_subject" value="New SKU RFQ — SZ Procure" />
          <input type="text" name="_gotcha" style="display:none" tabindex="-1" autocomplete="off" />

          <input type="hidden" name="category" id="rfq_category" value="[[CAT]]" />
          <input type="hidden" name="source_url" id="source_url" />
          <input type="hidden" name="rfq_type" id="rfq_type" value="sku_quote" />
          <input type="hidden" name="manufacturer" id="rfq_manufacturer" value="[[MFR]]" />
          <input type="hidden" name="country_source" id="country_source" />
          <input type="hidden" name="referrer" id="referrer" />
          <input type="hidden" name="submitted_at" id="submitted_at" />
          <input type="hidden" name="requirement_type" id="requirement_type" />

          <div class="rfq-form">
            <div class="rfq-field"><label for="part_number">Part Number</label><input id="part_number" name="part_number" type="text" value="[[PN]]" readonly></div>
            <div class="rfq-field"><label for="quantity">Quantity</label><input id="quantity" name="quantity" type="number" min="1" step="1" placeholder="e.g. 1,000"></div>
            <div class="rfq-field"><label for="target_price">Target Price</label><input id="target_price" name="target_price" type="text" placeholder="USD / piece"></div>
            <div class="rfq-field"><label for="email">Email <span class="req">*</span></label><input id="email" name="email" type="email" required placeholder="your@email.com"><div class="form-error" id="formError"></div></div>
            <div class="rfq-field"><label for="requirements">Requirements</label><textarea id="requirements" name="requirements" placeholder="e.g. Original/New, EOL, specific package, certification requirements"></textarea></div>
            <div class="rfq-field"><label for="attachment">Attachment <span style="font-weight:400;color:#6b7280">(optional)</span></label><span class="file-pick" style="display:inline-block;margin:.2rem 0"><label for="attachment" class="rfq-file-btn" style="display:inline-block;padding:.45rem .9rem;border:1px solid #c7cdd6;border-radius:8px;background:#f3f4f6;color:#1f2937;cursor:pointer;font-size:.9rem">Choose File</label><span id="attachment-name" style="margin-left:.5rem;color:#6b7280;font-size:.85rem">No file chosen</span></span><input id="attachment" name="attachment" type="file" accept=".pdf,.xls,.xlsx,.csv,.jpg,.jpeg,.png,application/pdf,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv,image/jpeg,image/png" style="position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);border:0"><p style="font-size:.8rem;color:#6b7280;margin:.3rem 0 0">PDF, Excel, CSV, JPG, PNG &mdash; max 10MB</p><div class="form-error" id="formFileError"></div></div>
          </div>

          <div class="form-error" id="formSubmitError"></div>
          <button class="rfq-btn" type="submit">Request a Quote</button>
        </form>

      <script>
      (function(){
        var form = document.getElementById("quote-form");
        if(!form) return;
        var fileInput = form.querySelector('input[type="file"][name="attachment"]');
        var fileErr = document.getElementById("formFileError");
        if(!fileInput || !fileErr) return;
        var MAX = 10 * 1024 * 1024;
        var ALLOWED_EXT = ["pdf","xls","xlsx","csv","jpg","jpeg","png"];
        var ALLOWED_MIME = ["application/pdf","application/vnd.ms-excel","application/vnd.openxmlformats-officedocument.spreadsheetml.sheet","text/csv","image/jpeg","image/png"];
        function fileValid(){
          fileErr.style.display = "none";
          fileErr.textContent = "";
          if(!fileInput.files || !fileInput.files.length) return true;
          var f = fileInput.files[0];
          var ext = (f.name.split(".").pop() || "").toLowerCase();
          var okType = (ALLOWED_EXT.indexOf(ext) !== -1) || (f.type && ALLOWED_MIME.indexOf(f.type) !== -1);
          if(!okType){
            fileErr.textContent = "Unsupported file type. Allowed: PDF, Excel, CSV, JPG, PNG (max 10MB).";
            fileErr.style.display = "block";
            return false;
          }
          if(f.size > MAX){
            fileErr.textContent = "File is too large. Maximum size is 10MB.";
            fileErr.style.display = "block";
            return false;
          }
          return true;
        }
        fileInput.addEventListener("change", function(){
          var nm = document.getElementById("attachment-name");
          if (nm) nm.textContent = (fileInput.files && fileInput.files.length) ? fileInput.files[0].name : "No file chosen";
          fileValid();
        });
        form.addEventListener("submit", function(e){
          if(!fileValid()){
            e.preventDefault();
            e.stopImmediatePropagation();
            if(fileInput) fileInput.focus();
          }
        }, true);
      })();
      </script>
      </aside>"""
    rfq_card = (rfq_card
                .replace("[[CAT]]", esc(cat))
                .replace("[[MFR]]", esc(mfr))
                .replace("[[PN]]", esc(pn)))

    # breadcrumb (same items as V2)
    fine_slug = slugify_name(cat) if cat else ""
    sub_crumb = (f'<a href="/components/{cat_slug}/{fine_slug}/">{esc(cat)}</a> › '
                 if (cat and cat_resolved) else "")
    crumb_items = [
        ("Home", f"{DOMAIN}/"),
        ("Components", f"{DOMAIN}/components/"),
    ]
    if cat_resolved:
        crumb_items.append((cat_top, f"{DOMAIN}/components/{cat_slug}/"))
    if cat and cat_resolved:
        crumb_items.append((cat, f"{DOMAIN}/components/{cat_slug}/{fine_slug}/"))
    crumb_items.append((pn, url))
    crumb = breadcrumb_jsonld(crumb_items)

    # Product JSON-LD — same as V2 (no price/availability/offers)
    alt_ld = ", ".join(f'"{esc(a)}"' for a in alts)
    product_jsonld = f"""
  <script type="application/ld+json">
  {{
    "@context": "https://schema.org",
    "@type": "Product",
    "name": "{esc(pn)}",
    "model": "{esc(pn)}",
    "mpn": "{esc(pn)}",
    "category": "{esc(cat_top)}",
    "brand": {{ "@type": "Brand", "name": "{esc(mfr)}", "@id": "https://www.szprocure.com/#szprocure-org" }},
    "description": "{esc(schema_overview)}",
    "url": "{url}"{(", \"alternatePart\": [" + alt_ld + "]") if alt_ld else ""}
  }}
  </script>"""

    # tab nav — only real sections present
    tabs = ['<a href="#specifications" class="tab-btn active">Specifications</a>',
            '<a href="#introduction" class="tab-btn">Introduction</a>']
    if features_section:
        tabs.append('<a href="#features" class="tab-btn">Features</a>')
    if apps_section:
        tabs.append('<a href="#applications" class="tab-btn">Applications</a>')
    if doc_section:
        tabs.append('<a href="#documentation" class="tab-btn">Datasheet</a>')
    if alt_section:
        tabs.append('<a href="#alternative" class="tab-btn">Alternative Parts</a>')
    if compliance_section:
        tabs.append('<a href="#compliance" class="tab-btn">Compliance &amp; Export Codes</a>')
    tab_nav = "\n          ".join(tabs)

    # scroll-spy group ids (presentational only)
    spy_ids = ["specifications", "introduction"]
    if features_section:
        spy_ids.append("features")
    if apps_section:
        spy_ids.append("applications")
    if doc_section:
        spy_ids.append("documentation")
    if alt_section:
        spy_ids.append("alternative")
    if compliance_section:
        spy_ids.append("compliance")
    spy_groups = ", ".join(f"{{ id: '{i}' }}" for i in spy_ids)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
{seo_head(title, desc, url, og_img, noindex=noindex)}
{enrich_meta}
  <link rel="stylesheet" href="/assets/styles.css" />
  <link rel="stylesheet" href="/assets/sku-v3.css?v=20260912g" />
  <style>
    /* Page-scoped: suppress global floating/bottom conversion UI so the page-level
       RFQ owns conversion. Does NOT modify global site.js / styles.css. */
    .float-quote-btn {{ display: none !important; }}
    .mobile-cta-bar {{ display: none !important; }}
  </style>
{crumb}
{product_jsonld}
{faq_jsonld}
{org_jsonld()}
</head>
<body>
  <div id="site-header"></div>
  <main class="sku-v3"{main_attrs}>
    <nav class="breadcrumb"><div class="container">
      <a href="/">Home</a> ›
      <a href="/components/">Components</a> ›
      {('' if not cat_resolved else f'<a href="/components/{cat_slug}/">{esc(cat_top)}</a> ›')}
      {sub_crumb}<span>{esc(pn)}</span>
    </div></nav>

    <div class="container page-grid">
      <!-- LEFT: Product hero + key identity (real MASTER data only) -->
      <div class="hero-left">
        <p class="mfr"><a href="/manufacturers/{mfr_slug}/">{esc(mfr)}</a></p>
        <h1 class="mpn">{esc(pn)}{rohs_html}</h1>
        <p class="type">{esc(subcat or cat)}</p>
        <div class="id-list">
{id_list_html}
        </div>
      </div>

      {rfq_card}

      <!-- TABS: sticky nav + scroll-spy; all panels in DOM (SEO-friendly) -->
      <div class="tab-wrap">
        <nav class="tab-nav">
          {tab_nav}
        </nav>

        <section id="specifications" class="tab-panel">
          <h2 class="section-title">Technical Specifications</h2>
          {specs_html}
        </section>

        <section id="introduction" class="tab-panel">
          <h2 class="section-title">Product Introduction</h2>
          {introduction_panel}
        </section>

        {features_section}

        {apps_section}

        {doc_section}

        {alt_section}

        {compliance_section}
      </div>

      <!-- FAQ: independent section (removed from TAB nav / scroll-spy on 2026-09-12), placed before Sourcing -->
      {faq_section}

      <!-- Sourcing (no stock/price/lead-time promises) -->
      <section class="sourcing">
        <h2>Sourcing Information</h2>
        {sourcing_html}
      </section>
    </div>
  </main>
  <div id="site-footer"></div>
  <script src="/assets/site.js" defer></script>
{ga4_script()}
  <!-- Page-scoped scroll-spy: toggles .active pill on V3 tabs (presentational only). -->
  <script>
  (function(){{
    var groups = [ {spy_groups} ];
    var btns = Array.prototype.slice.call(document.querySelectorAll('.sku-v3 .tab-btn'));
    function onScroll(){{
      var offset = 160;
      var pos = window.scrollY + offset;
      var current = groups[0].id;
      for (var i = 0; i < groups.length; i++){{
        var el = document.getElementById(groups[i].id);
        if (el && el.offsetTop <= pos) current = groups[i].id;
      }}
      btns.forEach(function(b){{
        if (b.getAttribute('href') === '#' + current) b.classList.add('active');
        else b.classList.remove('active');
      }});
    }}
    window.addEventListener('scroll', onScroll, {{ passive: true }});
    window.addEventListener('resize', onScroll);
    onScroll();
  }})();
  </script>
</body>
</html>"""


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
        f'<span class="muted">— {esc(resolve_native(p.get("native_l1")).get("l1_name") or p.get("category", ""))}</span></li>'
        for p in sorted(parts, key=lambda x: x["mpn"])
    )
    # related categories for this manufacturer (resolve native_l1 -> top scope)
    _mfr_cat_by_top = {}
    for _c in {p.get("native_l1") for p in parts}:
        _res = resolve_native(_c)
        if _res["status"] != "RESOLVED":
            continue
        _mfr_cat_by_top.setdefault(_res["top_slug"], _res["l1_name"])
    cat_links = "".join(
        f'<li><a href="/components/{esc(_t)}/">{esc(_n)}</a></li>'
        for _t, _n in sorted(_mfr_cat_by_top.items(), key=lambda kv: kv[1].lower())
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
def gen_component_category_page(cat_slug, cat_name, parts, all_rows=None, by_cat=None, noindex=None):
    url = f"{DOMAIN}/components/{cat_slug}/"
    n = len(parts)
    cat_lower = esc(cat_name).lower()
    title = f"{esc(cat_name)} Sourcing from Shenzhen, China | SZ Procure"
    desc = (f"Source {esc(cat_name)} from Shenzhen. Browse {n} "
            f"{cat_lower} we help global buyers procure — alternates, "
            f"lead-time and quote support.")

    # I3: gate category page indexing by the top scope's publish_status (hidden/review => noindex).
    if noindex is None:
        _top_ps = load_taxonomy()["tops"].get(cat_slug, {}).get("publish_status", "active")
        noindex = _top_ps in ("hidden", "review")

    # ---- 1. Subcategory aggregation (real L3 pages only) ----
    # Count by fine category; link only to subcategories that actually exist on
    # disk (generated by gen_component_subcategory_page). Deterministic + data-driven.
    # Category HTML grows with SUBCATEGORY count, never with SKU count.
    # I3/I4: aggregate by native L1 (MASTER.native_l1), not the legacy `category` string,
    # so subcategory links/slugs are consistent with the native top scope.
    sub_counts = {}  # l1_slug -> [l1_name, count]
    for p in parts:
        _e = _native_entry(p.get("native_l1"))
        if not _e:
            continue
        _s = sub_counts.get(_e["slug"])
        if _s is None:
            sub_counts[_e["slug"]] = [_e["name"], 1]
        else:
            _s[1] += 1
    sub_sorted = sorted(sub_counts.items(), key=lambda kv: (-kv[1][1], kv[0]))
    # Subcat cards MUST only link L3 pages that actually exist on disk — never a
    # fabricated URL. A native L1 with SKUs but no generated L3 page is omitted from the nav.
    cat_dir = os.path.join(ROOT, "components", cat_slug)
    existing_l3 = set()
    if os.path.isdir(cat_dir):
        for _n in os.listdir(cat_dir):
            if os.path.isfile(os.path.join(cat_dir, _n, "index.html")):
                existing_l3.add(_n)
    visible_subs = [(slug, name, cnt) for slug, (name, cnt) in sub_sorted if slug in existing_l3]
    if visible_subs:
        sub_cards = "".join(
            f'<a class="card subcat-card" href="/components/{esc(cat_slug)}/{esc(slug)}/">'
            f'<div class="sku-mpn">{esc(name)}</div>'
            f'<div class="sku-mfr">{cnt} SKUs</div>'
            f'<div class="muted small">Source {cnt} {esc(name).lower()} from the Shenzhen supply chain.</div>'
            f'</a>'
            for slug, name, cnt in visible_subs
        )
    else:
        sub_cards = (f'<a class="card subcat-card" href="/request-a-quote/" data-zh="获取报价">'
                     f'<div class="sku-mpn">{esc(cat_name)}</div>'
                     f'<div class="sku-mfr">Request a quote</div></a>')

    # ---- 2. Category-specific sourcing copy (data-driven, unique per category) ----
    # Built from REAL subcategory names + REAL top manufacturers in this category,
    # so the six category pages never share identical boilerplate. No fabricated
    # supplier/authorization/stock/price claims.
    top_subs = [name for slug, (name, cnt) in sub_sorted[:4]]
    mfr_counts = {}
    for p in parts:
        m = (p.get("manufacturer") or "").strip()
        if m:
            mfr_counts[m] = mfr_counts.get(m, 0) + 1
    top_mfrs = [m for m, _ in sorted(mfr_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:4]]
    sub_phrase = ", ".join(top_subs)
    mfr_phrase = ", ".join(top_mfrs)
    intro = (
        f"<p>Our {cat_lower} sourcing spans {esc(sub_phrase)} and more, from brands such as "
        f"{esc(mfr_phrase)}. SZ Procure helps global buyers source these parts from the "
        f"Shenzhen supply chain — covering popular families, hard-to-find versions and BOM "
        f"consolidation with verified suppliers and competitive quotes.</p>"
        f'<p class="muted small">Send the part number — our Shenzhen team cross-references '
        f"availability and quotes, whether you need production-volume reels or a single "
        f"hard-to-find variant.</p>"
    )

    # ---- 3. Representative Products (bounded 12; one per top subcategory) ----
    # V1 rule: prioritize subcats by SKU count; pick 1 deterministic rep SKU each;
    # never take global first-N-by-MPN. Future upgrade: if a part carries a
    # `representative` flag, prefer those (no popularity algorithm added this round).
    rep = []
    seen_sub = set()
    for fine, _ in sub_sorted:
        if fine in seen_sub:
            continue
        cands = [p for p in parts if (p.get("native_l1") or "").strip().lower() == fine]
        if not cands:
            continue
        cands.sort(key=lambda x: x["mpn"])
        rep.append(cands[0])
        seen_sub.add(fine)
        if len(rep) >= 12:
            break
    rep_cards = "".join(
        f'<a class="card sku-card" href="/products/{p.get("url_slug") or slugify(p["mpn"])}/">'
        f'<div class="sku-mpn">{esc(p["mpn"])}</div>'
        f'<div class="sku-mfr">{esc(p.get("manufacturer","").strip())}</div></a>'
        for p in rep
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
{seo_head(title, desc, url, noindex=noindex)}
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

    <!-- 1. Hero / Category Sourcing Intro (merged) -->
    <section class="page-head">
      <div class="container">
        <div class="eyebrow" data-zh="元器件分类">Component Category</div>
        <h1>{esc(cat_name)}</h1>
        <p class="lead">{n} {cat_lower} we help global buyers source — from the Shenzhen supply chain.</p>
        {intro}
        <div class="part-head-actions">
          <a class="btn btn-primary btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
          <a class="btn btn-ghost" href="https://wa.me/8613530888389">WhatsApp</a>
          <a class="btn btn-ghost" href="mailto:sales@szprocure.com">Email</a>
        </div>
      </div>
    </section>

    <!-- 2. Subcategory Navigation -->
    <section class="section" id="subcategories">
      <div class="container">
        <h2>{esc(cat_name)} Subcategories</h2>
        <div class="grid grid-4">{sub_cards}</div>
      </div>
    </section>

    <!-- 3. Representative Products (bounded 12) -->
    <section class="section">
      <div class="container">
        <h2>Representative {esc(cat_name)}</h2>
        <div class="grid grid-4">{rep_cards}</div>
        <p class="muted"><a href="#subcategories">Browse the full list in each subcategory →</a></p>
      </div>
    </section>

    <!-- 4. RFQ / Sourcing CTA -->
    <section class="section navy">
      <div class="container" style="text-align:center">
        <h2>Need {("an" if esc(cat_name)[:1].lower() in "aeiou" else "a")} {esc(cat_name)} part?</h2>
        <p>Send us the part number and quantity for a quote.</p>
        <a class="btn btn-white btn-lg" href="/request-a-quote/" data-zh="获取报价">Request a Quote</a>
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

def gen_component_subcategory_page(l2_slug, l2_name, l3_name, l3_slug, parts, all_rows=None, noindex=False):
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
{seo_head(title, desc, url, noindex=noindex)}
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
# Attribute key/value translation for the ENGLISH VISIBLE LAYER (CJK gate fix)
# ---------------------------------------------------------------------------
# The public storefront must render ZERO visible Chinese (permanent CJK gate).
# MASTER attributes_json may legitimately retain original (incl. Chinese) data;
# we translate to English ONLY at render time and NEVER mutate MASTER.
# Source lexicons (curated, machine-readable — same ones publish_normalizer
# uses):
#   - tools/attribute_dictionary.json  -> keys: raw attr KEY   -> English term
#   - tools/value_translation.json     -> value_map: raw VALUE -> English value
# gen_parts now also consults them so the generated EN storefront is Chinese-
# free. Unmappable CJK *values* are DROPPED from the visible layer (retained in
# MASTER) and logged as warnings — the gate is never lowered for them.
_ATTR_KEY_TRANS = {}   # raw key (any lang) -> english key
_VAL_TRANS = {}        # raw value (any lang) -> english value

def load_attr_key_translation(path):
    """Load raw-attr-key -> English term map from attribute_dictionary.json."""
    d = {}
    if not os.path.exists(path):
        print(f"  [WARN] attribute_dictionary.json missing: {path} — key translation off")
        return d
    try:
        obj = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] attribute_dictionary.json parse error: {e}")
        return d
    keys = obj.get("keys", {})
    if isinstance(keys, dict):
        d.update({str(k).strip(): str(v).strip() for k, v in keys.items()})
    return d

def load_value_translation(path):
    """Load raw-attr-value -> English value map from value_translation.json."""
    d = {}
    if not os.path.exists(path):
        print(f"  [WARN] value_translation.json missing: {path} — value translation off")
        return d
    try:
        obj = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        print(f"  [WARN] value_translation.json parse error: {e}")
        return d
    for x in (obj.get("value_map", []) or []):
        if isinstance(x, dict) and "zh" in x and "en" in x:
            d[str(x["zh"]).strip()] = str(x["en"]).strip()
    return d

def has_cjk(s):
    return bool(re.search(r"[\u4e00-\u9fff]", s))

def translate_attr_key(k):
    """Raw attr key -> English. Pure-ASCII keys pass through unchanged."""
    k = (k or "").strip()
    if not k:
        return k
    return _ATTR_KEY_TRANS.get(k, k)

def translate_attr_value(v):
    """English value, or None if it carries unmappable Chinese (-> drop).

    Numeric values are preserved as native JSON numbers (NOT stringified) so the
    deployable parts.json keeps its audited format (e.g. "data_rate": 100000000)
    and the precise field-value exemptions in tools/audit_exemptions.json keep
    matching. This restores the pre-CJK-fix parts.json value contract.
    """
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v  # keep native numeric type (audit exemption + HEAD format)
    vs = str(v).strip()
    if not vs:
        return vs
    if not has_cjk(vs):
        return vs
    return _VAL_TRANS.get(vs, None)

def translate_spec_pairs(pairs):
    """Map raw (k,v) spec pairs to English for the visible layer.
    Drops pairs whose value is unmappable Chinese (kept in MASTER)."""
    out = []
    for k, v in pairs:
        ek = translate_attr_key(k)
        if has_cjk(ek):
            continue  # key still Chinese & unmapped -> skip (defensive)
        ev = translate_attr_value(v)
        if ev is None:
            continue
        out.append([ek, ev])
    return out

# ---- P0-1 / P0-2: human-readable labels + deterministic unit formatting ----------
attribute_label_map = {
    "frequency_hz": "Maximum Clock Speed",
    "speed_hz": "Clock Speed",
    "core": "Core Processor",
    "package": "Package / Case",
    "flash_bytes": "Flash Memory",
    "ram_bytes": "SRAM",
    "memory_bytes": "Memory Size",
    "voltage_v": "Supply Voltage",
    "io_count": "Number of I/O",
    "id_a": "Continuous Drain Current",
    "vds_v": "Drain-Source Voltage",
    "vdss_v": "Drain-Source Voltage",
    "rds_on_mohm": "RDS(on)",
    "vgs_th_v": "Gate Threshold Voltage",
    "vf_v": "Forward Voltage",
    "voltage_rating_v": "Voltage Rating",
    "rated_voltage_v": "Rated Voltage",
    "max_voltage_v": "Maximum Voltage",
    "vreverse_v": "Reverse Voltage",
    "output_voltage_v": "Output Voltage",
    "current_rating_a": "Current Rating",
    "if_a": "Forward Current",
    "forward_current_a": "Forward Current",
    "ic_a": "Collector Current",
    "output_current_a": "Output Current",
    "ibias_a": "Input Bias Current",
    "resistance_ohm": "Resistance",
    "impedance_ohm": "Impedance",
    "dcr_ohm": "DC Resistance (DCR)",
    "temperature_coeff": "Temperature Coefficient",
    "temp_coef": "Temperature Coefficient",
    "tolerance": "Tolerance",
    "positions": "Number of Positions",
    "lines": "Number of Lines",
    "elem_count": "Number of Elements",
    "num_amps": "Number of Amplifiers",
    "mounting": "Mounting Type",
    "power_rating_w": "Power Rating",
    "capacitance_pf": "Capacitance",
    "capacitance": "Capacitance",
    "load_capacitance_pf": "Load Capacitance",
    "inductance_uh": "Inductance",
    "pitch_mm": "Pitch",
    "interface": "Interface",
    "organization": "Memory Organization",
    "qg_nc": "Gate Charge",
    "vrrm_v": "Repetitive Peak Reverse Voltage",
    "data_rate": "Data Rate",
    "tran_type": "Transistor Type",
    "vceo_v": "Collector-Emitter Breakdown Voltage",
    "cmrr_db": "Common Mode Rejection Ratio",
    "gbw_hz": "Gain Bandwidth Product",
}

def _fmt_num(x):
    if x == int(x):
        return str(int(x))
    return f"{x:.3f}".rstrip("0").rstrip(".")

def human_attr_label(k):
    k = (k or "").strip()
    if not k:
        return k
    if k in attribute_label_map:
        return attribute_label_map[k]
    if " " in k:
        return k
    if "_" in k:
        return k.replace("_", " ").title()
    return k.title()

def format_attr_value(k, v):
    if v is None:
        return v
    s = str(v).strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return v
    try:
        num = float(s)
    except ValueError:
        return v
    key = (k or "").lower()
    if key.endswith("_bytes"):
        if num >= 1_000_000:
            return _fmt_num(num / 1_000_000) + " MB"
        if num >= 1024:
            return _fmt_num(num / 1024) + " KB"
        return _fmt_num(num) + " B"
    if key.endswith("_hz"):
        if num >= 1_000_000_000:
            return _fmt_num(num / 1_000_000_000) + " GHz"
        if num >= 1_000_000:
            return _fmt_num(num / 1_000_000) + " MHz"
        if num >= 1000:
            return _fmt_num(num / 1000) + " kHz"
        return _fmt_num(num) + " Hz"
    if key.endswith("_v") or key.endswith("_volt"):
        return _fmt_num(num) + " V"
    if key.endswith("_a"):
        return _fmt_num(num) + " A"
    if key.endswith("_mohm"):
        return _fmt_num(num) + " mΩ"
    if key.endswith("_ohm"):
        return _fmt_num(num) + " Ω"
    if key.endswith("_pf"):
        return _fmt_num(num) + " pF"
    if key.endswith("_uh"):
        return _fmt_num(num) + " µH"
    if key.endswith("_w"):
        return _fmt_num(num) + " W"
    if key == "tolerance":
        return _fmt_num(num) + " %"
    return v

def build_en_attrs(raw):
    """Parse a raw attributes_json string and return an English-keyed/valued
    dict for the deployable parts.json. Unmappable CJK pairs are dropped
    (kept in MASTER). Returns {} on empty/invalid input."""
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    out = {}
    for k, v in obj.items():
        ek = translate_attr_key(k)
        if has_cjk(ek):
            continue
        ev = translate_attr_value(v)
        if ev is None:
            continue
        out[ek] = ev
    return out

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


# ==============================================================================
# PRODUCTION SOURCE GUARDS (P0-2) — make fake-data rebuilds impossible
# ==============================================================================
# These guards are the permanent backstop. Even if a synthetic-data generator
# (human- or AI-authored) is ever invoked, the build refuses to publish it.
SYNTHETIC_MPN_PATTERNS = [
    re.compile(r'^(MCU|MOS|RES|CAP|IND|DIO|CON|XTAL|MEM|WIFI|MOD|REG|AMP|OP|LED|PWR|IC)\d{6}', re.I),
    re.compile(r'100000\d{3}'),                 # the MCU100000xxx / MOS100000xxx family
    re.compile(r'^\d{6,}$'),                    # pure long numeric placeholder
    re.compile(r'PLACEHOLDER', re.I),
    re.compile(r'XXX$', re.I),
    re.compile(r'_(TEST|SAMPLE|MOCK)$', re.I),
]

# P0-2 WHITELIST: real manufacturer all-digit MPNs that match the synthetic-MPN
# pattern (^\d{6,}$) but are legitimate production parts (Molex uses purely numeric
# part numbers). Authorized 2026-09-11 to unblock the production build — these are
# NOT synthetic/test data. Future legitimate numeric MPNs go here.
SYNTHETIC_MPN_WHITELIST = {
    "5023520200",   # Molex
    "1054500101",   # Molex
}

FAKE_BRAND_TOKENS = re.compile(
    r'(Acme|Nova|Placeholder|Synthetic|Mock|Fake|TestCorp|DemoSemi|Injected)', re.I)

def _abort_build(reason_lines):
    print("\n" + "=" * 72)
    print("ERROR:")
    for line in reason_lines:
        print(line)
    print("=" * 72)
    sys.exit(2)

def validate_production_source(csv_path):
    """P0-2: only a v2+ Master under data/production/ may feed the build.
    Rejects sample/scale/test/pilot/founder sources and the deprecated v1.x
    test masters, so a future operator (human OR AI) can never rebuild the
    site from synthetic data."""
    low = csv_path.lower().replace("\\", "/")
    base = os.path.basename(low)
    if "data/production/" not in low:
        _abort_build([
            f"Source {csv_path} is not under data/production/.",
            "Only data/production/master_parts_*.csv may feed the production build.",
            "Production build aborted.",
        ])
    if not re.search(r'master_parts_[a-z0-9._-]+\.csv$', base):
        _abort_build([
            f"Source filename '{os.path.basename(csv_path)}' is not a master_parts_*.csv.",
            "Production build aborted.",
        ])
    if "v1" in base:
        _abort_build([
            "Old v1.x test master is forbidden (it contains synthetic SKUs).",
            "Use master_parts_v2.x.csv or later.",
            "Production build aborted.",
        ])
    for b in ("sample", "scale", "test", "deprecated", "mock", "fake", "_pilot", "founder"):
        if b in base:
            _abort_build([
                f"Blocked keyword '{b}' in source filename '{os.path.basename(csv_path)}'.",
                "Synthetic/sample sources are not allowed in production.",
                "Production build aborted.",
            ])
    return True

def detect_synthetic_mpn(rows):
    """P0-2: hard-stop if ANY row looks like a synthetic/test MPN or brand.
    Last line of defense — the build refuses to publish fake data even if a
    generator produced it."""
    bad = []
    for i, r in enumerate(rows, 1):
        mpn = (r.get("mpn") or "").strip()
        if mpn in SYNTHETIC_MPN_WHITELIST:
            continue  # authorized legitimate all-digit MPN (see SYNTHETIC_MPN_WHITELIST)
        mfr = (r.get("manufacturer") or "").strip()
        hit = None
        for pat in SYNTHETIC_MPN_PATTERNS:
            if pat.search(mpn):
                hit = f"synthetic MPN pattern '{pat.pattern}'"
                break
        if hit is None and FAKE_BRAND_TOKENS.search(mfr):
            hit = f"synthetic brand '{mfr}'"
        if hit:
            bad.append((i, mpn or "(no mpn)", hit))
    if bad:
        lines = ["Synthetic MPN detected.", "Production build aborted."]
        for i, mpn, why in bad[:25]:
            lines.append(f"  row {i}: {mpn}  ({why})")
        if len(bad) > 25:
            lines.append(f"  ... and {len(bad) - 25} more")
        _abort_build(lines)
    return True

# ===========================================================================
# PHASE 1 — Incremental Publishing: Build State / Build Key / Change Detection
# ---------------------------------------------------------------------------
# Authorized 2026-09-08. SCOPE: BUILD STATE ONLY.
#   * Adds build_manifest.json I/O, unified build_key(), and classify_sku().
#   * Prints a Publishing Plan via --incremental (dry-run, no HTML written).
#   * Bootstraps build_manifest.json from existing products/ (--bootstrap-manifest).
# HARD BOUNDARY (per project frozen rules):
#   * PHASE 2 write path (--incremental, non-dry) writes ONLY the CREATE/UPDATE
#     subset of products/*.html — never a full rebuild, never MASTER, never
#     sitemap-canonical/schema/URL-slug, never the V2/V3 renderer bodies.
#   * SKIP pages are filtered BEFORE the renderer is called (never written).
#   * A SCOPE GUARD aborts if any planned write is neither explicitly requested
#     nor a legitimate dependency-induced change (unexpected > 0 -> STOP).
#   * Global artifacts (sitemap/parts.json/search) are recomputed in a SEPARATE
#     step after all SKU HTML writes succeed; they never re-render SKU HTML.
#   * The existing production full-rebuild path (open(index.html,"w") + sitemap +
#     parts.json) is untouched; --incremental / --bootstrap-manifest short-circuit
#     and return before it. No --full-rebuild flag is implemented (anti-footgun).
# Reuses tools/factory/master_io.row_fingerprint + sha256_of (no second algorithm).
# ===========================================================================
MANIFEST_PATH = os.path.join(ROOT, "build_manifest.json")
TEMPLATE_VERSION = "2026.09.v3.phase1"   # bump when gen_part_page / gen_part_page_v3 body changes
SCOPE_CEILING_RATIO = 0.05              # Phase 2 Scope Guard ceiling (informational in Phase 1)
# Page-affecting MASTER columns that feed a SKU's data_fp. Deliberately EXCLUDES
# traceability / derived cols (source, source_url, supplier_reference, url_slug,
# clean_mpn, availability) so internal edits never spuriously rebuild a page.
INCREMENTAL_DATA_COLS = [
    "mpn", "manufacturer", "brand", "category", "subcategory",
    "description", "applications", "keywords", "attributes_json",
    "alternative_parts", "datasheet_url", "faq", "image",
]


class ManifestError(RuntimeError):
    """Raised when build_manifest.json is present but corrupt (fail-safe)."""


_master_io_cache = None


def _get_master_io():
    """Lazy import of tools/factory/master_io (reuses row_fingerprint / sha256_of)."""
    global _master_io_cache
    if _master_io_cache is not None:
        return _master_io_cache
    import sys
    tools_dir = os.path.join(ROOT, "tools")
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)
    from factory import master_io  # package import (tools/ on sys.path)
    _master_io_cache = master_io
    return master_io


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _build_by_cat(groups):
    by_cat = defaultdict(list)
    for g in groups:
        cslug, _ = resolve_cat((g.get("native_l1") or "").strip())
        by_cat[cslug].append(g)
    return by_cat


def _category_pool_fingerprint(cslug, by_cat):
    """O(n) deterministic fingerprint of a category's Related-rotation pool.

    Mirrors build_related_map's pool order (L2223): (mpn, url_slug) pairs in
    MASTER row order within the category. Any change to the pool sequence
    (insert / remove / rename / reorder a SKU) changes this hash -> the
    affected category's co-members become dependency-induced UPDATE.
    No O(n^2) global hash: Alternative coupling is handled in Phase 2 via a
    reverse-edge index, NOT folded into this fingerprint.
    """
    mio = _get_master_io()
    rows = [{"mpn": g["mpn"].strip(),
             "slug": (g.get("url_slug") or "").strip() or slugify(g["mpn"].strip())}
            for g in by_cat.get(cslug, []) if g.get("mpn", "").strip()]
    return mio.row_fingerprint(rows, ["mpn", "slug"])


def _related_parts_fingerprint(slug, related_map):
    """Precision page-level fingerprint of a SKU's OWN Related-Parts top-6.

    Replaces the coarse category-pool hash (_category_pool_fingerprint) for the
    actual change-decision. That coarse hash flipped for EVERY co-member whenever
    ANY SKU in the category changed, producing hundreds of false-positive
    Dependency UPDATEs (verified: 531 candidate -> only 30 real HTML changes).

    This hashes ONLY the (mpn, slug) neighbours that build_related_map() actually
    emits for THIS page — the identical top-6 that renders into the Related Parts
    section. Because master_io.row_fingerprint() is order-sensitive, any shift in
    the rendered neighbour sequence changes this hash (genuine UPDATE); an
    unchanged neighbour set is an exact SKIP. No false positives, no leaked cats,
    and the Alternative reverse-edge (target_slug -> source_slugs) is untouched.

    related_map: slug -> list[(pn, slug)] from build_related_map(by_cat, k=6).
    """
    mio = _get_master_io()
    rows = [{"mpn": pn, "slug": s} for (pn, s) in related_map.get(slug, [])]
    return mio.row_fingerprint(rows, ["mpn", "slug"])


def _enrich_fp_for(slug, mpn):
    """SHA256 of the SKU's enrichment JSON, or NO_ENRICH when absent.

    Resolution order mirrors load_enrichment() (files named by slug/mpn):
    data/enrich/<slug>.content.json | <SLUG> | <mpn> | <MPN>.
    """
    mio = _get_master_io()
    base = os.path.join(ROOT, "data", "enrich")
    for name in (slug, (slug or "").upper(), mpn, (mpn or "").upper()):
        if not name:
            continue
        p = os.path.join(base, f"{name}.content.json")
        if os.path.exists(p):
            return mio.sha256_of(p)
    return "NO_ENRICH"


def _asset_fp_for(renderer_v):
    """Stable hash of the CSS assets a SKU page actually loads.

    v3 page links both /assets/styles.css and /assets/sku-v3.css (L1635-1636);
    v2 links only /assets/styles.css. Missing file -> UNKNOWN (caller marks
    REQUIRES_REBUILD, never silently SKIPs).
    """
    mio = _get_master_io()
    rels = ["assets/sku-v3.css", "assets/styles.css"] if renderer_v == "v3" else ["assets/styles.css"]
    chunks = []
    for rel in rels:
        p = os.path.join(ROOT, rel)
        if not os.path.exists(p):
            return "UNKNOWN"
        chunks.append(mio.sha256_of(p))
    return hashlib.sha256("|".join(chunks).encode("utf-8")).hexdigest()


def _renderer_v_for(pn):
    return "v2" if pn.upper() in V2_LEGACY_EXCEPTIONS else "v3"


def _data_fp_for(row):
    mio = _get_master_io()
    cols = [c for c in INCREMENTAL_DATA_COLS if c in row]
    return mio.row_fingerprint([row], cols)


def compute_build_key(slug, row, by_cat, related_map=None):
    """Unified build_key (six components) for one SKU.

    Returns a dict with slug/mpn + the six fingerprints used by classify_sku().
    Both bootstrap and the incremental pipeline call THIS, so baseline and
    re-runs are guaranteed consistent (a no-op incremental run yields all SKIP).

    dependency_fp: when related_map is supplied, uses the PRECISE page-level
    Related-Parts fingerprint (_related_parts_fingerprint) — only pages whose
    own top-6 neighbours actually shift become Dependency UPDATE. When omitted
    (legacy callers / coarse stress-test comparison), it falls back to the
    category-pool fingerprint so behaviour stays well-defined.
    """
    pn = (row.get("mpn") or "").strip()
    renderer_v = _renderer_v_for(pn)
    cslug, _ = resolve_cat((row.get("native_l1") or "").strip())
    dependency_fp = (_related_parts_fingerprint(slug, related_map)
                     if related_map is not None
                     else _category_pool_fingerprint(cslug, by_cat))
    return {
        "slug": slug,
        "mpn": pn,
        "renderer_v": renderer_v,
        "data_fp": _data_fp_for(row),
        "enrich_fp": _enrich_fp_for(slug, pn),
        "template_v": TEMPLATE_VERSION,
        "asset_v": _asset_fp_for(renderer_v),
        "dependency_fp": dependency_fp,
    }


def classify_sku(desired, recorded):
    """CREATE / UPDATE / SKIP for one SKU.

    recorded is the manifest entry dict, or None (missing -> CREATE).
    Any change among the six fingerprints -> UPDATE; identical -> SKIP.
    """
    if recorded is None:
        return "CREATE"
    for f in ("renderer_v", "data_fp", "enrich_fp", "template_v", "asset_v", "dependency_fp"):
        if desired.get(f) != recorded.get(f):
            return "UPDATE"
    return "SKIP"


def _update_kind(desired, recorded):
    """Classify an UPDATE as Direct vs Dependency-induced (for the plan report)."""
    dep_only = (desired.get("dependency_fp") != recorded.get("dependency_fp")) and all(
        desired.get(f) == recorded.get(f)
        for f in ("renderer_v", "data_fp", "enrich_fp", "template_v", "asset_v"))
    return "dependency-induced" if dep_only else "direct"


def load_manifest(path=MANIFEST_PATH):
    """Load build_manifest.json. Returns None if missing (caller treats as empty).

    Raises ManifestError on corrupt JSON (fail-safe: never silently proceeds).
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        raise ManifestError(f"build_manifest.json at {path} is corrupt and cannot be "
                            f"parsed ({e}). Refusing to proceed silently.")


def save_manifest(path=MANIFEST_PATH, manifest=None, dry_run=False):
    """Atomic write of build_manifest.json (temp + validate + os.replace).

    Under dry_run, writes nothing and returns a description dict.
    """
    if manifest is None:
        manifest = {}
    if dry_run:
        return {"written": False, "dry_run": True, "path": path, "entries": len(manifest.get("skus", {}))}
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        with open(tmp, encoding="utf-8") as f:  # round-trip validate
            json.load(f)
        os.replace(tmp, path)  # atomic on same volume
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return {"written": True, "path": path, "entries": len(manifest.get("skus", {}))}


def bootstrap_manifest(args, groups, out_root, manifest_path=MANIFEST_PATH):
    """PHASE 1 baseline: scan current products/ and RECORD published state.

    Does NOT regenerate HTML, does NOT edit MASTER/sitemap/global artifacts.
    Writes build_manifest.json only when NOT --dry-run. Orphans (products pages
    with no MASTER row) are reported separately and NEVER added to the manifest.
    """
    products_dir = os.path.join(out_root, "products")
    published = set()
    if os.path.isdir(products_dir):
        for name in os.listdir(products_dir):
            if os.path.isfile(os.path.join(products_dir, name, "index.html")):
                published.add(name)
    row_by_slug = {g["url_slug"]: g for g in groups if g.get("url_slug")}
    by_cat = _build_by_cat(groups)
    related_map = build_related_map(by_cat, k=6)

    skus = {}
    orphans = []
    for slug in sorted(published):
        row = row_by_slug.get(slug)
        if row is None:
            orphans.append(slug)  # historical orphan (e.g. lm321 / lm393dt): report, do NOT manifest
            continue
        bk = compute_build_key(slug, row, by_cat, related_map)
        if "UNKNOWN" in (bk.get("asset_v"),):
            bk["status"] = "REQUIRES_REBUILD"  # honest: do NOT mark SKIP
        else:
            bk["status"] = "published"
        bk["last_written"] = "baseline"
        skus[slug] = bk

    missing = [g["url_slug"] for g in groups
               if g.get("url_slug") and g["url_slug"] not in published]

    manifest = {
        "meta": {
            "template_version": TEMPLATE_VERSION,
            "bootstrapped_at": _now_iso(),
            "sku_count": len(skus),
            "orphan_count": len(orphans),
            "missing_page_count": len(missing),
        },
        "skus": skus,
    }

    if args.dry_run:
        print(f"  [BOOTSTRAP dry-run] Would record {len(skus)} published SKUs; "
              f"{len(orphans)} orphan(s); {len(missing)} MASTER row(s) without a page.")
        if orphans:
            print(f"  [BOOTSTRAP] ORPHAN = {', '.join(orphans)}")
        print(f"  [BOOTSTRAP dry-run] build_manifest.json NOT written (--dry-run).")
        return {"written": False, "skus": len(skus), "orphans": orphans, "missing": missing}

    res = save_manifest(path=manifest_path, manifest=manifest, dry_run=False)
    print(f"  [BOOTSTRAP] Recorded {len(skus)} published SKUs -> {manifest_path}")
    print(f"  [BOOTSTRAP] ORPHAN = {len(orphans)} : {', '.join(orphans) if orphans else '(none)'}")
    if missing:
        print(f"  [BOOTSTRAP] {len(missing)} MASTER row(s) have no products/<slug>/ page "
              f"(would be CREATE on next incremental run).")
    return res


def _build_alt_reverse(groups, slug_set, slug_by_mpn):
    """O(edges) Alternative reverse-edge index: target_slug -> set(source_slug).

    Mirrors the renderer's Alternative resolution (L749/L1439: aslug = slugify(a);
    a clickable link is emitted only when aslug is in generated_slugs). A source
    lists the alt token `a`; the target slug is slugify(a) with an MPN fallback for
    registry-suffixed slugs. Targets that do NOT resolve to a real SKU slug create
    NO edge — design rule: "unparseable target -> no fabricated dependency".
    """
    rev = defaultdict(set)
    for g in groups:
        src = g.get("url_slug")
        if not src:
            continue
        alt_raw = (g.get("alternative_parts") or "").strip()
        if not alt_raw:
            continue
        for a in split_multi(alt_raw):
            if not slugify(a):
                continue
            tgt = slugify(a)
            if tgt not in slug_set:
                tgt = slug_by_mpn.get(a.strip().upper(), "")
            if tgt and tgt in slug_set:
                rev[tgt].add(src)
    return rev


def _write_sku_page_atomic(args, g, cslug, mfr_slug, related, generated_slugs, out_root):
    """Render one SKU via the production V2/V3 renderer and write it atomically.

    tempfile -> validate -> os.replace on the same volume. On ANY failure the temp
    is removed and the exception propagates; the caller aborts the whole batch and
    leaves build_manifest.json untouched (self-heal on the next run).
    """
    pn = g["mpn"].strip()
    slug = g["url_slug"]
    if pn.upper() in V2_LEGACY_EXCEPTIONS:
        page = gen_part_page(g, cslug, mfr_slug, related=related.get(slug, []),
                             generated_slugs=generated_slugs)
    else:
        page = gen_part_page_v3(g, cslug, mfr_slug, related=related.get(slug, []),
                                generated_slugs=generated_slugs, verbose=False)
    if "<html" not in page and "<!DOCTYPE" not in page.upper():
        raise RuntimeError(f"renderer produced no HTML for {slug} "
                           f"(refusing to write a broken page)")
    d = os.path.join(out_root, "products", slug)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".index.", suffix=".tmp.html")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp, os.path.join(d, "index.html"))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return os.path.join(d, "index.html")


def regen_global_artifacts(args, groups, out_root, by_cat, related_map, generated_slugs):
    """PHASE 2 global-artifact recompute — STRICTLY separated from SKU HTML.

    Rewrites sitemap_parts.xml (+index), parts.json, and /search/ shards from the
    current groups/urls. It does NOT render or touch any products/<slug>/index.html
    (those were already handled by the incremental write loop). URLs for
    manufacturer/category/L3/hub pages are preserved in the sitemap (those HTML
    pages persist from the last full build and are out of incremental scope).
    Called only after every SKU HTML write succeeded (and only when not --dry-run).
    """
    by_mfr = defaultdict(list)
    for g in groups:
        by_mfr[g["manufacturer"].strip()].append(g)

    rows = []
    try:
        with open(args.csv, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("mpn", "").strip()]
    except Exception:
        rows = []

    # I3: exclude hidden/review SKUs from the sitemap (they already get noindex on-page)
    urls = [
        f"{DOMAIN}/products/{g['url_slug']}/"
        for g in groups
        if g.get("url_slug") and effective_publish_status(g) not in ("hidden", "review")
    ]
    for mfr in by_mfr:
        urls.append(f"{DOMAIN}/manufacturers/{slugify_name(mfr)}/")
    urls.append(f"{DOMAIN}/manufacturers/")
    for cslug in TOP_CATEGORIES:
        _top_ps = load_taxonomy()["tops"].get(cslug, {}).get("publish_status", "active")
        parts = by_cat.get(cslug, [])
        if _top_ps == "active":
            urls.append(f"{DOMAIN}/components/{cslug}/")
        l3_groups = defaultdict(list)
        for p in parts:
            fine = (p.get("native_l1") or "").strip()
            if fine:
                l3_groups[fine].append(p)
        for fine in sorted(l3_groups):
            if _top_ps != "active":
                continue
            if l3_page_should_skip(fine):
                continue
            if resolve_native(fine)["publish_status"] in ("hidden", "review"):
                continue
            urls.append(f"{DOMAIN}/components/{cslug}/{slugify_name(fine)}/")
    urls.append(f"{DOMAIN}/components/")

    # ---- split sitemap (all indexed URLs) ----
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
        _native = resolve_native(g.get("native_l1"))
        cat = _native.get("l1_name") or (g.get("category") or "").strip()
        c_top = _native.get("top_slug") or ""
        p_slug = g["url_slug"]
        m_slug = slugify_name(mfr)
        key_p = ("p", pn.lower())
        if key_p not in seen:
            search_entries.append({"t": pn, "k": pn.lower(), "keys": pn_search_keys(pn),
                                   "ty": "Part", "u": f"/products/{p_slug}/",
                                   "sub": f"{mfr} \u00b7 {cat}"})
            seen.add(key_p)
        key_m = ("m", mfr.lower())
        if key_m not in seen:
            search_entries.append({"t": mfr, "k": mfr.lower(), "ty": "Manufacturer",
                                   "u": f"/manufacturers/{m_slug}/", "sub": "View all sourced parts"})
            seen.add(key_m)
        key_c = ("c", cat.lower())
        if key_c not in seen and c_top:
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

    # ---- parts.json (machine-readable; carries sources + needs_review) ----
    parts_json = []
    for g in groups:
        mpn = g["mpn"].strip()
        if not mpn:
            continue
        clean = (g.get("clean_mpn") or "").strip() or re.sub(r"[^A-Z0-9]", "", mpn.upper())
        uslug = g["url_slug"]
        raw = (g.get("attributes_json") or "").strip()
        attrs = build_en_attrs(raw)  # English visible-layer (CJK gate fix)
        parts_json.append({
            "mpn": mpn,
            "clean_mpn": clean,
            "manufacturer": g["manufacturer"].strip(),
            "brand": g.get("brand", g["manufacturer"]).strip(),
            "url_slug": uslug,
            # I3/I4: parts.json category/subcategory use the native_l1 taxonomy (single
            # source of truth), not the legacy `category` string. Defensive fallback to the
            # legacy value only if native_l1 cannot be resolved (never expected).
            "category": top_scope_name(resolve_native(g.get("native_l1")).get("top_slug") or "") or g.get("category", "").strip(),
            "subcategory": resolve_native(g.get("native_l1")).get("l1_name") or g.get("subcategory", "").strip(),
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

    # ---- components-data.js (LIVE Components Hub search source) ----
    # Derived from the SAME in-memory `groups` that feed parts.json/sitemap/legacy-search,
    # so it can never drift from the published SKU set. Regenerated on every global
    # artifact rebuild (incremental + full); never touches SKU HTML.
    _regen_components_data(args, groups, out_root, by_cat, related_map, generated_slugs)


def _regen_components_data(args, groups, out_root, by_cat, related_map, generated_slugs):
    """Regenerate components/components-data.js (window.SZ_COMPONENTS) — the LIVE
    Components Hub search source consumed by hub.js / search.js.

    Derived strictly from the in-memory `groups` already used to build parts.json /
    sitemap / legacy-search (NEVER from a stale components-data.js), guaranteeing the
    live search index stays consistent with the published SKU set after every incremental
    or full publish. Writes ONLY this single file — never SKU HTML.

    Field contract (frontend-compatible, unchanged):
      categories[]    : {slug, name, url, count, subcategories:[{slug,name,url,count}]}
      manufacturers[] : {slug, name, url, count}
      parts[]         : {mpn, mfr, subcat, cat, url, slug}
    Display label = real MPN (mpn); click URL = real url_slug (url); client-side aliases
    (original / cleaned / slug) are computed by hub.js partAliases(), NOT stored here.
    """
    by_mfr = defaultdict(list)
    for g in groups:
        by_mfr[g["manufacturer"].strip()].append(g)
    manufacturers = []
    for mfr in sorted(by_mfr, key=lambda m: m.lower()):
        mslug = slugify_name(mfr)
        manufacturers.append({
            "slug": mslug,
            "name": mfr,
            "url": f"/manufacturers/{mslug}/",
            "count": len(by_mfr[mfr]),
        })

    categories = []
    for cslug, cname in ACTIVE_TOP_CATEGORIES.items():
        cat_parts = by_cat.get(cslug, [])
        l3 = defaultdict(list)
        for p in cat_parts:
            fine = (p.get("native_l1") or "").strip()
            if fine:
                l3[fine].append(p)
        subcats = []
        for fine in sorted(l3, key=lambda f: f.lower()):
            fslug = slugify_name(fine)
            fentry = _native_entry(fine)
            subcats.append({
                "slug": fslug,
                "name": (fentry["name"] if fentry else fine),
                "url": f"/components/{cslug}/{fslug}/",
                "count": len(l3[fine]),
            })
        categories.append({
            "slug": cslug,
            "name": cname,
            "url": f"/components/{cslug}/",
            "count": len(cat_parts),
            "subcategories": subcats,
        })

    parts_out = []
    for g in groups:
        pn = g["mpn"].strip()
        if not pn:
            continue
        slug = g.get("url_slug") or ""
        if not slug:
            continue
        _cres = resolve_native(g.get("native_l1"))
        cslug = _cres.get("top_slug") or ""
        parts_out.append({
            "mpn": pn,
            "mfr": g["manufacturer"].strip(),
            "subcat": _cres.get("l1_name") or (g.get("category") or "").strip(),
            "cat": top_scope_name(cslug) if cslug else (g.get("category") or "").strip(),
            "url": f"/products/{slug}/",
            "slug": slug,
        })

    payload = {
        "generated_at": _now_iso(),
        "categories": categories,
        "manufacturers": manufacturers,
        "parts": parts_out,
    }
    out_dir = os.path.join(out_root, "components")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "components-data.js")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("// AUTO-GENERATED by regen_global_artifacts (gen_parts.py) — DO NOT EDIT BY HAND.\n")
        f.write("// Read-only derivation from the current publish-chain groups (same source as parts.json).\n")
        f.write("window.SZ_COMPONENTS = ")
        f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        f.write(";\n")
    print(f"components-data.js: {len(parts_out)} parts -> {out_path}")


def incremental_pipeline(args, groups, out_root, manifest_path=MANIFEST_PATH):
    """PHASE 2 write path: CREATE/UPDATE/SKIP -> atomic HTML writes + manifest txn.

    Under --dry-run: prints the Publishing Plan ONLY (no HTML, manifest unchanged).
    Non-dry and authorized:
      1. change-detect (CREATE/UPDATE/SKIP) via the six-component build_key.
      2. dependency impact: page-level Related-Parts fingerprint (auto via
         dependency_fp — only pages whose own top-6 neighbours shift UPDATE) +
         Alternative reverse-edge index (O(edges), no fabricated deps for
         unparseable targets).
      3. SCOPE GUARD: unexpected = planned - (requested ∪ dependency_induced);
         if unexpected > 0 -> STOP before any write.
      4. atomic write CREATE+UPDATE SKU HTML (SKIP filtered BEFORE the renderer).
      5. manifest transaction: build_manifest.json updated only AFTER all HTML
         writes succeed; any mid-batch failure leaves the manifest untouched.
      6. regenerate global artifacts (sitemap/parts.json/search) — separate step,
         never re-renders SKU HTML.
    Does NOT implement --full-rebuild; does NOT edit MASTER/sitemap-canonical/schema.
    A missing/corrupt manifest is fail-safe (None -> treat as empty; corrupt -> abort).
    """
    by_cat = _build_by_cat(groups)
    related_map = build_related_map(by_cat, k=6)
    full = load_manifest(manifest_path)
    if full is None:
        print(f"  [INCREMENTAL] No build_manifest.json at {manifest_path} "
              f"-> treated as empty baseline (every SKU = CREATE).")
        full = {"meta": {}, "skus": {}}
    skus = full.get("skus", {})

    slug_set = {g["url_slug"] for g in groups if g.get("url_slug")}
    slug_by_mpn = {g["mpn"].strip().upper(): g["url_slug"]
                   for g in groups if g.get("url_slug")}

    # explicit request set (from --single, comma-separated). Empty => full-sync.
    requested_mpns = set()
    if args.single:
        for tok in args.single.split(","):
            tok = tok.strip().upper()
            if tok:
                requested_mpns.add(tok)
    requested_slugs = {slug_by_mpn.get(m) for m in requested_mpns if m in slug_by_mpn}

    # change detection
    plan = {}
    for g in groups:
        slug = g.get("url_slug")
        if not slug:
            continue
        desired = compute_build_key(slug, g, by_cat, related_map)
        recorded = skus.get(slug)
        action = classify_sku(desired, recorded)
        plan[slug] = {"g": g, "desired": desired, "recorded": recorded,
                      "action": action, "kind": None}

    # dependency impact: Alternative reverse-edge (upgrade SKIP -> dependency-induced)
    alt_rev = _build_alt_reverse(groups, slug_set, slug_by_mpn)
    write_set = {s for s, p in plan.items() if p["action"] in ("CREATE", "UPDATE")}
    dep_from_alt = set()
    for s in write_set:
        for src in alt_rev.get(s, ()):
            dep_from_alt.add(src)
    for s, p in plan.items():
        if p["action"] == "SKIP" and s in dep_from_alt:
            p["action"] = "UPDATE"
            p["kind"] = "dependency-induced"
            write_set.add(s)

    # classify remaining UPDATE kind (direct vs dependency-induced)
    for s, p in plan.items():
        if p["action"] == "UPDATE" and p["kind"] is None:
            p["kind"] = _update_kind(p["desired"], p["recorded"])

    dep_induced_slugs = {s for s, p in plan.items()
                         if p["action"] == "UPDATE" and p["kind"] == "dependency-induced"}

    # ---- affected-node computation (Plan B v2: NODE write-set) ----
    # Each CREATE/UPDATE SKU (DIRECT or DEPENDENCY) deterministically implies its
    # Brand / Category / Fine(L3) pages must be refreshed from CURRENT full data,
    # so a newly published SKU appears on its Brand/Category/L3 listings immediately.
    # UNMAPPED / COLLISION / SELF_REFERENCE nodes are SKIPped (safe — no forced
    # resolution). Node pages are a pure function of the validated SKU write_set, so
    # they can NEVER be "unexpected"; they are written only AFTER M4-C scope guard
    # passes (M4-C itself is untouched — it classifies SKU slugs only).
    # NOTE: SKU *moves* (mfr/category change) refresh only the NEW affiliation; the
    # prior affiliation is not recoverable from the build_key, so old nodes are left
    # untouched rather than guessed at (per spec — "don't guess").
    by_mfr = defaultdict(list)
    for g in groups:
        by_mfr[g["manufacturer"].strip()].append(g)
    l3_groups = defaultdict(lambda: defaultdict(list))
    for cslug, _parts in by_cat.items():
        for p in _parts:
            fine = (p.get("native_l1") or "").strip()
            if fine:
                l3_groups[cslug][fine].append(p)

    affected_brands = set()   # manufacturer names
    affected_cats = set()     # top-level category slugs
    affected_l3 = set()       # (top_slug, fine_name, l3_slug)
    for s in write_set:
        g = plan[s]["g"]
        affected_brands.add(g["manufacturer"].strip())
        _status, _cslug, _cname = resolve_cat_state((g.get("native_l1") or "").strip())
        if _status in ("RESOLVED", "SELF_REFERENCE"):
            affected_cats.add(_cslug)
        _fine = (g.get("native_l1") or "").strip()
        if _fine and not l3_page_should_skip(_fine):
            affected_l3.add((_cslug, _fine, slugify_name(_fine)))
    # SCOPE GUARD: unexpected = planned - (requested ∪ dependency_induced)
    if args.single and not requested_slugs:
        # operator named SKUs that do not exist in MASTER -> refuse, do not full-sync
        print("=" * 72)
        print("  [SCOPE GUARD] ABORT: --single MPN(s) not found in MASTER: "
              f"{', '.join(sorted(requested_mpns))}")
        print("=" * 72)
        return {"aborted": True, "reason": "requested_not_in_master",
                "requested": sorted(requested_mpns)}
    if not requested_slugs:
        allowed = slug_set                      # full-sync: everything is requested
    else:
        allowed = requested_slugs | dep_induced_slugs
    unexpected = write_set - allowed

    n_create = sum(1 for p in plan.values() if p["action"] == "CREATE")
    n_update = sum(1 for p in plan.values() if p["action"] == "UPDATE")
    n_skip = sum(1 for p in plan.values() if p["action"] == "SKIP")
    n_direct = sum(1 for p in plan.values()
                   if p["action"] == "UPDATE" and p["kind"] == "direct")
    n_dep = sum(1 for p in plan.values()
                if p["action"] == "UPDATE" and p["kind"] == "dependency-induced")
    total = len(groups)

    # ---- dry-run: report and return (no HTML, manifest unchanged) ----
    if args.dry_run:
        print("=" * 72)
        print("  INCREMENTAL PUBLISHING PLAN  (dry-run — no HTML written, manifest unchanged)")
        print("=" * 72)
        print(f"  CREATE : {n_create}")
        print(f"  UPDATE : {n_update}")
        print(f"  SKIP   : {n_skip}")
        print(f"  FULL REBUILD: NO")
        print(f"  Planned HTML writes : {n_create + n_update}")
        print(f"  Direct changes            : {n_direct}")
        print(f"  Dependency-induced changes: {n_dep}")
        print(f"  Affected Brand    pages : {len(affected_brands)}")
        print(f"  Affected Category pages : {len(affected_cats)}")
        print(f"  Affected Fine(L3) pages : {len(affected_l3)}")
        print(f"  Unexpected changes        : {len(unexpected)}")
        if unexpected:
            print(f"  [SCOPE GUARD] UNEXPECTED (would STOP): {', '.join(sorted(unexpected))}")
        print("=" * 72)
        return {"create": n_create, "update": n_update, "skip": n_skip,
                "direct": n_direct, "dependency_induced": n_dep,
                "unexpected": sorted(unexpected), "write_set": sorted(write_set),
                "affected_brands": sorted(affected_brands),
                "affected_cats": sorted(affected_cats),
                "affected_l3": sorted((c, f, l) for c, f, l in affected_l3),}

    # ---- non-dry write path ----
    if unexpected:
        print("=" * 72)
        print("  [SCOPE GUARD] ABORT: unexpected writes detected -> nothing published.")
        print(f"  requested           = {sorted(requested_slugs)}")
        print(f"  dependency_induced  = {sorted(dep_induced_slugs)}")
        print(f"  UNEXPECTED ({len(unexpected)}) = {', '.join(sorted(unexpected))}")
        print("  Re-run with an explicit --single request covering these SKUs, or run")
        print("  a full sync (no --single) to publish all detected changes.")
        print("=" * 72)
        return {"aborted": True, "unexpected": sorted(unexpected)}

    if total and (n_create + n_update) > SCOPE_CEILING_RATIO * total:
        print(f"  [SCOPE GUARD] INFO: planned writes {n_create + n_update} exceed "
              f"{SCOPE_CEILING_RATIO * 100:.0f}% of {total} SKUs "
              f"(unexpected={len(unexpected)}; full-sync mode).")

    # generated_slugs seed: published (manifest) ∪ CREATE ∪ UPDATE (this run).
    # Guarantees old pages' Alternative links never degrade during a partial publish.
    generated_slugs = set(skus.keys()) | write_set

    written_paths = []
    try:
        for s in sorted(write_set):
            p = plan[s]
            g = p["g"]
            _, cslug, _ = resolve_cat_state((g.get("native_l1") or "").strip())
            mfr_slug = slugify_name(g["manufacturer"].strip())
            path = _write_sku_page_atomic(args, g, cslug, mfr_slug,
                                          related_map, generated_slugs, out_root)
            written_paths.append(path)
    except Exception as e:
        print(f"  [ATOMIC WRITE] FAILED mid-batch: {e}")
        print(f"  {len(written_paths)} page(s) written before the failure; "
              f"build_manifest.json is NOT updated (self-heal on next run).")
        raise

    # ---- manifest transaction: update only AFTER every SKU HTML succeeded ----
    # ---- NODE writes: refresh affected Brand / Category / Fine(L3) pages ----
    # Order: L3 first (so the category page's existing_l3 scan sees them), then
    # Category, then Brand. Each node is regenerated from CURRENT full data, so a
    # newly published SKU appears on its Brand/Category/L3 listings at once. Unaffected
    # nodes are never touched. A node-write failure aborts BEFORE the manifest txn
    # (build_manifest.json stays untouched -> self-heal on the next run).
    node_written = 0
    node_created = set()
    try:
        # Fine(L3) subcategory pages
        for (_cslug, _fine, _l3slug) in sorted(affected_l3):
            _d = os.path.join(out_root, "components", _cslug, _l3slug)
            _existed = os.path.isfile(os.path.join(_d, "index.html"))
            os.makedirs(_d, exist_ok=True)  # ROOT-FIX: L3 HTML via gen_subcategory.py
            node_written += 1
            if not _existed:
                node_created.add(("l3", _cslug, _l3slug))
        # Category (top) pages
        for _cslug in sorted(affected_cats):
            _d = os.path.join(out_root, "components", _cslug)
            _existed = os.path.isfile(os.path.join(_d, "index.html"))
            os.makedirs(_d, exist_ok=True)
            with open(os.path.join(_d, "index.html"), "w", encoding="utf-8") as _f:
                _f.write(gen_component_category_page(
                    _cslug, TOP_CATEGORIES.get(_cslug, _cslug), by_cat.get(_cslug, [])))
            node_written += 1
            if not _existed:
                node_created.add(("cat", _cslug))
        # Brand (manufacturer) pages
        for _mfr in sorted(affected_brands):
            _mslug = slugify_name(_mfr)
            _d = os.path.join(out_root, "manufacturers", _mslug)
            _existed = os.path.isfile(os.path.join(_d, "index.html"))
            os.makedirs(_d, exist_ok=True)
            with open(os.path.join(_d, "index.html"), "w", encoding="utf-8") as _f:
                _f.write(gen_manufacturer_page(_mfr, by_mfr.get(_mfr, []), {}))
            node_written += 1
            if not _existed:
                node_created.add(("brand", _mslug))
    except Exception as e:
        print(f"  [ATOMIC WRITE] NODE write FAILED mid-batch: {e}")
        print(f"  {len(written_paths)} SKU page(s) + {node_written} node page(s) written "
              f"before failure; build_manifest.json is NOT updated (self-heal on next run).")
        raise

    # ---- Hub: rewrite ONLY when a NEW node was created (structural change) ----
    # Spec: "Hub only if static content actually changes; never every run." A new
    # Brand / Category / Fine(L3) page alters the hub catalog, so both hub index files
    # are regenerated. An existing node merely gaining a SKU changes counts only -> the
    # hub shell is left untouched (live data is already refreshed via components-data.js).
    if node_created:
        _hub_dir = os.path.join(out_root, "components")
        os.makedirs(_hub_dir, exist_ok=True)
        _hub_path = os.path.join(_hub_dir, "index.html")
        if os.path.isfile(_hub_path):
            inject_hub_anchors(_hub_path, groups)
        _mhub_dir = os.path.join(out_root, "manufacturers")
        os.makedirs(_mhub_dir, exist_ok=True)
        with open(os.path.join(_mhub_dir, "index.html"), "w", encoding="utf-8") as _f:
            _f.write(gen_manufacturers_hub(by_mfr))

    new_skus = dict(skus)
    for s in write_set:
        g = plan[s]["g"]
        bk = compute_build_key(s, g, by_cat, related_map)
        bk["status"] = "published"
        bk["last_written"] = _now_iso()
        new_skus[s] = bk
    new_manifest = {
        "meta": {
            "template_version": full.get("meta", {}).get("template_version", TEMPLATE_VERSION),
            "bootstrapped_at": full.get("meta", {}).get("bootstrapped_at"),
            "updated_at": _now_iso(),
            "sku_count": len(new_skus),
            "orphan_count": full.get("meta", {}).get("orphan_count", 0),
            "missing_page_count": 0,
        },
        "skus": new_skus,
    }
    save_manifest(path=manifest_path, manifest=new_manifest, dry_run=False)

    # ---- global artifacts (separated from SKU HTML) ----
    regen_global_artifacts(args, groups, out_root, by_cat, related_map, generated_slugs)

    # ---- ROOT-FIX: delegate L3 subcategory rendering to gen_subcategory.py (v2.1) ----
    # parts.json is refreshed by regen_global_artifacts above, so L3 pages reflect the
    # current catalog. Idempotent: re-renders all v2.1 L3 pages from current parts.json.
    _subcat = os.path.join(ROOT, "gen_subcategory.py")
    if os.path.exists(_subcat):
        print("  [ROOT-FIX] Delegating L3 subcategory render to gen_subcategory.py ...")
        _r = subprocess.run([sys.executable, _subcat, "--apply"], cwd=ROOT)
        if _r.returncode != 0:
            print("  [WARN] gen_subcategory.py exited non-zero; L3 pages may be stale.")

    print("=" * 72)
    print(f"  [INCREMENTAL] Published: CREATE={n_create} UPDATE={n_update} SKIP={n_skip}")
    print(f"  [INCREMENTAL] Wrote {len(written_paths)} SKU HTML + {node_written} node HTML file(s) atomically.")
    print(f"  [INCREMENTAL] Affected nodes: brands={len(affected_brands)} "
          f"cats={len(affected_cats)} L3={len(affected_l3)} (created={len(node_created)}).")
    print(f"  [INCREMENTAL] build_manifest.json updated -> {manifest_path}")
    print("=" * 72)
    return {"create": n_create, "update": n_update, "skip": n_skip,
            "written": len(written_paths), "node_written": node_written,
            "affected_brands": sorted(affected_brands),
            "affected_cats": sorted(affected_cats),
            "affected_l3": sorted((c, f, l) for c, f, l in affected_l3),
            "node_created": sorted(node_created),
            "unexpected": []}


def main():
    ap = argparse.ArgumentParser()
    default_csv = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")  # P0-2: only a v2+ production Master may feed the build; v1.x test masters are forbidden
    ap.add_argument("--csv", default=default_csv)
    ap.add_argument("--out", default=ROOT)
    ap.add_argument("--mfr-map", default=os.path.join(ROOT, "data", "production", "mfr_canonical.csv"))  # PHASE E.3.2: production self-contained
    ap.add_argument("--attr-dict", default=os.path.join(ROOT, "data", "production", "attributes_dictionary.md"))  # PHASE E.3.2: production self-contained
    ap.add_argument("--attr-json", default=os.path.join(ROOT, "tools", "attribute_dictionary.json"),
                    help="Curated raw-attr-key -> English map (CJK gate fix).")
    ap.add_argument("--val-json", default=os.path.join(ROOT, "tools", "value_translation.json"),
                    help="Curated raw-attr-value -> English map (CJK gate fix).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Process + validate + report only. Writes test_p0_processed.csv and "
                         "review_queue.csv under --out, but does NOT generate HTML/sitemap/search.")
    ap.add_argument("--strict", action="store_true",
                    help="Hard gate: abort if any unknown manufacturer or unknown attribute key "
                         "is found (200k data-hygiene gate).")
    ap.add_argument("--single", default=None,
                    help="Incremental publish scope: comma-separated MPN(s) to publish "
                         "(e.g. 'STM32F103C8T6,ESP32-WROOM-32E'). Implies a SCOPE GUARD — "
                         "only these SKUs plus legitimate dependency-induced co-members are "
                         "written; any other detected change aborts. Empty (no --single) = "
                         "full incremental sync of all detected changes. Skips manufacturer/"
                         "hub/category/sitemap generation. For targeted single/batch publishing.")
    ap.add_argument("--incremental", action="store_true",
                    help="PHASE 1: run change-detection and print the Publishing Plan. "
                         "Does NOT write products/*.html and does NOT modify build_manifest.json.")
    ap.add_argument("--bootstrap-manifest", action="store_true",
                    help="PHASE 1: scan current products/ and record published state into "
                         "build_manifest.json (skipped under --dry-run). No HTML/MASTER changes.")
    args = ap.parse_args()

    csv_path = os.path.abspath(args.csv)
    out_root = os.path.abspath(args.out)
    os.makedirs(out_root, exist_ok=True)
    if not os.path.exists(csv_path):
        print("CSV not found:", csv_path); sys.exit(1)

    # ---- P0-2: reject synthetic / non-production sources BEFORE any work ----
    validate_production_source(csv_path)

    # ---- load reference dictionaries (Phase 2.1) ----
    mfr_map = load_mfr_canonical(args.mfr_map)
    attr_allow = load_attr_allowlist(args.attr_dict)
    print(f"Loaded mfr_canonical ({len(mfr_map)} aliases) + attributes allowlist "
          f"({len(attr_allow)} keys)")
    # ---- English visible-layer translation (CJK gate fix): load curated lexicons ----
    global _ATTR_KEY_TRANS, _VAL_TRANS
    _ATTR_KEY_TRANS = load_attr_key_translation(args.attr_json)
    _VAL_TRANS = load_value_translation(args.val_json)
    print(f"Loaded attr key-translation ({len(_ATTR_KEY_TRANS)} keys) + "
          f"value-translation ({len(_VAL_TRANS)} values) for EN storefront")

    with open(csv_path, encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("mpn", "").strip()]

    print(f"Loaded {len(rows)} parts from {csv_path}")

    # ---- P0-2: hard-stop on any synthetic/test MPN or fake brand ----
    detect_synthetic_mpn(rows)

    # ---- P0-4 + P0-2 + P0-3 : merge / canonicalize / validate ----
    review = []   # (mpn, canonical_brand, reason, detail)
    groups, stats = build_merged_groups(rows, mfr_map, attr_allow, review)

    # ---- P0-1 : deterministic, collision-free slug assignment ----
    registry = SlugRegistry()
    master_slugs = {}  # #1 regression: remember MASTER-seeded slug per MPN
    for g in groups:
        base = (g.get("url_slug") or "").strip() or slugify(g["mpn"].strip())
        master_slugs[g["mpn"].strip()] = base
        g["url_slug"] = registry.assign(base, g["manufacturer"].strip(), g["mpn"].strip())

    # ---- #1 regression guard: built slug MUST equal MASTER-seeded slug ----
    # SlugRegistry.assign is documented "stable for identical input order", so the
    # 550 live URLs must never drift. A collision-suffixed slug (registry.renamed)
    # is the only tolerated deviation (an intentional, logged resolution).
    _renamed = set(registry.renamed.values())
    for g in groups:
        seed = master_slugs.get(g["mpn"].strip(), "")
        if seed and g["url_slug"] != seed and g["url_slug"] not in _renamed:
            raise AssertionError(
                f"[#1] slug drift for {g.get('mpn')!r}: MASTER={seed!r} -> built={g['url_slug']!r}")

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

    # ---- PHASE 1 (authorized 2026-09-08): incremental build state only ----
    # Short-circuit BEFORE the existing --dry-run block and the full-rebuild loop.
    # Neither branch writes products/*.html or edits MASTER. bootstrap writes the
    # (gitignored) build_manifest.json only when NOT --dry-run.
    if args.incremental or args.bootstrap_manifest:
        if args.bootstrap_manifest:
            bootstrap_manifest(args, groups, out_root)
        if args.incremental:
            incremental_pipeline(args, groups, out_root)
        return

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
        cslug, _ = resolve_cat((g.get("native_l1") or "").strip())
        by_cat[cslug].append(g)

    # ---- P0-1 related-products pre-index (final slugs) ----
    related_map = build_related_map(by_cat, k=6)

    # ---- generate part pages ----
    # Risk #1 (2026-09-08): V3 is the DEFAULT renderer. SKUs listed in
    # V2_LEGACY_EXCEPTIONS (empty by default) render via the legacy gen_part_page();
    # every other SKU renders via the additive gen_part_page_v3(). V2 is preserved,
    # unchanged, and still used for any explicit legacy exception.
    written = 0
    urls = []
    generated_slugs = {g["url_slug"] for g in groups if g.get("url_slug")}
    for g in groups:
        pn = g["mpn"].strip()
        slug = g["url_slug"]
        if not slug:
            continue
        cslug, _ = resolve_cat((g.get("native_l1") or "").strip())
        mfr_slug = slugify_name(g["manufacturer"].strip())
        if pn.upper() in V2_LEGACY_EXCEPTIONS:
            page = gen_part_page(g, cslug, mfr_slug, related=related_map.get(slug, []), generated_slugs=generated_slugs)
        else:
            page = gen_part_page_v3(g, cslug, mfr_slug, related=related_map.get(slug, []), generated_slugs=generated_slugs, verbose=bool(args.single and args.single.strip().upper() == pn.upper()))
        # --single: skip every SKU except the target (do NOT write other pages)
        if args.single and args.single.strip().upper() != pn.upper():
            continue
        d = os.path.join(out_root, "products", slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as f:
            f.write(page)
        # I3: exclude hidden/review SKUs from the sitemap (page already noindex)
        if effective_publish_status(g) not in ("hidden", "review"):
            urls.append(f"{DOMAIN}/products/{slug}/")
        written += 1
        if args.single and args.single.strip().upper() == pn.upper():
            print(f"  [--single] Generated only {pn} -> products/{slug}/index.html")
            return  # skip manufacturer/hub/category/sitemap entirely

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
        if load_taxonomy()["tops"].get(cslug, {}).get("publish_status", "active") == "active":
            urls.append(f"{DOMAIN}/components/{cslug}/")

    # ---- component L3 subcategory pages (/components/<l2>/<l3>/) ----
    # ROOT-FIX: L3 pages are rendered EXCLUSIVELY by gen_subcategory.py (the v2.1
    # design: ItemList schema + /page/2/ pagination + Manufacturers section). gen_parts.py
    # must NEVER render L3 via the legacy gen_component_subcategory_page template -- that
    # clobbers v2.1 on every rebuild (regression on 2026-09-10). Here we only ensure the
    # directory exists so a new fine category is discoverable; actual HTML is produced in
    # the delegated pass AFTER parts.json is regenerated (see below).
    for cslug, cname in TOP_CATEGORIES.items():
        _top_ps = load_taxonomy()["tops"].get(cslug, {}).get("publish_status", "active")
        l3_groups = defaultdict(list)
        for p in by_cat.get(cslug, []):
            fine = (p.get("native_l1") or "").strip()
            if fine:
                l3_groups[fine].append(p)
        for fine, l3_parts in sorted(l3_groups.items()):
            # P1-B1: SELF_REFERENCE -> the L3 page would collapse onto the top page,
            # so we must NOT generate a same-named L3 page. COLLISION -> taxonomy
            # config error, never auto-number to foo-2. UNMAPPED -> no valid L3,
            # never emit a broken page. All three are recorded (counters +
            # quarantine) and the batch continues; nothing is silently numbered.
            if l3_page_should_skip(fine):
                continue
            if _top_ps != "active":
                continue
            if resolve_native(fine)["publish_status"] in ("hidden", "review"):
                continue
            l3_slug = slugify_name(fine)
            d = os.path.join(out_root, "components", cslug, l3_slug)
            os.makedirs(d, exist_ok=True)  # ROOT-FIX: directory only; HTML via gen_subcategory.py
            urls.append(f"{DOMAIN}/components/{cslug}/{l3_slug}/")

    # ---- component hub (P1-B2 / M2 — anchor-only injection, V2.4 shell preserved) ----
    # DEPRECATED generate_components_hub() is no longer called here; the hub is injected
    # between the explicit HUB-INJECT anchors so the hand-authored V2.4 shell / SEO /
    # visuals are never overwritten. Self-reference subcategories render as non-link spans.
    hub_dir = os.path.join(out_root, "components")
    os.makedirs(hub_dir, exist_ok=True)
    inject_hub_anchors(os.path.join(hub_dir, "index.html"), groups)
    urls.append(f"{DOMAIN}/components/")

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
        _native = resolve_native(g.get("native_l1"))
        cat = _native.get("l1_name") or (g.get("category") or "").strip()
        c_top = _native.get("top_slug") or ""
        p_slug = g["url_slug"]
        m_slug = slugify_name(mfr)
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
        if key_c not in seen and c_top:
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
        raw = (g.get("attributes_json") or "").strip()
        attrs = build_en_attrs(raw)  # English visible-layer (CJK gate fix)
        parts_json.append({
            "mpn": mpn,
            "clean_mpn": clean,
            "manufacturer": g["manufacturer"].strip(),
            "brand": g.get("brand", g["manufacturer"]).strip(),
            "url_slug": uslug,
            # I3/I4: parts.json category/subcategory use the native_l1 taxonomy (single
            # source of truth), not the legacy `category` string. Defensive fallback to the
            # legacy value only if native_l1 cannot be resolved (never expected).
            "category": top_scope_name(resolve_native(g.get("native_l1")).get("top_slug") or "") or g.get("category", "").strip(),
            "subcategory": resolve_native(g.get("native_l1")).get("l1_name") or g.get("subcategory", "").strip(),
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

    # ---- ROOT-FIX: delegate L3 subcategory rendering to gen_subcategory.py (v2.1) ----
    # Runs AFTER parts.json above is regenerated so L3 pages reflect current SKU data.
    _subcat = os.path.join(ROOT, "gen_subcategory.py")
    if os.path.exists(_subcat):
        print("  [ROOT-FIX] Delegating L3 subcategory render to gen_subcategory.py ...")
        _r = subprocess.run([sys.executable, _subcat, "--apply"], cwd=ROOT)
        if _r.returncode != 0:
            print("  [WARN] gen_subcategory.py exited non-zero; L3 pages may be stale.")
    else:
        print("  [WARN] gen_subcategory.py not found; L3 pages not re-rendered.")

    # ---- components-data.js (LIVE Components Hub search source, see _regen_components_data) ----
    _regen_components_data(args, groups, out_root, by_cat, related_map, generated_slugs)

    print(f"Generated {written} product pages under /products/")
    print(f"Manufacturer pages: {len(by_mfr)} under /manufacturers/")
    print(f"Component category pages: {len(by_cat)} under /components/<top-slug>/")
    print(f"Slug collisions resolved (no overwrite): {len(registry.renamed)}")
    print(f"Duplicate MPN rows merged: {stats['merged_dups']}")
    print(f"Sitemaps: {sm_paths} (+ sitemap_parts_index.xml)")
    print(f"Total indexed URLs this run: {len(urls)}")

if __name__ == "__main__":
    main()
