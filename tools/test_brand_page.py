#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import sys
from playwright.sync_api import sync_playwright
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
URL = "https://list.szlcsc.com/brand/11353.html"
with sync_playwright() as p:
    b = p.chromium.launch(executable_path=EDGE, headless=True)
    pg = b.new_context(user_agent=UA, locale="en-US").new_page()
    pg.goto(URL, wait_until="domcontentloaded", timeout=40000)
    pg.wait_for_timeout(4000)
    html = pg.content()
    print("title:", pg.title(), "len:", len(html))
    print("captcha?", "t.captcha.qq.com" in html or "captcha" in html.lower())
    # any product links?
    import re
    print("item.szlcsc links:", len(re.findall(r'item\.szlcsc\.com', html)))
    b.close()
