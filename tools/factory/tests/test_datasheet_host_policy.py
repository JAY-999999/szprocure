"""2026-09-24 (R23) — regression tests for the datasheet HOST policy.

The rule, in the operator's own words::

    "以后 原厂链接可以直接豁免，LCSC链接只能R2"

  * official / manufacturer (OEM) link -> EXEMPT, kept verbatim;
  * LCSC link                          -> only allowed as an R2-hosted copy;
  * every other host                   -> unapproved, therefore STOP.

Before R23 the only guard was ``pre_deploy_audit.DATASHEET_FORBIDDEN_HOSTS`` =
``re.compile(r"lcsc\\.com")``: a one-host blacklist, so it could never catch a
*new* third-party vendor and it silently passed anything that was not literally
lcsc.com. R23 turns it into an allow-list, which means the safe failure mode is
"unknown -> stop" instead of "unknown -> ship".

What is pinned here:

  1. Classification order — the HOST decides first, URL-shape checks only
     narrow the verdict (``http://datasheet.lcsc.com/x.pdf`` is BAD_LCSC, not
     BAD_SHAPE).
  2. OEM exemption covers the host, not broken syntax: an official link pointed
     at a placeholder or plain http still STOPs.
  3. Scope of the gate: ONLY the field that can actually reach MASTER is
     policed. ``MASTER_COLS`` has no ``source_datasheet_url``, the 02 candidate
     carries no datasheet field at all, and the PDF is bound later into
     ``datasheet_url`` (which is also what gen_parts.py:2394 renders) — so an
     LCSC ``source_datasheet_url`` with an empty ``datasheet_url`` is NODATA,
     not a violation. Blocking it would stall every not-yet-mapped SKU.
  4. A proven R2 mapping rewrites the row IN PLACE under the key it was read
     from, so the URL that actually reaches MASTER is the rewritten one.
  5. LCSC with no verified R2 mapping, an unapproved third-party host, and a
     broken URL shape all STOP with ``DATASHEET_NOT_R2_FAIL``.
  6. An MPN-only mapping is allowed but WARNed (clone risk).
  7. A row with no datasheet at all is ignored (NODATA), matching the old
     "only non-empty URLs are policed" behaviour.
  8. End to end: ``plan_release`` stops a candidate whose LCSC PDF is not on R2
     and rewrites one that is.

All tests are read-only w.r.t. production data. Run with pytest or directly.
"""
import csv
import os
import shutil
import sys
import tempfile

sys.path.insert(0, r"D:/SZ Procure/site/tools")
sys.path.insert(0, r"D:/SZ Procure/site")

from factory import datasheet_hosts, master_io, product_data, release_pipeline

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append((name, ok, f"got={got!r} want={want!r}"))
    print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"  [{got!r} != {want!r}]"))


def case(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else f"  [{detail}]"))


R2 = "https://pub-fd1103d4aed04a7c9cbf10d74caaaade.r2.dev/datasheets/C10002.pdf"
LCSC = "https://datasheet.lcsc.com/C10002.pdf"

# --------------------------------------------------------------------------
# 1. classification matrix
# --------------------------------------------------------------------------
_MATRIX = [
    (R2, datasheet_hosts.OK_R2),
    ("https://pub-xxx.r2.dev/datasheets/pdf/ab/cd1234.pdf", datasheet_hosts.OK_R2),
    ("https://www.nxp.com/docs/en/data-sheet/MC9S12.pdf", datasheet_hosts.OK_OEM),
    ("https://www.molex.com/pdm_datasheet/molex/ds123.pdf", datasheet_hosts.OK_OEM),
    ("https://www.nxp.com.cn/foo.pdf", datasheet_hosts.OK_OEM),
    ("http://www.lcsc.com/pdfs/C1.pdf", datasheet_hosts.BAD_LCSC),       # host first
    ("https://datasheet.lcsc.com/C1.pdf", datasheet_hosts.BAD_LCSC),
    ("http://www.nxp.com/docs/place_holder.pdf", datasheet_hosts.BAD_SHAPE),  # OEM does not excuse shape
    ("https://example.com/datasheet/test.pdf", datasheet_hosts.BAD_SHAPE),
    ("https://cdn.orientdb.com/ds/x.pdf", datasheet_hosts.BAD_UNKNOWN),
    ("http://www.example.com/d.pdf", datasheet_hosts.BAD_SHAPE),
]
check("classify/order matrix (%d cases)" % len(_MATRIX),
      [datasheet_hosts.classify(u)[0] for u, _v in _MATRIX], [v for _u, v in _MATRIX])

