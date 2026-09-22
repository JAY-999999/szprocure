"""Daily collect intake — the canonical 02 INTAKE call site for daily listing.

Wires the 01 acquirer's output directory (D:\\SZ Procure\\采集流水线\\基础数据)
into the 02 intake via an EXPLICIT ``source_path`` (方案 A). This is the ONLY
change needed to "connect the directory": the underlying ``intake_http_json``
already accepts ``source_path``; this script is the explicit-reading call site.

It does NOT modify the 01 acquirer, does NOT move/copy C*.json files, and does
NOT touch MASTER.

Stages
------
  * intake    : stream C*.json envelopes from SOURCE into the raw pool. SOURCE is
                read-only here; the raw pool is a local staging file (no copy of
                the source envelopes).
  * normalize : (--normalize) build rows, qualify, de-dup against MASTER (read-only),
                write candidates. Does NOT append MASTER.

Outputs land in an ISOLATED preview pool (ROOT) by default, so test/verify
batches never pollute the production pool or MASTER. To stage for a real release
(separate, authorized step) pass --root pointing at the production pool
(D:\\SZ Procure\\03_MASTER\\pool).
"""
import argparse
import collections
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

from tools.factory import product_data as pd
from tools.factory import gate

# 01 acquirer writes here; intake reads from here (方案 A: explicit source_path).
SOURCE = r"D:\SZ Procure\采集流水线\基础数据"
# Isolated preview pool so test/verify batches never touch the production pool.
DEFAULT_ROOT = "data/raw/_clean_preview/collect_daily"
PROD_MASTER = r"D:\SZ Procure\site\data\production\master_parts_v2.1.csv"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Daily collect intake (02) — read LCSC HTTP RAW from 采集流水线\\基础数据")
    ap.add_argument("batch_id", help="batch id, e.g. collect_test1")
    ap.add_argument("--source", default=SOURCE,
                    help="LCSC HTTP RAW envelope directory (read-only)")
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help="staging pool root (isolated preview by default)")
    ap.add_argument("--limit", type=int, default=None,
                    help="max envelopes to intake (test/sample)")
    ap.add_argument("--selector", default=None,
                    help="comma-separated MPN allow-list")
    ap.add_argument("--normalize", action="store_true",
                    help="also run normalize (build/qualify/dedup -> candidates)")
    ap.add_argument("--master-csv", default=PROD_MASTER,
                    help="MASTER csv for the read-only duplicate guard")
    ap.add_argument("--mass-dup-stop", action="store_true",
                    help="enable the mass-duplicate hard-stop (default OFF for "
                         "test batches; individual duplicates are still counted)")
    args = ap.parse_args(argv)

    selector = {s.strip().upper() for s in args.selector.split(",")} \
        if args.selector else None

    print("== intake_http_json ==")
    print(f"  source_path = {args.source}")
    if not os.path.exists(args.source):
        print(f"  ERROR: source_path not found: {args.source}")
        return 2
    res = pd.intake_http_json(batch_id=args.batch_id, source_path=args.source,
                              root=args.root, limit=args.limit, selector=selector)
    print(f"  input={res.input_count} written={res.written} "
          f"self_dups={res.self_duplicate_count} stop={res.stop}")
    if res.stop:
        print("  STOP during intake; aborting.")
        return 1

    if args.normalize:
        print("== normalize ==")
        norm = pd.normalize(batch_id=args.batch_id, master_csv=args.master_csv,
                            root=args.root,
                            skip_mass_duplicate_check=not args.mass_dup_stop)
        print(f"  input={norm.input_count} cleaned={norm.cleaned_count} "
              f"candidates={norm.candidate_count} rejected={norm.rejected_count} "
              f"dups={norm.duplicate_count} self_dups={norm.self_duplicate_count} "
              f"stop={norm.stop}")
        print(f"  candidates_path = {norm.path}")
        if norm.exceptions:
            ec = collections.Counter(e["code"] for e in norm.exceptions)
            print("  exception codes:")
            for code, n in ec.most_common():
                print(f"    {code}: {n} ({gate.severity_of(code)})")
        if norm.stop:
            print("  STOP during normalize; candidates written but not promoted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
