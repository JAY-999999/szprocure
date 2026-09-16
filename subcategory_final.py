#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SZProcure - production classification of fine-grained Subcategory fields.

This module is the OFFICIAL, self-contained source of the three fine-grained
fields attached to every parts.json row at build time:

    final_subcategory : fine subcategory NAME (e.g. "Microcontrollers")
    final_native_l1   : effective native_l1 slug (e.g. "microcontrollers")
    final_slug        : L3 slug for the fine page (e.g. "microcontrollers")

Deterministic rule (no AI / no random / no inference):
    MASTER.supplier_reference (+ native_l1)
        -> RAW(C*.json).catalogName / parentCatalogName
        -> frozen Phase-5 V1 classification (DIRECT / MERGE / REVIEW / EXCLUDE)
        -> action BUILD / MERGE / UNKNOWN
        -> final_subcategory / final_slug / final_native_l1

The frozen reference tables (MERGE_TARGET / CAND_MERGE_TARGET) and the REVIEW
split map (parentCatalogName -> native_l1) are inlined below or read from the
production data file `subcategory_review_aggregates.json` at repo root. There
is NO dependency on szprocure_audit_tmp or any temp script.

Runtime contract (used by gen_parts.py parts.json build):
    import subcategory_final
    FINAL_MAP = subcategory_final.build_sr_final_map()   # recompute from MASTER+RAW
    for p in parts:
        subcategory_final.attach_final_fields(p, FINAL_MAP)

The normal build recomputes from MASTER + RAW every run (deterministic, frozen
Phase-5 rules), so newly onboarded SKUs are auto-classified with NO manual step.
`python subcategory_final.py --build` only refreshes this standalone cache
artifact; it is NOT required for the normal 03 flow to classify new SKUs.

The classification is a pure function of (supplier_reference, native_l1,
catalogName, parentCatalogName) plus the LOCKED Phase-5 constants, so newly
onboarded SKUs whose catalogName already maps to a BUILD/MERGE fine subcategory
are classified automatically by the same rule. SKUs whose catalogName is new are
resolved by the same count-based rule (typically EXCLUDE/UNKNOWN -> no fine
page, consistent with the freeze).

