#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Explore LCSC product listing page DOM to find extractable real product rows."""
import sys, re, json
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://list.szlcsc.com/catalog/439.html"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=EDGE, headless=True)
    ctx = browser.new_context(user_agent=UA, locale="en-US")
    page = ctx.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    # scroll to trigger lazy load
    for _ in range(6):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(800)
    page.wait_for_timeout(2000)
    html = page.content()
    print("PAGE TITLE:", page.title())
    print("HTML LEN:", len(html))

    # Strategy A: anchors to item detail pages
    item_links = re.findall(r'href="(https?://item\.szlcsc\.com/\d+\.html[^"]*)"', html)
    print("\n[A] item.szlcsc.com detail links:", len(item_links))
    for u in item_links[:5]:
        print("   ", u)

    # Strategy B: any link with /product or part-number-ish text
    all_a = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    print("\n[B] total anchors:", len(all_a))

    # Strategy C: search for product-number tokens (e.g. C25804, RC0603...) in text
    body_text = re.sub(r'<[^>]+>', ' ', html)
    body_text = re.sub(r'\s+', ' ', body_text)
    mpn_like = re.findall(r'\b([A-Z0-9]{4,}[A-Z0-9\-/]{2,})\b', body_text)
    print("\n[C] mpn-like tokens in body text (sample 10):", mpn_like[:10], " total:", len(mpn_like))

    # Strategy D: embedded JSON with productList / data keys
    for key in ["productList", "product_list", "list", "\"data\"", "skuNumber", "productCode"]:
        if key in html:
            print(f"\n[D] marker '{key}' FOUND in HTML")
    # try to locate a JSON array near 'productName' or 'code'
    m = re.search(r'\{[^{}]*"productName"[^{}]*\}', html)
    if m:
        print("   sample productName JSON:", m.group(0)[:300])

    # Strategy E: look for table rows / common product card classes
    for cls in ["product", "goods", "item", "card", "list-item"]:
        n = len(re.findall(r'class="[^"]*\b' + cls + r'\b[^"]*"', html))
        if n:
            print(f"\n[E] class contains '{cls}': {n} elements")
    browser.close()
