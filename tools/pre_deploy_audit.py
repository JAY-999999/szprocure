"""Pre-Deploy Audit for SZ Procure static site — PERMANENT release gate.

Checks (per user 2026-08-27, finalized):
  1. Real MPN / No test-synthetic data (MCU100xxx, TEST, SAMPLE, pure-numeric, etc.)
  2. LCSC leak detection (lcsc.com / www / assets in any deployable output)
  3. URL / Sitemap check (product count vs master; slug ASCII; sitemap coverage; no CJK in URLs)
  4. Schema check (every product page has valid JSON-LD + a Product schema; no Chinese in schema)
  5. Random product-page spot-check: Title / H1 / Schema / RFQ
  6. Components hub safety (hand-written, git-tracked, generator cannot delete)
  7. CJK / English-layer gate (PERMANENT):
       BLOCK  -> Chinese in any deployable user-visible content
                 (products/*, manufacturers/*, components/*, root pages,
                  parts.json, search/*.json, sitemap*.xml, assets css/js)
       ALLOW  -> source-code comments, data-zh* i18n metadata, getLang zh-branch
                 (hidden bilingual layer, never rendered in EN-default view),
                 and non-deployable files (tools/, data/, README, *.md dev docs)

Run:  python tools/pre_deploy_audit.py
Exit 0 = PASS, 1 = FAIL.  Report also written to D:\\SZ Procure\\04_Audit_Report.
"""
import csv, os, re, glob, random, json, sys, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")

# ---- forbidden tokens for production output (audit focus) ----
FORBIDDEN = [
    ("lcsc.com (any sub)", re.compile(r"lcsc\.com", re.I)),
    ("www.lcsc.com", re.compile(r"www\.lcsc\.com", re.I)),
    ("assets.lcsc.com", re.compile(r"assets\.lcsc\.com", re.I)),
    ("MCU100xxx family", re.compile(r"MCU100", re.I)),
    ("100000xxx family", re.compile(r"100000\d{3}")),
    ("TEST token (data)", re.compile(r"(?<!\.)\bTEST\b", re.I)),
    ("SAMPLE token", re.compile(r"\bSAMPLE\b")),
    ("PLACEHOLDER (data)", re.compile(r"PLACEHOLDER")),
    ("XXX suffix", re.compile(r"XXX$", re.I)),
]
SYNTHETIC = [
    re.compile(r'^(MCU|MOS|RES| *CAP|IND|DIO|CON|XTAL|MEM|WIFI|MOD|REG|AMP|OP|LED|PWR|IC)\d{6}', re.I),
    re.compile(r'100000\d{3}'),
    re.compile(r'^\d{6,}$'),
    re.compile(r'PLACEHOLDER', re.I),
    re.compile(r'XXX$', re.I),
    re.compile(r'_(TEST|SAMPLE|MOCK)$', re.I),
]
FAKE_BRAND = re.compile(r'(Acme|Nova|Placeholder|Synthetic|Mock|Fake|TestCorp|DemoSemi|Injected)', re.I)

# Deployable output scopes — files that actually ship to Vercel and get rendered.
# Excludes source/provenance dirs (data/, tools/, .git, node_modules, .workbuddy)
# which may legitimately carry LCSC source_url for traceability (not rendered),
# and dev docs (README*, *.md) which may carry Chinese (user-approved, non-display).
EXCLUDE_DIRS = {".git", "node_modules", "tools", ".workbuddy", "data"}

# ---- CJK / English-layer gate (PERMANENT) ----
# The English storefront must carry ZERO *visible* Chinese characters.
# We MASK the hidden bilingual layer (not rendered in EN-default view) before
# scanning; any residual CJK in the visible text/JSON is a FAIL. This catches
# LCSC-source Chinese that slipped past normalize (defense-in-depth) and prevents
# recurrence at 5000 / 50000 SKU scale.
CJK = re.compile(r"[一-鿿]")
# Mask ALL `data-zh*` i18n metadata (data-zh, data-zh-ph, data-zh-ph-*, ...):
# intentional bilingual toggle attributes, hidden in EN-default view via applyLang().
DATAZH = re.compile(r'data-zh[a-z-]*\s*=\s*(?:"[^"]*"|\'[^\']*\')', re.I)
# Mask source-code + HTML comments (// , /* */ , <!-- -->): comments are not rendered.
COMMENT = re.compile(r'//[^\n]*|/\*.*?\*/|<!--.*?-->', re.S)
# Mask JS-side zh-only branch literals: bilingual validation strings live in
# `(getLang() === "zh") ? "Chinese" : "English"` ternaries. The zh literal is the
# JS counterpart of `data-zh`, never rendered in production (EN-only) view.
ZH_BRANCH = re.compile(r'(getLang\(\)\s*===\s*"zh"\s*\)\s*\?\s*)("[^"]*"|\'[^\']*\')(\s*:)')