Does NOT modify MASTER / RAW / 02 / gen_parts.py / gen_part_page_v3 / sitemap /
SKU HTML. Writing is limited to final_subcategory_map.json (deployable cache)
and reports.
"""
import os
import re
import sys
import csv
import json
import glob
import argparse
from collections import defaultdict, Counter

HERE = os.path.dirname(os.path.abspath(__file__))

# Deployed cache: mpn -> {final_subcategory, final_native_l1, final_slug}.
# Regenerable from MASTER + RAW via --build / build_sr_final_map(); kept as a
# standalone artifact / deployable checkpoint. The normal gen_parts.py build
# recomputes from MASTER + RAW directly, so this file is NOT a build-time input.
MAP_PATH = os.path.join(HERE, "final_subcategory_map.json")

# Production data: REVIEW split map (parentCatalogName -> native_l1) for
# catalogNames that span multiple native_l1. Promoted from szprocure_audit_tmp.
REVIEW_AGG_PATH = os.path.join(HERE, "subcategory_review_aggregates.json")

MASTER = os.path.join(HERE, "data", "production", "master_parts_v2.1.csv")
RAWGLOB = os.path.join(HERE, "data", "raw", "lcsc_http_scale500", "C*.json")

# ---------------------------------------------------------------------------
# Frozen Phase-5 V1 constants (verbatim from subcategory_mapping_freeze_v1).
# These are the LOCKED production decisions: 189 BUILD / 53 MERGE / 8 UNKNOWN.
# Do not edit casually - any change alters the classified page set.
# ---------------------------------------------------------------------------
MERGE_TARGET = {
    ("amplifiers-comparators", "Analog Multipliers, Dividers"): "Special Purpose Amplifiers",
    ("circuit-protection", "Fuseholders"): "PTC Resettable Fuses",
    ("circuit-protection", "Mixed Technology"): "TVS Diodes",
    ("circuit-protection", "Surge Suppression ICs"): "TVS Diodes",
    ("circuit-protection", "Thyristors"): "TVS Diodes",
    ("circuit-protection", "Varistors, MOVs"): "TVS Diodes",
    ("connectors", "Circular Connector Accessories"): "Multi Purpose",
    ("connectors", "Circular Connector Housings"): "Multi Purpose",
    ("connectors", "Coaxial Connector (RF) Contacts"): "Coaxial Connector (RF) Assemblies",
    ("connectors", "Free Hanging, Panel Mount"): "Multi Purpose",
    ("connectors", "Housings, Boots"): "Rectangular Connector Housings",
    ("connectors", "Modular/Ethernet Connector (RJ45) Jacks With Magnetics"): "Multi Purpose",
    ("connectors", "Modular/Ethernet Connector (RJ45, RJ11) Jacks"): "Multi Purpose",
    ("connectors", "Pluggable Connector Assemblies"): "Multi Purpose",
    ("data-converters", "Analog Front End (AFE)"): "ADCs/DACs - Special Purpose",
    ("data-converters", "Direct Digital Synthesis (DDS)"): "ADCs/DACs - Special Purpose",
    ("data-converters", "RMS to DC Converters"): "ADCs/DACs - Special Purpose",
    ("data-converters", "V/F and F/V Converters"): "ADCs/DACs - Special Purpose",
    ("diodes", "Zener Diode Arrays"): "Single Zener Diodes",
    ("displays", "LCD, OLED, Graphic"): "NO_SAFE_TARGET",
    ("fasteners-hardware", "RFI and EMI - Contacts, Fingerstock and Gaskets"): "NO_SAFE_TARGET",
    ("filters-emi-suppression", "Ceramic Filters"): "NO_SAFE_TARGET",
    ("filters-emi-suppression", "EMI/RFI Filters (LC, RC Networks)"): "Common Mode Chokes",
    ("filters-emi-suppression", "RF Filters"): "NO_SAFE_TARGET",
    ("interface-ics", "Analog Switches - Special Purpose"): "Analog Switches, Multiplexers, Demultiplexers",
    ("interface-ics", "CODECS"): "Specialized ICs",
    ("interface-ics", "Modems - ICs and Modules"): "Controllers",
    ("iot-communication-modules", "Modules"): "RF Transceiver Modules and Modems",
    ("led-display-drivers", "LED Drivers ICs"): "LED Drivers",
    ("logic-ics", "Counters, Dividers"): "Flip Flops",
    ("logic-ics", "Gates and Inverters - Multi-Function, Configurable"): "Gates and Inverters",
    ("logic-ics", "Latches"): "Flip Flops",
    ("magnetic-sensors", "Current Sensors"): "NO_SAFE_TARGET",
    ("magnetic-sensors", "Linear, Compass (ICs)"): "Angle, Linear Position Measuring",
    ("magnetic-sensors", "Switches (Solid State)"): "NO_SAFE_TARGET",
    ("office-supplies", "Memory Cards"): "NO_SAFE_TARGET",
    ("optocouplers", "Logic Output Optoisolators"): "Transistor, Photovoltaic Output Optoisolators",
    ("optocouplers", "Solid State Relays (SSR)"): "Transistor, Photovoltaic Output Optoisolators",
    ("power-management", "AC DC Converters, Offline Switchers"): "Power Management - Specialized",
    ("power-management", "Hot Swap Controllers"): "Power Distribution Switches, Load Drivers",
    ("power-management", "PFC (Power Factor Correction)"): "Power Management - Specialized",
    ("power-modules", "AC DC Configurable Power Supply Modules"): "DC DC Converters",
    ("relays", "Power Relays, Over 2 Amps"): "NO_SAFE_TARGET",
    ("relays", "Signal Relays, Up to 2 Amps"): "NO_SAFE_TARGET",
    ("rf-wireless", "Attenuators"): "NO_SAFE_TARGET",
    ("rf-wireless", "Balun"): "NO_SAFE_TARGET",
    ("rf-wireless", "RF Amplifiers"): "Special Purpose Amplifiers",
    ("rf-wireless", "RF Antennas"): "NO_SAFE_TARGET",
    ("rf-wireless", "RF Detectors"): "NO_SAFE_TARGET",
    ("rf-wireless", "RF Front End (LNA + PA)"): "Special Purpose Amplifiers",
    ("rf-wireless", "RF Mixers"): "NO_SAFE_TARGET",
    ("rf-wireless", "RF Power Dividers/Splitters"): "NO_SAFE_TARGET",
    ("rf-wireless", "RF Transmitters"): "RF Transceiver ICs",
    ("sensors", "Ambient Light, IR, UV Sensors"): "Analog and Digital Output",
    ("sensors", "Distance Measuring"): "Analog and Digital Output",
    ("sensors", "Humidity, Moisture Sensors"): "Analog and Digital Output",
    ("sensors", "NTC Thermistors"): "NO_SAFE_TARGET",
    ("sensors", "Sensor, Capacitive Touch"): "Analog and Digital Output",
    ("sensors", "Thermostats - Solid State"): "NO_SAFE_TARGET",
    ("switches", "Slide Switches"): "Tactile Switches",
    ("transistors", "Bipolar Transistor Arrays, Pre-Biased"): "Bipolar Transistor Arrays",
}

# Curated MERGE action for 1-4 SKU CANDIDATES (key=(native_l1, sub) -> target)
CAND_MERGE_TARGET = {
    ("amplifiers-comparators", "Power Supply Controllers, Monitors"): "Special Purpose Amplifiers",
    ("interface-ics", "Specialized"): "Specialized ICs",
    ("logic-ics", "Analog Switches, Multiplexers, Demultiplexers"): "Signal Switches, Multiplexers, Decoders",
    ("logic-ics", "Signal Buffers, Repeaters, Splitters"): "Buffers, Drivers, Receivers, Transceivers",
    ("memory", "Specialized"): "Memory",
    ("power-management", "Power Supply Controllers, Monitors"): "Power Management - Specialized",
    ("power-management", "Special Purpose Regulators"): "Power Management - Specialized",
    ("led-display-drivers", "Special Purpose Regulators"): "LED Drivers",
    ("transistors", "SCRs - Modules"): "SCRs",
}


def slugify(s):
    """Frozen slug rule (verbatim from Phase-5 freeze): lowercase, non-alphanumeric
    -> single hyphen, stripped. Stable, no SKU count, no random id."""
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or "sub"


# ---------------------------------------------------------------------------
# Core deterministic classification (READ-ONLY over MASTER + RAW + review agg).
# Returns (mpn2sr, sr_final) exactly as the locked Phase-5 generator.
# ---------------------------------------------------------------------------
def build_sr_final():
    # 1. MASTER: mpn -> sr, native_l1
    mpn2sr = {}
    sr_info = {}
    with open(MASTER, encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            sr = (row.get("supplier_reference") or "").strip()
            nl = (row.get("native_l1") or "").strip()
            mpn = (row.get("mpn") or "").strip()
            sr_info[sr] = (nl, mpn)
            if mpn:
                mpn2sr[mpn] = sr
    # 2. RAW
    raw = {}
    for fp in glob.glob(RAWGLOB):
        try:
            j = json.load(open(fp, encoding="utf-8"))
        except Exception:
            continue
        mp = j.get("source_raw", {}).get("main_product", {})
        stem = os.path.splitext(os.path.basename(fp))[0]
        raw[stem] = (mp.get("catalogName"), mp.get("parentCatalogName"))
    # 3. REVIEW within_map
    rev = {}
    if os.path.exists(REVIEW_AGG_PATH):
        rev = json.load(open(REVIEW_AGG_PATH, encoding="utf-8")).get("review", {})
    within_map = {name: r.get("parent_to_native_l1_within", {}) for name, r in rev.items()}
    # 4. classification + action (verbatim from freeze)
    cat_sku = Counter(); cat_nl = defaultdict(set); sku_records = []
    for sr, (nl, mpn) in sr_info.items():
        r = raw.get(sr)
        if not r or not r[0]:
            sku_records.append((sr, None, nl, None, "UNKNOWN")); continue
        cat, par = r
        cat_sku[cat] += 1; cat_nl[cat].add(nl)
        sku_records.append((sr, cat, nl, par, None))
    cls = {}
    for cat, nls in cat_nl.items():
        n = cat_sku[cat]
        if len(nls) > 1: cls[cat] = "REVIEW"
        elif n >= 5: cls[cat] = "DIRECT"
        elif n >= 2: cls[cat] = "MERGE"
        else: cls[cat] = "EXCLUDE"
    units = defaultdict(lambda: {"sku": 0, "par": set(), "raw": None, "nl": None})
    for sr, cat, nl, par, ucls in sku_records:
        if ucls == "UNKNOWN":
            continue
        c = cls[cat]
        if c == "DIRECT": key = (nl, cat)
        elif c == "REVIEW":
            eff = (within_map.get(cat, {}).get(par) or [nl])[0]; key = (eff, cat)
        else: key = (nl, cat)
        u = units[key]; u["sku"] += 1
        if par: u["par"].add(par)
        u["raw"] = c; u["nl"] = key[0]
    action = {}
    for k in units:
        c = units[k]["raw"]
        if c in ("DIRECT", "REVIEW"):
            action[k] = ("MERGE", CAND_MERGE_TARGET[k]) if k in CAND_MERGE_TARGET else ("BUILD", None)
        elif c == "MERGE":
            t = MERGE_TARGET.get(k)
            action[k] = ("MERGE", t) if (t and t != "NO_SAFE_TARGET") else ("BUILD", None)
        else:
            action[k] = ("BUILD", None)
    page_sku = {k: units[k]["sku"] for k in units}
    for k in units:
        a, tgt = action[k]
        if a == "MERGE" and tgt:
            tk = None
            for cand in units:
                if cand[1] == tgt and cand[0] == k[0]: tk = cand; break
            if tk is None:
                for cand in units:
                    if cand[1] == tgt: tk = cand; break
            if tk is not None: page_sku[tk] += units[k]["sku"]
    slug_seen = {}; slug_of = {}
    for k in sorted(units, key=lambda x: (x[0], x[1])):
        s = slugify(k[1])
        if s in slug_seen:
            s2 = slugify(k[1] + " " + k[0]); s = s2 if s2 not in slug_seen else f"{s}-{len(slug_seen)+1}"
        slug_seen[s] = True; slug_of[k] = s
    # SR -> final
    sr_final = {}
    for sr, cat, nl, par, ucls in sku_records:
        if ucls == "UNKNOWN":
            sr_final[sr] = None; continue
        c = cls[cat]
        if c == "DIRECT": key = (nl, cat)
        elif c == "REVIEW":
            eff = (within_map.get(cat, {}).get(par) or [nl])[0]; key = (eff, cat)
        else: key = (nl, cat)
        a, tgt = action[key]
        if a == "BUILD":
            fin_name = key[1]; fin_slug = slug_of[key]; fin_nl = key[0]
        else:
            tk = None
            for cand in units:
                if cand[1] == tgt and cand[0] == key[0]: tk = cand; break
            if tk is None:
                for cand in units:
                    if cand[1] == tgt: tk = cand; break
            fin_name = tgt; fin_slug = slug_of[tk] if tk else None; fin_nl = tk[0] if tk else key[0]
        sr_final[sr] = {"final_subcategory": fin_name, "final_slug": fin_slug, "final_native_l1": fin_nl}
    return mpn2sr, sr_final


def build_sr_final_map():
    """Return {mpn: {final_subcategory, final_native_l1, final_slug}} directly."""
    mpn2sr, sr_final = build_sr_final()
    out = {}
    for mpn, sr in mpn2sr.items():
        f = sr_final.get(sr)
        if f and f.get("final_subcategory"):
            out[mpn] = {
                "final_subcategory": f["final_subcategory"],
                "final_native_l1": f["final_native_l1"],
                "final_slug": f["final_slug"],
            }
    return out


def load_final_map():
    """Load the deployed mpn->final_* map. Loads cache if present; otherwise
    rebuilds from MASTER+RAW and caches it."""
    if os.path.exists(MAP_PATH):
        with open(MAP_PATH, encoding="utf-8") as f:
            return json.load(f)
    m = build_sr_final_map()
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return m


def rebuild_map():
    """Recompute final_subcategory_map.json from current MASTER+RAW (read-only)."""
    m = build_sr_final_map()
    with open(MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return m


def attach_final_fields(part, final_map):
    """Mutate a parts.json row in place: attach final_* when the mpn has a frozen
    mapping, otherwise strip final_* so gen_subcategory.py falls back to the
    existing coarse `subcategory` grouping."""
    mpn = (part.get("mpn") or "").strip()
    f = final_map.get(mpn)
    if f and f.get("final_subcategory"):
        part["final_subcategory"] = f["final_subcategory"]
        part["final_native_l1"] = f["final_native_l1"]
        part["final_slug"] = f["final_slug"]
    else:
        part.pop("final_subcategory", None)
        part.pop("final_native_l1", None)
        part.pop("final_slug", None)


def main():
    ap = argparse.ArgumentParser(description="Build/validate the frozen final_subcategory map.")
    ap.add_argument("--build", action="store_true",
                    help="Recompute final_subcategory_map.json from MASTER+RAW+freeze (read-only).")
    args = ap.parse_args()
    if args.build:
        m = rebuild_map()
        n_fin = sum(1 for v in m.values() if v.get("final_subcategory"))
        print(f"BUILD: wrote {MAP_PATH}")
        print(f"  total mpn keys : {len(m)}")
        print(f"  with final_*   : {n_fin}")
        print(f"  without (fallback coarse): {len(m) - n_fin}")
    else:
        m = load_final_map()
        n_fin = sum(1 for v in m.values() if v.get("final_subcategory"))
        print(f"LOAD: {MAP_PATH}")
        print(f"  total mpn keys : {len(m)}")
        print(f"  with final_*   : {n_fin}")


if __name__ == "__main__":
    main()
