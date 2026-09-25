# -*- coding: utf-8 -*-
"""Clone-part datasheet ownership safety (2026-09-25, R24).

THE DEFECT THIS FIXES
---------------------
AO3401A is a clone part: LCSC sells it under at least two manufacturers at
once (AOS / C15127 and UMW / C347476). The datasheet binding chain is

    build_datasheet_map.r2_key(mpn)  ->  datasheets/<mpn>.pdf   (R2 object)
    upload_datasheets.py             -> PUT  datasheets/<r2_key>.pdf
    apply_datasheet_map.py           -> MASTER.datasheet_url

and ``r2_key()`` derives the object name from the MPN ALONE. One R2 object
therefore serves every manufacturer of that MPN, and the last uploader wins.
Zero-knowledge of C# anywhere on that chain is what let the AOS page render a
UMW datasheet (Alpha & Omega Semiconductor on the page, "UMW AO3401A /
UTD Semiconductor / umw-ic.com" inside the PDF).

The identity gate (``release_pipeline.check_datasheet_identity``) does NOT
catch it: it only compares the candidates of ONE batch against each other, and
a clone family whose members were released in different batches never collides
inside a single batch. This module closes that blind spot by asking a different
question — "does the RAW envelope say this MPN belongs to more than one
manufacturer?" — and failing closed when a manufacturer of such a family is
about to be bound through an MPN-derived key.

RULES (levels mirror the existing gates: STOP | WARN)
------------------------------------------------------
  C1  row MPN is a RAW-confirmed clone family (>1 manufacturer) AND the row's
      datasheet is bound through an MPN-derived R2 key
                                                     -> STOP (fail-closed)
      Reason: that object name is shared with the other manufacturers of the
      same MPN, so the PDF this row points at is provably not guaranteed to be
      this manufacturer's. Shipping it is shipping a wrong datasheet.
  C2  the row's datasheet is bound through a C#-derived key while the MPN is a
      clone family                                    -> OK (no finding)
      A C#-keyed binding is per-LCSC-part, so it cannot leak across
      manufacturers. This is the shape the pipeline should aim for.
  C3  the row's datasheet was proven by an official/OEM host (not R2) -> OK
  C4  MPN not present in the RAW index, or no datasheet at all -> no finding
      (fail-OPEN on missing evidence: a missing RAW file must never block a
      batch, exactly like ``alternates.audit_release_rows`` warns instead)

SCOPE NOTE
----------
This module is deliberately read-only with respect to MASTER. It never edits a
row's datasheet; the caller decides whether to STOP or, for an already-released
SKU, to scrub the URL in an authorised in-place correction.

SOURCE OF TRUTH
---------------
The RAW envelopes in ``D:/SZ Procure/采集流水线/基础数据`` (same directory and
same envelope shape ``alternates.real_alternates`` already uses), specifically
``source_raw.main_product.{productModel, brandNameEn, productCode}``.
RAW is indexed by C#, NEVER by MPN — a clone part's MPN is only ever an
*attribute*, and treating it as an identity is the bug.
"""

import json
import os
import re

# Must stay byte-identical to ``build_datasheet_map.KEY_SAFE`` / ``r2_key``:
# the gate decides "is this URL MPN-derived" by reproducing the very function
# that produced it. If that function is ever re-keyed (e.g. by C#), this
# module WILL have to follow, otherwise the gate silently passes everything.
KEY_SAFE = re.compile(r"[^a-z0-9._-]")

# Same field order as release_pipeline._DS_FIELDS: first non-empty wins.
_DS_FIELDS = ("datasheet_url", "source_datasheet_url", "local_file",
              "r2_url", "r2_key", "pdf_url")

# Hosts that prove ownership by themselves (the R23 "原厂链接可以直接豁免" rule).
_OFFICIAL_HOST_HINTS = ("nxp.com", "nxp.com.cn", "molex.com", "stm.com",
                        "st.com", "ti.com", "analog.com", "onsemi.com")

_DEFAULT_RAW_DIR = r"D:/SZ Procure/采集流水线/基础数据"

_INDEX_CACHE = {}
_INDEX_META = {}


