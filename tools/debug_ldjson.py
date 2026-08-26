#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import re, json
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
    print("html len:", len(html))
    print("'ItemList' in html:", "ItemList" in html)
    print("'application/ld+json' (exact) in html:", "application/ld+json" in html)
    # quote-agnostic regex
    pat = re.compile(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S|re.I)
    blocks = pat.findall(html)
    print("ld+json blocks (quote-agnostic):", len(blocks))
    if blocks:
        for blk in blocks:
            if "ItemList" not in blk:
                continue
            try:
                d = json.loads(blk)
            except Exception as e:
                print("json parse err:", e); continue
            graph = d.get("@graph", [d])
            for node in graph:
                if isinstance(node, dict) and node.get("@type") == "ItemList":
                    elems = node.get("itemListElement", [])
                    print("ItemList node found. itemListElement count:", len(elems))
                    if elems:
                        print("first elem:", json.dumps(elems[0], ensure_ascii=False)[:500])
                    break
    b.close()
