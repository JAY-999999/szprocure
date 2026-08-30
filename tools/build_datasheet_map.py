"""Build the authoritative MPN -> Datasheet(PDF) unique mapping for SZ Procure.

Shadow-only tool. Reads the 489 local PDFs in the asset mirror + the 500-row
master, computes SHA256/size live, and produces a single source-of-truth map:

    D:/SZ Procure/02_CLEAN/datasheet_map.csv
    D:/SZ Procure/04_Audit_Report/datasheet_map_report.md

Matching rule (per project convention, 2026-08-27):
  * key = MPN (the canonical part number in the master).
  * A PDF is matched to a row when its FILENAME STEM equals the row's `mpn`
    (case-insensitive) or, failing that, the row's `clean_mpn`.
  * The R2 object KEY is always derived from `mpn` (lower-cased, alnum/./-/_),
    never from the source filename -- so one deterministic URL per SKU,
    regardless of whether the local file was named by mpn or clean_mpn.
  * 11 SKUs with no local PDF keep an EMPTY r2_url (no fake link, no placeholder).

The mapping is the ONLY thing that decides which SKU gets a datasheet button.
gen_parts.py already renders the button conditionally from `datasheet_url`,
which apply_datasheet_map.py fills from this map.

Run:  python tools/build_datasheet_map.py
Exit 0 = built (mapping always built; report lists mismatches/dupes/missing).
"""
import csv, os, re, hashlib, sys, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PDF_DIR = "D:/SZ Procure/01_RAW/ASSET/datasheets"
MASTER_C = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
MASTER_D = "D:/SZ Procure/03_MASTER/product_master/master_parts_v2.1.csv"
OUT_CSV = "D:/SZ Procure/02_CLEAN/datasheet_map.csv"
REPORT = "D:/SZ Procure/04_Audit_Report/datasheet_map_report.md"

# R2 public base. Configurable so the URL is correct when creds are supplied.
R2_PUBLIC_BASE = os.environ.get("SZ_R2_PUBLIC_BASE", "https://static.szprocure.com/datasheets").rstrip("/")

KEY_SAFE = re.compile(r"[^a-z0-9._-]")


