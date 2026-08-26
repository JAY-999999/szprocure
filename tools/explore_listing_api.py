#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capture LCSC's product data API by intercepting network responses."""
import sys, re, json
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = sys.argv[1] if len(sys.argv) > 1 else "https://list.szlcsc.com/catalog/439.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

captured = []
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=EDGE, headless=True)
    ctx = browser.new_context(user_agent=UA, locale="en-US")
    page = ctx.new_page()

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            u = resp.url
            if "json" in ct or "javascript" in ct or "api" in u.lower():
                body = resp.text()[:4000]
                if any(k in body for k in ["product", "Product", "code", "Code", "sku", "Sku", "brand", "Brand", "number", "Number"]):
                    captured.append((u, len(body), body))
        except Exception:
            pass

    page.on("response", on_response)
    page.goto(URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    for _ in range(4):
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(700)
    page.wait_for_timeout(1500)
    browser.close()

print("Captured candidate API responses:", len(captured))
for i, (u, n, body) in enumerate(captured[:12]):
    print(f"\n=== [{i}] {u}  (len {n}) ===")
    print(body[:1200])
