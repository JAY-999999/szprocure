"""Spec integrity guard for the JinShui (LCSC http) intake pipeline.

Run BEFORE any release batch so content-completeness regressions are caught
at the source, not on the live page:

    python tools/factory/spec_integrity_check.py --cids C15127,C20416655,...
    python tools/factory/spec_integrity_check.py --mpns AO3401A,BAV99S,...

Checks (per SKU, RAW paramVOList -> final attributes_json via the REAL chain
flatten_envelope -> product_data.build_row, never a naive re-merge):

  1. COMPLETENESS  every non-empty RAW param must reach the final attrs
                   (mapped canonical key OR forwarded unmapped key).
                   Catches silent drops (normalizer -> None, forward skips).
                   Allowed drops: CJK-only labels + _IDENTITY_KEY_BLACKLIST.
  2. COLLISION     two distinct RAW param names mapping to the SAME canonical
                   key (the later one silently overwrites the earlier, e.g.
                   Surge Current vs Current-Rectified -> forward_current_a).
  3. RANGE         raw value is a numeric range (e.g. 4V~80V) but the final
                   value was averaged into a single float.
  4. CONDITION     raw value contains '@' (e.g. 1.25V@150mA) but the final
                   value lost the test condition.
  5. IDENTITY      (a) the checked MPN resolves to >1 MASTER row with
                   different manufacturers -> ambiguous identity, datasheet
                   MPN single-key binding can cross-bind;
                   (b) other RAW files carry the SAME MPN under a different
                   supplier_reference (clone parts, e.g. AO3401A AOS C15127
                   vs UMW C347476) -> RAW must always be resolved by
                   supplier_reference, NEVER by MPN.

Exit code: 0 = all PASS (warnings allowed), 2 = at least one FAIL.
This file is NOT part of the frozen layer; keep checks aligned with
lcsc_http_adapter.py rules when those change.
"""

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tools.factory import lcsc_http_adapter as ha          # noqa: E402
from tools.factory import product_data as pd               # noqa: E402
from tools.factory import has_illegal_text                  # noqa: E402

RAW_DIR = ha.DEFAULT_HTTP_RAW
MASTER = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "production", "master_parts_v2.1.csv")

RANGE_RE = re.compile(r"\d[\d.]*\s*[~\uFF5E\u2013\u2014-]\s*\d[\d.]*")
IDENTITY_ALLOWED = set(ha._IDENTITY_KEY_BLACKLIST)


