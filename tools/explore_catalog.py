import json, sys
from playwright.sync_api import sync_playwright

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
URL = "https://www.szlcsc.com/catalog.html"

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=EDGE, headless=True,
                                    args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                                               "Chrome/124.0 Safari/537.36"))
        page = ctx.new_page()
        page.on("response", lambda r: None)
        print("goto", URL)
        page.goto(URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(3000)
        # collect anchors
        anchors = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(e => ({
                t: (e.innerText||'').trim(),
                h: e.getAttribute('href')||'',
                c: e.getAttribute('class')||''
            }))"""
        )
        print("total anchors:", len(anchors))
        # filter category-like
        cat_like = [a for a in anchors if a['h'] and ('categ' in a['h'].lower() or 'catalog' in a['h'].lower() or '/c/' in a['h'].lower())]
        print("category-like anchors:", len(cat_like))
        for a in cat_like[:40]:
            print("  ", a['t'][:40], "||", a['h'][:80])
        # save html
        html = page.content()
        open(r"C:/Users/Administrator.SC-202105071542/Desktop/szprocure-site/tools/_lcsc_catalog.html","w",encoding="utf-8").write(html)
        print("html bytes:", len(html))
        browser.close()

if __name__ == "__main__":
    main()
