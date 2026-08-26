#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harvest 100 REAL LCSC SKUs from brand pages (JSON-LD, no captcha) across
diverse component categories. Output: data/raw/lcsc_pilot_100.csv
Columns match clean_factory.py input: supplier,supplier_sku,mpn,manufacturer,
title,category,description,attributes,datasheet_url,stock,price
"""
import re, json, csv, os, sys
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
ROOT = r"C:/Users/Administrator.SC-202105071542/Desktop/szprocure-site"
BRANDS_JSON = os.path.join(ROOT, "tools", "_lcsc_brands.json")
OUT = os.path.join(ROOT, "data", "raw", "lcsc_pilot_100.csv")
SAMPLE = os.path.join(ROOT, "data", "raw", "lcsc_sample.csv")

# Curated (brand search substring -> canonical manufacturer, target fine-category)
# Categories chosen from strings known to resolve in gen_parts CATEGORY_MAP (lcsc_sample).
TARGETS = [
    ("diodes",         "Diodes Incorporated",       "Diode"),
    ("nxp",            "NXP Semiconductors",        "Microcontroller"),
    ("stmicro",        "STMicroelectronics",        "Microcontroller"),
    ("gigadevice",     "GigaDevice Semiconductor",  "Microcontroller"),
    ("microchip",      "Microchip Technology",      "Microcontroller"),
    ("nordic",         "Nordic Semiconductor",      "Microcontroller"),
    ("infineon",       "Infineon Technologies",     "MOSFET"),
    ("on semiconductor","ON Semiconductor",          "MOSFET"),
    ("alpha & omega",  "Alpha & Omega Semiconductor","MOSFET"),
    ("yageo",          "Yageo",                     "Resistor"),
    ("vishay",         "Vishay Intertechnology",    "Resistor"),
    ("wurth",          "Wurth Elektronik",           "Inductor"),
    ("murata",         "Murata Manufacturing",       "Capacitor"),
    ("molex",          "Molex",                     "USB Connectors"),
    ("jst",            "JST",                       "Pin Header"),
    ("hirose",         "Hirose Electric",           "USB Connectors"),
    ("amphenol",       "Amphenol",                  "USB Connectors"),
    ("espressif",      "Espressif Systems",         "WiFi Modules"),
    ("u-blox",         "u-blox",                    "GNSS Modules"),
    ("simcom",         "SIMCom",                    "Cellular Modules"),
    ("texas instruments","Texas Instruments",        "Microcontroller"),
    ("st",             "STMicroelectronics",         "Microcontroller"),  # fallback if above miss
]
PER_BRAND = 6  # ~20 brands * 6 = 120 candidate, trim to 100

def load_brand_map():
    data = json.load(open(BRANDS_JSON, encoding="utf-8"))
    m = {}
    for b in data:
        nm = (b.get("name") or "").lower()
        m[nm] = b.get("url")
    return m

def extract_products(html):
    out = []
    scripts = re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                         html, re.S | re.I)
    for s in scripts:
        s = s.strip()
        if "ItemList" not in s:
            continue
        try:
            data = json.loads(s)
        except Exception:
            continue
        # JSON-LD may be wrapped in @graph
        graph = data.get("@graph", [data]) if isinstance(data, dict) else [data]
        elems = []
        for node in graph:
            if isinstance(node, dict) and node.get("@type") == "ItemList":
                elems = node.get("itemListElement", [])
                break
        if not elems:
            elems = data.get("itemListElement", []) if isinstance(data, dict) else []
        for e in elems:
            item = e.get("item", e) if isinstance(e, dict) else {}
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "Product":
                continue
            offers = item.get("offers", {}) or {}
            price = offers.get("price")
            avail = offers.get("availability", "")
            stock = "In Stock" if (isinstance(avail, str) and "InStock" in avail) else avail
            mpn = (item.get("mpn") or item.get("name") or "").strip()
            sku = (item.get("sku") or "").strip()
            out.append({
                "supplier_sku": sku,
                "mpn": mpn,
                "title": (item.get("name") or "").strip(),
                "description": (item.get("description") or "").strip(),
                "price": price if price is not None else "",
                "stock": stock,
            })
    return out

def main():
    bmap = load_brand_map()
    collected = []  # dict rows for pilot csv
    seen_mpn = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True)
        ctx = browser.new_context(user_agent=UA, locale="en-US")
        page = ctx.new_page()
        for sub, canon, cat in TARGETS:
            url = None
            for nm, u in bmap.items():
                if sub in nm:
                    url = u
                    break
            if not url:
                print(f"  [skip] no brand match for {sub}")
                continue
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(2500)
                html = page.content()
            except Exception as e:
                print(f"  [err] {sub}: {e}")
                continue
            prods = extract_products(html)
            taken = 0
            for pr in prods:
                if not pr["mpn"] or pr["mpn"] in seen_mpn:
                    continue
                if taken >= PER_BRAND:
                    break
                seen_mpn.add(pr["mpn"])
                collected.append({
                    "supplier": "LCSC",
                    "supplier_sku": pr["supplier_sku"],
                    "mpn": pr["mpn"],
                    "manufacturer": canon,
                    "title": pr["title"],
                    "category": cat,
                    "description": pr["description"],
                    "attributes": "",
                    "datasheet_url": "",
                    "stock": pr["stock"],
                    "price": pr["price"],
                })
                taken += 1
            print(f"  [ok] {sub:18} {canon:30} cat={cat:16} got {taken} (total {len(collected)})")
            if len(collected) >= 100:
                break
        browser.close()

    # Merge with the 25 real rich sample rows (they carry attributes + datasheets)
    rich = []
    with open(SAMPLE, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("mpn", "").strip() and r["mpn"] not in seen_mpn:
                seen_mpn.add(r["mpn"])
                rich.append(r)
    print(f"Rich sample rows merged: {len(rich)}")

    final = rich + collected
    # trim to 100 if over
    if len(final) > 100:
        final = final[:100]
    print(f"Final pilot rows: {len(final)}")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    cols = ["supplier","supplier_sku","mpn","manufacturer","title","category",
            "description","attributes","datasheet_url","stock","price"]
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in final:
            w.writerow({c: row.get(c, "") for c in cols})
    print(f"WROTE {OUT}")

if __name__ == "__main__":
    main()
