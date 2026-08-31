"""P1-D-C — metrics regression (R2 count invariants + SPEC_THIN/BRAND trace).

Pure offline test: no network, no real R2, no MASTER, no git. Uses a mock
S3 client against a temp datasheet ledger so the REAL ``r2.upload_batch``
counting code is exercised end-to-end.

Verifies the exact distortion from the smoke batch is gone:
  * ``uploaded`` (all-UPLOADED total) is NEVER reported as "new"; only
    ``new_objects`` is. new_objects + already_exists == uploaded.
  * The 27-new / 17-already / 44-total smoke result is reproducible and
    expressible by the corrected metrics.
  * An idempotent re-run yields new_objects == 0, already_exists == total.
  * warning_codes are reported both raw and de-duplicated by (code, mpn).

Run:  python tests/test_p1dc_metrics.py
Exit 0 = all pass.
"""
import os
import sys
import json
import tempfile
import shutil
import hashlib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKBUDDY = r"C:\Users\Administrator.SC-202105071542\WorkBuddy\2026-07-24-02-25-03"
sys.path.insert(0, os.path.join(REPO, "tools"))
sys.path.insert(0, WORKBUDDY)

from botocore.exceptions import ClientError  # noqa: E402
from factory import r2, datasheet as ds, pool  # noqa: E402
import run_p1dc_smoke as drv  # noqa: E402

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
# mock R2 client (in-memory; surfaces the same head/put/get surface)
# --------------------------------------------------------------------------
class _MockClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class _Bytes:
    def __init__(self, b):
        self._b = b

    def read(self):
        return self._b


class MockR2Client:
    def __init__(self, existing=None):
        # existing: list of (key, sha256, body) pre-present in the bucket.
        # body MUST be the real PDF bytes so verify_remote's size + meta-sha
        # checks pass (mirrors a content-identical object already in R2).
        self.store = {}
        for key, sha, body in (existing or []):
            self.store[key] = {"body": body, "meta_sha": sha}

    def head_object(self, Bucket, Key):
        if Key not in self.store:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        o = self.store[Key]
        return {"ContentLength": len(o["body"]), "ETag": '"e"',
                "Metadata": {"sha256": o["meta_sha"]}}

    def put_object(self, Bucket, Key, Body, ContentType, Metadata):
        data = Body.read() if hasattr(Body, "read") else Body
        self.store[Key] = {"body": data, "meta_sha": Metadata.get("sha256")}
        return {"ETag": '"ok"'}

    def get_object(self, Bucket, Key):
        return {"Body": _Bytes(self.store[Key]["body"])}


def _make_ledger(root, n=44, seed_existing=17):
    pdf_dir = os.path.join(root, "pdfs")
    os.makedirs(pdf_dir, exist_ok=True)
    records, seeded = [], []
    for i in range(n):
        data = b"%PDF-1.4\n" + bytes([(i * 7) % 256]) * 1200 + b"\n%%EOF\n"
        p = os.path.join(pdf_dir, f"ds{i}.pdf")
        with open(p, "wb") as f:
            f.write(data)
        sha = hashlib.sha256(data).hexdigest()
        key = r2.r2_key_for(sha)
        records.append({
            "mpn": f"TEST{i:03d}", "status": ds.VERIFIED,
            "local_path": p, "sha256": sha, "file_size": len(data),
            "r2_key": key, "supplier_sku": f"S{i}",
        })
        if i < seed_existing:
            seeded.append((key, sha, data))
    return records, seeded


# ==========================================================================
# 1) R2 count invariants
# ==========================================================================
def test_invariants():
    print("\n[1] R2 metric invariants")
    # true smoke result: 27 new + 17 already, 44 total, 0 failed -> valid
    check("invariant OK (27/17/44/0)",
          r2.validate_r2_counts({"uploaded": 44, "new_objects": 27,
                                  "already_exists": 17, "total": 44,
                                  "failed": 0}) is None)
    # idempotent re-run: 0 new + 44 already, 44 total -> valid
    check("invariant OK idempotent (0/44/44/0)",
          r2.validate_r2_counts({"uploaded": 44, "new_objects": 0,
                                  "already_exists": 44, "total": 44,
                                  "failed": 0}) is None)
    # the OLD distortion: uploaded double-counted with already_exists
    check("invariant FAILS (uploaded==new+already violated 44/44/44)",
          r2.validate_r2_counts({"uploaded": 44, "new_objects": 44,
                                  "already_exists": 44, "total": 44,
                                  "failed": 0}) is not None)
    # total not exhausted by outcomes
    check("invariant FAILS (total != new+already+failed 44/27/17/1)",
          r2.validate_r2_counts({"uploaded": 44, "new_objects": 27,
                                  "already_exists": 17, "total": 44,
                                  "failed": 1}) is not None)


