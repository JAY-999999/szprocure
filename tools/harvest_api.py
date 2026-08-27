#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 1 (RAW capture) — Harvest the FULL popularity-ranked LCSC component list
via LCSC's internal listing API and emit a RAW archive CSV + JSON.

DESIGN (3-layer pipeline: RAW -> CLEAN -> MASTER -> BUILD):
  This script is RAW-ONLY. It does NOT curate, clean, or write the master.
  It captures the *entire* ranked list (up to 5000) so the downstream CLEAN
  layer (clean_factory.py) can curate any N (500 / 5000 / 50000) deterministically
  and self-host every asset with full traceability.

DATA SOURCE (discovered earlier):
  LCSC SSR search/category pages are JS-rendered shells that always return the
  same static "hot 16". The REAL data comes from an internal endpoint called by
  the browser:
      POST https://wmsc.lcsc.com/ftps/wm/home/discount/product/search/list
  Body: {"currentPage":N, "pageSize":100, "isHot":0}
  -> returns a popularity-ranked list of 5000 products (keyword/catalogId are
     ignored by the API, so we just page through the global ranking).
  Each list item already contains EVERYTHING we need, including:
     productModel(MPN), productCode(C-number), brandNameEn, encapStandard(pkg),
     paramVOList(params), productPriceList(price),
     pdfUrl(REAL datasheet PDF — datasheet.lcsc.com/...pdf),
     catalogName, catalogId, productImages/imgUrl(images), stockNumber,
     productNameEn(description).

OUTPUT:
  data/raw/lcsc_api_FULL_<DATE>.csv   (working copy, git-untracked)
  data/raw/lcsc_api_FULL_<DATE>.json  (full item dump for traceability)
  D:/SZ Procure/01_RAW/lcsc_api_FULL_<DATE>.csv (+.json)  (asset-root archive)
  Both archived files get a SHA256 written to D:/SZ Procure/01_RAW/

IMPORTANT — provenance vs. branding:
  The RAW captures *source* URLs (assets.lcsc.com images, datasheet.lcsc.com
  PDFs). These are NEVER rendered on the site. The CLEAN layer downloads them
  into local /assets/parts/ paths so the built site has ZERO lcsc identifiers.
