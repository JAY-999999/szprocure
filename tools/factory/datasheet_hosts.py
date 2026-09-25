# -*- coding: utf-8 -*-
"""Datasheet HOST policy — the SINGLE source of truth for "where may a
datasheet live".

FORMAL PRODUCTION RULE (user decision, 2026-09-24, R23)
-------------------------------------------------------
    "以后 原厂链接可以直接豁免，LCSC链接只能R2"

  * OEM / manufacturer (official) datasheet link  -> EXEMPT, keep verbatim.
  * LCSC datasheet link                            -> may only exist as an
                                                      R2-hosted copy. A bare
                                                      LCSC URL must never be
                                                      rendered on the storefront.
  * ANY other host                                 -> UNKNOWN, therefore NOT
                                                      shippable until a human
                                                      adds it to
                                                      OEM_HOST_PATTERNS.

WHY THIS MODULE EXISTS
----------------------
Before R23 the only guard was `pre_deploy_audit.DATASHEET_FORBIDDEN_HOSTS` =
`re.compile(r"lcsc\\.com")`. That is a blacklist of exactly one host, so it
(a) never caught a *new* third-party host, and (b) silently allowed anything
that is not literally lcsc.com. The rule above is an allow-list, so the
failure mode is the safe one: unknown -> stop, not unknown -> ship.

EVIDENCE BITES (measured on the production MASTER, 2026-09-24)
--------------------------------------------------------------
    host                                          rows
    pub-fd1103d4aed04a7c9cbf10d74caaaade.r2.dev  4756   (R2, the real host)
    www.nxp.com                                     33   (OEM, exempt)
    www.molex.com                                   28   (OEM, exempt)
    www.nxp.com.cn                                  19   (OEM, exempt)
                                                  ----
                                                  4836 populated
There is currently **zero** lcsc.com row in the live MASTER, so tightening the
gate cannot regress an already-listed SKU.

OEM_HOST_PATTERNS below carries ONLY the three hosts above, because those are
the only hosts the evidence supports. Do not add a host "just in case": an
unseen host is supposed to STOP here so that it gets looked at.
"""
from __future__ import annotations

import csv
import os
import re
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------
OK_R2 = "OK_R2"              # shippable: object-storage hosted
OK_OEM = "OK_OEM"            # shippable: official/manufacturer link (exempt)
BAD_LCSC = "BAD_LCSC"        # must be R2-hosted; rewriteable or not
BAD_UNKNOWN = "BAD_UNKNOWN"  # third-party host nobody has approved
BAD_SHAPE = "BAD_SHAPE"      # not a usable https datasheet URL

_SHIPPABLE = frozenset((OK_R2, OK_OEM))

# --------------------------------------------------------------------------
# host patterns
# --------------------------------------------------------------------------
# Every R2 public bucket is a *.r2.dev subdomain, so the suffix is enough and
# it survives an account/bucket rename. `datasheets/pdf/<..>` (content
# addressed, r2.R2_PREFIX) and `datasheets/<C# or mpn>.pdf` (the key the page
# actually uses) are BOTH covered — the URL shape is irrelevant, only the host.
R2_HOST_SUFFIX = ".r2.dev"

# LCSC is the only third-party *known* to leak; match the apex and any
# subdomain (datasheet.lcsc.com / www.lcsc.com / <cdn>.lcsc.com ...).
LCSC_HOST_SUFFIXES = (".lcsc.com",)

# Official / manufacturer hosts that are permanently EXEMPT.
# Measured evidence (see module docstring): nxp.com 33, molex.com 28,
# nxp.com.cn 19.
OEM_HOST_PATTERNS = (
    "nxp.com",
    "nxp.com.cn",
    "molex.com",
)

# A datasheet URL is only meaningful if it is https and looks like a file.
BAD_URL_TOKENS = re.compile(r"placeholder|example\.com|#$|\bTEST\b", re.I)

