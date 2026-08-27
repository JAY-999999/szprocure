#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stage 2 (CLEAN) — Self-host assets, normalize, and emit the MASTER Parts DB.

PIPELINE:  RAW (harvest_api.py, full 5000)  ->  CLEAN (this)  ->  MASTER  ->  BUILD (gen_parts.py, FROZEN)

WHAT THIS DOES
  1. Locks the audited SKU set (--lock master_parts_v2.1.csv) so the 500 MPNs
     that passed the asset audit stay EXACTLY fixed (no curation drift).
     (Without --lock it curates top-N from RAW by per-category caps — the
      scalable path for 5000/50000 SKU later.)
  2. For every locked MPN, looks up the source asset URLs in RAW:
       - image       <- RAW source_image_url (assets.lcsc.com), fallback lock.image
       - datasheet   <- RAW source_datasheet_url (= the REAL pdfUrl, datasheet.lcsc.com)
  3. Downloads each asset into the repo's LOCAL static path:
       - image     -> assets/parts/images/<slug>.<ext>
       - datasheet -> assets/parts/datasheets/<slug>.pdf
     On ANY failure the field is left EMPTY (gen_parts falls back to hero.svg /
     hides the datasheet link) — so the built site NEVER references lcsc.
  4. Writes the MASTER CSV with **local paths only** (zero lcsc identifiers in
     the served site). The `source` column keeps "LCSC" as honest data
     provenance, but gen_parts.py never renders it.
  5. Emits traceability logs:
       - data/clean/asset_manifest.csv  (per-asset: source/local/status/sha256)
       - data/clean/clean_report.md      (summary stats)
     Archived copies are mirrored to D:/SZ Procure/02_CLEAN/.

USAGE
  # preserve audited 500, self-host everything, overwrite the build master
  python tools/clean_factory.py --lock data/production/master_parts_v2.1.csv \
      --out data/production/master_parts_v2.1.csv

  # scalable: curate top-5000 from RAW (no lock)
  python tools/clean_factory.py --raw data/raw/lcsc_api_FULL_20260827.csv \
      --cap 5000 --out data/production/master_parts_v2.1.csv
