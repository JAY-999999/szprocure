"""P1-D-C — Wave / Slice Orchestrator tests (15 scenarios).

Runs entirely inside temp dirs (never touches production MASTER / pool / R2 /
git).  Uses a loopback HTTP server (fake datasheet host) and an in-memory Mock
R2 client — NO real 500 / 5000 / 10000 collection.

Scenarios
---------
 S1   5000 -> 10x500 slice planning
 S2   RAW internal duplicate MPN -> collapsed (counted)
 S3   540 MASTER MPNs excluded from slices
 S4   selector filter
 S5   generic exclude_mpns filter
 S6   Wave / Slice identity (wave_id + batch_id format)
 S7   2nd ingest idempotent (no RAW re-scan, no slice re-gen)
 S8   failed slice resume ALONE (other slices untouched)
 S9   failed-slice recovery via whole-wave retry
 S10  READY slice re-run -> NO re-download (new_downloads == 0)
 S11  PDF hash de-duplication (shared datasheet -> 1 physical file)
 S12  R2 idempotency (re-run -> already_exists == total, no put_object)
 S13  mid-crash recovery (DOWNLOADING record -> VERIFIED)
 S14  production MASTER SHA256 unchanged (read-only)
 S15  Run never triggers Build / Commit / Push / Deploy

Run:  python tests/test_p1dc_slice_wave.py
Exit 0 = all pass.
"""
import os
import sys
import csv
import json
import shutil
import tempfile
import threading
import hashlib
import http.server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import botocore.exceptions
from factory import slice_planner as sp, batch_runner as br, ids, manifest as MAN
from factory import product_data, datasheet as ds, r2, pool, gate

VENV = (r"C:\Users\Administrator.SC-202105071542\.workbuddy\binaries"
        r"\python\envs\default\Scripts\python.exe")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name}  -- {detail}")
    return cond


# --------------------------------------------------------------------------
# PDF server (loopback datasheet host; bytes vary by path -> distinct SHA256)
# --------------------------------------------------------------------------
class _PDFHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = (b"%PDF-1.4\n"
                + (self.path.encode() + b"X" * 2000)
                + b"\n%%EOF\n")
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def start_server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PDFHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# --------------------------------------------------------------------------
# Mock R2 (in-memory S3-compatible loopback)
# --------------------------------------------------------------------------
class _Bytes:
    def __init__(self, b):
        self.b = b

    def read(self):
        return self.b


class MockR2:
    def __init__(self):
        self.store = {}

    def put_object(self, Bucket, Key, Body, ContentType=None, Metadata=None):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = (data, dict(Metadata or {}))
        return {"ETag": '"%s"' % hashlib.md5(data).hexdigest()}

    def head_object(self, Bucket, Key):
        if Key not in self.store:
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "404", "Message": "NoSuchKey"}}, "HeadObject")
        data, meta = self.store[Key]
        return {"ContentLength": len(data),
                "ETag": '"%s"' % hashlib.md5(data).hexdigest(),
                "Metadata": meta}

    def get_object(self, Bucket, Key):
        if Key not in self.store:
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "404", "Message": "NoSuchKey"}}, "GetObject")
        data, meta = self.store[Key]
        return {"Body": _Bytes(data),
                "ContentLength": len(data),
                "ETag": '"%s"' % hashlib.md5(data).hexdigest(),
                "Metadata": meta}


# --------------------------------------------------------------------------
# sandbox + fixtures
# --------------------------------------------------------------------------
RAW_COLS = ["rank", "supplier", "supplier_sku", "mpn", "manufacturer_raw",
            "catalogName", "category", "description", "attributes_json",
            "source_image_url", "source_datasheet_url", "stock", "source"]


def new_sandbox():
    tmp = tempfile.mkdtemp(prefix="p1dc_sandbox_")
    pool_root = os.path.join(tmp, "pool")
    root = os.path.join(tmp, "batches")
    backup_root = os.path.join(tmp, "backups")
    os.makedirs(pool_root, exist_ok=True)
    os.makedirs(root, exist_ok=True)
    return tmp, pool_root, root, backup_root


