#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build Brand + Category reference seed CSVs for SZ Procure Data Factory v1.

Sources (all read-only / frozen where noted):
  - data/mfr_canonical.csv                 (existing 73 canonical / 246 aliases)  [SECOND source]
  - tools/_lcsc_brands.json                (harvested LCSC brand list, 3836)       [FIRST source]
  - gen_parts.py CATEGORY_MAP/TOP_CATEGORIES (6 active L1 + 14 fine_cat)          [frozen, hardcoded]
  - D:/SZ Procure/03_Category/category_taxonomy_v1.md (9-L1 standard)            [frozen, 3 planned L1 hardcoded]

Merge rule (brand):
  - Existing mfr_canonical aliases -> verified core (source=mfr_canonical, status=active, tier A/B).
  - LCSC brand whose cleaned name matches an existing canonical/alias -> mapped to that
    manufacturer_id (source=LCSC, status=active)  -> enriches raw_pool with LCSC spelling.
  - LCSC brand not matched -> NEW manufacturer_id (source=LCSC, status=candidate, tier=B).

Outputs (NEW assets; frozen files untouched):
  - D:/SZ Procure/02_Product_DB/manufacturer_seed.csv
  - D:/SZ Procure/03_Category/category_seed.csv

manufacturer_id / category_id assigned in deterministic order so re-runs keep
IDs stable for identical input (append new LCSC harvest -> new ids only for new brands).
"""
import csv
import json
import os

SITE = "C:/Users/Administrator.SC-202105071542/Desktop/szprocure-site"
MFR_SRC = os.path.join(SITE, "data", "mfr_canonical.csv")
LCSC_JSON = os.path.join(SITE, "tools", "_lcsc_brands.json")
MFR_OUT = "D:/SZ Procure/02_Product_DB/manufacturer_seed.csv"
CAT_OUT = "D:/SZ Procure/03_Category/category_seed.csv"

A_TIER = {
    "Texas Instruments", "STMicroelectronics", "NXP Semiconductors", "Infineon Technologies",
    "Analog Devices", "Microchip Technology", "ON Semiconductor", "Renesas Electronics",
    "Murata Manufacturing", "TDK Corporation", "Vishay Intertechnology", "Yageo",
    "KEMET Electronics", "Bourns", "Littelfuse", "TE Connectivity", "Molex", "Amphenol",
    "Panasonic", "Toshiba", "ROHM Semiconductor", "Diodes Incorporated", "Broadcom",
    "Qualcomm", "Maxim Integrated", "Skyworks Solutions", "Qorvo", "Micron Technology",
    "SK Hynix", "Samsung Electronics", "Winbond Electronics", "Macronix International",
    "GigaDevice Semiconductor", "ISSI", "Cypress Semiconductor", "Wolfspeed", "OSRAM",
    "Sharp", "Realtek", "Richtek Technology", "Monolithic Power Systems", "Silicon Labs",
    "Nordic Semiconductor", "Espressif Systems", "Semtech", "u-blox", "Quectel", "SIMCom",
    "Holtek Semiconductor", "Nuvoton Technology", "Wurth Elektronik", "Omron",
    "Alpha & Omega Semiconductor",
    "Fenghua", "Huaqiang", "Hanxin", "WCH", "Yangjie", "Hua Hong Semiconductor",
    "SGMICRO", "3PEAK", "UTC", "Walsin Technology", "Holy Stone", "Lite-On Technology",
    "Everlight Electronics", "Kingbright", "JST", "Hirose Electric", "JAE",
}

ACTIVE_L1 = {
    "integrated-circuits": "Integrated Circuits",
    "semiconductor-components": "Semiconductor Components",
    "passive-components": "Passive Components",
    "sensors": "Sensors & Transducers",
    "connectors": "Connectors & Electromechanical",
    "modules": "Modules & Communication Modules",
}
ACTIVE_FINE = [
    ("Microcontroller", "integrated-circuits"), ("Microcontrollers", "integrated-circuits"),
    ("MCU", "integrated-circuits"), ("Memory IC", "integrated-circuits"),
    ("Memory", "integrated-circuits"), ("Power Management IC", "integrated-circuits"),
    ("Voltage Regulator", "integrated-circuits"), ("Analog IC", "integrated-circuits"),
    ("Operational Amplifier", "integrated-circuits"), ("Interface IC", "integrated-circuits"),
    ("Logic IC", "integrated-circuits"), ("Semiconductor Components", "semiconductor-components"),
    ("Power MOSFET", "semiconductor-components"), ("MOSFET", "semiconductor-components"),
    ("Diode", "semiconductor-components"), ("Rectifier Diode", "semiconductor-components"),
    ("Transistor", "semiconductor-components"), ("IGBT", "semiconductor-components"),
    ("Rectifier", "semiconductor-components"), ("Thyristor", "semiconductor-components"),
    ("Passive Components", "passive-components"), ("Resistor", "passive-components"),
    ("Resistors", "passive-components"), ("Capacitor", "passive-components"),
    ("Capacitors", "passive-components"), ("Electrolytic Capacitor", "passive-components"),
    ("Inductor", "passive-components"), ("Inductors", "passive-components"),
    ("Crystal Oscillator", "passive-components"), ("LED Components", "passive-components"),
    ("Sensors & Transducers", "sensors"), ("Sensors", "sensors"),
    ("MEMS Sensor", "sensors"), ("Temperature Sensors", "sensors"),
    ("Pressure Sensors", "sensors"), ("Motion Sensors", "sensors"),
    ("Optical Sensors", "sensors"), ("Connectors & Electromechanical", "connectors"),
    ("Connectors", "connectors"), ("Pin Header", "connectors"),
    ("USB Connectors", "connectors"), ("FFC/FPC", "connectors"),
    ("Board-to-Board", "connectors"), ("Wire Connectors", "connectors"),
    ("Switches", "connectors"), ("Modules & Communication Modules", "modules"),
    ("Modules", "modules"), ("WiFi Modules", "modules"),
    ("Bluetooth Modules", "modules"), ("RF Modules", "modules"),
    ("Cellular Modules", "modules"), ("GNSS Modules", "modules"),
]
PLANNED_L1 = {
    "rf-wireless": "RF & Wireless",
    "optoelectronics": "Optoelectronics",
    "electromechanical": "Electromechanical",
}
PLANNED_L2 = [
    ("Sub-1GHz Transceiver", "rf-wireless"), ("2.4GHz Transceiver", "rf-wireless"),
    ("802.15.4 Transceiver", "rf-wireless"), ("LoRa Transceiver", "rf-wireless"),
    ("WiFi SoC", "rf-wireless"), ("BLE SoC", "rf-wireless"), ("NFC Controller", "rf-wireless"),
    ("LED", "optoelectronics"), ("RGB LED", "optoelectronics"), ("IR LED", "optoelectronics"),
    ("Optocoupler", "optoelectronics"), ("OLED Driver", "optoelectronics"),
    ("LCD Module", "optoelectronics"),
    ("Crystal", "electromechanical"), ("Relay", "electromechanical"),
    ("Tactile Switch", "electromechanical"), ("DIP Switch", "electromechanical"),
    ("Slide Switch", "electromechanical"), ("Buzzer", "electromechanical"),
    ("Rotary Encoder", "electromechanical"),
]


def build_manufacturer():
    # ---- verified core from mfr_canonical.csv ----
    core = []
    canon_to_id = {}
    canon_lower = {}
    alias_lower = {}
    canon_tier = {}
    with open(MFR_SRC, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            raw = (r.get("raw_name") or "").strip()
            canon = (r.get("canonical_brand") or "").strip()
            if not raw or not canon:
                continue
            if canon not in canon_to_id:
                canon_to_id[canon] = f"MFR-{len(canon_to_id) + 1:04d}"
                canon_lower[canon.lower()] = canon
                canon_tier[canon] = "A" if canon in A_TIER else "B"
            alias_lower[raw.lower()] = canon
            core.append({
                "raw_name": raw, "canonical_brand": canon,
                "manufacturer_id": canon_to_id[canon],
                "source": "mfr_canonical(existing)", "sku_count": "",
                "status": "active", "tier": canon_tier[canon], "source_url": "",
            })
    matched = 0
    new_rows = []
    seen_new = set()
    new_counter = len(canon_to_id)
    skipped = 0
    # description-blob markers: LCSC sometimes puts full company intro as link text
    DESC_KW = ("有限公司", "公司", "专注于", "研发", "生产", "销售",
               "高新技术企业", "股份", "集团")
    if os.path.exists(LCSC_JSON):
        brands = json.load(open(LCSC_JSON, encoding="utf-8"))
        for b in brands:
            display = (b.get("name") or "").split("\n")[0].strip()
            if not display:
                continue
            # drop description blobs (no real brand name)
            if len(display) > 40 or any(k in display for k in DESC_KW):
                skipped += 1
                continue
            canon_cand = display.split("(")[0].strip() if "(" in display else display
            key = canon_cand.lower()
            # SAFE merge: only full canonical-name match, or long alias (>3 chars)
            # to avoid short-abbreviation collisions (HC/AMS/AD/ST...).
            if key in canon_lower:
                canon = canon_lower[key]
                core.append(_row(display, canon, canon_to_id[canon], "LCSC",
                                 "active", canon_tier[canon], b.get("url", "")))
                matched += 1
            elif key in alias_lower and len(canon_cand) > 3:
                canon = alias_lower[key]
                core.append(_row(display, canon, canon_to_id[canon], "LCSC",
                                 "active", canon_tier[canon], b.get("url", "")))
                matched += 1
            else:
                if key in seen_new:
                    continue
                seen_new.add(key)
                new_counter += 1
                nid = f"MFR-{new_counter:04d}"
                new_rows.append(_row(display, canon_cand, nid, "LCSC",
                                     "candidate", "B", b.get("url", "")))
    print(f"        (LCSC: {skipped} description-blobs skipped, "
          f"{matched} matched to existing, {len(new_rows)} new candidate)")
    rows = core + new_rows
    os.makedirs(os.path.dirname(MFR_OUT), exist_ok=True)
    with open(MFR_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["raw_name", "canonical_brand", "manufacturer_id",
                                          "source", "sku_count", "status", "tier", "source_url"])
        w.writeheader()
        w.writerows(rows)
    print(f"[manufacturer] core={len(core)} (existing aliases + {matched} LCSC-matched) "
          f"+ new LCSC candidate={len(new_rows)} -> total {len(rows)} -> {MFR_OUT}")
    return rows


def _row(raw, canon, mid, source, status, tier, url):
    return {"raw_name": raw, "canonical_brand": canon, "manufacturer_id": mid,
            "source": source, "sku_count": "", "status": status,
            "tier": tier, "source_url": url or ""}


def build_category():
    rows = []
    cid = 0

    def nid():
        nonlocal cid
        cid += 1
        return f"CAT-{cid:04d}"

    for slug, name in ACTIVE_L1.items():
        rows.append(_cat(name, name, "", "", "CATEGORY_MAP(gen_parts)", "active", nid()))
    for fine, l1 in ACTIVE_FINE:
        rows.append(_cat(fine, fine, l1, fine, "CATEGORY_MAP(gen_parts)", "active", nid()))
    for slug, name in PLANNED_L1.items():
        rows.append(_cat(name, name, "", "", "taxonomy_v1", "planned", nid()))
    for l2, l1 in PLANNED_L2:
        rows.append(_cat(l2, l2, l1, l2, "taxonomy_v1", "planned", nid()))
    os.makedirs(os.path.dirname(CAT_OUT), exist_ok=True)
    with open(CAT_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["raw_name", "canonical_name", "l1_category",
                                          "l2_category", "source", "status", "sku_count", "category_id"])
        w.writeheader()
        w.writerows(rows)
    print(f"[category] {len(rows)} rows (active={sum(1 for r in rows if r['status']=='active')}, "
          f"planned={sum(1 for r in rows if r['status']=='planned')}) -> {CAT_OUT}")


def _cat(raw, canon, l1, l2, source, status, cid):
    return {"raw_name": raw, "canonical_name": canon, "l1_category": l1,
            "l2_category": l2, "source": source, "status": status,
            "sku_count": "", "category_id": cid}


if __name__ == "__main__":
    build_manufacturer()
    build_category()
    print("DONE")