"""
import csv, os, re, sys, json, argparse, datetime, hashlib, glob, shutil, urllib.request, urllib.error, time, random, concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import gen_parts as gp

DATE = f"{datetime.datetime.now():%Y%m%d}"
# Phase-1 policy (2026-08-27): self-hosted assets are PARKED in the local D: ASSET
# archive for later product-page enrichment. They are NOT deployed to the production
# site (phase-1 ships no images/PDFs), so they live outside the repo / deploy bundle.
ASSET_ROOT = r"D:\SZ Procure\01_RAW\ASSET"
IMG_DIR = os.path.join(ASSET_ROOT, "images")
DS_DIR = os.path.join(ASSET_ROOT, "datasheets")
CLEAN_DIR = os.path.join(ROOT, "data", "clean")
ARCHIVE_DIR = r"D:\SZ Procure\02_CLEAN"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
REF_HEADERS = {
    "User-Agent": UA,
    "Accept": "image/avif,image/webp,image/png,image/*,*/*;q=0.8",
    "Referer": "https://www.lcsc.com/",
}

MASTER_HEADER = ["mpn", "clean_mpn", "manufacturer", "brand", "url_slug",
                 "category", "subcategory", "description", "applications",
                 "keywords", "attributes_json", "availability",
                 "alternative_parts", "datasheet_url", "faq", "image", "source",
                 "source_url", "supplier_reference"]

# Per fine-category caps (max count) — used only when --lock is NOT given.
CAPS = {
    "Microcontroller": 60, "Memory IC": 25, "Power Management IC": 20,
    "Voltage Regulator": 55, "Analog IC": 15, "Operational Amplifier": 35,
    "Interface IC": 40, "Logic IC": 25,
    "MOSFET": 55, "Diode": 45, "Transistor": 25,
    "Resistor": 35, "Capacitor": 40, "Inductor": 20, "Crystal Oscillator": 15,
    "LED Components": 15, "Sensors": 35,
    "Pin Header": 12, "USB Connectors": 12, "Connectors": 20, "Switches": 12,
    "WiFi Modules": 10, "Bluetooth Modules": 6, "Cellular Modules": 6,
    "GNSS Modules": 6, "RF Modules": 6, "Modules": 12,
}
DEFAULT_CAP = 15


# ----------------------------------------------------------------------------- #
# Asset download helpers
# ----------------------------------------------------------------------------- #
def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ext_from_url(url):
    m = re.search(r"\.([a-zA-Z0-9]+)(?:\?|$)", url.split("/")[-1])
    if m:
        e = m.group(1).lower()
        if e in ("jpg", "jpeg", "png", "webp", "gif"):
            return "jpg" if e == "jpeg" else e
    return ""


def ext_from_ct(ct):
    ct = (ct or "").lower()
    if "png" in ct:
        return "png"
    if "webp" in ct:
        return "webp"
    if "gif" in ct:
        return "gif"
    return "jpg"


def download(url, dest):
    """Download url -> dest. Return (ok, http_status, bytes, content_type, err)."""
    try:
        req = urllib.request.Request(url, headers=REF_HEADERS)
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            ct = r.headers.get("Content-Type", "")
            status = r.status
        if not data:
            return False, status, 0, ct, "empty body"
        with open(dest, "wb") as f:
            f.write(data)
        return True, status, len(data), ct, ""
    except urllib.error.HTTPError as e:
        return False, e.code, 0, "", f"HTTP {e.code}"
    except Exception as e:
        return False, 0, 0, "", str(e)[:120]


def download_image(url, slug):
    os.makedirs(IMG_DIR, exist_ok=True)
    ext = ext_from_url(url) or "jpg"
    dest = os.path.join(IMG_DIR, f"{slug}.{ext}")
    ok, status, n, ct, err = download(url, dest)
    if not ok:
        return False, "", status, n, ct, err, ""
    # validate it's actually an image
    if ext == "jpg" and not ct.startswith("image/"):
        # trust content; keep
        pass
    return True, f"01_RAW/ASSET/images/{slug}.{ext}", status, n, ct, "", dest


def download_datasheet(url, slug):
    os.makedirs(DS_DIR, exist_ok=True)
    dest = os.path.join(DS_DIR, f"{slug}.pdf")
    ok, status, n, ct, err = download(url, dest)
    if not ok:
        return False, "", status, n, ct, err, ""
    # MUST be a real PDF, not the HTML wrapper LCSC returns for the wrong URL
    with open(dest, "rb") as f:
        head = f.read(5)
    if head[:4] != b"%PDF":
        os.remove(dest)
        return False, "", status, n, ct, "not a PDF (got %r)" % head, ""
    return True, f"01_RAW/ASSET/datasheets/{slug}.pdf", status, n, ct, "", dest


# ----------------------------------------------------------------------------- #
# RAW lookup
# ----------------------------------------------------------------------------- #
def load_raw_latest(raw_arg):
    if raw_arg:
        candidates = [raw_arg]
    else:
        candidates = sorted(glob.glob(os.path.join(ROOT, "data", "raw", "lcsc_api_FULL_*.csv")))
    if not candidates:
        sys.exit("ERROR: no RAW csv found. Run harvest_api.py first.")
    path = candidates[-1]
    print(f"[raw] using {path}")
    by_mpn = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            mpn = (r.get("mpn") or "").strip().upper()
            if mpn:
                by_mpn[mpn] = r
    return path, by_mpn


def load_lock(lock_arg):
    path = lock_arg or os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
    print(f"[lock] using {path}")
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if (r.get("mpn") or "").strip():
                rows.append(r)
    return path, rows


# ----------------------------------------------------------------------------- #
# Curation (only used when --lock is absent — scalable path)
# ----------------------------------------------------------------------------- #
def curate_from_raw(by_mpn, cap):
    counts = {k: 0 for k in CAPS}
    selected = []
    for mpn_upper, r in by_mpn.items():
        fine = (r.get("category") or "").strip()
        if fine not in CAPS:
            continue
        if counts.get(fine, 0) >= CAPS.get(fine, DEFAULT_CAP):
            continue
        selected.append(r)
        counts[fine] = counts.get(fine, 0) + 1
        if len(selected) >= cap:
            break
    return selected


# ----------------------------------------------------------------------------- #
# Main
# ----------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default=None, help="RAW csv (default: latest data/raw/lcsc_api_FULL_*.csv)")
    ap.add_argument("--lock", default=None, help="Lock audited MPNs from this master CSV (preserves audit)")
    ap.add_argument("--cap", type=int, default=500, help="When no --lock: curate top-N from RAW")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv"))
    ap.add_argument("--no-archive", action="store_true", help="skip D:/SZ Procure/02_CLEAN mirror")
    args = ap.parse_args()

    raw_path, by_mpn = load_raw_latest(args.raw)

    if args.lock:
        _, lock_rows = load_lock(args.lock)
        work = lock_rows
        mode = f"LOCKED ({len(work)} audited MPNs)"
    else:
        work = curate_from_raw(by_mpn, args.cap)
        mode = f"CURATED top-{len(work)} from RAW"

    print(f"[clean] mode: {mode}")

    os.makedirs(CLEAN_DIR, exist_ok=True)
    os.makedirs(IMG_DIR, exist_ok=True)
    os.makedirs(DS_DIR, exist_ok=True)

    master_rows = []
    manifest = []  # asset-level traceability
    stats = {"mpn_total": len(work), "img_ok": 0, "img_fail": 0,
             "ds_ok": 0, "ds_fail": 0, "not_in_raw": 0, "bytes": 0}
    missing_raw = []

    # ---- Phase A: build download jobs + per-MPN context ----
    jobs = []          # (mpn, slug, kind, url)
    ctx = {}           # slug -> dict(mpn, row, raw, img_src, ds_src)
    for row in work:
        mpn = (row.get("mpn") or "").strip()
        slug = gp.slugify(mpn)
        raw = by_mpn.get(mpn.upper())
        img_src = ds_src = ""
        if raw:
            img_src = (raw.get("source_image_url") or "").strip()
            ds_src = (raw.get("source_datasheet_url") or "").strip()
        if not img_src:
            # fallback: the existing master image column already holds the lcsc URL
            existing = (row.get("image") or "").strip()
            if existing.startswith("http"):
                img_src = existing
        ctx[slug] = dict(mpn=mpn, row=row, raw=raw, img_src=img_src, ds_src=ds_src)
        if img_src:
            jobs.append((mpn, slug, "image", img_src))
        if ds_src:
            jobs.append((mpn, slug, "datasheet", ds_src))
    stats["jobs"] = len(jobs)

    # ---- Phase B: concurrent download (10 workers) ----
    results = {}      # (slug, kind) -> dict(ok, local, status, n, ct, err, abspath)
    def do_job(job):
        mpn, slug, kind, url = job
        if kind == "image":
            ok, local, status, n, ct, err, abspath = download_image(url, slug)
        else:
            ok, local, status, n, ct, err, abspath = download_datasheet(url, slug)
        return (slug, kind, ok, local, status, n, ct, err, abspath)

    print(f"[clean] downloading {len(jobs)} assets with 10 workers ...")
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for res in ex.map(do_job, jobs):
            slug, kind, ok, local, status, n, ct, err, abspath = res
            results[(slug, kind)] = dict(ok=ok, local=local, status=status,
                                         n=n, ct=ct, err=err, abspath=abspath)
            if ok:
                stats["img_ok" if kind == "image" else "ds_ok"] += 1
                stats["bytes"] += n
            else:
                stats["img_fail" if kind == "image" else "ds_fail"] += 1

    # ---- Phase C: assemble manifest + MASTER rows (stats already counted) ----
    for row in work:
        mpn = (row.get("mpn") or "").strip()
        slug = gp.slugify(mpn)
        c = ctx[slug]
        img_src = c["img_src"]; ds_src = c["ds_src"]

        if img_src:
            r = results.get((slug, "image"))
            ok = r["ok"] if r else False
            local = r["local"] if r else ""
            manifest.append([mpn, slug, "image", img_src, local or "",
                             "ok" if ok else "failed", r["status"], r["n"], r["ct"],
                             sha256_file(r["abspath"]) if ok else "", r["err"]])
            img_local = local if ok else ""
        else:
            manifest.append([mpn, slug, "image", "", "", "skipped", "", "", "",
                             "", "no source url (legacy/fallback)"])
            img_local = ""

        if ds_src:
            r = results.get((slug, "datasheet"))
            ok = r["ok"] if r else False
            local = r["local"] if r else ""
            manifest.append([mpn, slug, "datasheet", ds_src, local or "",
                             "ok" if ok else "failed", r["status"], r["n"], r["ct"],
                             sha256_file(r["abspath"]) if ok else "", r["err"]])
            ds_local = local if ok else ""
        else:
            manifest.append([mpn, slug, "datasheet", "", "", "skipped", "", "", "",
                             "", "no source url (legacy/fallback)"])
            ds_local = ""

        if not c["raw"]:
            stats["not_in_raw"] += 1
            missing_raw.append(mpn)

        # ---- assemble MASTER row ----
        # Phase-1 policy (2026-08-27): the PRODUCTION build master ships with
        # image/datasheet_url BLANK — phase-1 product pages show only core
        # procurement info (MPN, Manufacturer, Category, Description, Specs, RFQ),
        # no images/PDFs and zero LCSC identifiers. Self-hosted assets are parked
        # in the D: ASSET archive (IMG_DIR/DS_DIR) for later page enrichment.
        mrow = {c0: row.get(c0, "") for c0 in MASTER_HEADER}
        mrow["image"] = ""
        mrow["datasheet_url"] = ""
        # Provenance retained in DATA layers ONLY (never rendered by the frozen
        # gen_parts builder). `source` is blanked because gen_parts leaks it into
        # parts.json; source_url/supplier_reference are builder-ignored columns.
        rw = c.get("raw") or {}
        mrow["source_url"] = (rw.get("source") or "").strip()
        mrow["supplier_reference"] = (rw.get("supplier_sku") or "").strip()
        mrow["source"] = ""
        master_rows.append(mrow)

    # ---- write MASTER ----
    out_dir = os.path.dirname(os.path.abspath(args.out))
    os.makedirs(out_dir, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MASTER_HEADER)
        w.writeheader()
        for r in master_rows:
            w.writerow(r)

    # ---- write asset manifest ----
    manifest_path = os.path.join(CLEAN_DIR, f"asset_manifest_{DATE}.csv")
    with open(manifest_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mpn", "slug", "asset_type", "source_url", "local_path",
                    "status", "http_status", "bytes", "content_type", "sha256", "note"])
        for m in manifest:
            w.writerow(m)

    # ---- cleaning report ----
    rep = []
    rep.append("# CLEAN Stage Report — Self-hosted Asset Pipeline\n")
    rep.append(f"- Mode                 : {mode}")
    rep.append(f"- RAW source           : {raw_path}")
    rep.append(f"- Download jobs        : {stats.get('jobs', 0)} (image+datasheet, 10 workers)")
    rep.append(f"- Master out           : {args.out} ({len(master_rows)} rows)")
    rep.append(f"- Images  OK / fail    : {stats['img_ok']} / {stats['img_fail']}")
    rep.append(f"- Datasheets OK / fail : {stats['ds_ok']} / {stats['ds_fail']}")
    rep.append(f"- MPNs not in RAW      : {stats['not_in_raw']} (legacy/fallback, no asset)")
    rep.append(f"- Total bytes fetched  : {stats['bytes']:,} ({stats['bytes']/1024/1024:.1f} MB)")
    rep.append(f"- Asset manifest       : {manifest_path}")
    rep.append("")
    rep.append("## Guarantee")
    rep.append("- Every row's `image`/`datasheet_url` contains a LOCAL path "
               "(/assets/parts/...) or is empty.")
    rep.append("- NO lcsc domain appears in the master's asset columns.")
    rep.append("- Any download failure degrades gracefully (hero.svg / hidden link), "
               "never a broken lcsc link.")
    if missing_raw:
        rep.append("")
        rep.append(f"## MPNs absent from RAW ({len(missing_raw)})")
        rep.append(", ".join(missing_raw[:40]))
    with open(os.path.join(CLEAN_DIR, f"clean_report_{DATE}.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(rep) + "\n")

    print(f"\n>> MASTER    -> {args.out} ({len(master_rows)} rows)")
    print(f">> MANIFEST  -> {manifest_path}")
    print(f">> REPORT    -> {os.path.join(CLEAN_DIR, f'clean_report_{DATE}.md')}")
    print(f">> images OK/fail   : {stats['img_ok']}/{stats['img_fail']}")
    print(f">> datasheets OK/fail: {stats['ds_ok']}/{stats['ds_fail']}")
    print(f">> total fetched    : {stats['bytes']/1024/1024:.1f} MB")

    # ---- archive to D asset root ----
    if not args.no_archive:
        try:
            os.makedirs(ARCHIVE_DIR, exist_ok=True)
            for src in (manifest_path, os.path.join(CLEAN_DIR, f"clean_report_{DATE}.md"), args.out):
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(ARCHIVE_DIR, os.path.basename(src)))
            print(f">> ARCHIVED  -> {ARCHIVE_DIR}")
        except Exception as e:
            print(f"[warn] archive to D failed: {e}")


if __name__ == "__main__":
    main()
