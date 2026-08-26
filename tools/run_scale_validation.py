#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Data Factory v1 — Scale validation runner (Phase 2.2 preflight).

For one level (100 / 500 / 1000):
  1. clean_factory  -> master (16-col) + cleaning review queue
  2. gen_parts --dry-run -> test_p0_processed.csv + review_queue.csv
  3. score against §5.4 gate thresholds
  4. EXPAND the (validation-copy) dictionaries with the found unknowns
     (simulates the human weekly clear -> the "lever" that drives review down)
  5. rerun clean + dry-run, rescore -> prove review volume drops

Uses COPIES of the frozen dictionaries (data/val_mfr_canonical.csv,
data/val_attributes_dictionary.md) so the frozen originals stay pristine.
Writes a per-level report and appends a line to the consolidated summary.

Usage:
  python tools/run_scale_validation.py --level 100 \
      --raw data/raw/scale_100.csv \
      --mfr data/val_mfr_canonical.csv \
      --attr data/val_attributes_dictionary.md \
      --master data/scale/master_100.csv \
      --outdir _gen_test_100 \
      --report reports/phase2.2/scale_100.md
"""
import csv, os, re, sys, subprocess, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GEN = os.path.join(ROOT, "gen_parts.py")
CLEAN = os.path.join(ROOT, "tools", "clean_factory.py")

THRESH = dict(dup_max=0.01, missing_min=0.99, mfr_min=0.95, attr_min=0.80)


def run(cmd):
    r = subprocess.run([sys.executable, *cmd], capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        print("CMD FAILED:", " ".join(cmd))
        print(r.stdout[-1500:]); print(r.stderr[-1500:])
        sys.exit(1)
    return r


def clean(mfr, attr, raw, master, report):
    run([CLEAN, "--raw", raw, "--out", master, "--report", report,
         "--mfr-map", mfr, "--attr-dict", attr])


def dryrun(mfr, attr, master, outdir):
    os.makedirs(outdir, exist_ok=True)
    run([GEN, "--csv", master, "--out", outdir, "--mfr-map", mfr,
         "--attr-dict", attr, "--dry-run"])


def master_count(master):
    with open(master, encoding="utf-8") as f:
        return sum(1 for _ in csv.DictReader(f))


def score(outdir, rows_in):
    proc = os.path.join(outdir, "test_p0_processed.csv")
    rq = os.path.join(outdir, "review_queue.csv")
    with open(proc, encoding="utf-8") as f:
        prows = list(csv.DictReader(f))
    with open(rq, encoding="utf-8") as f:
        rrows = list(csv.DictReader(f))

    groups = len(prows)
    merged_dups = max(0, rows_in - groups)
    dup_rate = merged_dups / rows_in if rows_in else 0

    needs_review = sum(1 for p in prows if p.get("needs_review") == "yes")
    missing = sum(1 for p in prows
                  if not p.get("mpn") or not p.get("canonical_brand")
                  or not p.get("category") or not p.get("subcategory"))
    missing_rate = missing / rows_in if rows_in else 0

    unk_mfr = sum(1 for r in rrows if r.get("reason") == "unknown_manufacturer")
    miss_mfr = sum(1 for r in rrows if r.get("reason") == "missing_manufacturer")
    mfr_match = (rows_in - unk_mfr - miss_mfr) / rows_in if rows_in else 0

    attr_ok = sum(1 for p in prows if not p.get("unknown_attr"))
    attr_match = attr_ok / rows_in if rows_in else 0

    structured = sum(1 for p in prows
                     if p.get("attributes_json") and "__raw__" not in p.get("attributes_json", ""))
    ai_proxy = structured / rows_in if rows_in else 0

    slugs = [p.get("url_slug") for p in prows]
    collisions = len(slugs) - len(set(slugs))

    return dict(rows_in=rows_in, groups=groups, merged_dups=merged_dups, dup_rate=dup_rate,
                needs_review=needs_review, missing=missing, missing_rate=missing_rate,
                unk_mfr=unk_mfr, miss_mfr=miss_mfr, mfr_match=mfr_match,
                attr_ok=attr_ok, attr_match=attr_match, ai_proxy=ai_proxy,
                review_items=len(rrows), collisions=collisions)


def gates(s):
    return dict(
        dup_ok=s["dup_rate"] < THRESH["dup_max"],
        missing_ok=s["missing_rate"] <= (1 - THRESH["missing_min"]),
        mfr_ok=s["mfr_match"] >= THRESH["mfr_min"],
        attr_ok=s["attr_match"] >= THRESH["attr_min"],
        seo="N/A(dry-run)",
        ai="N/A(manual)",
    )


def collect_unknowns(rq_path):
    """Parse gen_parts dry-run review_queue.csv.

    gen_parts writes reason as one of:
      - "unknown_manufacturer"        -> brand in `canonical_brand` column
      - "missing_manufacturer"        -> source gap, NOT fixable via dict expansion
      - "unknown_attribute_key"       -> key in `detail` ("key=VALUE")
      - "malformed_attributes"        -> source gap, NOT fixable via dict expansion
    (Future-proof: also accept reason == "unknown_attribute_key=VALUE".)
    """
    unk_mfrs, unk_attrs = set(), set()
    with open(rq_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            reason = (r.get("reason") or "").strip()
            if reason == "unknown_manufacturer":
                b = (r.get("canonical_brand") or r.get("brand") or "").strip()
                if b:
                    unk_mfrs.add(b)
            elif reason == "missing_manufacturer":
                pass  # source-data gap; not fixable via dict expansion
            elif reason == "unknown_attribute_key":
                detail = (r.get("detail") or "")
                if detail.startswith("key="):
                    key = detail[len("key="):].strip()
                    if key:
                        unk_attrs.add(key)
            elif reason.startswith("unknown_attribute_key="):
                unk_attrs.add(reason.split("=", 1)[1].strip())
    return unk_mfrs, unk_attrs


def expand_mfr(path, unk_mfrs):
    existing = set()
    lines = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            lines.append(line)
            parts = line.rstrip("\n").split("\t")
            if parts and parts[0].strip():
                existing.add(parts[0].strip())
    added = 0
    with open(path, "a", encoding="utf-8") as f:
        for name in sorted(unk_mfrs):
            if name in existing:
                continue
            f.write(f"{name}\t{name}\n")
            added += 1
    return added


def expand_attr(path, unk_attrs):
    txt = open(path, encoding="utf-8").read()
    present = set(re.findall(r"`([^`]+)`", txt))
    new_rows = []
    for key in sorted(unk_attrs):
        if key in present:
            continue
        new_rows.append(f"| `{key}` | string | — | 通用 | validation-added | 规模验证补充 |")
        present.add(key)
    if not new_rows:
        return 0
    lines = txt.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.startswith("## 5.")), len(lines))
    lines[idx:idx] = new_rows
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return len(new_rows)


def fmt_pct(x):
    return f"{x*100:.2f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", type=int, required=True)
    ap.add_argument("--raw", required=True)
    ap.add_argument("--mfr", required=True)
    ap.add_argument("--attr", required=True)
    ap.add_argument("--master", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--summary", required=True)
    args = ap.parse_args()

    # ---- pass 1: with validation dictionaries as-is ----
    clean(args.mfr, args.attr, args.raw, args.master, args.report)
    dryrun(args.mfr, args.attr, args.master, args.outdir)
    rows_in = master_count(args.master)
    s1 = score(args.outdir, rows_in)
    g1 = gates(s1)

    # ---- expand dictionaries with found unknowns (the lever) ----
    unk_mfrs, unk_attrs = collect_unknowns(os.path.join(args.outdir, "review_queue.csv"))
    n_mfr = expand_mfr(args.mfr, unk_mfrs)
    n_attr = expand_attr(args.attr, unk_attrs)

    # ---- pass 2: rerun with expanded dictionaries ----
    dryrun(args.mfr, args.attr, args.master, args.outdir)
    s2 = score(args.outdir, rows_in)
    g2 = gates(s2)

    # ---- write per-level report ----
    rep = []
    rep.append(f"# Scale Validation — Level {args.level}\n")
    rep.append(f"- Raw input : `{args.raw}` ({rows_in} rows)")
    rep.append(f"- Dictionaries expanded this level: +{n_mfr} brands, +{n_attr} attribute keys\n")
    rep.append("## Gate scores — PASS 1 (frozen dicts)\n")
    rep.append("| Metric | Value | Threshold | Result |")
    rep.append("|--------|-------|-----------|--------|")
    rep.append(f"| duplicate rate | {fmt_pct(s1['dup_rate'])} | < 1% | {'PASS' if g1['dup_ok'] else 'FAIL'} |")
    rep.append(f"| missing fields | {fmt_pct(s1['missing_rate'])} | ≤ 1% | {'PASS' if g1['missing_ok'] else 'FAIL'} |")
    rep.append(f"| manufacturer match | {fmt_pct(s1['mfr_match'])} | ≥ 95% | {'PASS' if g1['mfr_ok'] else 'FAIL'} |")
    rep.append(f"| attribute match | {fmt_pct(s1['attr_match'])} | ≥ 80% | {'PASS' if g1['attr_ok'] else 'FAIL'} |")
    rep.append(f"| SEO completeness | {g1['seo']} | 100% | — |")
    rep.append(f"| AI recall (proxy: structured) | {fmt_pct(s1['ai_proxy'])} | ≥ 90% (manual) | — |")
    rep.append(f"| needs_review rows | {s1['needs_review']} | — | — |")
    rep.append(f"| review_queue items | {s1['review_items']} | — | — |")
    rep.append(f"| slug collisions | {s1['collisions']} | 0 | {'PASS' if s1['collisions']==0 else 'FAIL'} |")
    rep.append("\n## Gate scores — PASS 2 (after dict expansion / weekly clear)\n")
    rep.append("| Metric | Value | Threshold | Result |")
    rep.append("|--------|-------|-----------|--------|")
    rep.append(f"| duplicate rate | {fmt_pct(s2['dup_rate'])} | < 1% | {'PASS' if g2['dup_ok'] else 'FAIL'} |")
    rep.append(f"| missing fields | {fmt_pct(s2['missing_rate'])} | ≤ 1% | {'PASS' if g2['missing_ok'] else 'FAIL'} |")
    rep.append(f"| manufacturer match | {fmt_pct(s2['mfr_match'])} | ≥ 95% | {'PASS' if g2['mfr_ok'] else 'FAIL'} |")
    rep.append(f"| attribute match | {fmt_pct(s2['attr_match'])} | ≥ 80% | {'PASS' if g2['attr_ok'] else 'FAIL'} |")
    rep.append(f"| AI recall (proxy) | {fmt_pct(s2['ai_proxy'])} | ≥ 90% (manual) | — |")
    rep.append(f"| needs_review rows | {s2['needs_review']} | — | — |")
    rep.append(f"| review_queue items | {s2['review_items']} | — | — |")
    rep.append(f"| slug collisions | {s2['collisions']} | 0 | {'PASS' if s2['collisions']==0 else 'FAIL'} |")
    rep.append(f"\n## Review reduction (lever proven)\n- review_queue: {s1['review_items']} → {s2['review_items']} "
               f"(−{s1['review_items']-s2['review_items']})")
    rep.append(f"- needs_review rows: {s1['needs_review']} → {s2['needs_review']}")
    rep.append("\n> Note: SEO completeness and full AI-query recall are enforced/measured at "
               "`--strict` publish; dry-run validates structure, dedup, brand & attribute hygiene.")
    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(rep) + "\n")

    # ---- append to consolidated summary ----
    summ_line = (f"| {args.level} | {rows_in} | {s1['review_items']}→{s2['review_items']} "
                 f"| {fmt_pct(s1['attr_match'])}→{fmt_pct(s2['attr_match'])} "
                 f"| {fmt_pct(s1['mfr_match'])}→{fmt_pct(s2['mfr_match'])} "
                 f"| {s1['collisions']} | +{n_mfr}mfr/+{n_attr}attr |")
    header = ("# Scale Validation Summary (Phase 2.2 preflight)\n\n"
              "| Level | Rows | review_queue (pre→post) | attr match (pre→post) | "
              "mfr match (pre→post) | collisions | dict expansion |\n"
              "|-------|------|--------------------------|---------------------------|"
              "----------------------|-----------|----------------|\n")
    if not os.path.exists(args.summary):
        with open(args.summary, "w", encoding="utf-8") as f:
            f.write(header)
    with open(args.summary, "a", encoding="utf-8") as f:
        f.write(summ_line + "\n")

    print(f"[Level {args.level}] review {s1['review_items']}->{s2['review_items']} | "
          f"attr {fmt_pct(s1['attr_match'])}->{fmt_pct(s2['attr_match'])} | "
          f"mfr {fmt_pct(s1['mfr_match'])}->{fmt_pct(s2['mfr_match'])} | "
          f"collisions {s1['collisions']} | expanded +{n_mfr}mfr/+{n_attr}attr")


if __name__ == "__main__":
    main()
