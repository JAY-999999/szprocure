"""P1-D-A — Sandbox full-pipeline + Run/Release isolation tests.

Runs the Factory Orchestrator entirely inside temp dirs (never touches
production MASTER / pool / R2 / git).  Covers the 11 required scenarios:

  S1  normal full batch                       -> READY_FOR_RELEASE
  S2  mid-stage failure (MASS_DUPLICATE STOP) -> downstream blocked, FAILED
  S3  failed-stage re-exec / resume           -> recovers to READY
  S4  DOWNLOADING crash recovery              -> recovers to VERIFIED
  S5  UPLOADING crash recovery                -> recovers to UPLOADED
  S6  batch restart after crash               -> recovers to READY
  S7  STOP blocks downstream (asserted in S2)
  S8  AUTO_SKIP not blocking                   -> continues to READY
  S9  READY generation (asserted in S1)
  S10 Run not triggering Release              -> status stays READY (not APPROVED)
  S11 Run not triggering Git/Build/Deploy      -> prod MASTER + git HEAD unchanged

Run:  python tests/test_p1d_sandbox.py
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
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

import http.server
import botocore.exceptions
from factory import batch_runner as br, stages, ids, manifest as MAN, pool
from factory import product_data, datasheet as ds, r2, gate

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
# PDF server (local datasheet host)
# --------------------------------------------------------------------------
PDF = b"%PDF-1.4\n" + b"x" * 1500 + b"\n%%EOF\n" + b" " * 200  # >=1024 bytes


class _PDFHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(PDF)))
        self.end_headers()
        self.wfile.write(PDF)

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
def new_sandbox():
    tmp = tempfile.mkdtemp(prefix="p1d_sandbox_")
    pool_root = os.path.join(tmp, "pool")
    root = os.path.join(tmp, "batches")
    backup_root = os.path.join(tmp, "backups")
    os.makedirs(pool_root, exist_ok=True)
    os.makedirs(root, exist_ok=True)
    return tmp, pool_root, root, backup_root


def write_raw_csv(path, mpns, port):
    cols = ["mpn", "manufacturer_raw", "category", "description",
            "attributes_json", "source_datasheet_url", "supplier_sku"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i, mpn in enumerate(mpns):
            brand = "STMicroelectronics" if mpn.startswith("STM") else "NXP"
            url = f"http://127.0.0.1:{port}/{mpn}.pdf"
            w.writerow({
                "mpn": mpn,
                "manufacturer_raw": brand,
                "category": "Microcontroller",
                "description": f"{brand} {mpn} 32-bit ARM Cortex-M4 MCU 64KB Flash",
                "attributes_json": json.dumps(
                    {"CPU内核": "ARM Cortex-M4", "CPU位数": "32", "CPU最大主频": "72"}),
                "source_datasheet_url": url,
                "supplier_sku": f"SUP{i:03d}",
            })


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


def base_opts(sandbox, port, master_mpns, mock, extra=None):
    tmp, pool_root, root, backup_root = sandbox
    raw = os.path.join(tmp, "raw.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    write_raw_csv(raw, master_mpns["mpns"], port)
    write_master_csv(master, master_mpns["master"])
    write_mfr_csv(mfr)
    o = {
        "source_path": raw,
        "master_csv": master,
        "mfr_csv": mfr,
        "require_datasheet": False,
        "workers": 4, "retries": 2, "timeout": 15,
        "r2_client": mock, "r2_bucket": "test",
    }
    if extra:
        o.update(extra)
    return o, pool_root, root, backup_root


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------
def scenario_normal(mock, port):
    print("\n[S1] normal full batch -> READY_FOR_RELEASE")
    sb = new_sandbox()
    mpns = [f"STM32F{i:03d}" for i in range(8)]
    o, pr, root, bk = base_opts(sb, port, {"mpns": mpns, "master": []}, mock)
    rr = br.run(batch_id="20260101_NORMAL_08", strategy="NORMAL",
                root=root, pool_root=pr, backup_root=bk, options=o)
    check("S1 ok", rr.ok, str(rr.summary()))
    check("S1 status READY", rr.manifest_status == MAN.READY_FOR_RELEASE,
          rr.manifest_status)
    check("S1 release_eligible", rr.release_eligible)
    check("S1 no STOP", not rr.stopped)
    # batch metrics present
    c = br.batch_status("20260101_NORMAL_08", root)["counts"]
    check("S1 input_count==8", c["input_count"] == 8, c)
    check("S1 candidate_count==8", c["candidate_count"] == 8, c)
    check("S1 r2_uploaded>0", c["r2_uploaded"] > 0, c)
    check("S1 datasheet_found>0", c["datasheet_found"] > 0, c)
    return sb


def scenario_mass_dup_stop(mock, port):
    print("\n[S2] mid-stage failure (MASS_DUPLICATE STOP) -> downstream blocked")
    sb = new_sandbox()
    mpns = [f"STM32F{i:03d}" for i in range(10)]
    master = mpns[:6]  # 6/10 = 60% > 20%, >=5 -> STOP
    o, pr, root, bk = base_opts(sb, port, {"mpns": mpns, "master": master}, mock)
    rr = br.run(batch_id="20260102_MASSDUP_10", strategy="MASSDUP",
                root=root, pool_root=pr, backup_root=bk, options=o)
    check("S2 stopped", rr.stopped, str(rr.summary()))
    check("S2 status FAILED", rr.manifest_status == MAN.FAILED, rr.manifest_status)
    names = [s.name for s in rr.stage_results]
    check("S2 reached normalize", "normalize" in names, names)
    check("S2 datasheet NOT run (STOP blocks downstream)",
          "datasheet" not in names, names)         # S7
    check("S2 r2 NOT run", "r2" not in names, names)
    check("S2 MASS_DUPLICATE reason",
          any("MASS_DUPLICATE" in r for r in rr.stop_reasons), rr.stop_reasons)
    return rr, sb, mpns


def scenario_failed_stage_re_exec(sb, mock, port):
    print("\n[S3] failed-stage re-exec / resume -> recovers to READY")
    tmp, pr, root, bk = sb
    raw = os.path.join(tmp, "raw.csv")
    master = os.path.join(tmp, "master.csv")
    mfr = os.path.join(tmp, "mfr.csv")
    # rewrite master with NO overlap -> fixes the mass-duplicate
    write_master_csv(master, [])
    o = {
        "source_path": raw, "master_csv": master, "mfr_csv": mfr,
        "require_datasheet": False, "workers": 4, "retries": 2, "timeout": 15,
        "r2_client": mock, "r2_bucket": "test",
    }
    rr2 = br.run(batch_id="20260102_MASSDUP_10", from_stage="harvest",
                 create=False, root=root, pool_root=pr,
                 backup_root=bk, options=o)
    check("S3 recovered ok", rr2.ok, str(rr2.summary()))
    check("S3 status READY", rr2.manifest_status == MAN.READY_FOR_RELEASE,
          rr2.manifest_status)
    check("S3 no STOP", not rr2.stopped)


def scenario_downloading_recovery(mock, port):
    print("\n[S4] DOWNLOADING crash recovery -> recovers to VERIFIED")
    sb = new_sandbox()
    mpns = [f"STM32F{i:03d}" for i in range(5)]
    o, pr, root, bk = base_opts(sb, port, {"mpns": mpns, "master": []}, mock)
    br.run(batch_id="20260104_DL_05", strategy="DL", root=root,
           pool_root=pr, backup_root=bk, options=o)
    # simulate a crash: one ledger record stranded in DOWNLOADING
    recs = ds.load_ledger("20260104_DL_05", pr)
    recs[0]["status"] = ds.DOWNLOADING
    ds.save_ledger("20260104_DL_05", recs, pr)
    sr = br.run_stage("20260104_DL_05", "datasheet",
                      root=root, pool_root=pr, backup_root=bk, options=o)
    recs2 = ds.load_ledger("20260104_DL_05", pr)
    check("S4 DOWNLOADING recovered", recs2[0]["status"] in (ds.VERIFIED, ds.DUPLICATE),
          recs2[0]["status"])
    check("S4 stage ok", sr.ok, sr.note)


def scenario_uploading_recovery(mock, port):
    print("\n[S5] UPLOADING crash recovery -> recovers to UPLOADED")
    sb = new_sandbox()
    mpns = [f"STM32F{i:03d}" for i in range(5)]
    o, pr, root, bk = base_opts(sb, port, {"mpns": mpns, "master": []}, mock)
    br.run(batch_id="20260105_UL_05", strategy="UL", root=root,
           pool_root=pr, backup_root=bk, options=o)
    # simulate crash: one record stranded in UPLOADING
    recs = ds.load_ledger("20260105_UL_05", pr)
    recs[0]["status"] = r2.UPLOADING
    recs[0][r2.F_PRE_UPLOAD] = ds.VERIFIED
    ds.save_ledger("20260105_UL_05", recs, pr)
    sr = br.run_stage("20260105_UL_05", "r2", root=root,
                      pool_root=pr, backup_root=bk, options=o)
    recs2 = ds.load_ledger("20260105_UL_05", pr)
    check("S5 UPLOADING recovered to UPLOADED",
          recs2[0]["status"] == ds.UPLOADED, recs2[0]["status"])
    check("S5 stage ok", sr.ok, sr.note)


def scenario_batch_restart(mock, port):
    print("\n[S6] batch restart after crash -> recovers to READY")
    sb = new_sandbox()
    mpns = [f"STM32F{i:03d}" for i in range(6)]
    o, pr, root, bk = base_opts(sb, port, {"mpns": mpns, "master": []}, mock)
    br.run(batch_id="20260106_RESTART_06", strategy="RESTART", root=root,
           pool_root=pr, backup_root=bk, options=o)
    # simulate crash during datasheet: strand one record + mark FAILED
    recs = ds.load_ledger("20260106_RESTART_06", pr)
    recs[0]["status"] = ds.DOWNLOADING
    ds.save_ledger("20260106_RESTART_06", recs, pr)
    m = MAN.BatchManifest.load("20260106_RESTART_06", root)
    m.data["status"] = MAN.FAILED
    m.save()
    # restart from harvest
    rr = br.run(batch_id="20260106_RESTART_06", from_stage="harvest",
                create=False, root=root, pool_root=pr, backup_root=bk, options=o)
    check("S6 restart ok", rr.ok, str(rr.summary()))
    check("S6 status READY", rr.manifest_status == MAN.READY_FOR_RELEASE,
          rr.manifest_status)


def scenario_auto_skip(mock, port):
    print("\n[S8] AUTO_SKIP not blocking -> continues to READY")
    sb = new_sandbox()
    mpns = [f"STM32F{i:03d}" for i in range(8)]
    o, pr, root, bk = base_opts(sb, port, {"mpns": mpns, "master": mpns[:1]},
                                 mock)  # 1 duplicate -> AUTO_SKIP
    rr = br.run(batch_id="20260108_SKIP_08", strategy="SKIP", root=root,
                pool_root=pr, backup_root=bk, options=o)
    check("S8 ok (AUTO_SKIP not blocking)", rr.ok, str(rr.summary()))
    check("S8 status READY", rr.manifest_status == MAN.READY_FOR_RELEASE,
          rr.manifest_status)
    m = MAN.BatchManifest.load("20260108_SKIP_08", root)
    skips = m.exceptions_by_severity(gate.AUTO_SKIP)
    check("S8 has AUTO_SKIP exception", len(skips) >= 1, skips)


def scenario_run_not_release(mock, port):
    print("\n[S10] Run not triggering Release -> stays READY (not APPROVED)")
    sb = new_sandbox()
    mpns = [f"STM32F{i:03d}" for i in range(5)]
    o, pr, root, bk = base_opts(sb, port, {"mpns": mpns, "master": []}, mock)
    br.run(batch_id="20260110_NOREL_05", strategy="NOREL", root=root,
           pool_root=pr, backup_root=bk, options=o)
    m = MAN.BatchManifest.load("20260110_NOREL_05", root)
    check("S10 status READY (not APPROVED)", m.status == MAN.READY_FOR_RELEASE,
          m.status)
    check("S10 not APPROVED", m.status != MAN.APPROVED)
    check("S10 not DEPLOYED", m.status != MAN.DEPLOYED)
    # re-run must NOT auto-approve
    br.run(batch_id="20260110_NOREL_05", from_stage="harvest", create=False,
           root=root, pool_root=pr, backup_root=bk, options=o)
    m2 = MAN.BatchManifest.load("20260110_NOREL_05", root)
    check("S10 re-run still READY (no auto-release)",
          m2.status == MAN.READY_FOR_RELEASE, m2.status)


def run_git(cmd):
    return subprocess.run(["git"] + cmd, cwd=REPO, capture_output=True,
                          text=True)


def run_regression_suite():
    print("\n[REG] P0-P1-C regression + Full Audit")
    # P0 regression
    p0 = os.path.join(REPO, "tests", "test_p0_regression.py")
    if os.path.exists(p0):
        r = subprocess.run([VENV, p0], cwd=REPO, capture_output=True, text=True)
        check("REG test_p0_regression.py", r.returncode == 0,
              r.stdout[-400:] + r.stderr[-400:])
    # Temp selftests (P0/P1-A/P1-B/P1-C).  Filenames: p0/p1a/p1b/p1c_selftest.py
    # live inside C:\...\Temp\skufac_<p0|p1a|p1b|p1c>\ .
    import glob
    temp_roots = []
    for cand in (tempfile.gettempdir(),
                 os.path.realpath(tempfile.gettempdir()),
                 r"C:\Users\Administrator.SC-202105071542\AppData\Local\Temp"):
        if cand and cand not in temp_roots:
            temp_roots.append(cand)
    selftest_map = {
        "skufac_p0": "p0_selftest.py",
        "skufac_p1a": "p1a_selftest.py",
        "skufac_p1b": "p1b_selftest.py",
        "skufac_p1c": "p1c_selftest.py",
    }
    for pat, fname in selftest_map.items():
        found = []
        for tr in temp_roots:
            found = glob.glob(os.path.join(tr, pat, fname))
            if found:
                break
        if not found:
            check(f"REG {pat} selftest present", False, "not found")
            continue
        # The P0/P1-x selftests create a `sandbox` dir (incl. a backup dir)
        # inside Temp on first run; re-running them from a stale sandbox makes
        # backup.take_backup raise BackupError ("backup directory already
        # exists").  Clean the stale sandbox so each re-run starts fresh.
        # Only test artifacts under Temp are touched — never repo / prod data.
        sk_root = os.path.dirname(found[0])
        stale_sb = os.path.join(sk_root, "sandbox")
        if os.path.isdir(stale_sb):
            shutil.rmtree(stale_sb)
        r = subprocess.run([VENV, found[0]], cwd=REPO, capture_output=True,
                           text=True)
        check(f"REG {pat} selftest", r.returncode == 0,
              r.stdout[-400:] + r.stderr[-400:])
    # Full Audit (permanent release gate) — must still PASS (untouched prod)
    audit = os.path.join(REPO, "tools", "pre_deploy_audit.py")
    r = subprocess.run([VENV, audit], cwd=REPO, capture_output=True, text=True)
    check("REG Full Audit (pre_deploy_audit) PASS", r.returncode == 0,
          r.stdout[-400:] + r.stderr[-400:])


def main():
    srv, port = start_server()
    mock = MockR2()
    # S11 baseline
    prod_master = os.path.join(REPO, "data", "production", "master_parts_v2.1.csv")
    head_before = run_git(["rev-parse", "HEAD"]).stdout.strip()
    master_sha_before = None
    if os.path.exists(prod_master):
        h = hashlib.sha256()
        with open(prod_master, "rb") as f:
            for c in iter(lambda: f.read(1 << 20), b""):
                h.update(c)
        master_sha_before = h.hexdigest()

    try:
        scenario_normal(mock, port)
        rr, sb, mpns = scenario_mass_dup_stop(mock, port)
        scenario_failed_stage_re_exec(sb, mock, port)
        scenario_downloading_recovery(mock, port)
        scenario_uploading_recovery(mock, port)
        scenario_batch_restart(mock, port)
        scenario_auto_skip(mock, port)
        scenario_run_not_release(mock, port)
        run_regression_suite()
    finally:
        srv.shutdown()

    # S11 post-check
    print("\n[S11] Run not triggering Git/Build/Deploy")
    head_after = run_git(["rev-parse", "HEAD"]).stdout.strip()
    check("S11 git HEAD unchanged", head_before == head_after,
          f"{head_before} -> {head_after}")
    master_sha_after = None
    if os.path.exists(prod_master):
        h = hashlib.sha256()
        with open(prod_master, "rb") as f:
            for c in iter(lambda: f.read(1 << 20), b""):
                h.update(c)
        master_sha_after = h.hexdigest()
    check("S11 production MASTER unchanged",
          master_sha_before == master_sha_after,
          f"{master_sha_before} -> {master_sha_after}")
    check("S11 no new commit", head_before == head_after)

    print(f"\n==== P1-D-A sandbox result: {len(PASS)} pass / {len(FAIL)} fail ====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("ALL SCENARIOS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
