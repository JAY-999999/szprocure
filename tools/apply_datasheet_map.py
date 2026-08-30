"""Apply the datasheet mapping to the master CSV(s) — populate datasheet_url.

Shadow-only data step. Reads datasheet_map.csv (built by build_datasheet_map.py)
and writes the R2 HTTPS URL into the `datasheet_url` column of the master file(s).
For SKUs with no PDF, datasheet_url stays EMPTY (no fake link, no placeholder).

Only the `datasheet_url` column is touched; every other column/row is preserved
exactly (Schema, URL, RFQ, SEO structure untouched). gen_parts.py already renders
a Datasheet button conditionally from this column, so populating it activates the
feature with zero template changes.

Targets (canonical build source verified = master_parts_v2.1.csv):
  C: data/production/master_parts_v2.1.csv   (gen_parts --csv source)
  C: data/production/master_parts_publish.csv (same 500 MPNs; kept in sync)
  D: 03_MASTER/product_master/master_parts_v2.1.csv  (source of truth)

------------------------------------------------------------------------------
P0 SAFETY FIX (SKU Factory Phase P0)
------------------------------------------------------------------------------
Previously the URL was resolved with:

    url = mpn_url.get(mpn, "")     # <-- MPN absent from the map => ""

so any MPN missing from datasheet_map.csv had its EXISTING datasheet_url
silently wiped. The map is generated from the current master, so this fires
whenever the map lags behind the master (e.g. new SKUs added, map not rebuilt).

New semantics:
  * MPN present in map, status=mapped  -> write the R2 URL
  * MPN present in map, status=missing -> write ""   (legitimately has no PDF)
  * MPN ABSENT from map                -> KEEP the existing datasheet_url
                                          (unless --prune is given)

Writes are now atomic: <file>.tmp -> validate (row count, column count, header
identity) -> os.replace. A failed validation never touches the real file.

Run:
  python tools/apply_datasheet_map.py                 # apply (atomic)
  python tools/apply_datasheet_map.py --dry-run       # report only, write nothing
  python tools/apply_datasheet_map.py --prune         # allow clearing unmatched URLs
"""
import argparse
import csv
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_CSV = "D:/SZ Procure/02_CLEAN/datasheet_map.csv"
TARGETS = [
    os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv"),
    os.path.join(ROOT, "data", "production", "master_parts_publish.csv"),
    "D:/SZ Procure/03_MASTER/product_master/master_parts_v2.1.csv",
]


def load_mapping(map_csv):
    """Return {mpn: r2_url}. Entries with status != 'mapped' map to ''."""
    mpn_url = {}
    with open(map_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            mpn = (r.get("mpn") or "").strip()
            mpn_url[mpn] = (r.get("r2_url") or "").strip() if r.get("status") == "mapped" else ""
    return mpn_url


def compute_rows(rows, mpn_url, prune=False):
    """Apply the mapping. Returns (out_rows, stats)."""
    out_rows = []
    stats = {"set": 0, "empty": 0, "preserved": 0, "pruned": 0}
    for r in rows:
        mpn = (r.get("mpn") or "").strip()
        if mpn in mpn_url:
            url = mpn_url[mpn]
            if url:
                stats["set"] += 1
            else:
                stats["empty"] += 1
        else:
            # MPN not covered by the mapping -> keep whatever is already there
            existing = (r.get("datasheet_url") or "").strip()
            if prune:
                url = ""
                if existing:
                    stats["pruned"] += 1
                else:
                    stats["empty"] += 1
            else:
                url = existing
                if existing:
                    stats["preserved"] += 1
                else:
                    stats["empty"] += 1
        r["datasheet_url"] = url
        out_rows.append(r)
    return out_rows, stats


def atomic_write_csv(path, cols, out_rows, dry_run=False):
    """Write <path>.tmp then validate then os.replace. Returns True if written."""
    if dry_run:
        return False
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(out_rows)
        # validate the temp file before it becomes real
        with open(tmp, encoding="utf-8", newline="") as f:
            rd = csv.DictReader(f)
            t_cols = list(rd.fieldnames or [])
            t_rows = list(rd)
        if t_cols != cols:
            raise RuntimeError(f"header changed for {path}")
        if len(t_rows) != len(out_rows):
            raise RuntimeError(f"row count mismatch for {path}: "
                               f"{len(t_rows)} != {len(out_rows)}")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Apply datasheet mapping to master CSV(s).")
    ap.add_argument("--map", default=MAP_CSV, help="datasheet map CSV")
    ap.add_argument("--target", action="append", default=None,
                    help="explicit target CSV (repeatable). Default: the 3 canonical masters.")
    ap.add_argument("--prune", action="store_true",
                    help="allow clearing datasheet_url for MPNs absent from the map "
                         "(legacy behaviour). OFF by default.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args(argv)

    targets = args.target or TARGETS
    mpn_url = load_mapping(args.map)
    print(f"Loaded {len(mpn_url)} mappings from map. "
          f"mode={'DRY-RUN' if args.dry_run else 'APPLY'}"
          f"{' +PRUNE' if args.prune else ''}")

    totals = {"set": 0, "empty": 0, "preserved": 0, "pruned": 0}
    for path in targets:
        if not os.path.exists(path):
            print(f"SKIP (missing): {path}")
            continue
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if not rows or "datasheet_url" not in rows[0]:
            print(f"SKIP (no datasheet_url col): {path}")
            continue
        cols = list(rows[0].keys())
        out_rows, stats = compute_rows(rows, mpn_url, prune=args.prune)
        written = atomic_write_csv(path, cols, out_rows, dry_run=args.dry_run)
        for k in totals:
            totals[k] += stats[k]
        rel = os.path.relpath(path, ROOT) if path.startswith(ROOT) else path
        print(f"{'WOULD UPDATE' if args.dry_run else 'UPDATED'} {rel}: "
              f"set={stats['set']}, empty={stats['empty']}, "
              f"preserved={stats['preserved']}, pruned={stats['pruned']}"
              f"{'' if written else '  (not written)'}")
    print(f"\nTotals across targets: set={totals['set']}, empty={totals['empty']}, "
          f"preserved={totals['preserved']}, pruned={totals['pruned']}")
    if args.dry_run:
        print("DRY-RUN complete - no file was modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