def _row(mpn, port, url_override=None, brand=None):
    brand = brand or ("STMicroelectronics" if mpn.startswith("STM")
                      else "NXP" if mpn.startswith("LPC")
                      else "Generic")
    url = url_override or f"http://127.0.0.1:{port}/{mpn}.pdf"
    return {
        "rank": "1", "supplier": "C1", "supplier_sku": f"SUP-{mpn}",
        "mpn": mpn, "manufacturer_raw": brand, "catalogName": "MCU",
        "category": "Microcontroller",
        "description": f"{brand} {mpn} 32-bit ARM Cortex-M4 MCU 64KB Flash",
        "attributes_json": json.dumps({"CPU位数": "32", "CPU最大主频": "72"}),
        "source_image_url": "", "source_datasheet_url": url,
        "stock": "100", "source": "LCSC",
    }


def write_raw_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RAW_COLS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_master_csv(path, mpns):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["mpn", "manufacturer", "brand", "category",
                    "subcategory", "description", "attributes_json"])
        for m in mpns:
            w.writerow([m, "x", "x", "Microcontroller", "MCU", "d", "{}"])


def write_mfr_csv(path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("STMicroelectronics\tSTMicroelectronics\n")
        f.write("NXP\tNXP\n")
        f.write("Generic\tGeneric\n")


def run_opts(sandbox, raw, master, mfr, port, mock, extra=None):
    tmp, pr, root, bk = sandbox
    o = {
        "raw_source": raw, "master_csv": master, "mfr_csv": mfr,
        "require_datasheet": True, "workers": 3, "retries": 5,
        "timeout": 60, "backoff": 2,
        "r2_client": mock, "r2_bucket": "test",
    }
    if extra:
        o.update(extra)
    return o, pr, root, bk


def count_physical_pdfs(pool_root):
    base = os.path.join(pool_root, "datasheets", "pdf")
    n = 0
    for _dir, _, files in os.walk(base):
        n += len([f for f in files if f.endswith(".pdf")])
    return n


# --------------------------------------------------------------------------
# S1 — 5000 -> 10x500
# --------------------------------------------------------------------------
def s1_5000_slice_planning(port):
    print("\n[S1] 5000 -> 10x500 slice planning")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw5000.csv")
    rows = [_row(f"STM32F{i:05d}", port) for i in range(5000)]
    write_raw_csv(raw, rows)
    res = sp.ingest(raw_source=raw, strategy="OPAMP", slice_size=500,
                    pool_root=pr, root=root)
    check("S1 slice_count == 10", res.slice_count == 10, res.summary())
    check("S1 each slice 500", all(
        s["mpn_count"] == 500 for s in
        sp._read_wave_manifest(sp.wave_manifest_path(pr, res.wave_id))["slices"]),
        "slice sizes")
    check("S1 selected == 5000", res.selected == 5000, res.summary())


# --------------------------------------------------------------------------
# S2 — RAW internal duplicate MPN collapsed
# --------------------------------------------------------------------------
def s2_raw_internal_dup(port):
    print("\n[S2] RAW internal duplicate MPN -> collapsed")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_dup.csv")
    rows = [_row(f"STM32F{i:03d}", port) for i in range(10)]
    # append 5 duplicate rows (same mpn, repeated)
    for d in range(5):
        rows.append(_row(f"STM32F{d:03d}", port))
    write_raw_csv(raw, rows)
    res = sp.ingest(raw_source=raw, strategy="DUP", slice_size=500,
                    pool_root=pr, root=root)
    check("S2 input_total == 15", res.input_total == 15, res.summary())
    check("S2 internal_dups == 5", res.internal_dups == 5, res.summary())
    check("S2 selected == 10 (unique)", res.selected == 10, res.summary())


# --------------------------------------------------------------------------
# S3 — 540 MASTER MPNs excluded
# --------------------------------------------------------------------------
def s3_master_exclude(port):
    print("\n[S3] 540 MASTER MPNs excluded from slices")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw600.csv")
    master = os.path.join(tmp, "master540.csv")
    m_ids = [f"STM32F{i:04d}" for i in range(600)]
    write_raw_csv(raw, [_row(m, port) for m in m_ids])
    write_master_csv(master, m_ids[:540])   # first 540 overlap
    res = sp.ingest(raw_source=raw, strategy="MEX", master_csv=master,
                    slice_size=500, pool_root=pr, root=root)
    check("S3 excluded == 540", res.excluded == 540, res.summary())
    check("S3 selected == 60", res.selected == 60, res.summary())
    # assert none of the 540 master mpns appear in any slice CSV
    wm = sp._read_wave_manifest(sp.wave_manifest_path(pr, res.wave_id))
    slice_mpns = set()
    for sl in wm["slices"]:
        with open(sl["path"], encoding="utf-8") as f:
            for r in csv.DictReader(f):
                slice_mpns.add(r["mpn"])
    overlap = slice_mpns & set(m_ids[:540])
    check("S3 no master MPN in slices", len(overlap) == 0, overlap)


# --------------------------------------------------------------------------
# S4 — selector filter
# --------------------------------------------------------------------------
def s4_selector(port):
    print("\n[S4] selector filter")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_sel.csv")
    mpns = [f"STM32F{i:03d}" for i in range(10)]
    write_raw_csv(raw, [_row(m, port) for m in mpns])
    sel = set(mpns[:3])
    res = sp.ingest(raw_source=raw, strategy="SEL", selector=sel,
                    slice_size=500, pool_root=pr, root=root)
    check("S4 selected == 3", res.selected == 3, res.summary())
    wm = sp._read_wave_manifest(sp.wave_manifest_path(pr, res.wave_id))
    got = set()
    for sl in wm["slices"]:
        with open(sl["path"], encoding="utf-8") as f:
            for r in csv.DictReader(f):
                got.add(r["mpn"])
    check("S4 only selector mpns", got == sel, got)


# --------------------------------------------------------------------------
# S5 — generic exclude_mpns
# --------------------------------------------------------------------------
def s5_exclude_mpns(port):
    print("\n[S5] generic exclude_mpns filter")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_exc.csv")
    mpns = [f"STM32F{i:03d}" for i in range(10)]
    write_raw_csv(raw, [_row(m, port) for m in mpns])
    excl = set(mpns[2:4])
    res = sp.ingest(raw_source=raw, strategy="EXC", exclude_mpns=excl,
                    slice_size=500, pool_root=pr, root=root)
    check("S5 excluded == 2", res.excluded == 2, res.summary())
    check("S5 selected == 8", res.selected == 8, res.summary())


# --------------------------------------------------------------------------
# S6 — Wave / Slice identity
# --------------------------------------------------------------------------
def s6_identity(port):
    print("\n[S6] Wave / Slice identity")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_id.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(9)])
    res = sp.ingest(raw_source=raw, strategy="OPAMP", slice_size=4,
                    pool_root=pr, root=root)
    import re
    check("S6 wave_id format", re.fullmatch(r"\d{8}_[A-Z0-9]+", res.wave_id),
          res.wave_id)
    wm = sp._read_wave_manifest(sp.wave_manifest_path(pr, res.wave_id))
    expected_ids = [f"{res.wave_id}_{i}" for i in range(1, len(wm["slices"]) + 1)]
    got_ids = [s["batch_id"] for s in wm["slices"]]
    check("S6 slice batch_ids == wave_id_idx", got_ids == expected_ids, got_ids)


