# -*- coding: utf-8 -*-
"""Alternative Parts — the SINGLE source of truth for the alternate list.

FORMAL PRODUCTION RULE (final, 2026-09-25). DO NOT duplicate this logic
anywhere else: the 02 layer (lcsc_http_adapter.flatten_envelope, which writes
MASTER `alternative_parts`) and the 03 renderer (gen_parts.resolve_alternatives)
both call into this module so the two can never drift apart.

    "Alternative"      -> RAW `alternatePartList`          (the 02 source)
    MASTER column      -> `alternative_parts`              (written by 02)
    03 source          -> `split_multi(alternative_parts)` + RAW alts

THE RULE IS PURE AS-IS REPLAY
-----------------------------
Whatever the 01-collected RAW `alternatePartList` carries is what the page
shows, in RAW order. There is deliberately NO filter layer on top of it:

  * no manufacturer gate  — cross-brand entries are part of LCSC's own
                            Alternative Parts list and are shown with it;
  * no LCSC stock gate    — an out-of-stock alternate IS still an alternate.
                            LCSC's own page hides zero-stock entries, but we
                            are not LCSC: the parts LCSC cannot supply, we can
                            source from Huaqiangbei. Dropping them would hide
                            exactly the scarce / EOL opportunities we sell
                            (business decision 2026-09-25).
  * no `hasAlternatePart` gate — measured over all 8,701 RAW files the flag is
                            the string 'False' for 44,138/44,138 entries and
                            'True' for none, so gating on it would switch the
                            whole feature off rather than filter anything;
  * no self-exclusion     — if LCSC lists the part itself, that is what the
                            source says (cf. AO3400 -> "AO3400; FS3402; ...");
  * no de-duplication     — the page mirrors the RAW list 1:1;
  * entries skipped: non-dict entries and entries whose `productModel` is empty
    (nothing to render for such an entry).

KNOWN LIMITATION (snapshot drift, NOT a filter): the RAW snapshot is a
point-in-time capture (2026-09-14 batch). Entries LCSC added AFTER the capture
(e.g. C2931360 / 62684-402100ALF now also lists HC-FPC-0.5-40P-CSH20, which
does not exist anywhere in our RAW file) cannot appear on our page until the
SKU is re-collected. The pipeline must NEVER fabricate such entries.

The 5-field shape below is the canonical normalized record; the 02 layer
serializes `mpn` into `alternative_parts` and the 03 layer renders it.
"""

# Fields carried by every normalized alternate (the canonical 5-field shape).
ALT_FIELDS = ("mpn", "manufacturer", "lcsc", "type", "package")

# Package/type placeholder kept for the structured detail column; unused by the
# page layout, which renders plain MPN links.
ALT_DEFAULT_TYPE = "Similar"


def default_raw_dir():
    """Directory the 01-collected RAW envelopes live in (the AS-IS source)."""
    return r"D:/SZ Procure/采集流水线/基础数据"


def audit_release_rows(rows, rawdir=None):
    """Release gate: does every candidate still match its RAW AS-IS list?

    Called by ``release_pipeline.plan_release`` so a future batch can never
    ship a MASTER row whose ``alternative_parts`` disagrees with the RAW
    ``alternatePartList`` — the exact defect this module was created to fix
    (02 used to read ``source_raw.substitutes``, a field 01 never captures,
    so every SKU was written with an EMPTY list).

    ``rows``      — 02-produced candidate records (they carry
                    ``alternative_parts`` / ``alternative_parts_detail``).
    ``rawdir``    — RAW directory; defaults to ``default_raw_dir()``.

    Returns ``(fails, warns, report)``:
      * ``fails``  — C#'s whose candidate value != the RAW AS-IS replay.
                     A FAIL is a stop (fail-closed): the SKU is released with
                     the wrong alternate list.
      * ``warns``  — C#'s with no readable RAW file in ``rawdir`` (e.g. the
                     SKU came from another source). Not a stop; the renderer's
                     RAW index can still answer, and we never want a missing
                     RAW file to block a batch.
      * ``report`` — human-readable lines, mirrored onto the ReleasePlan.
    """
    import glob as _glob
    import json as _json
    import os as _os

    rawdir = rawdir or default_raw_dir()
    lookup = {}
    for fp in _glob.glob(_os.path.join(rawdir, "C*.json")):
        cid = _os.path.basename(fp)
        lookup[cid[:-5].upper()] = fp

    fails, warns, report = [], [], []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        # A 02 candidate record carries `supplier_sku`; a MASTER row carries
        # `supplier_reference`. Both are the C# (supplier_reference) identity,
        # so either one answers the gate.
        cid = str(r.get("supplier_reference") or r.get("supplier_sku") or "").strip()
        if not cid:
            continue
        fp = lookup.get(cid.upper())
        if not fp:
            warns.append((cid, "no RAW file in %s" % rawdir))
            continue
        try:
            with open(fp, encoding="utf-8") as fh:
                d = _json.load(fh)
        except (OSError, ValueError):
            warns.append((cid, "RAW unreadable"))
            continue
        src = d.get("source_raw", d)
        mp = (src.get("main_product") or {}) if isinstance(src, dict) else {}
        if not isinstance(src, dict) or not isinstance(mp, dict):
            warns.append((cid, "RAW envelope shape unexpected"))
            continue
        asis = real_alternates(src, mp)
        want = "; ".join(s["mpn"] for s in asis)
        got = str(r.get("alternative_parts") or "").strip()
        if got != want:
            fails.append((cid, want, got, len(asis)))
            report.append("FAIL %s: candidate alternative_parts != RAW AS-IS "
                          "(%d entries; RAW=%r candidate=%r)"
                          % (cid, len(asis), want[:80], got[:80]))
    for cid, why in warns:
        report.append("WARN %s: %s (skipped by the AS-IS gate)" % (cid, why))
    return fails, warns, report


def real_alternates(src, mp):
    """Return the SKU's RAW alternate list, in RAW order.

    `src`  — the RAW envelope (or its `source_raw` mapping).
    `mp`   — the `source_raw.main_product` mapping of the same SKU.
    Returns a list of {mpn, manufacturer, lcsc, type, package}; [] when the
    SKU has no alternate list. Never raises, never guesses, never fills.
    """
    if not isinstance(src, dict) or not isinstance(mp, dict):
        return []
    lst = src.get("alternatePartList")
    if not isinstance(lst, list):
        lst = mp.get("alternatePartList")
    if not isinstance(lst, list):
        return []
    out = []
    for al in lst:
        if not isinstance(al, dict):
            continue
        model = str(al.get("productModel") or al.get("mpn") or "").strip()
        if not model:
            continue                       # nothing to render for this entry
        out.append({
            "mpn": model,
            "manufacturer": str(al.get("brandNameEn") or al.get("manufacturer") or "").strip(),
            "lcsc": str(al.get("productCode") or al.get("lcsc") or "").strip(),
            "type": str(al.get("type") or ALT_DEFAULT_TYPE).strip() or ALT_DEFAULT_TYPE,
            "package": str(al.get("encapStandard") or al.get("package") or "").strip(),
        })
    return out
