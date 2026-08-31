"""P2 — Release Pipeline sandbox tests (tempfile + mock, NO production writes).

This suite exercises release_pipeline.py entirely against tempfile copies of a
synthetic 540-row MASTER and synthetic candidate pools. It NEVER touches the
real production MASTER, never calls gen_parts / publish_normalizer /
build_datasheet_map / apply_datasheet_map / upload_datasheets / pre_deploy_audit,
and never Builds / Commits / Pushes / Deploys.

Run:  python tests/test_release_pipeline.py
Exit 0 = all pass.
"""
import os
import sys
import json
import tempfile
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))

from factory import (release_pipeline as RP, master_io, manifest as MAN,
                     pool, product_data, category, gate, dedup, MASTER_COLS)

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
# mock builders
# --------------------------------------------------------------------------
def build_mock_master(n):
    rows = []
    for i in range(1, n + 1):
        mpn = f"EXIST-{i:04d}"
        rows.append({
            "mpn": mpn, "clean_mpn": "", "manufacturer": "RealBrand",
            "brand": "RealBrand", "url_slug": f"exist-{i}",
            "category": "Resistor", "subcategory": "SMD Resistor",
            "description": f"RealBrand {mpn} resistor",
            "applications": "general", "keywords": f"{mpn}; resistor",
            "attributes_json": '{"resistance":"10k"}',
            "availability": "active", "alternative_parts": "",
            "datasheet_url": "", "faq": "", "image": "",
            "source": "", "source_url": "LCSC", "supplier_reference": "",
        })
    return rows


def build_mock_candidates(n, start=1, prefix="NEW"):
    rows = []
    for i in range(start, start + n):
        mpn = f"{prefix}-{i:04d}"
        rows.append({
            "mpn": mpn, "clean_mpn": "", "manufacturer": "RealBrand",
            "brand": "RealBrand", "url_slug": f"new-{i}",
            "category": "Capacitor", "subcategory": "MLCC",
            "description": f"RealBrand {mpn} capacitor",
            "applications": "general", "keywords": f"{mpn}; capacitor",
            "attributes_json": '{"capacitance":"1uF","voltage":"16V","tolerance":"10%"}',
            "availability": "active", "alternative_parts": "",
            "datasheet_url": "", "faq": "", "image": "",
            "source": "", "source_url": "LCSC", "supplier_reference": "",
            "_source_datasheet_url": "http://example.com/x.pdf",
            "_asset_key": mpn.lower(),
            "_spec_key_count": 3,
            "_needs_review": False,
            "_detect_signals": "{}",
        })
    return rows


def write_master(tmpd, rows):
    path = os.path.join(tmpd, "master_parts_v2.1.csv")
    master_io.atomic_write_master(path, MASTER_COLS, rows)
    return path


def write_candidates(pool_root, batch_id, rows):
    pool.ensure(pool_root)
    path = pool.candidates_path(batch_id, pool_root)
    payload = {"batch_id": batch_id, "rows": rows,
               "counts": {"candidate_count": len(rows)}}
    pool.atomic_write_json(path, payload)
    return path