def r2_key(mpn: str) -> str:
    return KEY_SAFE.sub("-", mpn.strip().lower())


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    # 1. index local PDFs: stem(lower) -> [(realpath, size)]
    pdf_files = [f for f in os.listdir(PDF_DIR) if f.lower().endswith(".pdf")]
    stem_map = {}
    norm_stem_map = {}  # alphanumeric-only normalized stem -> [paths]
    for f in pdf_files:
        stem = f[:-4].lower()
        stem_map.setdefault(stem, []).append(os.path.join(PDF_DIR, f))
        nstem = re.sub(r"[^a-z0-9]", "", stem)
        norm_stem_map.setdefault(nstem, []).append(os.path.join(PDF_DIR, f))
    # detect duplicate stems (two files, same stem)
    dup_stems = {s: ps for s, ps in stem_map.items() if len(ps) > 1}

    # 2. read master
    rows = list(csv.DictReader(open(MASTER_C, encoding="utf-8")))
    # sort stable by original order; we also key by mpn
    mpn_to_rows = {}
    for r in rows:
        mpn_to_rows.setdefault((r.get("mpn") or "").strip().lower(), []).append(r)

    # 3. match
    out = []
    missing = []
    matched = 0
    method_mpn = 0
    method_clean = 0
    method_mpn_norm = 0
    method_clean_norm = 0
    filedup_groups = {}  # sha256 -> [stems]
    file_seen_sha = {}
    collisions = []  # mpn collision: same mpn normalized key used by 2 different files
    norm_collisions = []  # two master mpns normalize to the same alnum string

    # detect master mpn normalization collisions (two distinct parts collapse)
    norm_mpn_seen = {}
    for r in rows:
        n = re.sub(r"[^a-z0-9]", "", (r.get("mpn") or "").strip().lower())
        if n:
            norm_mpn_seen.setdefault(n, []).append((r.get("mpn") or "").strip())
    for n, ms in norm_mpn_seen.items():
        if len(ms) > 1:
            norm_collisions.append((n, ms))

    for r in rows:
        mpn = (r.get("mpn") or "").strip()
        clean = (r.get("clean_mpn") or "").strip()
        key = r2_key(mpn)
        local = None
        method = ""
        # priority: mpn stem -> clean_mpn stem -> mpn normalized -> clean_mpn normalized
        if mpn and mpn.lower() in stem_map:
            local = stem_map[mpn.lower()]
            method = "mpn"
        elif clean and clean.lower() in stem_map:
            local = stem_map[clean.lower()]
            method = "clean_mpn"
        elif mpn and re.sub(r"[^a-z0-9]", "", mpn.lower()) in norm_stem_map:
            local = norm_stem_map[re.sub(r"[^a-z0-9]", "", mpn.lower())]
            method = "mpn_norm"
        elif clean and re.sub(r"[^a-z0-9]", "", clean.lower()) in norm_stem_map:
            local = norm_stem_map[re.sub(r"[^a-z0-9]", "", clean.lower())]
            method = "clean_mpn_norm"
        if local is None:
            missing.append(mpn)
            out.append({
                "mpn": mpn, "clean_mpn": clean, "match_method": "",
                "local_file": "", "r2_key": key, "r2_url": "",
                "sha256": "", "size_bytes": "", "status": "missing",
            })
            continue
        # pick first file if dup stem (report later)
        path = local[0]
        if len(local) > 1:
            collisions.append((mpn, [os.path.basename(p) for p in local]))
        sha = sha256_of(path)
        size = os.path.getsize(path)
        url = f"{R2_PUBLIC_BASE}/{key}.pdf"
        # content duplication tracking
        filedup_groups.setdefault(sha, []).append(mpn)
        prev = file_seen_sha.get(sha)
        if prev is not None and prev != key:
            # same content already mapped under a different key -> duplicate content
            pass
        file_seen_sha[sha] = key
        matched += 1
        if method == "mpn":
            method_mpn += 1
        elif method == "clean_mpn":
            method_clean += 1
        elif method == "mpn_norm":
            method_mpn_norm += 1
        else:
            method_clean_norm += 1
        out.append({
            "mpn": mpn, "clean_mpn": clean, "match_method": method,
            "local_file": os.path.basename(path), "r2_key": key, "r2_url": url,
            "sha256": sha, "size_bytes": size, "status": "mapped",
        })

    # write CSV
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["mpn", "clean_mpn", "match_method",
                                          "local_file", "r2_key", "r2_url",
                                          "sha256", "size_bytes", "status"])
        w.writeheader()
        w.writerows(out)

    # duplicate content groups (content-identical PDFs across different SKUs)
    dup_content = {sha: ms for sha, ms in filedup_groups.items() if len(ms) > 1}

    # report
    lines = []
    lines.append("# Datasheet Mapping Report (Shadow, pre-upload)")
    lines.append("")
    lines.append(f"R2 public base: `{R2_PUBLIC_BASE}`")
    lines.append("")
    lines.append(f"- Master rows: **{len(rows)}**")
    lines.append(f"- Local PDFs: **{len(pdf_files)}**")
    lines.append(f"- Mapped (have PDF): **{matched}**  (exact mpn: {method_mpn}, exact clean_mpn: {method_clean}, norm mpn: {method_mpn_norm}, norm clean_mpn: {method_clean_norm})")
    lines.append(f"- Missing (no PDF, kept empty): **{len(missing)}**")
    cover = matched / len(rows) * 100
    lines.append(f"- **PDF coverage: {cover:.1f}%** ({matched}/{len(rows)})")
    lines.append("")
    lines.append(f"- Duplicate filename stems (2 files same name): {len(dup_stems)}")
    for s, ps in list(dup_stems.items())[:10]:
        lines.append(f"    - `{s}`: {[os.path.basename(p) for p in ps]}")
    lines.append(f"- Content-identical PDF groups (same SHA256, >1 SKU): {len(dup_content)}")
    for sha, ms in list(dup_content.items())[:10]:
        lines.append(f"    - sha256 {sha[:12]}… -> {ms[:6]}")
    if collisions:
        lines.append(f"- MPN filename collisions (2 files for one mpn): {len(collisions)}")
        for mpn, fs in collisions[:10]:
            lines.append(f"    - `{mpn}`: {fs}")
    if norm_collisions:
        lines.append(f"- ⚠️ Master MPN normalization collisions (two distinct parts collapse to same alnum key): {len(norm_collisions)} — review before trusting norm matches")
        for n, ms in norm_collisions[:10]:
            lines.append(f"    - `{n}` -> {ms}")
    lines.append("")
    lines.append("## Missing SKUs (datasheet_url stays EMPTY — no fake link):")
    for mpn in missing:
        lines.append(f"- `{mpn}`")
    lines.append("")
    lines.append(f"Mapping written: {OUT_CSV}")
    rep = "\n".join(lines)
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(rep)
    print(rep)
    # machine-readable summary for downstream scripts
    sum_path = "D:/SZ Procure/02_CLEAN/datasheet_map_summary.json"
    json.dump({
        "r2_public_base": R2_PUBLIC_BASE,
        "master_rows": len(rows),
        "local_pdfs": len(pdf_files),
        "mapped": matched, "missing": len(missing),
        "coverage_pct": round(cover, 1),
        "method_mpn": method_mpn, "method_clean": method_clean,
        "method_mpn_norm": method_mpn_norm, "method_clean_norm": method_clean_norm,
        "dup_stems": len(dup_stems), "dup_content_groups": len(dup_content),
        "mpn_collisions": len(collisions), "norm_collisions": len(norm_collisions),
        "missing_mpns": missing,
    }, open(sum_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Summary written: {sum_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
