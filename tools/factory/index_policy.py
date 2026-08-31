"""
SZ Procure — single-source Noindex / robots policy (Phase 3, YELLOW-1/2/3).

Pure, side-effect free. Consumed by the NON-FROZEN post-processors
(apply_index_policy.py / sitemap_prune.py). gen_parts.py is deliberately NOT
modified — this module is the one source of truth for "should this URL be
indexed", and both the robots <meta> and the sitemap are derived from it, so
they can never disagree (gate: NOINDEX ∩ SITEMAP = ∅).

Final policy (2026-08-31 architectural decision):
  index, follow    for: normal MPN pages, SPEC_THIN pages,
                       empty-attr-with-real-MPN pages
  noindex, follow  for: UNKNOWN_CATEGORY, NO_MPN, SYNTHETIC_MPN,
                       true-DUPLICATE (non-primary only)
  NEVER nofollow.  Fail-open: any unexpected input -> index, follow.

Duplicate primary selection (§四): brand-native match first
(brand == manufacturer), else deterministic (category, manufacturer, url_slug).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# --- thresholds ---------------------------------------------------------------
# A page with fewer than this many non-empty structured spec keys is "spec thin".
# Per final policy SPEC_THIN is STILL indexable, so this is informational only.
DEFAULT_MIN_SPEC_KEYS = 1

# --- directives ---------------------------------------------------------------
ROBOTS_INDEX_FOLLOW = "index, follow"
ROBOTS_NOINDEX_FOLLOW = "noindex, follow"

# Every decision preserves ``follow`` — this set is the contract.
_ALLOWED_DIRECTIVES = (ROBOTS_INDEX_FOLLOW, ROBOTS_NOINDEX_FOLLOW)

# --- classification reason codes ----------------------------------------------
REASON_INDEX = "INDEX"                       # normal / SPEC_THIN / empty-attr-with-mpn
REASON_UNKNOWN_CATEGORY = "UNKNOWN_CATEGORY"  # category detection failed
REASON_NO_MPN = "NO_MPN"                      # no real manufacturer part number
REASON_SYNTHETIC_MPN = "SYNTHETIC_MPN"        # pattern-matched test/placeholder MPN
REASON_DUPLICATE = "DUPLICATE"                # non-primary of a normalized-MPN group

# --- synthetic MPN patterns (mirror gen_parts.SYNTHETIC_MPN_PATTERNS) ----------
_SYNTHETIC_MPN_PATTERNS = [
    re.compile(r'^(MCU|MOS|RES|CAP|IND|DIO|CON|XTAL|MEM|WIFI|MOD|REG|AMP|OP|LED|PWR|IC)\d{6}', re.I),
    re.compile(r'100000\d{3}'),                 # the MCU100000xxx / MOS100000xxx family
    re.compile(r'^\d{6,}$'),                    # pure long numeric placeholder
    re.compile(r'PLACEHOLDER', re.I),
    re.compile(r'XXX$', re.I),
    re.compile(r'_(TEST|SAMPLE|MOCK)$', re.I),
]

# Tokens that mean "category detection failed / unresolved".
_UNKNOWN_TOKENS = {"", "uncategorized", "unknown", "unmapped", "none"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def normalize_mpn(mpn: Any) -> str:
    """Normalize an MPN for duplicate comparison: ignore case / space / hyphen."""
    if not isinstance(mpn, str):
        return ""
    return re.sub(r'[\s-]', '', mpn).lower()


def is_synthetic_mpn(mpn: Any) -> bool:
    """True when the MPN matches a known synthetic / test / placeholder pattern."""
    if not isinstance(mpn, str):
        return False
    s = mpn.strip()
    if not s:
        return False
    return any(p.search(s) for p in _SYNTHETIC_MPN_PATTERNS)


def is_unknown_category(category: Any) -> bool:
    """True when category detection failed (empty / None / UNKNOWN sentinel)."""
    if not isinstance(category, str):
        return True
    return category.strip().lower() in _UNKNOWN_TOKENS


def _attr_count(attributes_json: Any) -> int:
    """Count non-empty structured spec keys in attributes_json (str|dict|None)."""
    if attributes_json is None:
        return 0
    if isinstance(attributes_json, str):
        s = attributes_json.strip()
        if not s:
            return 0
        try:
            data = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return 0  # invalid JSON -> safe default (no usable specs)
    elif isinstance(attributes_json, dict):
        data = attributes_json
    else:
        return 0
    if not isinstance(data, dict):
        return 0
    return sum(1 for v in data.values() if v not in (None, "", []))


def is_spec_thin(attributes_json: Any, min_keys: int = DEFAULT_MIN_SPEC_KEYS) -> bool:
    """True when the page carries too few structured spec keys.

    NOTE: per final policy a spec-thin page is STILL indexable — this helper is
    kept for reporting / diagnostics only and must NOT drive noindex.
    """
    return _attr_count(attributes_json) < min_keys


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
@dataclass
class PartClassification:
    """Result of classifying one part page against the Noindex policy."""
    url_slug: str
    indexable: bool
    reason: str
    canonical_to: Optional[str] = None  # primary url_slug for DUPLICATE (non-primary)

    @property
    def robots_meta(self) -> str:
        return ROBOTS_INDEX_FOLLOW if self.indexable else ROBOTS_NOINDEX_FOLLOW


def classify_part_record(rec: dict) -> PartClassification:
    """Classify a single part record (no duplicate-group awareness).

    rec fields used: url_slug, category, mpn, clean_mpn (brand/manufacturer are
    only consulted during duplicate primary selection in classify_parts()).
    """
    slug = (rec.get("url_slug") or "").strip()
    category = rec.get("category")
    mpn = (rec.get("mpn") or "").strip()

    # 1) unknown category -> noindex, follow (crawler should not index an
    #    unresolved taxonomy page; internal links remain follow).
    if is_unknown_category(category):
        return PartClassification(slug, False, REASON_UNKNOWN_CATEGORY)
    # 2) no real MPN -> noindex, follow (cannot be a canonical part page).
    if not mpn:
        return PartClassification(slug, False, REASON_NO_MPN)
    # 3) synthetic / test MPN -> noindex, follow (we refuse to index fake data).
    #    Mirrors gen_parts.detect_synthetic_mpn: only the RAW mpn is tested,
    #    NEVER the normalized clean_mpn. A real hyphenated MPN like "1909763-1"
    #    would otherwise be falsely flagged by its pure-numeric clean form
    #    ("19097631"), wrongly noindexing a genuine TE Connectivity part.
    if is_synthetic_mpn(mpn):
        return PartClassification(slug, False, REASON_SYNTHETIC_MPN)
    # 4) everything else is indexable. SPEC_THIN and empty-attr-with-real-MPN
    #    pages stay index, follow (final policy §三).
    return PartClassification(slug, True, REASON_INDEX)


def _pick_primary(group: list[dict]) -> dict:
    """Duplicate primary selection (§四): brand-native match first, else
    deterministic (category, manufacturer, url_slug)."""
    # Brand-native: first-party listing where brand == manufacturer.
    for r in group:
        b = (r.get("brand") or "").strip().lower()
        m = (r.get("manufacturer") or "").strip().lower()
        if b and b == m:
            return r
    # Deterministic fallback (stable regardless of input order).
    return sorted(
        group,
        key=lambda r: (
            (r.get("category") or "").strip().lower(),
            (r.get("manufacturer") or "").strip().lower(),
            (r.get("url_slug") or "").strip().lower(),
        ),
    )[0]


def classify_parts(records: Iterable[dict]) -> dict[str, PartClassification]:
    """Classify every record, applying duplicate grouping across the catalog.

    Returns {url_slug: PartClassification}. A duplicate group (normalized-MPN
    match) keeps exactly one primary (indexable); all other members become
    DUPLICATE (noindex, follow) with canonical_to = primary url_slug.
    """
    recs = list(records)
    by_norm: dict[str, list[dict]] = {}
    for r in recs:
        mpn = (r.get("mpn") or "").strip() or (r.get("clean_mpn") or "").strip()
        key = normalize_mpn(mpn)
        if key:
            by_norm.setdefault(key, []).append(r)

    result: dict[str, PartClassification] = {}
    for r in recs:
        slug = (r.get("url_slug") or "").strip()
        result[slug] = classify_part_record(r)

    for group in by_norm.values():
        if len(group) <= 1:
            continue
        primary = _pick_primary(group)
        primary_slug = (primary.get("url_slug") or "").strip()
        for r in group:
            slug = (r.get("url_slug") or "").strip()
            if slug == primary_slug:
                continue
            result[slug] = PartClassification(
                slug, False, REASON_DUPLICATE, canonical_to=primary_slug)

    return result


# ---------------------------------------------------------------------------
# legacy-compatible decision helper
# ---------------------------------------------------------------------------
def decide_robots_meta(*, classification: Optional[PartClassification] = None,
                        category: Any = None, mpn: Any = None,
                        clean_mpn: Any = None, brand: Any = None,
                        manufacturer: Any = None, url_slug: Any = None,
                        attributes_json: Any = None,
                        min_spec_keys: int = DEFAULT_MIN_SPEC_KEYS) -> str:
    """Return the robots directive for a part page.

    Accepts EITHER a precomputed ``PartClassification`` OR the raw record fields
    (kept for backward compatibility / direct unit testing). ``follow`` is
    ALWAYS preserved; the function is fail-open (anything unexpected ->
    index, follow).

    NOTE: ``attributes_json`` / ``min_spec_keys`` no longer affect the outcome —
    SPEC_THIN is indexable by final policy. They are retained only for a
    compatible call surface.
    """
    if classification is not None:
        cls = classification
    else:
        cls = classify_part_record({
            "url_slug": url_slug,
            "category": category,
            "mpn": mpn,
            "clean_mpn": clean_mpn,
            "brand": brand,
            "manufacturer": manufacturer,
            "attributes_json": attributes_json,
        })
    assert cls.robots_meta in _ALLOWED_DIRECTIVES, f"unexpected {cls.robots_meta!r}"
    assert "follow" in cls.robots_meta, "policy must always preserve follow"
    return cls.robots_meta