# --------------------------------------------------------------------------
# 2. shippable()
# --------------------------------------------------------------------------
check("shippable()",
      [datasheet_hosts.shippable(v) for v in
       (datasheet_hosts.OK_R2, datasheet_hosts.OK_OEM, datasheet_hosts.BAD_LCSC,
        datasheet_hosts.BAD_UNKNOWN, datasheet_hosts.BAD_SHAPE)],
      [True, True, False, False, False])

# --------------------------------------------------------------------------
# 3. scope: only what reaches MASTER is policed
# --------------------------------------------------------------------------
_idx = {"cid": {"C99999": R2}, "mpn": {}, "sources": [], "counts": {}}
# An 02 candidate whose RAW pdfUrl is still LCSC and which has no R2 binding
# yet: nothing LCSC reaches MASTER (MASTER_COLS drops source_datasheet_url), so
# this is NODATA. It must NOT stop -- that is the 121 empty-datasheet SKUs.
r = {"mpn": "UNMAPPED", "supplier_reference": "C88888",
     "datasheet_url": "", "source_datasheet_url": LCSC}
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("LCSC raw pdfUrl with no bound datasheet_url is NODATA (not a stop)", f, [])
# Projection proof: the source key really is dropped on the way into MASTER.
check("projection drops the raw source key",
      product_data.master_row(r).get("source_datasheet_url"), None)
# What DOES reach MASTER is `datasheet_url`: an LCSC value there is a violation
# even when the raw source key is the same URL (same SKU, two names).
r = {"mpn": "BOUND", "supplier_reference": "C88889", "datasheet_url": LCSC,
     "source_datasheet_url": LCSC}
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("LCSC in the MASTER column is policed",
      ([x[0] for x in f], [x[1] for x in f]), (["STOP"],
                                                [release_pipeline.DATASHEET_NOT_R2_FAIL]))

# --------------------------------------------------------------------------
# 4/5/6. rewrite paths
# --------------------------------------------------------------------------
def _row(cid, mpn, url, **kw):
    # The MASTER column is `datasheet_url` (the only key the projection keeps);
    # the RAW key is kept too, mirroring a real candidate that carries both.
    r = {"mpn": mpn, "supplier_reference": cid, "datasheet_url": url,
         "source_datasheet_url": url}
    r.update(kw)
    return r


# 4a. C#-exact -> rewrite, cid warn, no stop
r = _row("C99999", "AMS1117", LCSC)
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("cid-exact rewrite commits into the row",
      (r["datasheet_url"], [x[1] for x in f], [x[0] for x in f]),
      (R2, ["DATASHEET_R2_BY_CID"], ["WARN"]))
check("rewrite target is the field that reaches MASTER",
      product_data.master_row(r)["datasheet_url"], R2)

# 6. MPN-only mapping -> allowed but WARNed (clone risk)
r = _row("C77777", "SOMEFOO", LCSC)
mpn_idx = {"cid": {}, "mpn": {"SOMEFOO": R2}, "sources": [], "counts": {}}
f = release_pipeline.check_datasheet_host_policy([r], mpn_idx)
check("mpn-keyed rewrite warns, never stops",
      (r["datasheet_url"], [x[0] for x in f], [x[1] for x in f]),
      (R2, ["WARN"], ["DATASHEET_R2_BY_MPN"]))

# 5a. LCSC with no proven mapping -> STOP, row untouched
r = _row("C55555", "NOPE1", LCSC)
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("unmapped LCSC stops",
      (len(f), f[0][0], f[0][1], r["datasheet_url"]),
      (1, "STOP", release_pipeline.DATASHEET_NOT_R2_FAIL, LCSC))

# 5b. unapproved third-party host -> STOP
r = _row("C55556", "NOPE2", "https://cdn.vendor.example.net/ds.pdf")
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("unapproved third-party host stops",
      (len(f), f[0][0], f[0][1]), (1, "STOP", release_pipeline.DATASHEET_NOT_R2_FAIL))