# --------------------------------------------------------------------------
# S7 — 2nd ingest idempotent (RAW deleted between calls)
# --------------------------------------------------------------------------
def s7_idempotent_ingest(port):
    print("\n[S7] 2nd ingest idempotent (no RAW re-scan / no slice re-gen)")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_idem.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(12)])
    res1 = sp.ingest(raw_source=raw, strategy="IDEM", slice_size=5,
                     pool_root=pr, root=root)
    all_csv = res1.all_csv
    h1 = hashlib.sha256(open(all_csv, "rb").read()).hexdigest()
    # DELETE the RAW source to prove the 2nd ingest does not re-scan it
    os.remove(raw)
    res2 = sp.ingest(raw_source=raw, strategy="IDEM", slice_size=5,
                     pool_root=pr, root=root)
    h2 = hashlib.sha256(open(all_csv, "rb").read()).hexdigest()
    check("S7 2nd call idempotent flag", res2.idempotent is True, res2.summary())
    check("S7 same slice_count", res2.slice_count == res1.slice_count,
          (res1.slice_count, res2.slice_count))
    check("S7 _all.csv unchanged (no re-scan)", h1 == h2, (h1, h2))


# --------------------------------------------------------------------------
# S8 — failed slice resume ALONE (other slices untouched)
# --------------------------------------------------------------------------
def s8_resume_alone(port, mock):
    print("\n[S8] failed slice resume ALONE (other slices untouched)")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_r1.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(8)])
    write_master_csv(master, [])
    write_mfr_csv(mfr)
    m, pr, root, bk = run_opts(sb, raw, master, mfr, port, mock)
    wr = sp.process_wave(raw_source=raw, strategy="RALONE", master_csv=master,
                         mfr_csv=mfr, slice_size=4, pool_root=pr, root=root,
                         backup_root=bk, r2_client=mock, r2_bucket="test")
    check("S8 wave ready", wr["status"] == sp.WAVE_READY, wr)
    wm = sp._read_wave_manifest(sp.wave_manifest_path(pr, wr["wave_id"]))
    slice1 = wm["slices"][0]
    slice2 = wm["slices"][1]
    # simulate slice1 crash: FAILED + a stranded DOWNLOADING ledger record
    recs = ds.load_ledger(slice1["batch_id"], pr)
    recs[0]["status"] = ds.DOWNLOADING
    ds.save_ledger(slice1["batch_id"], recs, pr)
    mm = MAN.BatchManifest.load(slice1["batch_id"], root)
    mm.data["status"] = MAN.FAILED
    mm.save()
    slice1["state"] = sp.SLICE_FAILED
    sp._write_wave_manifest(sp.wave_manifest_path(pr, wr["wave_id"]), wm)
    slice2_updated_before = MAN.BatchManifest.load(
        slice2["batch_id"], root).data["updated_at"]
    # resume ONLY slice1
    rr = sp.resume_slice(wr["wave_id"], 1, pool_root=pr, root=root,
                         backup_root=bk, master_csv=master, mfr_csv=mfr,
                         r2_client=mock, r2_bucket="test")
    check("S8 resumed slice ok", rr.ok, rr.summary())
    m1 = MAN.BatchManifest.load(slice1["batch_id"], root)
    check("S8 slice1 READY_FOR_RELEASE", m1.status == MAN.READY_FOR_RELEASE,
          m1.status)
    m2 = MAN.BatchManifest.load(slice2["batch_id"], root)
    check("S8 slice2 NOT re-processed",
          m2.data["updated_at"] == slice2_updated_before,
          (slice2_updated_before, m2.data["updated_at"]))