"""
import os, re, json, csv, sys, random, argparse, datetime, hashlib, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gen_parts as gp

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
API = "https://wmsc.lcsc.com/ftps/wm/home/discount/product/search/list"
PAGE_SIZE = 100
DATE = f"{datetime.datetime.now():%Y%m%d}"

RAW_OUT = os.path.join(ROOT, "data", "raw", f"lcsc_api_FULL_{DATE}.csv")
RAW_JSON = os.path.join(ROOT, "data", "raw", f"lcsc_api_FULL_{DATE}.json")

# Asset-root archive (D). Mirrors the working copy; this is the source of truth
# for audits. If D is unavailable we still keep the working copy under data/raw/.
ARCHIVE_DIR = r"D:\SZ Procure\01_RAW"
ARCHIVE_CSV = os.path.join(ARCHIVE_DIR, f"lcsc_api_FULL_{DATE}.csv")
ARCHIVE_JSON = os.path.join(ARCHIVE_DIR, f"lcsc_api_FULL_{DATE}.json")

# RAW schema — flat, source-faithful. CLEAN layer maps/derives from these.
RAW_HEADER = ["rank", "supplier", "supplier_sku", "mpn", "manufacturer_raw",
              "catalogName", "category", "description", "attributes_json",
              "source_image_url", "source_datasheet_url", "stock", "source"]

# LCSC catalogName (keyword) -> site fine category. Used only to RECORD the
# mapped fine category in RAW for faster downstream curation. Same map as the
# original curation logic.
CAT_KEYWORDS = [
    ("microcontroller", "Microcontroller"), ("mcu", "Microcontroller"),
    ("memory", "Memory IC"), ("flash", "Memory IC"), ("eeprom", "Memory IC"),
    ("power management", "Power Management IC"), ("pmic", "Power Management IC"),
    ("voltage regulator", "Voltage Regulator"), ("ldo", "Voltage Regulator"),
    ("dc-dc", "Voltage Regulator"), ("buck", "Voltage Regulator"),
    ("voltage reference", "Voltage Regulator"),
    ("amplifier", "Operational Amplifier"), ("op amp", "Operational Amplifier"),
    ("comparator", "Operational Amplifier"),
    ("interface", "Interface IC"), ("transceiver", "Interface IC"),
    ("isolator", "Interface IC"), ("can ", "Interface IC"), ("rs-485", "Interface IC"),
    ("rs232", "Interface IC"), ("uart", "Interface IC"), ("usb ic", "Interface IC"),
    ("logic", "Logic IC"), ("gate", "Logic IC"),
    ("mosfet", "MOSFET"), ("igbt", "MOSFET"),
    ("rectifier", "Diode"), ("diode", "Diode"), ("zener", "Diode"),
    ("schottky", "Diode"), ("tvs", "Diode"),
    ("transistor", "Transistor"), ("darlington", "Transistor"),
    ("thyristor", "Transistor"), ("triac", "Transistor"),
    ("resistor", "Resistor"), ("resist", "Resistor"),
    ("capacitor", "Capacitor"), ("cap ", "Capacitor"), ("mlcc", "Capacitor"),
    ("inductor", "Inductor"), ("choke", "Inductor"), ("ferrite", "Inductor"),
    ("crystal", "Crystal Oscillator"), ("oscillator", "Crystal Oscillator"),
    ("led", "LED Components"),
    ("sensor", "Sensors"), ("accelerometer", "Sensors"), ("magnetic", "Sensors"),
    ("gyroscope", "Sensors"), ("temperature", "Sensors"), ("pressure", "Sensors"),
    ("pin header", "Pin Header"), ("header", "Pin Header"),
    ("usb", "USB Connectors"), ("type-c", "USB Connectors"), ("connector", "Connectors"),
    ("switch", "Switches"), ("relay", "Switches"),
    ("wifi", "WiFi Modules"), ("wireless", "WiFi Modules"),
    ("bluetooth", "Bluetooth Modules"),
    ("cellular", "Cellular Modules"), ("gsm", "Cellular Modules"),
    ("gnss", "GNSS Modules"), ("gps", "GNSS Modules"),
    ("rf", "RF Modules"), ("radio", "RF Modules"),
    ("module", "Modules"), ("ethernet", "Modules"),
    ("analog to digital", "Analog IC"), ("adc", "Analog IC"), ("dac", "Analog IC"),
    ("converter", "Analog IC"), ("timer", "Logic IC"), ("counter", "Logic IC"),
    ("shift register", "Logic IC"), ("inverter", "Logic IC"), ("gate ", "Logic IC"),
    ("motor driver", "Power Management IC"), ("battery", "Power Management IC"),
    ("led driver", "Power Management IC"),
    ("level shifter", "Interface IC"), ("translator", "Interface IC"),
    ("io expander", "Interface IC"), ("buffer", "Interface IC"),
    ("repeater", "Interface IC"), ("driver", "Interface IC"), ("signal", "Interface IC"),
    ("thermistor", "Resistor"), ("varistor", "Resistor"),
    ("relay", "Switches"),
]


def map_cat(lcsc_cat):
    if not lcsc_cat:
        return ""
    low = lcsc_cat.lower()
    for kw, fine in CAT_KEYWORDS:
        if kw in low:
            return fine
    return ""


def parse_params(param_vo_list):
    attrs = {}
    if not isinstance(param_vo_list, list):
        return attrs
    for it in param_vo_list:
        if not isinstance(it, dict):
            continue
        name = (it.get("paramName") or it.get("name") or it.get("key") or "").strip()
        val = (it.get("paramValue") or it.get("value") or it.get("text") or "").strip()
        if name and val and val not in ("-", "N/A", ""):
            attrs[name] = val
    return attrs


def first_image(it):
    for f in ("productImages", "imgUrl", "productImageUrlBig"):
        v = it.get(f)
        if isinstance(v, list) and v:
            return v[0]
        if isinstance(v, str) and v.startswith("http"):
            return v
    return ""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=60, help="max API pages (5000/100)")
    ap.add_argument("--no-archive", action="store_true",
                    help="skip copying to D:/SZ Procure/01_RAW")
    args = ap.parse_args()

    # fetch ranked list (full popularity ranking)
    ranked = []
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EDGE, headless=True)
        ctx = b.new_context(user_agent=UA, locale="en-US", viewport={"width": 1366, "height": 900})
        pg = ctx.new_page()
        pg.goto("https://www.lcsc.com/", wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(2500)
        js = """async (body) => {
          const r = await fetch('%s', {method:'POST',credentials:'include',
            headers:{'Content-Type':'application/json;charset=UTF-8','Accept':'application/json, text/plain, */*'},
            body: JSON.stringify(body)});
          return await r.json();
        }""" % API
        total_pages = args.pages
        for pn in range(1, args.pages + 1):
            j = pg.evaluate(js, {"currentPage": pn, "pageSize": PAGE_SIZE, "isHot": 0})
            res = (j or {}).get("result") or {}
            dl = res.get("dataList") or []
            if not dl:
                total_pages = pn - 1
                print(f"  page {pn}: empty -> stop")
                break
            ranked.extend(dl)
            if pn % 10 == 0 or pn == 1:
                print(f"  fetched page {pn}: +{len(dl)} (cum {len(ranked)})")
            pg.wait_for_timeout(random.randint(80, 200))
        b.close()
    print(f"[fetch] total raw items: {len(ranked)}")

    # write RAW (flat, source-faithful)
    os.makedirs(os.path.dirname(RAW_OUT), exist_ok=True)
    with open(RAW_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(RAW_HEADER)
        for rank, it in enumerate(ranked, 1):
            mpn = (it.get("productModel") or "").strip()
            code = (it.get("productCode") or "").strip()
            brand_raw = (it.get("brandNameEn") or "").strip()
            cat = (it.get("catalogName") or "").strip()
            fine = map_cat(cat)
            desc = (it.get("productNameEn") or "").strip()
            attrs = parse_params(it.get("paramVOList"))
            img = first_image(it)
            pdf = (it.get("pdfUrl") or "").strip()
            pdf = pdf.split("?")[0] if pdf else ""   # REAL PDF url (datasheet.lcsc.com)
            stock = it.get("stockNumber")
            w.writerow([rank, "LCSC", code, mpn, brand_raw, cat, fine, desc,
                        json.dumps(attrs, ensure_ascii=False), img, pdf,
                        stock if stock is not None else "", "LCSC"])
    # JSON full dump (traceability)
    with open(RAW_JSON, "w", encoding="utf-8") as f:
        json.dump(ranked, f, ensure_ascii=False)

    print(f"\n>> RAW CSV  -> {RAW_OUT}  ({len(ranked)} rows)")
    print(f">> RAW JSON -> {RAW_JSON}")

    # archive to D asset root + SHA256
    if not args.no_archive:
        try:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            shutil.copy2(RAW_OUT, ARCHIVE_CSV)
            shutil.copy2(RAW_JSON, ARCHIVE_JSON)
            csv_sha = sha256_file(ARCHIVE_CSV)
            json_sha = sha256_file(ARCHIVE_JSON)
            print(f">> ARCHIVE  -> {ARCHIVE_CSV}")
            print(f">> ARCHIVE  -> {ARCHIVE_JSON}")
            print(f">> SHA256 CSV  : {csv_sha}")
            print(f">> SHA256 JSON : {json_sha}")
            with open(os.path.join(ARCHIVE_DIR, f"lcsc_api_FULL_{DATE}.sha256"), "w") as f:
                f.write(f"{csv_sha}  lcsc_api_FULL_{DATE}.csv\n")
                f.write(f"{json_sha}  lcsc_api_FULL_{DATE}.json\n")
        except Exception as e:
            print(f"[warn] archive to D failed: {e}")


if __name__ == "__main__":
    main()
