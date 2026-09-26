"""Build the authoritative MPN/C# -> Datasheet(PDF) unique mapping for SZ Procure.

Shadow-only tool. Reads the local PDF library + the master, computes
SHA256/size live, and produces a single source-of-truth map:

    D:/SZ Procure/02_CLEAN/datasheet_map.csv
    D:/SZ Procure/04_Audit_Report/datasheet_map_report.md

Matching rule (per project convention, 2026-09-27 R24-followup):
  * key = the LCSC C# (``supplier_reference``), NOT the MPN.
    A clone part (same MPN, several manufacturers) therefore gets ONE R2
    object per LCSC part -- no two manufacturers ever share an object, so the
    last-uploader-wins overwrite (the AO3401A AOS/UMW cross-contamination bug)
    cannot recur. MPN is only a fallback key, used for legacy rows that have no
    C# in the master.
  * The R2 object KEY is always derived from the C#
    (``KEY_SAFE(supplier_reference)``, e.g. ``c15127``), never from the source
    filename -- so one deterministic URL per LCSC part regardless of how the
    local file was named.
  * Local PDFs live under ``资料PDF/datasheets`` and are named
    ``<sha256>__<id>.pdf`` where ``<id>`` is either an MPN or a C#. We index
    them by that ``<id>`` (and its alnum-normalised form) so a row can be
    matched by C# first, then by MPN.
  * FORWARD-ONLY preservation (2026-09-27): a master row that already carries a
    valid R2 ``datasheet_url`` keeps it EXACTLY as-is. We never re-derive a new
    key for an already-published SKU, so existing live datasheet links are never
    disturbed. Only rows with an empty/absent datasheet_url get a freshly derived
    C#-keyed URL.

The mapping is the ONLY thing that decides which SKU gets a datasheet button.
gen_parts.py already renders the button conditionally from ``datasheet_url``,
which apply_datasheet_map.py fills from this map.

Run:  python tools/build_datasheet_map.py
Exit 0 = built (mapping always built; report lists mismatches/dupes/missing).
"""
import csv, os, re, hashlib, sys, json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Real PDF library (2026-09-27: the legacy 01_RAW/ASSET/datasheets was empty;
# the 5017 PDFs actually live here, named <sha>__<mpn|c#|unknown>.pdf).
PDF_DIR = "D:/SZ Procure/资料PDF/datasheets"
MASTER_C = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
MASTER_D = "D:/SZ Procure/03_MASTER/product_master/master_parts_v2.1.csv"
OUT_CSV = "D:/SZ Procure/02_CLEAN/datasheet_map.csv"
REPORT = "D:/SZ Procure/04_Audit_Report/datasheet_map_report.md"

# R2 public base. Configurable so the URL is correct when creds are supplied.
R2_PUBLIC_BASE = os.environ.get("SZ_R2_PUBLIC_BASE", "https://static.szprocure.com/datasheets").rstrip("/")

KEY_SAFE = re.compile(r"[^a-z0-9._-]")


def r2_key(cid=None, mpn=None) -> str:
    """R2 object name, derived from the LCSC C# (supplier_reference).

    Per-LCSC-part, so clone families (same MPN, different C#) never share one
    object. Falls back to MPN only when no C# is present (legacy rows).
    """
    src = (cid or "").strip() or (mpn or "").strip()
    if not src:
        return ""
    return KEY_SAFE.sub("-", src.strip().lower())


def is_r2_url(u: str) -> bool:
    """A URL we treat as an already-published R2 datasheet link."""
    if not u:
        return False
    u = u.strip().lower()
    return ".r2.dev" in u or R2_PUBLIC_BASE.rstrip("/").lower() in u


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _ident_of(stem: str) -> str:
    """The '<id>' portion of a '<sha>__<id>' PDF filename stem."""
    return stem.split("__")[-1] if "__" in stem else stem


def main():
    # 1. index local PDFs by stem, normalised stem, and the <id> portion.
    # PDFs live in date sub-directories (资料PDF/datasheets/<YYYY-MM-DD>/...),
    # so walk recursively.
    pdf_files = []
    for root, _dirs, files in os.walk(PDF_DIR):
        for f in files:
            if f.lower().endswith(".pdf"):
                pdf_files.append(os.path.join(root, f))
    stem_map = {}        # full filename stem (no .pdf) lower -> [paths]
    norm_stem_map = {}   # alnum-only full stem -> [paths]
    id_map = {}          # <id> (mpn or C#) lower -> [paths]
    norm_id_map = {}     # alnum-only <id> -> [paths]
    for fp in pdf_files:
        f = os.path.basename(fp)
        stem = f[:-4].lower()
        stem_map.setdefault(stem, []).append(fp)
        nstem = re.sub(r"[^a-z0-9]", "", stem)
        norm_stem_map.setdefault(nstem, []).append(fp)
        ident = _ident_of(stem)
        id_map.setdefault(ident, []).append(fp)
        nident = re.sub(r"[^a-z0-9]", "", ident)
        if nident:
            norm_id_map.setdefault(nident, []).append(fp)
    # detect duplicate stems (two files, same stem)
    dup_stems = {s: ps for s, ps in stem_map.items() if len(ps) > 1}

    # 2. read master
    rows = list(csv.DictReader(open(MASTER_C, encoding="utf-8")))
    # detect master mpn normalization collisions (two distinct parts collapse)
    norm_mpn_seen = {}
    for r in rows:
        n = re.sub(r"[^a-z0-9]", "", (r.get("mpn") or "").strip().lower())
        if n:
            norm_mpn_seen.setdefault(n, []).append((r.get("mpn") or "").strip())
    norm_collisions = [(n, ms) for n, ms in norm_mpn_seen.items() if len(ms) > 1]

    # 3. match
    out = []
    missing = []
    matched = 0
    method_cid = 0
    method_mpn = 0
    method_mpn_norm = 0
    filedup_groups = {}
    file_seen_sha = {}
    collisions = []  # id collision: same id normalized key used by 2 different files

    for r in rows:
        cid = (r.get("supplier_reference") or "").strip()
        mpn = (r.get("mpn") or "").strip()
        clean = (r.get("clean_mpn") or "").strip()
        existing = (r.get("datasheet_url") or "").strip()

        # ---- FORWARD-ONLY: preserve an already-published R2 link ----
        if is_r2_url(existing):
            key = r2_key(cid, mpn)
            out.append({
                "supplier_reference": cid, "mpn": mpn, "clean_mpn": clean,
                "match_method": "preserved", "local_file": "",
                "r2_key": key, "r2_url": existing,
                "sha256": "", "size_bytes": "", "status": "mapped",
            })
            continue

        # ---- derive a fresh C#-keyed URL for rows with no datasheet ----
        key = r2_key(cid, mpn)
        if not key:
            missing.append(mpn)
            out.append({
                "supplier_reference": cid, "mpn": mpn, "clean_mpn": clean,
                "match_method": "", "local_file": "", "r2_key": "",
                "r2_url": "", "sha256": "", "size_bytes": "", "status": "missing",
            })
            continue

        local = None
        method = ""
        # priority: C# id -> MPN id -> MPN norm id -> clean_mpn id
        cid_l = cid.lower()
        mpn_l = mpn.lower()
        cln_l = clean.lower()
        if cid and cid_l in id_map:
            local = id_map[cid_l]; method = "cid"
        elif mpn and mpn_l in id_map:
            local = id_map[mpn_l]; method = "mpn"
        elif mpn and re.sub(r"[^a-z0-9]", "", mpn.lower()) in norm_id_map:
            local = norm_id_map[re.sub(r"[^a-z0-9]", "", mpn.lower())]
            method = "mpn_norm"
        elif clean and cln_l in id_map:
            local = id_map[cln_l]; method = "clean_mpn"
        if local is None:
            missing.append(mpn)
            out.append({
                "supplier_reference": cid, "mpn": mpn, "clean_mpn": clean,
                "match_method": "", "local_file": "", "r2_key": key,
                "r2_url": "", "sha256": "", "size_bytes": "", "status": "missing",
            })
            continue
        # pick first file if dup stem (report later)
        path = local[0]
        if len(local) > 1:
            collisions.append((mpn or cid, [os.path.basename(p) for p in local]))
        sha = sha256_of(path)
        size = os.path.getsize(path)
        url = f"{R2_PUBLIC_BASE}/{key}.pdf"
        filedup_groups.setdefault(sha, []).append(mpn or cid)
        file_seen_sha[sha] = key
        matched += 1
        if method == "cid":
            method_cid += 1
        elif method == "mpn":
            method_mpn += 1
        else:
            method_mpn_norm += 1
        out.append({
            "supplier_reference": cid, "mpn": mpn, "clean_mpn": clean,
            "match_method": method, "local_file": os.path.basename(path),
            "r2_key": key, "r2_url": url,
            "sha256": sha, "size_bytes": size, "status": "mapped",
        })

    # write CSV
    os.makedirs(os.path.dirname(OUT_CSV), exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["supplier_reference", "mpn", "clean_mpn",
                                          "match_method", "local_file", "r2_key",
                                          "r2_url", "sha256", "size_bytes", "status"])
        w.writeheader()
        w.writerows(out)

    # duplicate content groups (content-identical PDFs across different SKUs)
    dup_content = {sha: ms for sha, ms in filedup_groups.items() if len(ms) > 1}
    preserved = sum(1 for o in out if o["match_method"] == "preserved")

    # report
    lines = []
    lines.append("# Datasheet Mapping Report (Shadow, pre-upload)")
    lines.append("")
    lines.append(f"R2 public base: `{R2_PUBLIC_BASE}`")
    lines.append("")
    lines.append(f"- Master rows: **{len(rows)}**")
    lines.append(f"- Local PDFs: **{len(pdf_files)}**")
    lines.append(f"- Already-published (preserved, untouched): **{preserved}**")
    lines.append(f"- Mapped (fresh C#-keyed, have PDF): **{matched}**  "
                 f"(by C#: {method_cid}, by mpn: {method_mpn}, by mpn_norm: {method_mpn_norm})")
    lines.append(f"- Missing (no PDF, kept empty): **{len(missing)}**")
    cover = (preserved + matched) / len(rows) * 100 if rows else 0
    lines.append(f"- **Datasheet coverage: {cover:.1f}%** ({preserved + matched}/{len(rows)})")
    lines.append("")
    lines.append(f"- Duplicate filename stems (2 files same name): {len(dup_stems)}")
    for s, ps in list(dup_stems.items())[:10]:
        lines.append(f"    - `{s}`: {[os.path.basename(p) for p in ps]}")
    lines.append(f"- Content-identical PDF groups (same SHA256, >1 SKU): {len(dup_content)}")
    for sha, ms in list(dup_content.items())[:10]:
        lines.append(f"    - sha256 {sha[:12]}… -> {ms[:6]}")
    if collisions:
        lines.append(f"- ID filename collisions (2 files for one id): {len(collisions)}")
        for mpn, fs in collisions[:10]:
            lines.append(f"    - `{mpn}`: {fs}")
    if norm_collisions:
        lines.append(f"- ⚠️ Master MPN normalization collisions: {len(norm_collisions)}")
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
        "preserved": preserved, "mapped": matched, "missing": len(missing),
        "coverage_pct": round(cover, 1),
        "method_cid": method_cid, "method_mpn": method_mpn, "method_mpn_norm": method_mpn_norm,
        "dup_stems": len(dup_stems), "dup_content_groups": len(dup_content),
        "id_collisions": len(collisions), "norm_collisions": len(norm_collisions),
        "missing_mpns": missing,
    }, open(sum_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"Summary written: {sum_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