# --------------------------------------------------------------------------
# S9 — failed-slice recovery via whole-wave retry
# --------------------------------------------------------------------------
def s9_recovery_retry(port, mock):
    print("\n[S9] failed-slice recovery via whole-wave retry")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_r2.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, [_row(f"LPC{i:03d}", port) for i in range(8)])
    write_master_csv(master, [])
    write_mfr_csv(mfr)
    wr = sp.process_wave(raw_source=raw, strategy="RRETRY", master_csv=master,
                         mfr_csv=mfr, slice_size=4, pool_root=pr, root=root,
                         backup_root=bk, r2_client=mock, r2_bucket="test")
    check("S9 first wave ready", wr["status"] == sp.WAVE_READY, wr)
    wm = sp._read_wave_manifest(sp.wave_manifest_path(pr, wr["wave_id"]))
    s1 = wm["slices"][0]
    # strand + FAILED
    recs = ds.load_ledger(s1["batch_id"], pr)
    recs[0]["status"] = ds.DOWNLOADING
    ds.save_ledger(s1["batch_id"], recs, pr)
    mm = MAN.BatchManifest.load(s1["batch_id"], root)
    mm.data["status"] = MAN.FAILED
    mm.save()
    s1["state"] = sp.SLICE_FAILED
    sp._write_wave_manifest(sp.wave_manifest_path(pr, wr["wave_id"]), wm)
    # whole-wave retry: only the failed slice is re-run
    wr2 = sp.process_wave(raw_source=raw, strategy="RRETRY", master_csv=master,
                          mfr_csv=mfr, slice_size=4, pool_root=pr, root=root,
                          backup_root=bk, r2_client=mock, r2_bucket="test")
    check("S9 recovery wave ready", wr2["status"] == sp.WAVE_READY, wr2)
    check("S9 failed slice recovered",
          MAN.BatchManifest.load(s1["batch_id"], root).status
          == MAN.READY_FOR_RELEASE,
          MAN.BatchManifest.load(s1["batch_id"], root).status)


