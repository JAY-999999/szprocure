#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re
from playwright.sync_api import sync_playwright
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EDGE, headless=True)
    pg = b.new_context(user_agent=UA, locale="en-US").new_page()
    pg.goto("https://list.szlcsc.com/brand/11353.html", wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(4000)
    html = pg.content()

    # find first item link and show surrounding HTML
    m = re.search(r'.{400}item\.szlcsc\.com/\d+\.html.{400}', html, re.S)
    if m:
        print("=== context around first product link ===")
        print(m.group(0))

    # count product-like entries: a part number near price
    # LCSC part numbers on listing often like Cxxxxx or alphanumeric
    part_ids = re.findall(r'item\.szlcsc\.com/(\d+)\.html', html)
    print("\nproduct ids found:", len(part_ids), "sample:", part_ids[:5])

    # try to detect price pattern
    prices = re.findall(r'[￥¥]\s?[\d,]+\.?\d*', html)
    print("price-like tokens:", len(prices), "sample:", prices[:5])

    # detect a product table / list container class
    for cls in ["product", "goods", "pro-list", "list-table", "table", "prod"]:
        n = len(re.findall(r'class="[^"]*\b' + cls + r'\b', html))
        if n:
            print(f"class '{cls}': {n}")
    b.close()
