#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data Factory v1 — Scale-batch generator (Phase 2.2 preflight validation).

Produces realistic supplier-export raw CSVs (the same 11-column shape the
cleaning pipeline consumes) at 100 / 500 / 1000 rows. Deterministic per level
(seed = BASE + level) so validation is reproducible.

Design goals for the validation:
  * Cover all 14 fine-categories that resolve to the 6 implementation slugs.
  * Mix KNOWN canonical brands, KNOWN attribute keys (incl. legacy aliases
    that exercise LEGACY_ATTR_MAP), and INJECTED unknowns (unknown brand /
    missing manufacturer / unknown attribute key / malformed JSON) so the
    review_queue routing is exercised at scale.

Usage:
  python tools/make_scale_batch.py --level 100  --out data/raw/scale_100.csv
  python tools/make_scale_batch.py --level 500  --out data/raw/scale_500.csv
  python tools/make_scale_batch.py --level 1000 --out data/raw/scale_1000.csv
"""
import csv, os, sys, random, argparse

SEED_BASE = 20260826
SUPPLIERS = ["LCSC", "HQEW", "Hanxin", "Digikey", "Mouser"]

# fine_cat -> (canonical brand pool, fictional unknown pool, mpn prefix, attr templates)
# attr template = (label, value) ; label may be a dictionary key OR a legacy alias
CATS = {
    "Microcontroller": dict(
        mfrs=["STMicroelectronics", "GigaDevice Semiconductor", "Microchip Technology", "Nuvoton Technology", "Holtek Semiconductor"],
        unk=["AcmeSemi", "NovaMicro", "PioneerIC", "QubitCore"],
        mpn="MCU",
        attrs=[("Core", "ARM Cortex-M4"), ("frequency_hz", "72000000"), ("flash_bytes", "131072"),
               ("ram_bytes", "20480"), ("voltage_v", "3.3"), ("io_count", "48"), ("package", "LQFP-48"),
               ("Clock Speed", "72MHz"), ("Program Memory", "128KB"), ("RAM Size", "20KB"), ("Operating Voltage", "3.3V")],
        unk_attrs=[("Thermal Resistance", "45 C/W"), ("ESD Rating", "2kV"), ("Operating Temp", "-40~85")],
    ),
    "MOSFET": dict(
        mfrs=["Infineon Technologies", "ON Semiconductor", "Vishay Intertechnology", "Alpha & Omega Semiconductor", "Yangjie"],
        unk=["VoltTech", "HexaFab", "RiverSemi"],
        mpn="MOS",
        attrs=[("vds_v", "55"), ("id_a", "30"), ("rds_on_mohm", "8"), ("vgs_th_v", "2.5"), ("qg_nc", "45"), ("package", "SOT-23"),
               ("Drain Source Voltage", "55V"), ("On Resistance", "8mΩ"), ("Continuous Drain Current", "30A")],
        unk_attrs=[("Gate Charge Type", "standard"), ("Leakage Current", "1uA")],
    ),
    "Resistor": dict(
        mfrs=["Yageo", "KEMET Electronics", "Bourns", "Vishay Intertechnology", "Fenghua"],
        unk=["OhmWorks", "ResTech", "UniPassive"],
        mpn="RES",
        attrs=[("resistance_ohm", "10000"), ("tolerance", "±1%"), ("power_rating_w", "0.25"), ("package", "0805"), ("temperature_coeff", "X7R"),
               ("Resistor Value", "10kΩ")],
        unk_attrs=[("Lifecycle Status", "Active"), ("MTBF Hours", "100000")],
    ),
    "Capacitor": dict(
        mfrs=["Murata Manufacturing", "TDK Corporation", "KEMET Electronics", "Walsin Technology", "Holy Stone"],
        unk=["CapraMicro", "DeltaCap", "SigmaStore"],
        mpn="CAP",
        attrs=[("capacitance_pf", "100000"), ("tolerance", "±10%"), ("voltage_rating_v", "50"), ("package", "0603"), ("temperature_coeff", "X7R"),
               ("Capacitor Value", "100nF")],
        unk_attrs=[("Ripple Current", "100mA")],
    ),
    "Inductor": dict(
        mfrs=["TDK Corporation", "Murata Manufacturing", "Bourns", "Wurth Elektronik"],
        unk=["InduMax", "CoilWorks"],
        mpn="IND",
        attrs=[("inductance_uh", "4.7"), ("current_rating_a", "1.5"), ("tolerance", "±20%"), ("package", "0805")],
        unk_attrs=[("Self Resonant Freq", "50MHz")],
    ),
    "Sensors": dict(
        mfrs=["STMicroelectronics", "BoschDummy", "NXP Semiconductors", "TE Connectivity"],
        unk=["SenseQ", "AeroSense", "LumenDetect"],
        mpn="SEN",
        attrs=[("sensitivity", "260 LSB/g"), ("resolution_bits", "12"), ("range", "0-40"), ("accuracy", "±0.5"), ("interface", "I2C")],
        unk_attrs=[("Output Type", "Analog"), ("Response Time", "5ms")],
    ),
    "Connectors": dict(
        mfrs=["TE Connectivity", "Molex", "Amphenol", "JST", "Hirose Electric"],
        unk=["ConnX", "PlugWorks", "LinkFab"],
        mpn="CON",
        attrs=[("positions", "40"), ("pitch_mm", "2.54"), ("current_rating_a", "3"), ("mounting", "SMD"), ("voltage_rating_v", "50")],
        unk_attrs=[("Mating Cycles", "500"), ("Contact Plating", "Gold")],
    ),
    "Modules": dict(
        mfrs=["Espressif Systems", "Quectel", "u-blox", "SIMCom", "Semtech"],
        unk=["ModuLink", "MeshWorks", "AirByte"],
        mpn="MOD",
        attrs=[("data_rate_bps", "2000000"), ("output_power_dbm", "20"), ("sensitivity_dbm", "-95"), ("interface", "UART"), ("modulation", "GFSK"),
               ("Data Rate", "2Mbps")],
        unk_attrs=[("Antenna Type", "PCB"), ("Protocol Stack", "3.0")],
    ),
    "Analog IC": dict(
        mfrs=["Analog Devices", "Texas Instruments", "Maxim Integrated", "Silicon Labs"],
        unk=["OpAmpInc", "LinearX"],
        mpn="OPA",
        attrs=[("gain_db", "100"), ("bandwidth_hz", "1000000"), ("supply_current_ua", "200"), ("package", "SOIC-8")],
        unk_attrs=[("Phase Margin", "60°"), ("Input Noise", "10nV")],
    ),
    "Power Management IC": dict(
        mfrs=["Monolithic Power Systems", "Texas Instruments", "Infineon Technologies", "Richtek Technology", "SGMICRO"],
        unk=["PowerLite", "VoltRegInc"],
        mpn="PMIC",
        attrs=[("voltage_in_min_v", "4.5"), ("voltage_in_max_v", "28"), ("voltage_out_v", "5"), ("output_current_a", "3"), ("efficiency_percent", "92"), ("package", "SOT-23")],
        unk_attrs=[("Line Regulation", "0.5%"), ("Power Good", "yes")],
    ),
    "Diode": dict(
        mfrs=["Diodes Incorporated", "ON Semiconductor", "Vishay Intertechnology", "Littelfuse"],
        unk=["DiodeX", "RectFab"],
        mpn="DIO",
        attrs=[("vrrm_v", "100"), ("if_a", "1"), ("vf_v", "0.7")],
        unk_attrs=[("Reverse Recovery", "50ns")],
    ),
    "LED Components": dict(
        mfrs=["Everlight Electronics", "Kingbright", "Lite-On Technology", "OSRAM"],
        unk=["LedWorks", "PhotonInc"],
        mpn="LED",
        attrs=[("wavelength_nm", "650"), ("forward_voltage_v", "2.1"), ("forward_current_ma", "20"), ("package", "0805")],
        unk_attrs=[("Luminous Intensity", "100mcd")],
    ),
    "Memory IC": dict(
        mfrs=["Winbond Electronics", "Micron Technology", "Macronix International", "SK Hynix", "ISSI"],
        unk=["MemCore", "StoreX"],
        mpn="MEM",
        attrs=[("memory_bytes", "16777216"), ("interface", "SPI"), ("speed_hz", "104000000"), ("organization", "x8")],
        unk_attrs=[("Access Time", "70ns")],
    ),
    "Crystal Oscillator": dict(
        mfrs=["Murata Manufacturing", "TDK Corporation", "AbraconX", "Wurth Elektronik"],
        unk=["CrystaWorks", "OscInc"],
        mpn="XTAL",
        attrs=[("frequency_hz", "8000000"), ("load_capacitance_pf", "18"), ("package", "SMD")],
        unk_attrs=[("Frequency Tolerance", "±20ppm")],
    ),
}

# injection rates (realistic: a fraction will need review)
P_UNKNOWN_MFR = 0.08
P_MISSING_MFR = 0.015
P_UNKNOWN_ATTR = 0.12      # chance a row carries one unknown attribute label
P_USE_LEGACY = 0.45        # chance a row uses >=1 legacy alias (exercises LEGACY_ATTR_MAP)
P_MALFORMED = 0.01         # chance attributes is a malformed JSON array

HEADER = ["supplier", "supplier_sku", "mpn", "manufacturer", "title",
          "category", "description", "attributes", "datasheet_url", "stock", "price"]


def build_attrs(cat, rnd):
    """Return an attributes free-text string for one row."""
    if rnd.random() < P_MALFORMED:
        return "[1, 2, 3]"  # malformed -> parse_attrs_free flags __raw__
    templates = list(CATS[cat]["attrs"])
    rnd.shuffle(templates)
    n = rnd.randint(2, min(5, len(templates)))
    pick = templates[:n]
    use_legacy = rnd.random() < P_USE_LEGACY
    parts = []
    used_legacy = False
    for label, val in pick:
        # legacy aliases are the Capitalized multi-word ones; dict keys are snake_case
        is_legacy = " " in label or any(c.isupper() for c in label[1:]) and "_" not in label
        if use_legacy and is_legacy and not used_legacy:
            parts.append(f"{label}: {val}")
            used_legacy = True
        elif not is_legacy:
            parts.append(f"{label}: {val}")
    # maybe add an unknown attribute label
    if rnd.random() < P_UNKNOWN_ATTR:
        lab, val = rnd.choice(CATS[cat]["unk_attrs"])
        parts.append(f"{lab}: {val}")
    if not parts:  # fallback to at least one known
        label, val = CATS[cat]["attrs"][0]
        parts.append(f"{label}: {val}")
    return "; ".join(parts)


def gen_level(level, n, out_path):
    rnd = random.Random(SEED_BASE + level)
    cat_keys = list(CATS.keys())
    rows = []
    for i in range(n):
        cat = cat_keys[i % len(cat_keys)]
        c = CATS[cat]
        # manufacturer selection
        roll = rnd.random()
        if roll < P_MISSING_MFR:
            mfr = ""
        elif roll < P_MISSING_MFR + P_UNKNOWN_MFR:
            mfr = rnd.choice(c["unk"])
        else:
            mfr = rnd.choice(c["mfrs"])
        mpn = f"{c['mpn']}{level}{i:05d}"
        supplier = rnd.choice(SUPPLIERS)
        sku = f"{supplier[:3].upper()}-{level}-{i:05d}"
        title = f"{mfr or 'Unknown'} {mpn} {cat}"
        desc = f"{mpn} {cat} from {mfr or 'unspecified'}. " \
               f"Sourced via professional procurement support; specs below."
        attrs = build_attrs(cat, rnd)
        ds = f"https://example.com/datasheet/{mpn}.pdf"
        stock = rnd.randint(0, 5000)
        price = f"{rnd.uniform(0.01, 25.0):.4f}"
        rows.append([supplier, sku, mpn, mfr, title, cat, desc, attrs, ds, stock, price])

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(rows)
    print(f"Generated level {level}: {n} rows -> {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, required=True, help="100 / 500 / 1000 (also seeds the RNG)")
    ap.add_argument("--out", required=True, help="Output raw CSV path")
    ap.add_argument("--n", type=int, default=0, help="Override row count (default = level)")
    args = ap.parse_args()
    n = args.n or args.level
    gen_level(args.level, n, args.out)


if __name__ == "__main__":
    main()