# --------------------------------------------------------------------------
# S10 — READY slice re-run -> no re-download
# --------------------------------------------------------------------------
def s10_no_redownload(port, mock):
    print("\n[S10] READY slice re-run -> NO re-download (new_downloads == 0)")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_rd.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(4)])
    write_master_csv(master, [])
    write_mfr_csv(mfr)
    wr = sp.process_wave(raw_source=raw, strategy="REDOWN", master_csv=master,
                         mfr_csv=mfr, slice_size=4, pool_root=pr, root=root,
                         backup_root=bk, r2_client=mock, r2_bucket="test")
    check("S10 wave ready", wr["status"] == sp.WAVE_READY, wr)
    bid = wr["wave_id"] + "_1"
    dsum1 = pool.read_json(pool.summary_path(bid, pr), default=None)
    check("S10 first run downloaded", dsum1.get("new_downloads", 0) > 0, dsum1)
    # force re-run the READY slice
    sp.process_slice(wr["wave_id"], 1, force=True, pool_root=pr, root=root,
                     backup_root=bk, master_csv=master, mfr_csv=mfr,
                     r2_client=mock, r2_bucket="test")
    dsum2 = pool.read_json(pool.summary_path(bid, pr), default=None)
    check("S10 re-run new_downloads == 0",
          dsum2.get("new_downloads", 0) == 0, dsum2)


# --------------------------------------------------------------------------
# S11 — PDF hash de-duplication
# --------------------------------------------------------------------------
def s11_pdf_dedup(port, mock):
    print("\n[S11] PDF hash de-duplication (shared datasheet -> 1 physical file)")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_dd.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    shared_url = f"http://127.0.0.1:{port}/SHARED.pdf"
    rows = [_row(f"STM32F{i:03d}", port) for i in range(6)]
    # mpn 0 and mpn 1 share the SAME datasheet URL -> same bytes -> 1 PDF
    rows[1] = _row("STM32F001", port, url_override=shared_url)
    rows[0] = _row("STM32F000", port, url_override=shared_url)
    write_raw_csv(raw, rows)
    write_master_csv(master, [])
    write_mfr_csv(mfr)
    wr = sp.process_wave(raw_source=raw, strategy="DEDUP", master_csv=master,
                         mfr_csv=mfr, slice_size=6, pool_root=pr, root=root,
                         backup_root=bk, r2_client=mock, r2_bucket="test")
    check("S11 wave ready", wr["status"] == sp.WAVE_READY, wr)
    physical = count_physical_pdfs(pr)
    check("S11 physical PDFs < 6 (1 shared)", physical < 6, physical)
    rep = sp.wave_report(wr["wave_id"], pool_root=pr, root=root)
    check("S11 datasheet duplicate >= 1",
          rep["datasheet_total"]["duplicate"] >= 1, rep["datasheet_total"])


# --------------------------------------------------------------------------
# S12 — R2 idempotency
# --------------------------------------------------------------------------
def s12_r2_idempotent(port, mock):
    print("\n[S12] R2 idempotency (re-run -> already_exists == total)")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_r2i.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(4)])
    write_master_csv(master, [])
    write_mfr_csv(mfr)
    wr = sp.process_wave(raw_source=raw, strategy="R2IDEM", master_csv=master,
                         mfr_csv=mfr, slice_size=4, pool_root=pr, root=root,
                         backup_root=bk, r2_client=mock, r2_bucket="test")
    check("S12 wave ready", wr["status"] == sp.WAVE_READY, wr)
    store_before = len(mock.store)
    r2sum1 = pool.read_json(
        os.path.join(pool.report_dir(wr["wave_id"] + "_1", pr),
                     "r2_batch_summary.json"), default=None)
    # force re-run
    sp.process_slice(wr["wave_id"], 1, force=True, pool_root=pr, root=root,
                     backup_root=bk, master_csv=master, mfr_csv=mfr,
                     r2_client=mock, r2_bucket="test")
    r2sum2 = pool.read_json(
        os.path.join(pool.report_dir(wr["wave_id"] + "_1", pr),
                     "r2_batch_summary.json"), default=None)
    check("S12 already_exists == total (no re-upload)",
          r2sum2["already_exists"] == r2sum2["total"], r2sum2)
    check("S12 new_objects == 0 on re-run",
          r2sum2["new_objects"] == 0, r2sum2)
    check("S12 mock store unchanged (no put_object)",
          len(mock.store) == store_before, (store_before, len(mock.store)))