# --------------------------------------------------------------------------
# LCSC -> R2 rewrite index
# --------------------------------------------------------------------------
# The rewrite is only allowed when a *verified* mapping already exists. Two
# evidence sources, nothing else:
#   1. an existing MASTER row whose datasheet_url is on R2 (keyed by both the
#      supplier C# and the MPN);
#   2. 02_CLEAN/datasheet_map.csv (r2_url column, `r2_mpn` matched).
# Nothing is ever constructed from a filename guess or a name-intersection
# with the local PDF library: R2 keys and local `sha256__mpn.pdf` names live in
# different spaces and matching them would ship the wrong PDF.
DEFAULT_MAP_CSV = r"D:\SZ Procure\02_CLEAN\datasheet_map.csv"

# Field order deliberately mirrors release_pipeline._DS_FIELDS: the first
# non-empty one is the datasheet this identity actually binds.
DS_FIELDS = ("datasheet_url", "source_datasheet_url", "local_file",
             "r2_url", "r2_key", "pdf_url")

# CANDIDATE shape (an 02 record on its way into MASTER). After checking the
# real RAW / 02 / release chain, ONLY `datasheet_url` can reach MASTER:
#
#   * MASTER_COLS has no `source_datasheet_url` column, and
#     product_data.master_row() is a plain projection over MASTER_COLS, so
#     `source_datasheet_url` / `_source_datasheet_url` are DROPPED at staging;
#   * gen_parts.py:2394 renders exactly `row["datasheet_url"]`;
#   * an 02 candidate (lcsc_http_adapter.http_build_category_row) carries no
#     datasheet field at all — the PDF is bound later, by apply_datasheet_map
#     / build_datasheet_map, straight into `datasheet_url`.
#
# Policing `source_datasheet_url` on top of that would STOP every not-yet-mapped
# SKU (their datasheet_url is empty and the RAW pdfUrl is LCSC) for no benefit,
# because the value never reaches the page. Hence: one field, and write any
# rewrite back to the very key it was read from.
CANDIDATE_DS_FIELDS = ("datasheet_url",)


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------
def host_of(url):
    """Return the lower-cased netloc of *url*, or "" when unparsable."""
    if not url:
        return ""
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _matches(host, patterns):
    """True when *host* is *p* itself or a subdomain of it.

    Patterns in this module carry their own leading dot (``".lcsc.com"``), so
    the subdomain test is a plain ``endswith(p)`` — adding another dot would
    look for ``..lcsc.com`` and never match. A bare-pattern host (``"nxp.com"``
    in OEM_HOST_PATTERNS) therefore also matches ``www.nxp.com``, which is
    exactly what we want.
    """
    h = (host or "").lower()
    if not h:
        return False
    for p in patterns:
        p = p.lower()
        if h == p or h.endswith(p):
            return True
    return False


def is_r2(url, host=None):
    return _matches(host if host is not None else host_of(url),
                    (R2_HOST_SUFFIX,))


def is_lcsc(url, host=None):
    return _matches(host if host is not None else host_of(url),
                    LCSC_HOST_SUFFIXES)


def is_oem(url, host=None):
    return _matches(host if host is not None else host_of(url),
                    OEM_HOST_PATTERNS)


def classify(url):
    """-> (verdict, host, why). Pure function, touches nothing.

    ORDER MATTERS.  Host identity is decided FIRST, because the host is the
    actual policy subject ("where may this datasheet live?").  The URL-shape
    checks (https / placeholder) only ever *narrow* the verdict, they never
    pre-empt it.  Two consequences, both deliberate:

      * ``http://datasheet.lcsc.com/x.pdf``      -> BAD_LCSC   (LCSC wins:
        "left by mis-administration" is the more useful diagnosis than
        "not https");
      * ``https://example.com/d.pdf``            -> BAD_SHAPE  (the fake-link
        check fires on top of an unrecognised host).

    An OEM exemption covers the *host*, not broken syntax: an official link
    pointed at a placeholder or at plain http is still a broken link, so it
    still STOPs. That matches the pre-existing pre_deploy_audit semantics and
    keeps the gate fail-closed.
    """
    u = (url or "").strip()
    if not u:
        return ("", "", "empty")
    host = host_of(u)

    # 1) who is hosting this?  Policy question first.
    if is_r2(u, host):
        verdict = OK_R2
        why = "object storage (R2)"
    elif is_lcsc(u, host):
        verdict = BAD_LCSC
        why = "LCSC third-party link — must be R2-hosted"
    elif is_oem(u, host):
        verdict = OK_OEM
        why = "official/manufacturer link (exempt)"
    else:
        verdict = BAD_UNKNOWN
        why = ("unrecognised third-party host — add to OEM_HOST_PATTERNS "
               "or move the datasheet to R2")

    # 2) is it a usable URL at all?
    #    Narrowing is allowed for OK_* (travelling in broken) and for
    #    BAD_UNKNOWN (a fake link is a shape problem, not a host problem).
    #    It is NOT allowed to override BAD_LCSC: an LCSC link stays LCSC no
    #    matter what is wrong with its syntax.
    if verdict in (OK_R2, OK_OEM, BAD_UNKNOWN):
        if BAD_URL_TOKENS.search(u):
            return (BAD_SHAPE, host, "placeholder / # / TEST fake link")
        if not u.lower().startswith("https://"):
            return (BAD_SHAPE, host, "not an https URL")
    return (verdict, host, why)


