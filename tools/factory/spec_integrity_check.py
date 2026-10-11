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

# 2026-10-09: intentional multi-source MPNs (operator-authorised). These MPNs
# legitimately carry >1 manufacturer in MASTER (we resell them from several
# sources). Our datasheet binding is keyed by C# (supplier_reference), never by
# MPN, so the 5a cross-bind risk does not apply. 5a is downgraded to WARN for
# these (audit-trail only) instead of a hard FAIL.
IDENTITY_5A_WHITELIST = {
    "1N4148W", "SMAJ5.0A", "SS54", "LL4148", "AMS1117-3.3V",
}

# --------------------------------------------------------------------------
# MULTI-VALUE detection — DELIBERATELY SELF-CONTAINED (2026-10-09)
# The gate must NEVER reuse the production normalisers' own helper: if the
# mapping layer regresses, a shared helper regresses with it and the gate goes
# blind (exactly how '3V;5V -> 4V' shipped green before this fix). This
# duplicate is intentional and is covered by a negative test that disables the
# production guard and asserts the gate still FAILs.
# --------------------------------------------------------------------------
_MEASURE_FRAGMENT_RE = re.compile(
    r"^\s*[-+]?[\d.]+\s*[\w\u00b5\u03bc\u03a9\u2126%^\u00b0/*\s]*$")
_FRACTIONAL_POWER_RE = re.compile(r"\d+\s*/\s*\d+\s*W(?![A-Za-z])", re.I)


def _gate_multi_value(s):
    """True when a RAW value lists several measurements ('3V;5V', '16/32 Bit').

    Ranges ('4V~80V') and conditions ('1.25V@150mA') are NOT multi-values.
    """
    if not s:
        return False
    t = str(s).strip()
    if not t:
        return False
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)      # 1,000V -> 1000V
    for sep in (";", ","):
        if sum(1 for p in t.split(sep) if _MEASURE_FRAGMENT_RE.match(p)) >= 2:
            return True
    t2 = _FRACTIONAL_POWER_RE.sub("", t)               # '1/4W' is one value
    return sum(1 for p in t2.split("/")
               if re.search(r"\d", p) and _MEASURE_FRAGMENT_RE.match(p)) >= 2


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

            # --- check 3b: MULTI-VALUE preserved (no averaging/truncation) ---
            # A RAW value that lists several measurements ('3V;5V',
            # '1.8V;2.5V;3.3V', '5.5A;4A', '16/32 Bit', '1;2') must reach the
            # page as that list. If the canonical value came out as a single
            # int/float, the pipeline averaged or truncated it and INVENTED a
            # rating the datasheet does not state -> hard FAIL (2026-10-09).
            if _gate_multi_value(ha.normalize_text(val)) and key in final:
                fv = final.get(key)
                if not (isinstance(fv, str) and any(sep in fv for sep in ";,/")):
                    problems.append(("FAIL", "MULTIVALUE",
                        "raw %r is a multi-value list but %s=%r is a single "
                        "value (averaged/truncated -- must stay verbatim)"
                        % (val, key, fv)))

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
        if mpn.strip().upper() in IDENTITY_5A_WHITELIST:
            problems.append(("WARN", "IDENTITY",
                "MPN %r exists in MASTER under multiple manufacturers %s; "
                "intentional multi-source (whitelisted) -- datasheet bound by "
                "C#, never by MPN" % (mpn, sorted(mset))))
        else:
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


def _check_publish_chain(cid, env, cand_row=None):
    """New-SKU publish path: verify the candidate forms a complete, traceable
    chain RAW -> MASTER -> Release Gate.

    This REPLACES the old-SKU identity check (5a/5b cross-binding against an
    existing MASTER row) which is meaningless for a brand-new SKU. The gate is
    NOT weakened: completeness / collision / range / condition (checks 1-4) are
    still exercised by check_sku; here we additionally require the four minimum
    identity fields to be present and self-consistent, and the PUBLISHED
    datasheet host to be shippable. A genuinely broken new SKU still FAILs here.

    Datasheet host policy: the published ``datasheet_url`` (the candidate MASTER
    row, post-R2-upload) is what actually ships -- it is already policed by
    ``release_pipeline.check_datasheet_host_policy`` on the same candidate rows.
    We therefore validate THAT url here, never the RAW ``pdfUrl`` (which is
    always LCSC and would false-fail every LCSC-sourced new SKU). When no
    candidate row is available (standalone CLI run) the host check is skipped --
    the dedicated host-policy gate covers it in the real pipeline.
    """
    from . import datasheet_hosts
    problems = []
    rec = ha.flatten_envelope(env)
    mpn = (rec.get("mpn") or "").strip()
    mfr = (rec.get("manufacturer_raw") or "").strip()
    sref = (rec.get("supplier_reference") or rec.get("supplier_sku") or "").strip()
    if not mpn:
        problems.append(("FAIL", "CHAIN",
                         "MPN empty in RAW -- cannot publish"))
    if not mfr:
        problems.append(("FAIL", "CHAIN",
                         "Manufacturer empty in RAW -- cannot publish"))
    if not sref:
        problems.append(("FAIL", "CHAIN",
                         "supplier_reference empty in RAW -- cannot publish"))
    elif sref.upper() != cid.upper():
        problems.append(("WARN", "CHAIN",
            "supplier_reference %r != C# filename %r" % (sref, cid)))
    # datasheet: validate the PUBLISHED url (candidate row, post-R2-upload),
    # NOT the RAW pdfUrl (always LCSC). The dedicated host-policy gate already
    # polices this url in plan_release; here we mirror it so a standalone
    # check_cids(candidate_rows=...) reproduces the release verdict.
    if cand_row is not None:
        ds = (cand_row.get("datasheet_url") or "").strip()
        if not ds:
            problems.append(("WARN", "CHAIN",
                "no published datasheet_url in candidate row -- page will "
                "ship without a datasheet"))
        else:
            verdict, host, why = datasheet_hosts.classify(ds)
            if not datasheet_hosts.shippable(verdict):
                problems.append(("FAIL", "CHAIN",
                    "published datasheet host not shippable: %s (%s: %s)"
                    % (ds, verdict, why)))
    # RAW source present (so the chain is traceable back to 01 acquisition)
    if "source_raw" not in env:
        problems.append(("FAIL", "CHAIN",
                         "RAW envelope missing source_raw -- cannot trace source"))
    return problems


