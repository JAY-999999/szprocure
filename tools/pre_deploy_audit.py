"""Pre-Deploy Audit for SZ Procure static site.

Checks (per user 2026-08-27):
  1. 500 SKUs are real MPNs, no test/synthetic residue (MCU100xxx, TEST, SAMPLE, pure-numeric, etc.)
  2. Full scan of production files (HTML / JSON / sitemap / search / static css+js)
     for lcsc.com / www.lcsc.com / assets.lcsc.com / TEST / MCU100xxx.
  3. Random product-page spot-check: Title / H1 / JSON-LD Schema / RFQ link.
  4. components/ hub safety: hand-written, git-tracked, generator cannot delete/overwrite.

Run:  python tools/pre_deploy_audit.py
Exit 0 = pass, 1 = fail.
"""
import csv, os, re, glob, random, json, sys

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
# Synthetic MPN patterns used by gen_parts (mirrored for an independent audit line)
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
# which may legitimately carry LCSC source_url for traceability (not rendered).
EXCLUDE_DIRS = {".git", "node_modules", "tools", ".workbuddy", "data"}

def iter_deploy_files():
    """Yield absolute paths of deployable output files."""
    files = []
    # generated HTML (products/*, manufacturers/*, components/*, root pages)
    for fp in glob.glob(os.path.join(ROOT, "**", "*.html"), recursive=True):
        rel = os.path.relpath(fp, ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.split(os.sep)):
            continue
        files.append(fp)
    # static assets
    for fp in glob.glob(os.path.join(ROOT, "assets", "**", "*"), recursive=True):
        if fp.endswith((".css", ".js")):
            files.append(fp)
    # root-level data outputs
    for name in ("sitemap.xml", "robots.txt", "parts.json"):
        fp = os.path.join(ROOT, name)
        if os.path.exists(fp):
            files.append(fp)
    for fp in glob.glob(os.path.join(ROOT, "search", "*.json")):
        files.append(fp)
    return files

