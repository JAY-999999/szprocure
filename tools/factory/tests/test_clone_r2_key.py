# -*- coding: utf-8 -*-
"""2026-09-27 (R24-followup) — regression tests for the clone-safe R2 key.

Locks the fix: ``build_datasheet_map.r2_key`` must be C#-derived so a clone
family (same MPN, several manufacturers) never shares one R2 object. The legacy
MPN-derived key must stay detectable by ``datasheet_clones`` (so a clone still
bound to ``datasheets/<mpn>.pdf`` is caught by the C1 STOP gate).

Run:  python tools/factory/tests/test_clone_r2_key.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "tools", "factory"))

import build_datasheet_map as bdm
from datasheet_clones import clone_r2_key, _is_mpn_derived, check_clone_part_key

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, "PASS" if cond else "FAIL", detail))
    if not cond:
        print("  FAIL:", name, detail)


def test_r2_key_is_cid_derived():
    k1 = bdm.r2_key("C15127", "AO3401A")      # AOS
    k2 = bdm.r2_key("C347476", "AO3401A")     # UMW
    check("C#-derived key (AOS)", k1 == "c15127", k1)
    check("C#-derived key (UMW)", k2 == "c347476", k2)
    check("clone family -> DISTINCT keys (no overwrite)",
          k1 != k2, "%s vs %s" % (k1, k2))


def test_r2_key_fallback_to_mpn():
    # legacy rows with no C# still resolve to an MPN-derived key
    check("mpn fallback when no cid", bdm.r2_key("", "AO3401A") == "ao3401a",
          bdm.r2_key("", "AO3401A"))


def test_legacy_detector_still_mpn():
    # clone_r2_key MUST stay the legacy MPN-derived form, so the gate can still
    # catch a clone bound through the dangerous shared object.
    check("legacy detector is MPN-derived", clone_r2_key("AO3401A") == "ao3401a",
          clone_r2_key("AO3401A"))
    ref = "https://pub-x.r2.dev/datasheets/ao3401a.pdf"
    check("legacy MPN binding detected as unsafe",
          _is_mpn_derived(ref, "AO3401A") is True)


def test_gate_allows_cid_key_clone():
    # A clone family whose datasheet is bound through a C#-derived key must NOT
    # be stopped (rule C2: per-LCSC-part, cannot leak across manufacturers).
    rows = [{
        "mpn": "AO3401A",
        "supplier_reference": "C347476",
        "datasheet_url": "https://pub-x.r2.dev/datasheets/c347476.pdf",
    }]
    findings = check_clone_part_key(rows)
    stops = [f for f in findings if f[0] == "STOP"]
    check("C#-keyed clone NOT stopped", not stops, str(stops))


def test_gate_stops_mpn_key_clone():
    # A clone family still bound through the shared MPN-derived object MUST be
    # stopped (rule C1: fail-closed).
    rows = [{
        "mpn": "AO3401A",
        "supplier_reference": "C347476",
        "datasheet_url": "https://pub-x.r2.dev/datasheets/ao3401a.pdf",
    }]
    findings = check_clone_part_key(rows)
    stops = [f for f in findings if f[0] == "STOP"
             and f[1] == "CLONE_PART_MPN_KEY_FAIL"]
    check("MPN-keyed clone IS stopped", bool(stops), str(findings))


def main():
    test_r2_key_is_cid_derived()
    test_r2_key_fallback_to_mpn()
    test_legacy_detector_still_mpn()
    test_gate_allows_cid_key_clone()
    test_gate_stops_mpn_key_clone()
    npass = sum(1 for _, s, _ in RESULTS if s == "PASS")
    print("\n".join("%-4s %s %s" % (s, n, d) for n, s, d in RESULTS))
    print("\n%d PASS / %d FAIL" % (npass, len(RESULTS) - npass))
    return 0 if npass == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