# --------------------------------------------------------------------------
# S13 — mid-crash recovery (DOWNLOADING -> VERIFIED)
# --------------------------------------------------------------------------
def s13_crash_recovery(port, mock):
    print("\n[S13] mid-crash recovery (DOWNLOADING record -> VERIFIED)")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_cr.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(4)])
    write_master_csv(master, [])
    write_mfr_csv(mfr)
    wr = sp.process_wave(raw_source=raw, strategy="CRASH", master_csv=master,
                         mfr_csv=mfr, slice_size=4, pool_root=pr, root=root,
                         backup_root=bk, r2_client=mock, r2_bucket="test")
    check("S13 wave ready", wr["status"] == sp.WAVE_READY, wr)
    bid = wr["wave_id"] + "_1"
    recs = ds.load_ledger(bid, pr)
    recs[0]["status"] = ds.DOWNLOADING
    ds.save_ledger(bid, recs, pr)
    sp.process_slice(wr["wave_id"], 1, force=True, pool_root=pr, root=root,
                     backup_root=bk, master_csv=master, mfr_csv=mfr,
                     r2_client=mock, r2_bucket="test")
    recs2 = ds.load_ledger(bid, pr)
    check("S13 DOWNLOADING recovered (terminal state)",
          recs2[0]["status"] in (ds.VERIFIED, ds.DUPLICATE, ds.UPLOADED),
          recs2[0]["status"])


# --------------------------------------------------------------------------
# S14 — production MASTER SHA256 unchanged (read-only)
# --------------------------------------------------------------------------
def s14_master_readonly(port, mock):
    print("\n[S14] production MASTER SHA256 unchanged (read-only)")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_mr.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(6)])
    write_master_csv(master, [f"STM32F9{i:03d}" for i in range(3)])  # no overlap
    write_mfr_csv(mfr)
    before = hashlib.sha256(open(master, "rb").read()).hexdigest()
    sp.process_wave(raw_source=raw, strategy="MRO", master_csv=master,
                   mfr_csv=mfr, slice_size=6, pool_root=pr, root=root,
                   backup_root=bk, r2_client=mock, r2_bucket="test")
    after = hashlib.sha256(open(master, "rb").read()).hexdigest()
    check("S14 master bytes unchanged", before == after, (before, after))


# --------------------------------------------------------------------------
# S15 — Run never triggers Build / Commit / Push / Deploy
# --------------------------------------------------------------------------
def s15_no_release(port, mock):
    print("\n[S15] Run never triggers Build / Commit / Push / Deploy")
    sb = new_sandbox()
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw_nr.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, [_row(f"STM32F{i:03d}", port) for i in range(6)])
    write_master_csv(master, [])
    write_mfr_csv(mfr)
    wr = sp.process_wave(raw_source=raw, strategy="NORELEASE", master_csv=master,
                         mfr_csv=mfr, slice_size=6, pool_root=pr, root=root,
                         backup_root=bk, r2_client=mock, r2_bucket="test")
    wm = sp._read_wave_manifest(sp.wave_manifest_path(pr, wr["wave_id"]))
    all_ready = all(s["state"] == sp.SLICE_READY_FOR_RELEASE for s in wm["slices"])
    check("S15 all slices READY_FOR_RELEASE", all_ready, wm["slices"])
    # none may have been auto-released / deployed
    any_released = any(s["batch_status"] in (MAN.APPROVED, MAN.DEPLOYED)
                       for s in wm["slices"])
    check("S15 NO auto APPROVED/DEPLOYED", not any_released, wm["slices"])
    check("S15 wave status READY (not a deploy state)",
          wr["status"] == sp.WAVE_READY, wr["status"])


# --------------------------------------------------------------------------
def main():
    srv, port = start_server()
    mock = MockR2()
    try:
        s1_5000_slice_planning(port)
        s2_raw_internal_dup(port)
        s3_master_exclude(port)
        s4_selector(port)
        s5_exclude_mpns(port)
        s6_identity(port)
        s7_idempotent_ingest(port)
        s8_resume_alone(port, mock)
        s9_recovery_retry(port, mock)
        s10_no_redownload(port, mock)
        s11_pdf_dedup(port, mock)
        s12_r2_idempotent(port, mock)
        s13_crash_recovery(port, mock)
        s14_master_readonly(port, mock)
        s15_no_release(port, mock)
    finally:
        srv.shutdown()
    print(f"\n==== P1-D-C result: {len(PASS)} pass / {len(FAIL)} fail ====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("ALL 15 SCENARIOS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