# --------------------------------------------------------------------------
# RAW clone index
# --------------------------------------------------------------------------
def default_raw_dir():
    """Directory the 01-collected RAW envelopes live in."""
    return _DEFAULT_RAW_DIR


def clone_r2_key(mpn: str) -> str:
    """The R2 object name build_datasheet_map would derive for ``mpn``."""
    return KEY_SAFE.sub("-", (mpn or "").strip().lower())


def norm_mpn(pn: str) -> str:
    """Identity-normalised MPN: case/whitespace/punctuation insensitive."""
    return re.sub(r"[^a-z0-9]", "", (pn or "").strip().lower())


def build_clone_index(rawdir=None, refresh=False):
    """Index RAW envelopes as ``{norm_mpn: {...}}``.

    Each entry::

        {"mpn":      "AO3401A",
         "brands":   {"alphaandomegasemiconductor", "umw"},   # normalized
         "brand":    {"C15127": "AOS", "C347476": "UMW"},     # C# -> brand
         "cids":     ["C15127", "C347476"],
         "files":    n}                                        # RAW files read

    Reads only. Result is memoised per directory because plan_release() calls
    this for every batch and the RAW corpus is ~8.7k JSON files.
    """
    rawdir = rawdir or default_raw_dir()
    if not refresh and rawdir in _INDEX_CACHE:
        return _INDEX_CACHE[rawdir]

    index = {}
    read_fail = 0
    if not os.path.isdir(rawdir):
        _INDEX_CACHE[rawdir] = index
        return index

    # NB: endswith is CASE-SENSITIVE. `.upper()` on the whole name would turn
    # ".json" into ".JSON" and filter out every file (this exact bug silently
    # emptied the index on the first run of this gate). Match the suffix in
    # lower case, the prefix in upper.
    for fn in sorted(os.listdir(rawdir)):
        if not fn.lower().endswith(".json") or not fn.upper().startswith("C"):
            continue
        cid = os.path.splitext(fn)[0]
        fp = os.path.join(rawdir, fn)
        try:
            with open(fp, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            read_fail += 1
            continue
        if not isinstance(d, dict):
            read_fail += 1
            continue
        src = d.get("source_raw", d)
        if not isinstance(src, dict):
            read_fail += 1
            continue
        mp = src.get("main_product")
        if not isinstance(mp, dict):
            read_fail += 1
            continue
        mpn = str(mp.get("productModel") or mp.get("mpn") or "").strip()
        if not mpn:
            continue
        brand = str(mp.get("brandNameEn") or mp.get("brandName") or "").strip()
        k = norm_mpn(mpn)
        slot = index.setdefault(k, {"mpn": mpn, "brands": set(),
                                    "brand": {}, "cids": [], "files": 0})
        slot["files"] += 1
        if brand:
            nb = norm_mpn(brand)
            if nb:
                slot["brands"].add(nb)
                slot["brand"].setdefault(cid, brand)
        if cid not in slot["cids"]:
            slot["cids"].append(cid)

    for slot in index.values():
        slot["brands"] = sorted(slot["brands"])

    # The meta is kept OUT of the returned dict on purpose: callers iterate the
    # index to build synthetic test rows, and a stray "__meta__" entry there is
    # a KeyError waiting to happen (it already was in the first self-test).
    _INDEX_META[rawdir] = {"rawdir": rawdir, "read_fail": read_fail,
                           "mpns": len(index)}
    _INDEX_CACHE[rawdir] = index
    return index


def index_meta(rawdir=None):
    """Provenance of the last index build (rawdir / read_fail / mpns)."""
    rawdir = rawdir or default_raw_dir()
    return dict(_INDEX_META.get(rawdir, {}))


def is_clone_mpn(mpn: str, index) -> bool:
    """True when RAW shows ``mpn`` under more than one manufacturer."""
    slot = (index or {}).get(norm_mpn(mpn))
    return bool(slot) and len(slot.get("brands", [])) > 1


def brand_for_cid(cid: str, index):
    """The RAW brand of a specific C#, or '' when RAW cannot answer."""
    if not cid:
        return ""
    for slot in (index or {}).values():
        if not isinstance(slot, dict):
            continue
        b = slot.get("brand", {})
        if cid in b:
            return b[cid]
    return ""


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------
def _ds_ref(r):
    for f in _DS_FIELDS:
        v = (r.get(f) or "").strip()
        if v:
            return f, v
    return None, ""


def _object_name(ref: str) -> str:
    """The last path segment of a datasheet URL, lower-cased, .pdf stripped."""
    s = ref.rstrip("/")
    s = s.rsplit("/", 1)[-1]
    if s.lower().endswith(".pdf"):
        s = s[:-4]
    return s.strip().lower()


def _is_mpn_derived(ref: str, mpn: str) -> bool:
    return _object_name(ref) == clone_r2_key(mpn)


def _is_official(ref: str) -> bool:
    low = ref.lower()
    return any(h in low for h in _OFFICIAL_HOST_HINTS)


def check_clone_part_key(rows, index=None, rawdir=None):
    """Gate: a clone-family SKU may not be bound through an MPN-derived key.

    Returns a list of ``(level, kind, message)`` with levels ``STOP`` / ``WARN``.
    Pure function: reads rows and the index, mutates nothing.
    """
    index = index if index is not None else build_clone_index(rawdir)
    out = []
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        mpn = str(r.get("mpn") or "").strip()
        if not mpn:
            continue
        slot = index.get(norm_mpn(mpn))
        if not slot or len(slot.get("brands", [])) <= 1:
            continue                                    # C4: RAW says no clone
        f, ref = _ds_ref(r)
        cid = str(r.get("supplier_reference") or r.get("supplier_sku") or "").strip()
        raw_brand = brand_for_cid(cid, index)
        who = "%s / %s" % (mpn, cid or "-")

        if not ref or not f:
            continue                                    # C4: no datasheet
        if _is_official(ref):
            continue                                    # C3: OEM link proves it
        if _is_mpn_derived(ref, mpn):
            out.append(("STOP", "CLONE_PART_MPN_KEY_FAIL",
                        "%s: MPN %r is a RAW-confirmed clone family "
                        "(manufacturers: %s) but binds the MPN-derived R2 "
                        "object %r. RAW says C# %s belongs to %s, so that "
                        "object -- which the other manufacturers of %r also "
                        "resolve to -- is not guaranteed to be that "
                        "manufacturer's datasheet. Rebind through a C#-derived "
                        "key before release."
                        % (who, mpn, ", ".join(slot["brands"]), ref,
                           cid or "?", raw_brand or "this manufacturer", mpn)))
        # C2: anything else (C#-derived key) is per-LCSC-part and stays allowed.
    return out


def scrub_clone_mpn_key_rows(rows, index=None, rawdir=None):
    """Fail-safe: blank out MPN-derived datasheets on clone-family rows.

    Used for ALREADY-RELEASED SKUs, where a STOP is not an option (the batch is
    over; the defect is historical). Dropping the URL makes the page render no
    datasheet button rather than the WRONG one — "no datasheet" is honest,
    "somebody else's datasheet" is a factual error on a customer-facing page.

    Returns ``(n_scrubbed, logs)``. Only touch this for a row the human
    explicitly authorised (release_pipeline.allow_row_update_mpns).
    """
    index = index if index is not None else build_clone_index(rawdir)
    scrubbed, logs = 0, []
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        mpn = str(r.get("mpn") or "").strip()
        if not mpn:
            continue
        slot = index.get(norm_mpn(mpn))
        if not slot or len(slot.get("brands", [])) <= 1:
            continue
        f, ref = _ds_ref(r)
        if not ref or not _is_mpn_derived(ref, mpn):
            continue
        if _is_official(ref):
            continue
        r[f] = ""
        for g in _DS_FIELDS:                            # keep the row coherent
            if g != f and (r.get(g) or "").strip() and _is_mpn_derived(str(r[g]), mpn):
                r[g] = ""
        scrubbed += 1
        logs.append("%s: %s=%r -> '' (clone family %s; refused to render "
                    "another manufacturer's datasheet)"
                    % (mpn, f, ref, ", ".join(slot["brands"])))
    return scrubbed, logs


__all__ = ["default_raw_dir", "clone_r2_key", "norm_mpn", "build_clone_index",
           "is_clone_mpn", "brand_for_cid", "check_clone_part_key",
           "scrub_clone_mpn_key_rows"]