def make_ready_manifest(batch_root, batch_id):
    m = MAN.BatchManifest.create(batch_id, "BATCH", root=batch_root)
    # jump straight to READY_FOR_RELEASE (test harness; production reaches it
    # via the full run() state machine).
    m.data["status"] = MAN.READY_FOR_RELEASE
    m.save()
    return m


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------
def t_normal_release():
    print("\n== t_normal_release ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    batch_root = os.path.join(tmpd, "batches")
    os.makedirs(batch_root, exist_ok=True)
    try:
        orig = build_mock_master(540)
        master_path = write_master(tmpd, orig)
        rows = build_mock_candidates(44)
        write_candidates(pool_root, "20260830_TEST_44", rows)
        m = make_ready_manifest(batch_root, "20260830_TEST_44")

        out = RP.release("20260830_TEST_44", master_path, root=pool_root,
                         approved_by="tester", manifest=m)

        check("released True", out.released)
        check("new_count==44", out.new_count == 44, out.new_count)
        check("after_count==584", out.after_count == 584, out.after_count)
        check("manifest APPROVED", m.status == MAN.APPROVED, m.status)

        cols, after = master_io.read_master(master_path)
        check("master row count 584", len(after) == 584, len(after))
        # 540 pre-existing rows byte-for-field unchanged
        same = all({k: (after[i].get(k) or "") for k in cols} ==
                   {k: (orig[i].get(k) or "") for k in cols}
                   for i in range(540))
        check("pre-existing 540 rows unchanged", same)
        # MPN set equals old union new
        before_mpns = {r["mpn"] for r in orig}
        new_mpns = {r["mpn"] for r in rows}
        after_mpns = {r["mpn"] for r in after}
        check("mpn set == old U new", after_mpns == before_mpns | new_mpns)
        # SHA matches reported
        check("sha matches reported",
              master_io.sha256_of(master_path) == out.master_after_sha)
        # no build/deploy dirs created by default
        rel_dir = os.path.join(tmpd, "releases", "20260830_TEST_44")
        check("no build dir by default",
              not os.path.isdir(os.path.join(rel_dir, "build")))
        check("no deploy dir by default",
              not os.path.isdir(os.path.join(rel_dir, "deploy")))
        check("build not prepared", not out.build_prepared)
        check("deploy not prepared", not out.deploy_prepared)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_partial_release():
    print("\n== t_partial_release ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    batch_root = os.path.join(tmpd, "batches")
    os.makedirs(batch_root, exist_ok=True)
    try:
        master_path = write_master(tmpd, build_mock_master(540))
        rows = build_mock_candidates(44)
        write_candidates(pool_root, "20260830_TEST_44", rows)
        subset = [r["mpn"] for r in rows[:10]]
        out = RP.release("20260830_TEST_44", master_path, root=pool_root,
                         approved_by="x", subset_mpns=subset)
        check("new_count==10", out.new_count == 10, out.new_count)
        check("after_count==550", out.after_count == 550, out.after_count)
        # partial release leaves the other 34 candidates still releasable
        data = pool.read_json(pool.candidates_path("20260830_TEST_44", pool_root))
        check("candidates file unchanged (44)",
              len(data.get("rows", [])) == 44, len(data.get("rows", [])))
        remain = RP.plan_release(master_path, data.get("rows", []),
                                 batch_id="20260830_TEST_44")
        check("34 unselected SKUs still releasable",
              len(remain.new_mpns) == 34, len(remain.new_mpns))
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_duplicate_mpn_stop():
    print("\n== t_duplicate_mpn_stop ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    try:
        master_path = write_master(tmpd, build_mock_master(540))
        rows = build_mock_candidates(44)
        # force two rows to share an MPN
        rows[5]["mpn"] = "NEW-0099"
        rows[6]["mpn"] = "NEW-0099"
        write_candidates(pool_root, "20260830_TEST_44", rows)

        plan = RP.plan_release(master_path, rows, batch_id="20260830_TEST_44")
        codes = {s["code"] for s in plan.stops}
        check("BATCH_SELF_DUPLICATE in plan stops",
              gate.BATCH_SELF_DUPLICATE in codes, codes)

        raised = False
        try:
            RP.release("20260830_TEST_44", master_path, root=pool_root, approved_by="x")
        except RP.ReleaseStop:
            raised = True
        check("release() raised ReleaseStop", raised)
        cols, after = master_io.read_master(master_path)
        check("master unchanged (540)", len(after) == 540, len(after))
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_master_hash_anomaly_stop():
    print("\n== t_master_hash_anomaly_stop ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    try:
        orig = build_mock_master(540)
        master_path = write_master(tmpd, orig)
        rows = build_mock_candidates(44)
        write_candidates(pool_root, "20260830_TEST_44", rows)

        plan = RP.plan_release(master_path, rows, batch_id="20260830_TEST_44")
        # tamper: rewrite master with one extra row (changes SHA)
        extra = dict(orig[0])
        extra["mpn"] = "TAMPER-0001"
        master_io.atomic_write_master(master_path,
                                      master_io.read_master(master_path)[0],
                                      orig + [extra])
        check("master tampered to 541 rows",
              len(master_io.read_master(master_path)[1]) == 541)

        raised = False
        try:
            RP.stage_master(plan, master_path, "x")
        except RP.ReleaseStop as e:
            raised = (e.code == RP.MASTER_HASH_MISMATCH)
        check("stage_master raised MASTER_HASH_MISMATCH", raised)
        check("our rows NOT appended (still 541)",
              len(master_io.read_master(master_path)[1]) == 541)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_candidate_anomaly_stop():
    print("\n== t_candidate_anomaly_stop ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    try:
        master_path = write_master(tmpd, build_mock_master(540))
        # CJK leak
        rows_cjk = build_mock_candidates(5)
        rows_cjk[0]["description"] = "RealBrand NEW-0001 电容 leak"
        plan = RP.plan_release(master_path, rows_cjk, batch_id="20260830_TEST_44")
        codes = {s["code"] for s in plan.stops}
        check("CJK_LEAK detected", gate.CJK_LEAK in codes, codes)

        # missing required field (category empty)
        rows_miss = build_mock_candidates(5)
        rows_miss[0]["category"] = ""
        plan2 = RP.plan_release(master_path, rows_miss, batch_id="20260830_TEST_44")
        codes2 = {s["code"] for s in plan2.stops}
        check("SPEC_THIN (missing category) detected",
              gate.SPEC_THIN in codes2, codes2)

        raised = False
        try:
            # the CJK batch is the one written to the pool
            write_candidates(pool_root, "20260830_TEST_44", rows_cjk)
            RP.release("20260830_TEST_44", master_path, root=pool_root, approved_by="x",
                       manifest=None)
        except RP.ReleaseStop:
            raised = True
        check("release() refused on bad candidate", raised)
        check("master unchanged (540)",
              len(master_io.read_master(master_path)[1]) == 540)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_build_gate_fail_stop():
    print("\n== t_build_gate_fail_stop ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    try:
        master_path = write_master(tmpd, build_mock_master(540))
        rows = build_mock_candidates(44)
        write_candidates(pool_root, "20260830_TEST_44", rows)
        plan = RP.plan_release(master_path, rows, batch_id="20260830_TEST_44")
        # corrupt the plan gate
        plan.add_stop(RP.BUILD_GATE_FAIL, "forced")
        out_dir = os.path.join(tmpd, "build")
        raised = False
        try:
            RP.prepare_build(master_path, out_dir, plan)
        except RP.ReleaseStop as e:
            raised = (e.code == RP.BUILD_GATE_FAIL)
        check("prepare_build refused on bad gate", raised)

        # missing staged file
        raised2 = False
        try:
            RP.prepare_build(os.path.join(tmpd, "nope.csv"), out_dir, None)
        except RP.ReleaseStop as e:
            raised2 = (e.code == RP.BUILD_GATE_FAIL)
        check("prepare_build refused on missing staged master", raised2)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_rollback():
    print("\n== t_rollback ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    batch_root = os.path.join(tmpd, "batches")
    os.makedirs(batch_root, exist_ok=True)
    try:
        orig = build_mock_master(540)
        master_path = write_master(tmpd, orig)
        rows = build_mock_candidates(44)
        write_candidates(pool_root, "20260830_TEST_44", rows)
        m = make_ready_manifest(batch_root, "20260830_TEST_44")
        before_sha = master_io.sha256_of(master_path)

        def fake_verify(mp, pl):
            raise RP.ReleaseStop(RP.CONSISTENCY_FAIL, "simulated failure")

        raised = False
        try:
            RP.release("20260830_TEST_44", master_path, root=pool_root, approved_by="x",
                       manifest=m, verify_fn=fake_verify)
        except RP.ReleaseStop:
            raised = True
        check("release() raised after staging", raised)
        check("master rolled back to 540",
              len(master_io.read_master(master_path)[1]) == 540)
        check("master sha restored",
              master_io.sha256_of(master_path) == before_sha)
        check("manifest FAILED", m.status == MAN.FAILED, m.status)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_idempotent():
    print("\n== t_idempotent ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    batch_root = os.path.join(tmpd, "batches")
    os.makedirs(batch_root, exist_ok=True)
    try:
        master_path = write_master(tmpd, build_mock_master(540))
        rows = build_mock_candidates(44)
        write_candidates(pool_root, "20260830_TEST_44", rows)
        m1 = make_ready_manifest(batch_root, "20260830_TEST_44")
        out1 = RP.release("20260830_TEST_44", master_path, root=pool_root,
                          approved_by="x", manifest=m1)
        check("first release new_count==44", out1.new_count == 44, out1.new_count)
        sha1 = master_io.sha256_of(master_path)

        m2 = make_ready_manifest(batch_root, "20260830_TESTB_44")
        out2 = RP.release("20260830_TEST_44", master_path, root=pool_root,
                          approved_by="x", manifest=m2)
        check("idempotent new_count==0", out2.new_count == 0, out2.new_count)
        check("idempotent already_count==44",
              out2.already_count == 44, out2.already_count)
        check("idempotent after_count==584", out2.after_count == 584)
        check("idempotent sha unchanged",
              master_io.sha256_of(master_path) == sha1)
        check("idempotent still released True", out2.released)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_ready_retention():
    print("\n== t_ready_retention ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    batch_root = os.path.join(tmpd, "batches")
    os.makedirs(batch_root, exist_ok=True)
    try:
        master_path = write_master(tmpd, build_mock_master(540))
        rows_a = build_mock_candidates(44, start=1, prefix="A")
        rows_b = build_mock_candidates(10, start=1, prefix="B")
        write_candidates(pool_root, "20260830_TESTA_44", rows_a)
        write_candidates(pool_root, "20260830_TESTB_10", rows_b)
        m_a = make_ready_manifest(batch_root, "20260830_TESTA_44")
        make_ready_manifest(batch_root, "20260830_TESTB_10")

        RP.release("20260830_TESTA_44", master_path, root=pool_root,
                   approved_by="x", manifest=m_a)
        ready = RP.list_ready_batches(batch_root)
        check("20260830_TESTB_10 still ready", "20260830_TESTB_10" in ready, ready)
        check("20260830_TESTA_44 no longer ready (released)", "20260830_TESTA_44" not in ready, ready)
        # candidate files not deleted
        da = pool.read_json(pool.candidates_path("20260830_TESTA_44", pool_root))
        db = pool.read_json(pool.candidates_path("20260830_TESTB_10", pool_root))
        check("20260830_TESTA_44 candidates retained", len(da.get("rows", [])) == 44)
        check("20260830_TESTB_10 candidates retained", len(db.get("rows", [])) == 10)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def t_run_release_separation():
    print("\n== t_run_release_separation (static) ==")
    import factory.batch_runner as br
    src = open(br.__file__, encoding="utf-8").read()
    check("batch_runner does NOT import release_pipeline",
          "release_pipeline" not in src)
    # run() must not reference release()
    check("batch_runner.run does not call release()",
          "release(" not in src or "def release(" in src)
    # release() must not import/call batch_runner.run()
    rpsrc = open(RP.__file__, encoding="utf-8").read()
    check("release_pipeline does not import batch_runner",
          "import batch_runner" not in rpsrc and "from . import batch_runner" not in rpsrc)
    check("release_pipeline does not call batch_runner.run()",
          "batch_runner." not in rpsrc and "batch_runner(" not in rpsrc)


def t_release_build_deploy_separation():
    print("\n== t_release_build_deploy_separation ==")
    tmpd = tempfile.mkdtemp()
    pool_root = os.path.join(tmpd, "pool")
    try:
        master_path = write_master(tmpd, build_mock_master(540))
        rows = build_mock_candidates(44)
        write_candidates(pool_root, "20260830_TEST_44", rows)

        # default: no build/deploy
        out = RP.release("20260830_TEST_44", master_path, root=pool_root, approved_by="x")
        rel_dir = os.path.join(tmpd, "releases", "20260830_TEST_44")
        check("default: build not prepared",
              not out.build_prepared and
              not os.path.isdir(os.path.join(rel_dir, "build")))
        check("default: deploy not prepared",
              not out.deploy_prepared and
              not os.path.isdir(os.path.join(rel_dir, "deploy")))

        # opt-in: build + deploy prepared (artifacts only, no execution)
        out2 = RP.release("20260830_TEST_44", master_path, root=pool_root, approved_by="x",
                          also_prepare_build=True, also_prepare_deploy=True)
        check("opt-in: build prepared",
              out2.build_prepared and
              os.path.exists(os.path.join(rel_dir, "build", "build_manifest.json")))
        check("opt-in: deploy prepared",
              out2.deploy_prepared and
              os.path.exists(os.path.join(rel_dir, "deploy", "deploy_candidate.json")))
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def main():
    # regression guard: release_pipeline imports cleanly and reuses shared gates
    check("module imports", RP is not None)
    check("RELEASE_SCOPE documented", bool(RP.RELEASE_SCOPE))

    t_normal_release()
    t_partial_release()
    t_duplicate_mpn_stop()
    t_master_hash_anomaly_stop()
    t_candidate_anomaly_stop()
    t_build_gate_fail_stop()
    t_rollback()
    t_idempotent()
    t_ready_retention()
    t_run_release_separation()
    t_release_build_deploy_separation()

    print(f"\n==== P2 Release Pipeline result: {len(PASS)} pass / "
          f"{len(FAIL)} fail ====")
    if FAIL:
        print("FAILED:", FAIL)
        return 1
    print("ALL P2 RELEASE PIPELINE SCENARIOS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