def shippable(verdict):
    return verdict in _SHIPPABLE


# --------------------------------------------------------------------------
# LCSC -> R2 rewrite index
# --------------------------------------------------------------------------
def build_r2_index(master_rows=(), map_csv=DEFAULT_MAP_CSV, extra=None):
    """Build the rewrite table. Returns
    ``{"cid": {C#: url}, "mpn": {MPN: url}, "sources": [..], "counts": {...}}``.

    Never raises on a missing file: an absent index only means "nothing is
    rewriteable", which is the STOP outcome anyway (fail closed).
    """
    idx = {"cid": {}, "mpn": {}, "sources": [], "counts": {}}
    seen_cid = set()
    seen_mpn = set()

    def _add_cid(cid, url):
        cid = (cid or "").strip()
        if not cid or cid in seen_cid:
            return
        seen_cid.add(cid)
        idx["cid"][cid] = url

    def _add_mpn(mpn, url):
        mpn = (mpn or "").strip()
        if not mpn or mpn in seen_mpn:
            return
        seen_mpn.add(mpn)
        idx["mpn"][mpn] = url

    for r in (master_rows or ()):
        url = ""
        for f in DS_FIELDS:
            url = (r.get(f) or "").strip()
            if url:
                break
        v, _h, _why = classify(url)
        if v != OK_R2:
            continue
        _add_cid(r.get("supplier_reference"), url)
        _add_mpn(r.get("mpn"), url)
    idx["sources"].append("master_rows=%d" % len(master_rows or ()))

    if map_csv and os.path.exists(map_csv):
        n = 0
        skipped = 0
        try:
            with open(map_csv, encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    url = (row.get("r2_url") or "").strip()
                    if not is_r2(url):
                        skipped += 1
                        continue
                    # MPN only: datasheet_map.csv carries no supplier C#.
                    # local_file (`datasheets/<r2key>.pdf`) is a *path*, not
                    # a C#, and is deliberately NOT used as a cid key —
                    # keying on it would silently mis-map everything.
                    _add_mpn(row.get("mpn"), url)
                    n += 1
        except Exception as exc:                      # fail closed, but say so
            idx["sources"].append("map_csv=UNREADABLE:%s" % exc)
            idx["counts"] = {"cid": len(idx["cid"]), "mpn": len(idx["mpn"])}
            return idx
        idx["sources"].append("map_csv=%s usable=%d skipped=%d"
                              % (map_csv, n, skipped))

    for a, b, url in (extra or ()):
        if not is_r2(url):
            continue
        _add_cid(a, url)
        _add_mpn(b, url)

    idx["counts"] = {"cid": len(idx["cid"]), "mpn": len(idx["mpn"])}
    return idx


def resolve_r2(row, index):
    """Try to turn a row's LCSC datasheet into a verified R2 URL.

    Returns ``(url_or_None, path, why)`` where *path* is ``"cid"`` (exact C#
    hit — safe) or ``"mpn"`` (MPN-only hit — allowed but clone-risky, the
    caller must WARN) or ``None``.
    """
    if not index:
        return (None, None, "no index")
    cid = (row.get("supplier_reference") or "").strip()
    mpn = (row.get("mpn") or "").strip()
    if cid and cid in index.get("cid", {}):
        return (index["cid"][cid], "cid", "C#-exact R2 mapping")
    if mpn and mpn in index.get("mpn", {}):
        return (index["mpn"][mpn], "mpn", "MPN-keyed R2 mapping (clone risk)")
    return (None, None, "no verified R2 mapping for this identity")


# --------------------------------------------------------------------------
# row-level check (what the release gate / deploy audit call)
# --------------------------------------------------------------------------
def check_row(row, index=None, fields=None):
    """Inspect one candidate row.

    ``fields`` selects the key order (see DS_FIELDS / CANDIDATE_DS_FIELDS);
    it defaults to the MASTER order. Callers working on 02 candidates that are
    about to be projected with product_data.master_row() MUST pass
    CANDIDATE_DS_FIELDS, otherwise the field that actually reaches MASTER is
    not the one being policed.

    Returns a dict with keys:
        verdict / host / why / url / field
        rewritten   URL after a successful LCSC->R2 rewrite ("" if none)
        rewrite_path "cid" | "mpn" | ""   how the rewrite was proven
        action      "SHIP" | "REWRITE" | "STOP" | "NODATA"
    """
    url = ""
    field = ""
    for f in (fields or DS_FIELDS):
        url = (row.get(f) or "").strip()
        if url:
            field = f
            break

    # No datasheet bound at all is NOT a policy violation — it is simply a row
    # with no datasheet. The old audit only policed non-empty URLs, so keeping
    # it out of the STOP list matches today's behaviour and stops 121 empty
    # rows from drowning the report.
    if not url:
        return {"verdict": "", "host": "", "why": "no datasheet bound",
                "url": "", "field": "", "rewritten": "",
                "rewrite_path": "", "action": "NODATA"}

    verdict, host, why = classify(url)

    out = {"verdict": verdict, "host": host, "why": why, "url": url,
           "field": field, "rewritten": "", "rewrite_path": "", "action": ""}

    if verdict == OK_R2:
        out["action"] = "SHIP"
        return out
    if verdict == OK_OEM:
        out["action"] = "SHIP"
        return out
    if verdict == BAD_SHAPE:
        out["action"] = "STOP"
        return out

    # BAD_LCSC / BAD_UNKNOWN -> only LCSC with a proven mapping may survive,
    # and only the release gate is allowed to rewrite. A third-party non-LCSC
    # host never gets an auto-fix: a human must decide.
    if verdict == BAD_LCSC and index:
        new_url, path, rwhy = resolve_r2(row, index)
        if new_url:
            out["rewritten"] = new_url
            out["rewrite_path"] = path
            out["action"] = "REWRITE"
            out["why"] = "LCSC link rewritten to R2 via %s (%s)" % (path, rwhy)
            return out

    out["action"] = "STOP"
    return out


def check_rows(rows, index=None, fields=None):
    """-> (stops, rewrites, ships, warnings, nodata) for a batch of rows.

    ``fields`` behaves exactly as in check_row(). Callers working on 02
    candidates must pass CANDIDATE_DS_FIELDS for the reason spelled out there.

    ``ships`` holds SHIP rows as ``(i, identity, verdict, host)``;
    ``stops`` as ``(i, identity, why, url)``; ``rewrites`` as
    ``(i, identity, old_url, new_url, path)``.
    """
    stops, rewrites, ships, warns, nodata = [], [], [], [], []
    for i, r in enumerate(rows, 1):
        res = check_row(r, index, fields)
        cid = (r.get("supplier_reference") or "").strip()
        mpn = (r.get("mpn") or "").strip()
        who = "%s / %s" % (mpn, cid)
        if res["action"] == "NODATA":
            nodata.append((i, who))
        elif res["action"] == "STOP":
            stops.append((i, who, res["why"], res["url"]))
        elif res["action"] == "REWRITE":
            rewrites.append((i, who, res["url"], res["rewritten"],
                             res["rewrite_path"]))
            if res["rewrite_path"] == "mpn":
                warns.append((i, who,
                              "R2 mapping proven by MPN only; verify the "
                              "manufacturer before shipping"))
        else:
            ships.append((i, who, res["verdict"], res["host"]))
    return stops, rewrites, ships, warns, nodata
