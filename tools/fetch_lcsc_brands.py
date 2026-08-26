#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Harvest the full LCSC brand list via system Edge (Playwright).
Saves tools/_lcsc_brands.json = [{name, url}] for all brand pages found.
Idempotent-ish: re-run refreshes the JSON.
"""
import json
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "https://www.szlcsc.com/brand.html"
OUT = "tools/_lcsc_brands.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")

def main():
    brands = []
    seen = set()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=EDGE, headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(
            user_agent=UA, locale="zh-CN",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        page = ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)
        for _ in range(10):
            page.mouse.wheel(0, 3000)
            page.wait_for_timeout(700)
        links = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({t: (e.innerText||'').trim(), "
            "h: (e.getAttribute('href')||'')}))")
        for l in links:
            h, t = l["h"], l["t"]
            if "list.szlcsc.com/brand/" in h and t:
                clean_h = h.split("?")[0]
                key = (t, clean_h)
                if key in seen:
                    continue
                seen.add(key)
                brands.append({"name": t, "url": clean_h})
        browser.close()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(brands, f, ensure_ascii=False, indent=2)
    print(f"LCSC brands harvested: {len(brands)} -> {OUT}")

if __name__ == "__main__":
    main()