def check_cids(cids, candidate_rows=None):
    """Library entry point (wired into release_pipeline.plan_release).

    Run all 5 checks for a list of supplier_reference C#s. Returns
    (total_fail, total_warn, lines) where lines are human-readable report
    rows (CLI prints them; pipeline logs them into the ReleasePlan).

    Distinguishes two release modes so the gate is correct for both:
      * Existing SKU (C# in MASTER supplier_reference) -> full check_sku
        including the MASTER cross-binding identity checks (5a/5b).
      * New SKU publish (C# NOT in MASTER) -> check_sku (checks 1-4:
        completeness / collision / range / condition) PLUS a publish-chain
        validation (_check_publish_chain). The old-SKU identity check is
        intentionally skipped (there is no existing MASTER row to cross-bind
        against); the gate is NOT bypassed -- missing/dropped specs or a
        non-shippable datasheet still FAIL.

    ``candidate_rows`` (optional) carries the PUBLISHED candidate MASTER rows
    (post-R2-upload) keyed by supplier_reference, so _check_publish_chain can
    validate the real, shippable datasheet_url instead of the RAW LCSC pdfUrl.
    Omit it for standalone CLI checks (host policy is still enforced by the
    dedicated check_datasheet_host_policy gate in plan_release).
    """
    master_rows, mfrs_by_mpn = load_master_index()
    lines = []

    cand_map = {}
    for r in (candidate_rows or []):
        ref = (r.get("supplier_reference") or "").strip()
        if ref:
            cand_map[ref] = r

    inv = {}
    for mpn, rows in master_rows.items():
        for r in rows:
            ref = (r.get("supplier_reference") or "").strip()
            if ref:
                inv.setdefault(ref, mpn)

    existing, new = [], []
    for cid in cids:
        (existing if cid in inv else new).append(cid)

    # Preload NEW-SKU RAW envelopes (filename == C#.json) once.
    new_envs = {}
    for cid in new:
        fp = os.path.join(RAW_DIR, cid + ".json")
        if os.path.exists(fp):
            try:
                with open(fp, encoding="utf-8") as f:
                    new_envs[cid] = json.load(f)
            except Exception:
                pass

    # mpn list for the clone-scan: existing mpns + recovered new mpns.
    scan_mpns = [inv[c] for c in existing]
    for cid, env in new_envs.items():
        m = (ha.flatten_envelope(env).get("mpn") or "").strip().upper()
        if m:
            scan_mpns.append(m)
    raw_identity = scan_raw_identity(scan_mpns)

    total_fail = total_warn = 0

    # ---- existing SKUs (original behaviour) ----
    for cid in existing:
        mpn = inv[cid]
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
        else:
            for level, code, msg in problems:
                lines.append("  %s %s: %s" % (level, code, msg))
                if level == "FAIL":
                    total_fail += 1
                else:
                    total_warn += 1

    # ---- new SKUs (publish path; no MASTER cross-binding) ----
    for cid in new:
        lines.append("===== NEW SKU (C# %s) -- publish-chain validation =====" % cid)
        if cid not in new_envs:
            lines.append("  FAIL COMPLETENESS: RAW file missing: %s"
                         % os.path.join(RAW_DIR, cid + ".json"))
            total_fail += 1
            continue
        env = new_envs[cid]
        problems = check_sku(cid, env, master_rows, mfrs_by_mpn, raw_identity)
        problems += _check_publish_chain(cid, env, cand_map.get(cid))
        if not problems:
            lines.append("  PASS (spec integrity + publish chain)")
        else:
            for level, code, msg in problems:
                lines.append("  %s %s: %s" % (level, code, msg))
                if level == "FAIL":
                    total_fail += 1
                else:
                    total_warn += 1

    lines.append("=== SUMMARY: %d SKU checked, %d FAIL, %d WARN ==="
                 % (len(cids), total_fail, total_warn))
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