def load_master_index():
    """Return (rows_by_mpn, mpn -> set(manufacturer))."""
    rows = {}
    mfrs = {}
    with open(MASTER, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            mpn = (r.get("mpn") or "").strip()
            if not mpn:
                continue
            rows.setdefault(mpn, []).append(r)
            m = (r.get("manufacturer") or "").strip()
            mfrs.setdefault(mpn, set()).add(m)
    return rows, mfrs


def scan_raw_identity(mpns):
    """Scan RAW dir once; return mpn -> list of (cid, manufacturer) in RAW."""
    wanted = {m.strip().upper() for m in mpns if m}
    found = {}
    if not os.path.isdir(RAW_DIR):
        return found
    for fn in os.listdir(RAW_DIR):
        if not fn.lower().endswith(".json"):
            continue
        fp = os.path.join(RAW_DIR, fn)
        try:
            with open(fp, encoding="utf-8") as f:
                env = json.load(f)
            rec = ha.flatten_envelope(env)
        except Exception:
            continue
        mpn = (rec.get("mpn") or "").strip().upper()
        if mpn in wanted:
            found.setdefault(mpn, []).append(
                (rec.get("supplier_reference") or fn[:-5],
                 rec.get("manufacturer_raw") or ""))
    return found


def check_sku(cid, env, master_rows, mfrs_by_mpn, raw_identity):
    """Run checks 1-5 for one SKU. Returns list of (level, code, message)."""
    problems = []
    rec = ha.flatten_envelope(env)
    mpn = rec.get("mpn") or ""
    mfr_master = master_rows.get(mpn)
    mfr_ref = (mfr_master[0].get("manufacturer") or "") if mfr_master else ""
    row, _meta = pd.build_row(rec, mpn, mfr_ref)
    final = json.loads(row.get("attributes_json") or "{}")
    mp = env["source_raw"]["main_product"]
    params = mp.get("paramVOList") or []

    canon_owner = {}   # canonical key -> first RAW param name that claimed it
    mapped_names = set()
    for item in params:
        name = (item.get("paramNameEn") or item.get("paramName") or "").strip()
        val = (item.get("paramValueEn") or item.get("paramValue") or "").strip()
        if not name or not val:
            continue

        # --- check 2: COLLISION on the canonical map (pre-normalization) ---
        if name in ha.HTTP_ATTR_MAP:
            key = ha.HTTP_ATTR_MAP[name][0]
            owner = canon_owner.setdefault(key, name)
            if owner != name:
                problems.append(("FAIL", "COLLISION",
                    "RAW params %r and %r both map to canonical key %r; "
                    "the later overwrites the earlier (data loss)"
                    % (owner, name, key)))
            mapped_names.add(name)

            # --- check 3: RANGE preserved (no averaging) ---
            if RANGE_RE.search(val) and isinstance(final.get(key), (int, float)):
                problems.append(("FAIL", "RANGE",
                    "raw %r is a range but %s=%r is a single averaged number"
                    % (val, key, final.get(key))))

            # --- check 4: '@' test condition preserved ---
            if "@" in val and not (isinstance(final.get(key), str)
                                   and "@" in final[key]):
                problems.append(("FAIL", "CONDITION",
                    "raw %r carries a test condition but %s=%r lost it"
                    % (val, key, final.get(key))))

            # --- check 1a: mapped param must land in final attrs ---
            if key not in final:
                problems.append(("FAIL", "COMPLETENESS",
                    "mapped param %r (%s) missing from final attrs "
                    "(normalizer returned None -> silent drop)" % (name, key)))
        else:
            # --- check 1b: unmapped param must be forwarded verbatim ---
            if has_illegal_text(name):
                continue                      # CJK-only label: allowed drop
            if name.lower().strip() in IDENTITY_ALLOWED:
                continue                      # identity key: allowed drop
            if name not in final:
                problems.append(("FAIL", "COMPLETENESS",
                    "unmapped param %r=%r not forwarded into final attrs "
                    "(silently dropped)" % (name, val)))
            elif name in mapped_names:
                problems.append(("FAIL", "COLLISION",
                    "unmapped param %r collides with an existing canonical "
                    "key and was skipped by _forward_unmapped_specs" % name))

    # --- check 5a: MPN resolves to >1 MASTER manufacturer ---
    mset = mfrs_by_mpn.get(mpn, set())
    if len(mset) > 1:
        problems.append(("FAIL", "IDENTITY",
            "MPN %r exists in MASTER under multiple manufacturers %s; "
            "datasheet MPN single-key binding can cross-bind"
            % (mpn, sorted(mset))))

    # --- check 5b: same MPN from other RAW supplier_references (clones) ---
    clones = [c for c in raw_identity.get(mpn.upper(), [])
              if (c[0] or "").strip() != cid]
    if clones:
        problems.append(("WARN", "IDENTITY",
            "MPN %r also exists in RAW under other supplier_reference(s) %s; "
            "always resolve RAW by supplier_reference, never by MPN"
            % (mpn, clones)))

    return problems


def check_cids(cids):
    """Library entry point (wired into release_pipeline.plan_release).

    Run all 5 checks for a list of supplier_reference C#s. Returns
    (total_fail, total_warn, lines) where lines are human-readable report
    rows (CLI prints them; pipeline logs them into the ReleasePlan).
    """
    master_rows, mfrs_by_mpn = load_master_index()
    mpn_order, cid_by_mpn, lines = [], {}, []

    inv = {}
    for mpn, rows in master_rows.items():
        for r in rows:
            ref = (r.get("supplier_reference") or "").strip()
            if ref:
                inv.setdefault(ref, mpn)
    for cid in cids:
        mpn = inv.get(cid)
        if mpn is None:
            lines.append("FAIL IDENTITY: C# %s not found in MASTER "
                         "supplier_reference" % cid)
            return 1, 0, lines
        cid_by_mpn[mpn] = cid
        mpn_order.append(mpn)

    raw_identity = scan_raw_identity(list(mpn_order))

    total_fail = total_warn = 0
    for mpn in mpn_order:
        cid = cid_by_mpn[mpn]
        fp = os.path.join(RAW_DIR, cid + ".json")
        lines.append("===== %s  (C# %s) =====" % (mpn, cid))
        if not os.path.exists(fp):
            lines.append("  FAIL COMPLETENESS: RAW file missing: %s" % fp)
            total_fail += 1
            continue
        with open(fp, encoding="utf-8") as f:
            env = json.load(f)
        problems = check_sku(cid, env, master_rows, mfrs_by_mpn, raw_identity)
        if not problems:
            lines.append("  PASS (all 5 checks)")
            continue
        for level, code, msg in problems:
            lines.append("  %s %s: %s" % (level, code, msg))
            if level == "FAIL":
                total_fail += 1
            else:
                total_warn += 1

    lines.append("=== SUMMARY: %d SKU checked, %d FAIL, %d WARN ==="
                 % (len(mpn_order), total_fail, total_warn))
    return total_fail, total_warn, lines


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--cids", help="comma-separated LCSC C# (supplier_reference)")
    g.add_argument("--mpns", help="comma-separated MPNs (resolved via MASTER "
                                  "supplier_reference; fails if ambiguous)")
    args = ap.parse_args()

    if args.cids:
        cids = [c.strip() for c in args.cids.split(",") if c.strip()]
    else:
        master_rows, mfrs_by_mpn = load_master_index()
        cids, mpns = [], [m.strip() for m in args.mpns.split(",") if m.strip()]
        for m in mpns:
            rows = master_rows.get(m, [])
            refs = {(r.get("supplier_reference") or "").strip() for r in rows}
            mset = mfrs_by_mpn.get(m, set())
            if len(rows) > 1 and len(refs) > 1:
                print("FAIL IDENTITY: MPN %r ambiguous in MASTER "
                      "(rows=%d, C#s=%s, mfr=%s); use --cids instead"
                      % (m, len(rows), sorted(refs), sorted(mset)))
                return 2
            if not rows:
                print("FAIL IDENTITY: MPN %r not in MASTER" % m)
                return 2
            cids.append(next(iter(refs)))

    total_fail, _total_warn, lines = check_cids(cids)
    for ln in lines:
        print(ln)
    return 2 if total_fail else 0


if __name__ == "__main__":
    sys.exit(main())
