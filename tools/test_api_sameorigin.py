#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test calling LCSC product search API from a same-origin loaded page context."""
import json
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=EDGE, headless=True)
    ctx = browser.new_context(user_agent=UA, locale="en-US")
    page = ctx.new_page()
    # Load a same-origin page that works (brand page harvested fine earlier)
    print("Loading brand.html ...")
    page.goto("https://www.szlcsc.com/brand.html", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    print("Loaded. title:", page.title())

    api = "https://www.szlcsc.com/api/products/search?keyword=STM32F103C8T6&page=1"
    result = page.evaluate(
        """async (url) => {
            try {
                const r = await fetch(url, {credentials: 'include',
                    headers: {'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}});
                const t = await r.text();
                return {status: r.status, body: t.slice(0, 1500)};
            } catch(e) { return {error: String(e)}; }
        }""", api)
    print("\nAPI result:")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:1800])
    browser.close()
