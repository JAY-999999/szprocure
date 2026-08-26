#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Probe candidate LCSC product-data API endpoints for real JSON (no captcha)."""
import sys
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CANDIDATES = [
    "https://www.szlcsc.com/api/products/search?keyword=STM32F103C8T6&page=1",
    "https://www.szlcsc.com/api/products/suggest?keyword=STM32",
    "https://list.szlcsc.com/api/products?catalogId=439&page=1",
    "https://www.szlcsc.com/api/product/search?q=resistor&page=1",
    "https://www.szlcsc.com/products/search?q=STM32F103C8T6",
    "https://so.szlcsc.com/search?q=STM32F103C8T6",
]

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=EDGE, headless=True)
    ctx = browser.new_context(user_agent=UA, locale="en-US")
    page = ctx.new_page()
    for url in CANDIDATES:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(2000)
            txt = page.content()
            title = page.title()
            has_captcha = ("captcha" in txt.lower() or "t.captcha.qq.com" in txt)
            # look for JSON-ish product markers
            markers = [k for k in ["productNumber","productCode","\"code\"","productName",
                                    "\"number\"","skuNumber","brandName","productList"] if k in txt]
            print(f"\n### {url}")
            print(f"   title={title!r} len={len(txt)} captcha={has_captcha} markers={markers}")
            if markers and not has_captcha:
                print("   >>> PROMISING. snippet:")
                print("   ", txt[:600].replace("\n"," "))
        except Exception as e:
            print(f"\n### {url}\n   ERROR: {e}")
    browser.close()
