"""Frozen native_l1 (56-L1 taxonomy slug) mapping — single source of truth.

This module is the CANONICAL home of the native_l1 deterministic remap. It was
extracted verbatim from ``tools/clean_factory.py`` (validated 2026-09) so that
BOTH the legacy 02 pipeline (``clean_factory.main``) AND the SKU Factory
(``tools/factory/product_data.build_row``) reuse the EXACT SAME rules. No
second classification logic is introduced.

The maps below are frozen production constants. They MUST NOT be edited except
via an explicit, separately-authorised change. Do not "improve" or re-derive
them locally — that would break the single-source-of-truth guarantee.

Rule (deterministic, repeatable, no guessing):
  TIER 1: exact normalised catalogName (leaf) override -> specific L1 slug
          (Circuit-Protection leaf items whose parentCatalogName misleads)
  TIER 2: exact normalised parentCatalogName -> L1 slug (35-entry 1:1 map)
  TIER 3: if neither matches -> '' (unmapped, never guessed)
"""

import json
import os

# Project root = the directory that contains this file (szprocure-site/).
ROOT = os.path.dirname(os.path.abspath(__file__))
L1_RAWDIR = os.path.join(ROOT, "data", "raw", "lcsc_http_scale500")
L1_TAX = os.path.join(ROOT, "data", "category_taxonomy.json")

# TIER 2: parentCatalogName (LCSC immediate parent) -> 56-L1 slug
NATIVE_L1_PARENT_MAP = {
    "power management (pmic)": "power-management",
    "transistors/thyristors": "transistors",
    "capacitors": "capacitors",
    "connectors": "connectors",
    "amplifiers/comparators": "amplifiers-comparators",
    "interface": "interface-ics",
    "diodes": "diodes",
    "embedded processors & controllers": "microcontrollers",
    "logic": "logic-ics",
    "data acquisition": "data-converters",
    "inductors, coils, chokes": "inductors-coils-transformers",
    "motor driver ics": "motor-driver-ics",
    "memory": "memory",
    "sensors": "sensors",
    "resistors": "resistors",
    "rf and wireless": "rf-wireless",
    "power modules": "power-modules",
    "filters": "filters-emi-suppression",
    "optoelectronics": "optoelectronics",
    "signal isolation devices": "signal-isolators",
    "switches": "switches",
    "optoisolators": "optocouplers",
    "clock/timing": "clock-timing",
    "iot/communication modules": "iot-communication-modules",
    "led drivers": "led-display-drivers",
    "crystals, oscillators, resonators": "oscillators-resonators",
    "magnetic sensors": "magnetic-sensors",
    "circuit protection": "circuit-protection",
    "audio products / vibration motors": "audio-signal-devices",
    "terminal": "terminals",
    "relays": "relays",
    "displays": "displays",
}

# TIER 1: leaf catalogName overrides -> L1 slug (Circuit-Protection items)
NATIVE_L1_LEAF_OVERRIDE = {
    "tvs diodes": "circuit-protection",
    "varistors, movs": "circuit-protection",
    "ptc resettable fuses": "circuit-protection",
    "fuseholders": "circuit-protection",
    "fuses": "circuit-protection",
    "gas discharge tube arresters (gdt)": "circuit-protection",
    "surge suppression ics": "circuit-protection",
    "mixed technology": "circuit-protection",
    "thyristors": "circuit-protection",
    # --- 2026-09-19 corrective overrides ---
    # LCSC mis-files these under industrial/hardware/office legacy buckets via
    # parentCatalogName; the leaf catalogName below is authoritative, so we pin
    # the correct L1 here (evaluated before TIER-2 parentCatalogName).
    "quick connects, quick disconnect connectors": "terminals",
    "rfi and emi - contacts, fingerstock and gaskets": "filters-emi-suppression",
    "memory cards": "memory",
}


def _norm_l1(s):
    if not s:
        return ""
    return " ".join(str(s).strip().lower().split())


def _load_native_l1_taxonomy_slugs():
    with open(L1_TAX, encoding="utf-8") as f:
        d = json.load(f)
    return set(x["slug"] for x in d["l1_categories"])


def compute_native_l1(parent_catalog_name, catalog_name):
    """Return the 56-L1 slug for a SKU, or '' when it cannot be determined.

    Never guesses: only TIER-1 leaf overrides and TIER-2 parentCatalogName
    matches produce a value; everything else stays empty.
    """
    cn = _norm_l1(catalog_name)
    if cn in NATIVE_L1_LEAF_OVERRIDE:
        return NATIVE_L1_LEAF_OVERRIDE[cn]
    pc = _norm_l1(parent_catalog_name)
    if pc in NATIVE_L1_PARENT_MAP:
        return NATIVE_L1_PARENT_MAP[pc]
    return ""


def _raw_l1_categories(supplier_reference):
    """Return (parentCatalogName, catalogName) from the JSON RAW file, or None
    when the RAW file is missing/unreadable (caller keeps the current value)."""
    if not supplier_reference:
        return None
    f = os.path.join(L1_RAWDIR, "%s.json" % supplier_reference)
    if not os.path.exists(f):
        return None
    try:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        mp = d["source_raw"]["main_product"]
        return (mp.get("parentCatalogName") or "", mp.get("catalogName") or "")
    except Exception:
        return None


def compute_native_l1_for_row(supplier_reference):
    """Convenience wrapper used by BOTH production chains.

    Given a SKU's supplier_reference (C-number), return the 56-L1 slug derived
    from its JSON RAW classification, or '' when the RAW is missing/unmapped
    (never guessed). This is the exact per-row core of
    ``recompute_native_l1`` so the live 02 pipeline and the SKU Factory produce
    identical results.
    """
    cats = _raw_l1_categories(supplier_reference)
    if cats is None:
        return ""
    return compute_native_l1(cats[0], cats[1])


def _self_check():
    """Assert every map target is a real taxonomy-v2 L1 slug. Import-time safe."""
    slugs = _load_native_l1_taxonomy_slugs()
    for s in (set(NATIVE_L1_PARENT_MAP.values())
              | set(NATIVE_L1_LEAF_OVERRIDE.values())):
        assert s in slugs, ("native_l1 map targets a slug absent from "
                            "taxonomy v2: %s" % s)


if __name__ == "__main__":
    _self_check()
    print("[native_l1_mapper] OK: maps valid, "
          "%d parent + %d leaf rules"
          % (len(NATIVE_L1_PARENT_MAP), len(NATIVE_L1_LEAF_OVERRIDE)))
