#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Harvest 500 REAL, popular LCSC component SKUs via LCSC's internal listing API
and build data/production/master_parts_v2.1.csv (17 cols, committed).

DATA SOURCE (discovered this session):
  LCSC SSR search/category pages are JS-rendered shells that always return the
  same static "hot 16". The REAL data comes from an internal endpoint called
  by the browser:
      POST https://wmsc.lcsc.com/ftps/wm/home/discount/product/search/list
  Body: {"currentPage":N, "pageSize":100, "isHot":0}
  -> returns a popularity-ranked list of 5000 products (keyword/catalogId are
     ignored by the API, so we just page through the global ranking).
  Each list item already contains EVERYTHING we need:
     productModel(MPN), productCode(C-number), brandNameEn, encapStandard(pkg),
     paramVOList(params), productPriceList(price), pdfUrl(datasheet),
     catalogName, catalogId, productImages/imgUrl(images), stockNumber,
     productNameEn(description).
  No per-detail-page fetch required -> fast & robust.

"Popular" definition: LCSC's own popularity ranking (top of the 5000 list).
Curation: greedy by rank, per-category caps to balance the catalog toward the
site's 6 L1 families and the user's B2B industrial/auto/power strength.
Preserves the existing 19 SKUs in master_parts_v2.0.csv.
"""
import os, re, json, csv, sys, random, argparse, datetime
from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gen_parts as gp

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
API = "https://wmsc.lcsc.com/ftps/wm/home/discount/product/search/list"
RAW_OUT = os.path.join(ROOT, "data", "raw", f"lcsc_api_{datetime.datetime.now():%Y%m%d}.csv")
MASTER_OUT = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
BASE_CSV = os.path.join(ROOT, "data", "production", "master_parts_v2.0.csv")

MASTER_HEADER = ["mpn", "clean_mpn", "manufacturer", "brand", "url_slug",
                 "category", "subcategory", "description", "applications",
                 "keywords", "attributes_json", "availability",
                 "alternative_parts", "datasheet_url", "faq", "image", "source"]

GLOBAL_CAP = 500
PAGE_SIZE = 100

# Per fine-category caps (max count in the final 500). Sum >> 500 so greedy fill
# reaches 500; caps prevent any single family from dominating. Tuned toward the
# user's B2B industrial / auto / power / connector strength + natural demand.
CAPS = {
    "Microcontroller": 60, "Memory IC": 25, "Power Management IC": 20,
    "Voltage Regulator": 55, "Analog IC": 15, "Operational Amplifier": 35,
    "Interface IC": 40, "Logic IC": 25,
    "MOSFET": 55, "Diode": 45, "Transistor": 25,
    "Resistor": 35, "Capacitor": 40, "Inductor": 20, "Crystal Oscillator": 15,
    "LED Components": 15, "Sensors": 35,
    "Pin Header": 12, "USB Connectors": 12, "Connectors": 20, "Switches": 12,
    "WiFi Modules": 10, "Bluetooth Modules": 6, "Cellular Modules": 6,
    "GNSS Modules": 6, "RF Modules": 6, "Modules": 12,
}
DEFAULT_CAP = 15

# LCSC catalogName (keyword) -> site fine category (must be a CATEGORY_MAP key)
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
    # extended popular families (were unmapped)
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
        return None
    low = lcsc_cat.lower()
    for kw, fine in CAT_KEYWORDS:
        if kw in low:
            return fine
    return None


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=GLOBAL_CAP)
    ap.add_argument("--pages", type=int, default=60, help="max API pages to fetch (5000/100)")
    args = ap.parse_args()
    cap = args.cap

    # load base (existing) SKUs
    base = {}
    if os.path.exists(BASE_CSV):
        with open(BASE_CSV, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                base[row["mpn"].strip().upper()] = row
    print(f"[base] loaded {len(base)} existing SKUs from v2.0")

    # fetch ranked list
    seen = set(base.keys())
    ranked = []
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=EDGE, headless=True)
        ctx = b.new_context(user_agent=UA, locale="en-US", viewport={"width":1366,"height":900})
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

    # curate 500 with per-category caps, greedy by popularity rank
    counts = {k: 0 for k in CAPS}
    selected = []
    unmapped = []
    for it in ranked:
        mpn = (it.get("productModel") or "").strip()
        if not mpn:
            continue
        key = mpn.upper()
        if key in seen:
            continue
        # skip synthetic/test MPNs (gen_parts guard hard-aborts on these)
        if any(p.search(mpn) for p in gp.SYNTHETIC_MPN_PATTERNS):
            continue
        fine = map_cat(it.get("catalogName"))
        if fine is None:
            unmapped.append((mpn, it.get("catalogName")))
            continue
        if counts.get(fine, 0) >= CAPS.get(fine, DEFAULT_CAP):
            continue
        # build row
        code = (it.get("productCode") or "").strip()
        brand_raw = (it.get("brandNameEn") or "").strip()
        canon, _ = gp.canonicalize_brand(brand_raw, gp.load_mfr_canonical(os.path.join(ROOT, "data", "mfr_canonical.csv")))
        if gp.FAKE_BRAND_TOKENS.search(canon):
            continue
        attrs = parse_params(it.get("paramVOList"))
        attrs_json = json.dumps(attrs, ensure_ascii=False)
        img = first_image(it)
        ds = f"https://www.lcsc.com/datasheet/{code}.pdf" if code else ""
        if not ds and it.get("pdfUrl"):
            ds = str(it["pdfUrl"]).split("?")[0]
        stock = it.get("stockNumber")
        row = {
            "mpn": mpn,
            "clean_mpn": re.sub(r"[^A-Z0-9]", "", mpn.upper()),
            "manufacturer": canon,
            "brand": canon,
            "url_slug": gp.slugify(mpn),
            "category": fine,
            "subcategory": fine,
            "description": (it.get("productNameEn") or "").strip(),
            "applications": "",
            "keywords": "",
            "attributes_json": attrs_json,
            "availability": "active" if (stock is None or stock > 0) else "active",
            "alternative_parts": "",
            "datasheet_url": ds,
            "faq": "",
            "image": img,
            "source": "LCSC",
        }
        selected.append(row)
        seen.add(key)
        counts[fine] = counts.get(fine, 0) + 1
        if len(selected) >= (cap - len(base)):
            break

    final = list(base.values()) + selected
    final = final[:cap]
    print(f"[curate] selected {len(selected)} new + {len(base)} base = {len(final)} final")
    print("[curate] category distribution:")
    from collections import Counter
    for k, v in Counter(r["category"] for r in final).most_common():
        print(f"    {k:22} {v}")

    # write RAW (archive, not committed)
    os.makedirs(os.path.dirname(RAW_OUT), exist_ok=True)
    with open(RAW_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["supplier", "supplier_sku", "mpn", "manufacturer", "title",
                    "category", "description", "attributes", "datasheet_url", "stock", "source"])
        for r in final:
            w.writerow(["LCSC", r["clean_mpn"], r["mpn"], r["brand"], r["mpn"],
                        r["category"], r["description"], r["attributes_json"],
                        r["datasheet_url"], "", "LCSC"])
    # write MASTER v2.1
    os.makedirs(os.path.dirname(MASTER_OUT), exist_ok=True)
    with open(MASTER_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_HEADER)
        w.writeheader()
        for row in final:
            w.writerow({c: row.get(c, "") for c in MASTER_HEADER})
    print(f"\n>> RAW    -> {RAW_OUT}")
    print(f">> MASTER -> {MASTER_OUT} ({len(final)} rows)")

    if unmapped:
        print(f">> UNMAPPED catalogNames ({len(unmapped)}):")
        for mpn, c in unmapped[:30]:
            print(f"     {mpn}: {c}")


if __name__ == "__main__":
    main()