# ==========================================================================
# 2) summarize_r2_counts exposes non-overlapping view
# ==========================================================================
def test_summarize():
    print("\n[2] summarize_r2_counts non-overlapping view")
    sm = r2.summarize_r2_counts({"uploaded": 44, "new_objects": 27,
                                 "already_exists": 17, "failed": 0,
                                 "bytes_uploaded": 999, "total": 44})
    check("uploaded_total == 44", sm["uploaded_total"] == 44, sm)
    check("new_objects == 27 (the ONLY 'new')", sm["new_objects"] == 27, sm)
    check("already_exists == 17", sm["already_exists"] == 17, sm)
    check("uploaded_total == new + already",
          sm["uploaded_total"] == sm["new_objects"] + sm["already_exists"], sm)
    check("invariant_ok True", sm["invariant_ok"] is True, sm)


# ==========================================================================
# 3) REAL upload_batch on a temp ledger (mock client)
# ==========================================================================
def test_upload_batch_counts():
    print("\n[3] upload_batch real counting (mock R2 client)")
    root = tempfile.mkdtemp(prefix="p1dc_metrics_")
    try:
        batch_id = "METRIC_TEST_44"
        records, seeded = _make_ledger(root, n=44, seed_existing=17)
        ds.save_ledger(batch_id, records, root)
        client = MockR2Client(existing=seeded)
        res = r2.upload_batch(batch_id, root=root, workers=1,
                              client=client, bucket="test-bucket")
        check("new_objects == 27", res.new_objects == 27, res.as_dict())
        check("already_exists == 17", res.already_exists == 17, res.as_dict())
        check("uploaded == 44", res.uploaded == 44, res.as_dict())
        check("failed == 0", res.failed == 0, res.as_dict())
        check("invariant holds on real result",
              r2.validate_r2_counts(res.as_dict()) is None, res.as_dict())

        # idempotent re-run: every record now UPLOADED at start
        res2 = r2.upload_batch(batch_id, root=root, workers=1,
                               client=client, bucket="test-bucket")
        check("rerun new_objects == 0", res2.new_objects == 0, res2.as_dict())
        check("rerun already_exists == 44",
              res2.already_exists == 44, res2.as_dict())
        check("rerun invariant holds",
              r2.validate_r2_counts(res2.as_dict()) is None, res2.as_dict())
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ==========================================================================
# 4) collect_metrics mapping (the exact smoke-driver fix)
# ==========================================================================
def test_collect_metrics_mapping():
    print("\n[4] collect_metrics maps new_objects (not uploaded) as 'new'")
    tmp = tempfile.mkdtemp(prefix="p1dc_cm_")
    try:
        cand = [{"mpn": "A", "category": "Resistor",
                 "attributes_json": '{"resistance_ohm": 100}',
                 "manufacturer": "Yageo"}]
        cpath = os.path.join(tmp, "cand.json")
        with open(cpath, "w", encoding="utf-8") as f:
            json.dump({"rows": cand}, f)
        drv.pool.candidates_path = lambda *a, **k: cpath
        drv.PLAN = os.path.join(tmp, "plan.json")
        with open(drv.PLAN, "w", encoding="utf-8") as f:
            json.dump({"adapters": {}}, f)

        class _FakeMan:
            def exceptions_by_severity(self, sev):
                # two identical warnings -> raw 2, unique 1
                return [
                    {"code": "SPEC_THIN", "mpn": "A", "severity": "WARNING"},
                    {"code": "SPEC_THIN", "mpn": "A", "severity": "WARNING"},
                ]
        drv.BatchManifest.load = staticmethod(lambda *a, **k: _FakeMan())

        class _Stage:
            def __init__(self, name, counts):
                self.name = name
                self.counts = counts
                self.note = ""

        class _RR:
            manifest_status = "READY_FOR_RELEASE"
            ok = True
            stopped = False
            stop_reasons = []
            stage_results = [_Stage("r2", {
                "total": 44, "uploaded": 44, "new_objects": 27,
                "already_exists": 17, "failed": 0, "bytes_uploaded": 999})]

        m = drv.collect_metrics(_RR(), "METRIC_TEST")
        check("r2_uploaded_new == 27 (NOT 44)", m["r2_uploaded_new"] == 27, m)
        check("r2_already_exists == 17", m["r2_already_exists"] == 17, m)
        check("r2_uploaded_total == 44", m["r2_uploaded_total"] == 44, m)
        check("r2_invariant_ok True", m["r2_invariant_ok"] is True, m)
        check("warning_codes_raw SPEC_THIN == 2",
              m["warning_codes"].get("SPEC_THIN") == 2, m)
        check("warning_codes_unique SPEC_THIN == 1",
              m["warning_codes_unique"].get("SPEC_THIN") == 1, m)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    test_invariants()
    test_summarize()
    test_upload_batch_counts()
    test_collect_metrics_mapping()
    print("\n" + "=" * 60)
    print(f"metrics regression: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
