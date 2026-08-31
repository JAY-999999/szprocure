"""
#14 Robots index policy (pre-scale hardening, Phase 2) — sandbox-only, no gen_parts edit.

Decides the ``<meta name="robots">`` directive for a generated part page.

Design intent (validated in tests/test_index_policy.py, NOT wired into the frozen
gen_parts.py yet):

  * A fully-qualified page              -> ``index, follow``
  * SPEC_THIN  (too few structured spec keys, which INCLUDES an empty
                ``attributes_json``) -> ``noindex, follow``
  * UNKNOWN_CATEGORY (category detection failed) -> ``noindex, follow``

Crucially the directive is ``follow`` in EVERY case — we never ``nofollow``, so
internal-link equity is preserved and thin pages stay crawlable. The policy is
PURE and side-effect free: it only reads ``category`` + ``attributes_json`` and
returns a string. It deliberately does NOT touch the sitemap or the canonical
link, so wiring it into ``gen_part_page`` later cannot regress either.

Planned integration (requires user authorization to edit the frozen layer):
    meta = decide_robots_meta(category=g["category"], attributes_json=g["attributes_json"])
    # inject into <head>:  <meta name="robots" content="{meta}">
    # sitemap_parts / canonical generation: UNCHANGED (all pages still listed,
    # canonical stays self-referential).
"""
from __future__ import annotations

import json
from typing import Any

# --- thresholds ---------------------------------------------------------------
# A page with fewer than this many non-empty structured spec keys is "spec thin".
# (The production min-specs gate lives in category.py; this module only needs a
#  count, defaulting to "at least 1 key".)
DEFAULT_MIN_SPEC_KEYS = 1

# --- directives ---------------------------------------------------------------
ROBOTS_INDEX_FOLLOW = "index, follow"
ROBOTS_NOINDEX_FOLLOW = "noindex, follow"

# Every decision preserves ``follow`` — this set is the contract.
_ALLOWED_DIRECTIVES = (ROBOTS_INDEX_FOLLOW, ROBOTS_NOINDEX_FOLLOW)


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
            # Invalid JSON is treated as "no usable specs" (safe default).
            return 0
    elif isinstance(attributes_json, dict):
        data = attributes_json
    else:
        return 0
    if not isinstance(data, dict):
        return 0
    return sum(1 for v in data.values() if v not in (None, "", []))


def is_spec_thin(attributes_json: Any, min_keys: int = DEFAULT_MIN_SPEC_KEYS) -> bool:
    """True when the page carries too few structured spec keys to index well."""
    return _attr_count(attributes_json) < min_keys


# Tokens that mean "category detection failed / unresolved". Mirrors the
# UNKNOWN_CATEGORY sentinel in category.py ("Uncategorized") plus obvious variants.
_UNKNOWN_TOKENS = {"", "uncategorized", "unknown", "unmapped", "none"}


def is_unknown_category(category: Any) -> bool:
    """True when category detection failed (empty / None / UNKNOWN sentinel)."""
    if not isinstance(category, str):
        return True
    return category.strip().lower() in _UNKNOWN_TOKENS


def decide_robots_meta(*, category: Any, attributes_json: Any,
                        min_spec_keys: int = DEFAULT_MIN_SPEC_KEYS) -> str:
    """Return the robots directive for a part page.

    Rules (``follow`` is ALWAYS preserved; we never ``nofollow``):
      - unknown category  -> noindex, follow
      - spec-thin         -> noindex, follow   (covers empty attributes_json)
      - otherwise         -> index, follow
    """
    if is_unknown_category(category):
        directive = ROBOTS_NOINDEX_FOLLOW
    elif is_spec_thin(attributes_json, min_keys=min_spec_keys):
        directive = ROBOTS_NOINDEX_FOLLOW
    else:
        directive = ROBOTS_INDEX_FOLLOW

    assert directive in _ALLOWED_DIRECTIVES, f"unexpected directive {directive!r}"
    assert "follow" in directive, "policy must always preserve follow"
    return directive