def iter_deploy_files():
    """Yield absolute paths of deployable output files (user-visible)."""
    files = []
    for fp in glob.glob(os.path.join(ROOT, "**", "*.html"), recursive=True):
        rel = os.path.relpath(fp, ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.split(os.sep)):
            continue
        base = os.path.basename(fp).lower()
        if base.startswith("readme") or base.endswith(".md"):
            continue
        files.append(fp)
    for fp in glob.glob(os.path.join(ROOT, "assets", "**", "*"), recursive=True):
        if fp.endswith((".css", ".js")):
            files.append(fp)
    for name in ("sitemap.xml", "sitemap_parts.xml", "sitemap_parts_index.xml", "robots.txt", "parts.json"):
        fp = os.path.join(ROOT, name)
        if os.path.exists(fp):
            files.append(fp)
    for fp in glob.glob(os.path.join(ROOT, "search", "*.json")):
        files.append(fp)
    return files


def load_master():
    with open(MASTER, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def audit_mpn(rows):
    """Return (failures, stats). Fake-data / synthetic-MPN detection."""
    stats = {"rows": len(rows), "empty_mpn": 0, "empty_mfr": 0, "empty_cat": 0, "empty_desc": 0}
    bad = []
    for i, r in enumerate(rows, 1):
        mpn = (r.get("mpn") or "").strip()
        mfr = (r.get("manufacturer") or "").strip()
        cat = (r.get("category") or "").strip()
        desc = (r.get("description") or "").strip()
        if not mpn: stats["empty_mpn"] += 1
        if not mfr: stats["empty_mfr"] += 1
        if not cat: stats["empty_cat"] += 1
        if not desc: stats["empty_desc"] += 1
        hit = None
        for pat in SYNTHETIC:
            if pat.search(mpn):
                hit = "synthetic MPN pattern " + pat.pattern
                break
        if hit is None and FAKE_BRAND.search(mfr):
            hit = "synthetic brand " + mfr
        if hit:
            bad.append((i, mpn or "(no mpn)", hit))
    return bad, stats


def audit_files():
    """LCSC leak + forbidden-token scan across deployable files."""
    files = iter_deploy_files()
    total = 0
    hits = {name: [] for name, _ in FORBIDDEN}
    for fp in files:
        try:
            data = open(fp, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        total += 1
        for name, rx in FORBIDDEN:
            found = rx.findall(data)
            if found:
                ctx = []
                for m in rx.finditer(data):
                    s = max(0, m.start() - 40); e = min(len(data), m.end() + 40)
                    ctx.append(data[s:e].replace("\n", " ")[:90])
                    if len(ctx) >= 3:
                        break
                hits[name].append((os.path.relpath(fp, ROOT), ctx))
    return total, hits


def product_slugs():
    d = os.path.join(ROOT, "products")
    if not os.path.isdir(d):
        return set()
    return {slug for slug in os.listdir(d)
            if os.path.isdir(os.path.join(d, slug)) and os.path.exists(os.path.join(d, slug, "index.html"))}


def audit_urls_sitemap():
    """URL / Sitemap check (read-only, never modifies)."""
    slugs = product_slugs()
    rows = load_master()
    issues = []
    # 1. product page count vs master
    if len(slugs) != len(rows):
        issues.append(f"product page count {len(slugs)} != master rows {len(rows)}")
    # 2. slug ASCII (no Chinese in URL)
    bad_slug = [s for s in slugs if not all(ord(c) < 128 for c in s)]
    if bad_slug:
        issues.append(f"{len(bad_slug)} product slugs contain non-ASCII (Chinese in URL): {bad_slug[:5]}")
    # 3. sitemap present + coverage + no CJK in locs
    sp = os.path.join(ROOT, "sitemap_parts.xml")
    locs = []
    if not os.path.exists(sp):
        issues.append("sitemap_parts.xml missing")
    else:
        txt = open(sp, encoding="utf-8").read()
        locs = re.findall(r"<loc>(.*?)</loc>", txt)
        cjk_locs = [l for l in locs if CJK.search(l)]
        if cjk_locs:
            issues.append(f"{len(cjk_locs)} sitemap <loc> contain Chinese (URL must be ASCII)")
        missing = [s for s in slugs if f"/products/{s}" not in txt]
        if missing:
            issues.append(f"{len(missing)} product URLs missing from sitemap_parts.xml: {missing[:5]}")
    # 4. index references parts sitemap
    idx = os.path.join(ROOT, "sitemap_parts_index.xml")
    if os.path.exists(idx):
        if "sitemap_parts.xml" not in open(idx, encoding="utf-8").read():
            issues.append("sitemap_parts_index.xml does not reference sitemap_parts.xml")
    else:
        issues.append("sitemap_parts_index.xml missing")
    return {"product_count": len(slugs), "master_count": len(rows),
            "sitemap_locs": len(locs), "issues": issues}


def audit_schema():
    """Schema check (read-only): valid JSON-LD + Product schema + no Chinese in schema."""
    slugs = sorted(product_slugs())
    total = len(slugs)
    pages_with_ld = 0
    pages_with_product = 0
    pages_schema_cjk = 0
    sample_bad = []
    for slug in slugs:
        html = open(os.path.join(ROOT, "products", slug, "index.html"), encoding="utf-8", errors="replace").read()
        blocks = re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I)
        if not blocks:
            if len(sample_bad) < 10:
                sample_bad.append((slug, "no JSON-LD"))
            continue
        pages_with_ld += 1
        has_product = False
        cjk = False
        for b in blocks:
            if CJK.search(b):
                cjk = True
            try:
                j = json.loads(b)
            except Exception:
                if len(sample_bad) < 10:
                    sample_bad.append((slug, "JSON-LD parse error"))
                continue
            if isinstance(j, dict) and j.get("@type") == "Product":
                has_product = True
        if has_product:
            pages_with_product += 1
        if cjk:
            pages_schema_cjk += 1
            if len(sample_bad) < 10 and not any(s == slug for s, _ in sample_bad):
                sample_bad.append((slug, "Chinese in schema"))
    issues = []
    if pages_with_ld != total:
        issues.append(f"{total - pages_with_ld} product pages missing any JSON-LD")
    if pages_with_product != total:
        issues.append(f"{total - pages_with_product} product pages missing a Product schema")
    if pages_schema_cjk:
        issues.append(f"{pages_schema_cjk} product pages have Chinese in JSON-LD schema")
    return {"total": total, "with_ld": pages_with_ld, "with_product": pages_with_product,
            "schema_cjk": pages_schema_cjk, "issues": issues, "sample_bad": sample_bad}


def audit_cjk(files):
    """CJK / English-layer gate (PERMANENT). BLOCK visible Chinese in deployable
    user-visible content; ALLOW hidden bilingual layer (comments + data-zh* + zh-branch)."""
    bad = []
    for fp in files:
        rel = os.path.relpath(fp, ROOT)
        try:
            data = open(fp, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        # Mask the hidden bilingual layer + comments (allowed, never rendered in EN view)
        masked = COMMENT.sub("", data)
        masked = DATAZH.sub("", masked)
        masked = ZH_BRANCH.sub(lambda m: m.group(1) + '""' + m.group(3), masked)
        if CJK.search(masked):
            ctx = []
            for m in CJK.finditer(masked):
                s = max(0, m.start() - 40); e = min(len(masked), m.end() + 40)
                ctx.append(masked[s:e].replace("\n", " ")[:90])
                if len(ctx) >= 3:
                    break
            bad.append((rel, ctx))
    return bad


def audit_random_pages(n=8):
    dirs = [d for d in glob.glob(os.path.join(ROOT, "products", "*")) if os.path.isdir(d)]
    random.seed(20260827)
    sample = random.sample(dirs, min(n, len(dirs)))
    results = []
    for d in sample:
        fp = os.path.join(d, "index.html")
        if not os.path.exists(fp):
            results.append({"slug": os.path.basename(d), "missing": True})
            continue
        html = open(fp, encoding="utf-8", errors="replace").read()
        title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        schema = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S)
        rfq = re.search(r'(request-a-quote\?[^\"\'>\s]+|Request a Quote|Submit RFQ)', html)

        def clean(x):
            if not x:
                return ""
            t = re.sub(r"<[^>]+>", " ", x.group(1) if hasattr(x, "group") else x)
            return re.sub(r"\s+", " ", t).strip()[:140]
        results.append({
            "slug": os.path.basename(d),
            "title": clean(title),
            "h1": clean(h1),
            "schema": bool(schema),
            "rfq": bool(rfq),
            "rfq_detail": (rfq.group(0)[:80] if rfq else ""),
        })
    return results


def audit_hub():
    hub = os.path.join(ROOT, "components", "index.html")
    info = {"exists": os.path.exists(hub)}
    try:
        out = subprocess.run(["git", "ls-files", "components/index.html"], cwd=ROOT, capture_output=True, text=True)
        info["git_tracked"] = "components/index.html" in out.stdout
    except Exception:
        info["git_tracked"] = "unknown"
    gp = open(os.path.join(ROOT, "gen_parts.py"), encoding="utf-8", errors="replace").read()
    info["generator_deletes"] = bool(re.search(r"rmtree|os\.remove|shutil\.rmtree", gp))
    return info


def main():
    rows = load_master()
    bad_mpn, stats = audit_mpn(rows)
    total_files, hits = audit_files()
    deploy_files = iter_deploy_files()
    cjk_bad = audit_cjk(deploy_files)
    pages = audit_random_pages(8)
    hub = audit_hub()
    url_info = audit_urls_sitemap()
    schema_info = audit_schema()

    lines = []
    lines.append("# SZ Procure - Pre-Deploy Audit Report (PERMANENT GATE)")
    lines.append("")
    lines.append("Generated: " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")

    # 1 MPN / fake data
    lines.append("## 1. Real MPN / No Test Data (假数据检测)")
    lines.append(f"- Master rows: **{stats['rows']}**")
    lines.append(f"- Empty MPN: {stats['empty_mpn']} | Empty Manufacturer: {stats['empty_mfr']} | Empty Category: {stats['empty_cat']} | Empty Description: {stats['empty_desc']}")
    if bad_mpn:
        lines.append(f"- ❌ **{len(bad_mpn)} synthetic/test MPNs detected:**")
        for i, mpn, why in bad_mpn[:30]:
            lines.append(f"    - row {i}: {mpn} ({why})")
    else:
        lines.append("- ✅ **0 synthetic/test MPNs** - all are real-part candidates.")
    lines.append("")

    # 2 LCSC leak
    lines.append("## 2. LCSC Leak Detection (LCSC泄漏检测)")
    lines.append(f"- Files scanned: **{total_files}**")
    any_leak = False
    for name, _ in FORBIDDEN:
        occ = hits[name]
        if occ:
            any_leak = True
            lines.append(f"- ❌ **{name}**: {sum(len(c) for _, c in occ)} hits across {len(occ)} files")
            for fp, ctx in occ[:3]:
                lines.append(f"    - `{fp}`: …{ctx[0]}…")
        else:
            lines.append(f"- ✅ {name}: 0 hits")
    lines.append("")
    lines.append("- Scope: deployable output only (generated HTML, sitemap*.xml, robots.txt, parts.json, search/*.json, assets css/js). Source/provenance dirs (data/, tools/) excluded - they may carry LCSC `source_url` for traceability but are NOT rendered.")
    lines.append("")

    # 3 URL / Sitemap
    lines.append("## 3. URL / Sitemap Check (URL/Sitemap检查)")
    lines.append(f"- Product pages: **{url_info['product_count']}** | Master rows: {url_info['master_count']} | Sitemap <loc>: {url_info['sitemap_locs']}")
    if url_info["issues"]:
        lines.append(f"- ❌ **{len(url_info['issues'])} issue(s):**")
        for x in url_info["issues"]:
            lines.append(f"    - {x}")
    else:
        lines.append("- ✅ Product count matches master; all slugs ASCII; every product URL present in sitemap_parts.xml; no Chinese in any <loc>; sitemap index references parts sitemap.")
    lines.append("")

    # 4 Schema
    lines.append("## 4. Schema Check (Schema检查)")
    lines.append(f"- Product pages: {schema_info['total']} | with JSON-LD: {schema_info['with_ld']} | with Product schema: {schema_info['with_product']} | Chinese in schema: {schema_info['schema_cjk']}")
    if schema_info["issues"]:
        lines.append(f"- ❌ **{len(schema_info['issues'])} issue(s):**")
        for x in schema_info["issues"]:
            lines.append(f"    - {x}")
        for slug, why in schema_info["sample_bad"][:5]:
            lines.append(f"    - e.g. `{slug}`: {why}")
    else:
        lines.append("- ✅ Every product page has valid JSON-LD and a Product schema; no Chinese in any schema block.")
    lines.append("")

    # 5 random pages
    lines.append("## 5. Random Product-Page Spot-Check (Title / H1 / Schema / RFQ)")
    for p in pages:
        if p.get("missing"):
            lines.append(f"- ❌ `{p['slug']}`: MISSING_PAGE")
            continue
        ok = bool(p["title"]) and bool(p["h1"]) and p["schema"] and p["rfq"]
        tag = "✅" if ok else "⚠️"
        lines.append(f"- {tag} **{p['slug']}**")
        lines.append(f"    - Title: {p['title'] or 'MISSING'}")
        lines.append(f"    - H1: {p['h1'] or 'MISSING'}")
        lines.append(f"    - Schema JSON-LD: {'present' if p['schema'] else 'MISSING'}")
        lines.append(f"    - RFQ: {'present' if p['rfq'] else 'MISSING'} {('('+p['rfq_detail']+')') if p['rfq_detail'] else ''}")
    lines.append("")

    # 6 hub
    lines.append("## 6. Components Hub Safety")
    lines.append(f"- `components/index.html` exists: {hub['exists']}")
    lines.append(f"- git-tracked: {hub['git_tracked']}")
    lines.append(f"- generator `rmtree`/`remove` present in gen_parts.py: {hub['generator_deletes']}")
    if hub["exists"] and hub["git_tracked"] and not hub["generator_deletes"]:
        lines.append("- ✅ Hub is hand-written, tracked, and the rebuild never deletes/overwrites it.")
    else:
        lines.append("- ⚠️ Hub safety needs manual confirmation.")
    lines.append("")

    # 7 CJK gate
    lines.append("## 7. CJK / English-Layer Gate (PERMANENT)")
    lines.append(f"- Deployable user-visible files scanned: **{len(deploy_files)}**")
    lines.append("- BLOCK: Chinese in products/*, manufacturers/*, components/*, root pages, parts.json, search/*.json, sitemap*.xml, assets css/js.")
    lines.append("- ALLOW (masked before scan): source-code comments, `data-zh*` i18n metadata, getLang zh-branch literals; non-deployable files (tools/, data/, README, *.md) excluded entirely.")
    if cjk_bad:
        lines.append(f"- ❌ **{len(cjk_bad)} files carry visible Chinese:**")
        for fp, ctx in cjk_bad[:15]:
            lines.append(f"    - `{fp}`: …{ctx[0]}…")
    else:
        lines.append("- ✅ **0 visible Chinese characters** across all deployable user-visible content. English-layer is Chinese-free.")
    lines.append("")

    # verdict
    url_ok = (not url_info["issues"])
    schema_ok = (not schema_info["issues"])
    pages_ok = all(bool(p.get("title")) and bool(p.get("h1")) and p.get("schema") and p.get("rfq") for p in pages)
    hub_ok = hub["exists"] and hub["git_tracked"] and not hub["generator_deletes"]
    ok = (not bad_mpn) and (not any_leak) and url_ok and schema_ok and pages_ok and hub_ok and (not cjk_bad)
    lines.append("## VERDICT")
    lines.append("- **" + ("✅ PASS - ready for commit/deploy" if ok else "❌ FAIL - resolve above first") + "**")
    report = "\n".join(lines)

    out_path = os.path.join("D:\\SZ Procure\\04_Audit_Report", "Phase_Master_v2.1_PreDeploy_Audit.md")
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(report)
    except Exception as e:
        report = report + f"\n\n(Report file write skipped: {e})"
    print(report)
    if os.path.exists(out_path):
        print(f"\nReport written: {out_path}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