# 5c. broken shape on an OEM link -> STOP (the exemption is the host, not syntax)
r = _row("C55557", "NOPE3", "http://www.nxp.com/docs/place_holder.pdf")
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("broken OEM link still stops",
      (len(f), f[0][0], f[0][1]), (1, "STOP", release_pipeline.DATASHEET_NOT_R2_FAIL))

# 2/7. OEM passes untouched, NODATA ignored
r = _row("C55558", "OKNXP", "https://www.nxp.com/docs/en/ds.pdf")
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("OEM untouched",
      (r["datasheet_url"], f), (r["datasheet_url"], []))
r = _row("C55559", "NODATA1", "")
f = release_pipeline.check_datasheet_host_policy([r], _idx)
check("row without a datasheet is ignored", f, [])

# --------------------------------------------------------------------------
# 8. end to end through plan_release (read-only: a throw-away MASTER copy)
# --------------------------------------------------------------------------
def _plan_smoke(rows, index_rows):
    """plan_release against a throw-away MASTER that already carries *index_rows*.

    The index rows are written as real MASTER rows (``datasheet_url`` is the
    MASTER column; ``source_datasheet_url`` is not one of them)."""
    tmpdir = tempfile.mkdtemp(prefix="ds_host_gate_")
    try:
        mpath = os.path.join(tmpdir, "master.csv")
        _cols, _old = master_io.read_master(
            r"D:/SZ Procure/site/data/production/master_parts_v2.1.csv", None)
        cols = list(_cols)
        with open(mpath, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in index_rows:
                w.writerow({c: (r.get(c) or "") for c in cols if c in r})
        return release_pipeline.plan_release(mpath, rows)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _candidate(mpn, cid, url):
    # `datasheet_url` is the MASTER column the candidate will be staged with.
    return {"mpn": mpn, "manufacturer": "AOS", "description": "x",
            "supplier_reference": cid, "datasheet_url": url,
            "source_datasheet_url": url, "category": "Integrated Circuits",
            "native_l1": "Integrated Circuits", "attributes_json": "{}"}


try:
    ok_master = os.path.exists(r"D:/SZ Procure/site/data/production/master_parts_v2.1.csv")
    if ok_master:
        # C100026 / BCX56-16,115 is a real RAW SKU (so the spec-integrity gate
        # passes) but its MPN is absent from datasheet_map.csv, so neither the
        # C# nor the MPN resolves -> the LCSC link must STOP the batch.
        # (AO3401A would NOT do here: it is in the map and gets rewritten by
        # MPN, which is the next case.)
        p = _plan_smoke([_candidate("BCX56-16,115", "C100026", LCSC)], [])
        codes = [s["code"] for s in p.stops]
        case("plan_release STOPs an LCSC candidate with no R2 mapping",
             release_pipeline.DATASHEET_NOT_R2_FAIL in codes, str(codes))
        p = _plan_smoke([_candidate("AO3401A", "C15127", LCSC)],
                        [{"mpn": "AO3401A", "supplier_reference": "C15127",
                          "datasheet_url": R2}])
        kinds = [k for _l, k, _m in p.datasheet_host_policy]
        case("plan_release rewrites an LCSC candidate whose C# is on R2",
              "DATASHEET_R2_BY_CID" in kinds
              and release_pipeline.DATASHEET_NOT_R2_FAIL not in
              [s["code"] for s in p.stops], str(kinds))
        case("plan_release leaves no LCSC in the projected row",
              all("lcsc" not in (r.get("datasheet_url") or "").lower()
                  for r in p.new_rows), "see rewrites above")
    else:
        print("SKIP  plan_release smoke (production MASTER not found)")
except Exception as exc:                                   # pragma: no cover
    case("plan_release smoke", False, "%s: %s" % (type(exc).__name__, exc))

# --------------------------------------------------------------------------
print()
bad = [r for r in RESULTS if not r[1]]
print(f"{len(RESULTS) - len(bad)} PASS / {len(bad)} FAIL")
if bad:
    for n, _ok, d in bad:
        print("  FAIL:", n, d)
sys.exit(1 if bad else 0)