def load_master():
    rows = []
    with open(MASTER, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows

def audit_mpn(rows):
    """Return (failures, stats)."""
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
                # collect up to 3 contexts
                ctx = []
                for m in rx.finditer(data):
                    s = max(0, m.start() - 40); e = min(len(data), m.end() + 40)
                    ctx.append(data[s:e].replace("\n", " ")[:90])
                    if len(ctx) >= 3: break
                hits[name].append((os.path.relpath(fp, ROOT), ctx))
    return total, hits

def audit_random_pages(n=8):
    product_dirs = [d for d in glob.glob(os.path.join(ROOT, "products", "*"))
                    if os.path.isdir(d)]
    random.seed(20260827)
    sample = random.sample(product_dirs, min(n, len(product_dirs)))
    results = []
    for d in sample:
        fp = os.path.join(d, "index.html")
        if not os.path.exists(fp):
            results.append((os.path.basename(d), "MISSING_PAGE", {}, "", "", "", ""))
            continue
        html = open(fp, encoding="utf-8", errors="replace").read()
        title = re.search(r"<title[^>]*>(.*?)</title>", html, re.S)
        h1 = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
        schema = re.search(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S)
        rfq = re.search(r'(request-a-quote\?[^\"\'>\s]+|Request a Quote|Submit RFQ)', html)
        # strip tags for display
        def clean(x):
            if not x: return ""
            t = re.sub(r"<[^>]+>", " ", x.group(1) if hasattr(x,'group') else x)
            return re.sub(r"\s+", " ", t).strip()[:140]
        results.append({
            "slug": os.path.basename(d),
            "title": clean(title),
            "h1": clean(h1),
            "schema": (schema.group(1).strip()[:200] if schema else ""),
            "rfq": bool(rfq),
            "rfq_detail": (rfq.group(0)[:80] if rfq else ""),
        })
    return results

def audit_hub():
    hub = os.path.join(ROOT, "components", "index.html")
    info = {}
    info["exists"] = os.path.exists(hub)
    # git-tracked?
    import subprocess
    try:
        out = subprocess.run(["git", "ls-files", "components/index.html"],
                             cwd=ROOT, capture_output=True, text=True)
        info["git_tracked"] = "components/index.html" in out.stdout
    except Exception:
        info["git_tracked"] = "unknown"
    # generator deletes? scan gen_parts for rmtree/remove of components
    gp = open(os.path.join(ROOT, "gen_parts.py"), encoding="utf-8", errors="replace").read()
    info["generator_deletes"] = bool(re.search(r"rmtree|os\.remove|shutil\.rmtree", gp))
    return info

def main():
    rows = load_master()
    bad_mpn, stats = audit_mpn(rows)
    total_files, hits = audit_files()
    pages = audit_random_pages(8)
    hub = audit_hub()

    # ---- render report ----
    lines = []
    lines.append("# SZ Procure — Pre-Deploy Audit Report")
    lines.append("")
    lines.append("Generated: " + __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("")

    # 1 MPN
    lines.append("## 1. Real MPN / No Test Data")
    lines.append(f"- Master rows: **{stats['rows']}**")
    lines.append(f"- Empty MPN: {stats['empty_mpn']} | Empty Manufacturer: {stats['empty_mfr']} | Empty Category: {stats['empty_cat']} | Empty Description: {stats['empty_desc']}")
    if bad_mpn:
        lines.append(f"- ❌ **{len(bad_mpn)} synthetic/test MPNs detected:**")
        for i, mpn, why in bad_mpn[:30]:
            lines.append(f"    - row {i}: {mpn} ({why})")
    else:
        lines.append("- ✅ **0 synthetic/test MPNs** — all 500 are real-part candidates.")
    lines.append("")

    # 2 file scan
    lines.append("## 2. Full Production-File Scan (lcsc / TEST / MCU100xxx)")
    lines.append(f"- Files scanned: **{total_files}**")
    any_leak = False
    for name, _ in FORBIDDEN:
        occ = hits[name]
        if occ:
            any_leak = True
            lines.append(f"- ❌ **{name}**: {sum(len(c) for _,c in occ)} hits across {len(occ)} files")
            for fp, ctx in occ[:3]:
                lines.append(f"    - `{fp}`: …{ctx[0]}…")
        else:
            lines.append(f"- ✅ {name}: 0 hits")
    lines.append("")
    lines.append("- Scope: deployable output only (generated HTML, sitemap.xml, robots.txt, parts.json, search/*.json, assets css/js). Source/provenance dirs (data/, tools/) are excluded — they may carry LCSC `source_url` for traceability but are NOT rendered.")
    lines.append("- `TEST` excludes JS `.test()`; `PLACEHOLDER` is case-sensitive (CSS `::placeholder`/HTML `placeholder=` are benign UI, not data leakage).")
    lines.append("")

    # 3 random pages
    lines.append("## 3. Random Product-Page Spot-Check (Title / H1 / Schema / RFQ)")
    for p in pages:
        if isinstance(p, tuple):
            lines.append(f"- ❌ `{p[0]}`: {p[1]}")
            continue
        ok = bool(p["title"]) and bool(p["h1"]) and bool(p["schema"]) and p["rfq"]
        tag = "✅" if ok else "⚠️"
        lines.append(f"- {tag} **{p['slug']}**")
        lines.append(f"    - Title: {p['title'] or 'MISSING'}")
        lines.append(f"    - H1: {p['h1'] or 'MISSING'}")
        lines.append(f"    - Schema JSON-LD: {'present' if p['schema'] else 'MISSING'}")
        lines.append(f"    - RFQ: {'present' if p['rfq'] else 'MISSING'} {('('+p['rfq_detail']+')') if p['rfq_detail'] else ''}")
    lines.append("")

    # 4 hub
    lines.append("## 4. Components Hub Safety")
    lines.append(f"- `components/index.html` exists: {hub['exists']}")
    lines.append(f"- git-tracked: {hub['git_tracked']}")
    lines.append(f"- generator `rmtree`/`remove` present in gen_parts.py: {hub['generator_deletes']} (if False → hub never deleted by rebuild)")
    if hub["exists"] and hub["git_tracked"] and not hub["generator_deletes"]:
        lines.append("- ✅ Hub is hand-written, tracked, and the rebuild never deletes/overwrites it.")
    else:
        lines.append("- ⚠️ Hub safety needs manual confirmation.")
    lines.append("")

    # verdict
    ok = (not bad_mpn) and (not any_leak) and all(
        isinstance(p, dict) and bool(p["title"]) and bool(p["h1"]) and bool(p["schema"]) and p["rfq"]
        for p in pages) and hub["exists"] and hub["git_tracked"] and not hub["generator_deletes"]
    lines.append("## VERDICT")
    lines.append("- **" + ("✅ PASS — ready for commit/deploy" if ok else "❌ FAIL — resolve above first") + "**")
    report = "\n".join(lines)

    # emit report file
    out_path = os.path.join("D:\\SZ Procure\\04_Audit_Report", "Phase_Master_v2.1_PreDeploy_Audit.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(report)

    print(report)
    print(f"\nReport written: {out_path}")
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
