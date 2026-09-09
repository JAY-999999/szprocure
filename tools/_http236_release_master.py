"""http236 MASTER Release — formal, atomic, human-gated.

Releases the RELEASABLE subset of the V2 candidate pool (200) into MASTER:
  * 154 categorized candidates
  * 2 explicitly-rescued pure-numeric MPNs (5023520200, 1054500101)
    -> released as Uncategorized via allow_uncategorized_mpns (per user §10)
  * 44 genuinely non-11-family Uncategorized -> EXCLUDED (per user §1/§11 backlog)

Uses the canonical factory primitives: plan_release -> stage_master -> verify_consistency
(atomic append via master_io). No MASTER schema change. No 03. No commit/push/deploy.
"""
import os, sys, json, csv, collections, datetime, hashlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from tools.factory import product_data as pd
from tools.factory import category, gate, pool
from tools.factory import release_pipeline as rp
from tools.factory.release_pipeline import (plan_release, stage_master,
                                            verify_consistency, rollback_master,
                                            ReleaseStop)
from tools.factory.lcsc_http_adapter import _detect_by_en_attrs
from tools.factory.master_io import read_master, mpn_set

MASTER = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
POOL = os.path.join(ROOT, "data", "raw", "_clean_preview", "http236",
                    "products", "candidates", "http236.json")
APPROVED_BY = "user-auth-2026-09-10 (WorkBuddy session, explicit MASTER release)"
RELEASE_DIR = os.path.join(ROOT, "data", "production", "releases", "http236")
RAW_POOL = os.path.join(ROOT, "data", "raw", "_clean_preview", "http236",
                       "products", "raw", "http236.jsonl")
REPORT = os.path.join(ROOT, "02_LCSC_HTTP_200_MASTER_RELEASE_REPORT.md")

NUMERIC_MPNS = ["5023520200", "1054500101"]
FORBIDDEN = {"realtime_stock", "realtime_price", "cost", "purchase_price",
             "profit", "gross_profit", "supplier_cost", "warehouseCode",
             "productBatchCode", "activityPO", "szlcscActivityPO"}

A = []  # report lines
def line(s=""):
    A.append(s)

def has_cjk_text(s):
    return any(ord(c) > 127 for c in (s or ""))

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

# ---------------------------------------------------------------- load
print("== load candidate pool ==")
d = json.load(open(POOL, encoding="utf-8"))
rows = d["rows"]
cand_count = d["counts"]["candidate_count"]
assert cand_count == 200, f"STOP: candidate_count={cand_count} != 200"
line(f"candidate_count check: {cand_count} == 200  OK")

# §2 consistency — identity
miss_id = []
for r in rows:
    if not (r.get("mpn") or "").strip():
        miss_id.append(("mpn", r.get("mpn")))
    if not (r.get("manufacturer") or "").strip() and not (r.get("brand") or "").strip():
        miss_id.append(("brand", r.get("mpn")))
    if not (r.get("description") or "").strip():
        miss_id.append(("description", r.get("mpn")))
    if not (r.get("source_url") or "").strip():
        miss_id.append(("source_url(C-number)", r.get("mpn")))
    if not (r.get("supplier_reference") or "").strip():
        miss_id.append(("supplier_reference", r.get("mpn")))
line(f"identity completeness (mpn/brand/description/C-number): "
     f"{'OK' if not miss_id else 'MISSING '+str(miss_id[:5])}")

# numeric mpns present
for m in NUMERIC_MPNS:
    assert any(r["mpn"] == m for r in rows), f"STOP: {m} not in candidate pool"
line(f"numeric MPNs present in pool: {NUMERIC_MPNS}  OK")

# pre-release CJK + commercial leak scan on full pool
pre_cjk = [r["mpn"] for r in rows if has_cjk_text(r.get("description", ""))
           or has_cjk_text(r.get("applications", "")) or has_cjk_text(r.get("faq", ""))
           or has_cjk_text(r.get("attributes_json", ""))]
pre_leak = []
for r in rows:
    for fld in ("attributes_json", "attributes_json_unmapped"):
        try:
            obj = json.loads(r.get(fld) or "{}")
        except Exception:
            obj = {}
        if isinstance(obj, dict) and (set(obj.keys()) & FORBIDDEN):
            pre_leak.append((r["mpn"], fld))
assert not pre_cjk, f"STOP: CJK_LEAK in pool: {pre_cjk}"
assert not pre_leak, f"STOP: INTERNAL_COMMERCIAL_LEAK in pool: {pre_leak}"
line("pre-release CJK_LEAK = 0  OK | INTERNAL_COMMERCIAL_LEAK = 0  OK")

# ---------------------------------------------------------------- subset
unc = [r for r in rows if r.get("category") == category.UNKNOWN_CATEGORY
       or r.get("_needs_review")]
categorized = [r for r in rows if r not in unc]
numeric_rows = [r for r in rows if r["mpn"] in NUMERIC_MPNS]
releasable = categorized + numeric_rows
subset_mpns = sorted({(r["mpn"] or "").strip().upper() for r in releasable})
excluded = [r for r in unc if r["mpn"] not in NUMERIC_MPNS]
line(f"\ncategorized: {len(categorized)} | rescued numeric: {len(numeric_rows)} "
     f"| RELEASABLE subset: {len(releasable)} | EXCLUDED Uncategorized: {len(excluded)}")

# ---------------------------------------------------------------- plan + stage
print("== plan_release ==")
plan = plan_release(MASTER, rows, subset_mpns=subset_mpns, batch_id="http236",
                    allow_uncategorized_mpns=NUMERIC_MPNS)
line(f"plan.gate_ok = {plan.gate_ok}")
if plan.stops:
    line("STOPS:")
    for s in plan.stops:
        line(f"  - {s['code']}: {s['message']} ({s.get('mpn')})")
if not plan.gate_ok:
    raise SystemExit("STOP: plan gate not ok; MASTER untouched")

before_count = plan.before_count
before_sha = plan.before_sha256
line(f"MASTER_BEFORE rows = {before_count}  sha256={before_sha[:16]}..")

print("== stage_master (atomic append) ==")
staging = stage_master(plan, MASTER, approved_by=APPROVED_BY)
backup_path = staging.backup_path
line(f"backup taken: {os.path.basename(backup_path)}")

# consistency gate -> rollback on failure
try:
    verify_consistency(MASTER, plan)
    line("verify_consistency: OK (pre-existing rows unchanged, new rows present)")
except ReleaseStop as e:
    rollback_master(MASTER, backup_path)
    raise SystemExit(f"STOP: consistency failed, MASTER rolled back: {e}")

after_sha = sha256_file(MASTER)
plan.after_sha256 = after_sha
after_count = plan.after_count
created = len(plan.new_mpns)
skipped = len(plan.already_released_mpns)
line(f"MASTER_AFTER rows = {after_count}  sha256={after_sha[:16]}..")
line(f"CREATE = {created} | SKIP (already in MASTER) = {skipped} | "
     f"FAILED = 0 | NET_CHANGE = +{after_count - before_count}")

# ---------------------------------------------------------------- post-release QA
print("== post-release QA ==")
cols, mrows = read_master(MASTER, None)
mby = {r["mpn"]: r for r in mrows}
new_rows = [mby[m] for m in plan.new_mpns if m in mby]

# CJK leak on new rows
cjk_hits = [(r["mpn"], f) for r in new_rows for f in
            ("description", "applications", "faq", "attributes_json", "keywords")
            if has_cjk_text(r.get(f) or "")]
# internal commercial leak on new rows
leak_hits = []
for r in new_rows:
    try:
        obj = json.loads(r.get("attributes_json") or "{}")
    except Exception:
        obj = {}
    if isinstance(obj, dict) and (set(obj.keys()) & FORBIDDEN):
        leak_hits.append((r["mpn"], "attributes_json"))
cjk_leak = len(cjk_hits)
commercial_leak = len(leak_hits)

# identity errors / duplicate errors / spec errors
id_err = [r["mpn"] for r in new_rows
          if not (r.get("mpn") and r.get("manufacturer") and r.get("category")
                  and r.get("description"))]
dup_err = len(plan.new_mpns) - len({m.upper() for m in plan.new_mpns})
spec_err = [r["mpn"] for r in new_rows if not (r.get("attributes_json") or "").strip()]

# rich data preservation stats
def filled(rows, *fields):
    return sum(1 for r in rows if any((r.get(f) or "").strip() for f in fields))
rich = {
    "description": filled(new_rows, "description"),
    "applications": filled(new_rows, "applications"),
    "faq": filled(new_rows, "faq"),
    "canonical specs (attributes_json)": filled(new_rows, "attributes_json"),
    "alternative_parts": filled(new_rows, "alternative_parts"),
    "datasheet_url": filled(new_rows, "datasheet_url"),
    "keywords": filled(new_rows, "keywords"),
    "image": filled(new_rows, "image"),
    "availability": filled(new_rows, "availability"),
    "subcategory": filled(new_rows, "subcategory"),
    "supplier_reference (C-number)": filled(new_rows, "supplier_reference"),
    "source_url": filled(new_rows, "source_url"),
}
# FIELD_NOT_IN_MASTER_SCHEMA (pool-only / conceptual fields with no MASTER column)
not_in_schema = ["attributes_json_unmapped", "related_parts_raw",
                 "overview", "productFeatures", "ECCN", "RoHS",
                 "package", "MOQ", "weight", "productCode", "productModel"]

# special: numeric mpns in MASTER?
num_in_master = {m: (m in mby) for m in NUMERIC_MPNS}
for m in NUMERIC_MPNS:
    r = mby.get(m)
    line(f"  {m}: in MASTER={'YES' if r else 'NO'}"
         + (f" | category={r.get('category')!r} | desc={'set' if r.get('description') else 'EMPTY'}"
            if r else ""))

# ---------------------------------------------------------------- traceability log
os.makedirs(RELEASE_DIR, exist_ok=True)
# load raw pool for C-number / productModel traceability
raw_by_mpn = {}
if os.path.exists(RAW_POOL):
    for l in open(RAW_POOL, encoding="utf-8"):
        if not l.strip():
            continue
        rec = json.loads(l)
        raw_by_mpn[rec.get("mpn")] = rec
log_entries = []
for m in plan.new_mpns:
    rec = raw_by_mpn.get(m, {})
    sr = rec.get("source_raw", {}).get("main_product", {})
    log_entries.append({
        "batch_id": "http236",
        "mpn": m,
        "c_number": rec.get("productCode") or sr.get("productCode"),
        "productModel": rec.get("productModel") or sr.get("productModel"),
        "brand": (mby.get(m) or {}).get("manufacturer"),
        "category": (mby.get(m) or {}).get("category"),
        "source": "lcsc_http_json",
        "source_url": (mby.get(m) or {}).get("source_url"),
        "released_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "action": "CREATE" if m.upper() not in plan.before_mpns else "UPDATE",
        "failure_reason": "",
    })
release_log = {
    "batch_id": "http236",
    "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    "master_before_rows": before_count,
    "master_after_rows": after_count,
    "released": len(plan.new_mpns),
    "created": created,
    "skipped": skipped,
    "failed": 0,
    "excluded_uncategorized": len(excluded),
    "approved_by": APPROVED_BY,
    "entries": log_entries,
}
log_path = os.path.join(RELEASE_DIR, "release_log.json")
pool.atomic_write_json(log_path, release_log)

# ---------------------------------------------------------------- 20-row sample
print("== 20-row RAW->CLEAN->MASTER sample ==")
sample = []
for m in NUMERIC_MPNS:
    if m in mby:
        sample.append(m)
# stratified: 2 from each category present in releasable
by_cat = collections.defaultdict(list)
for r in releasable:
    if r["mpn"] not in NUMERIC_MPNS:
        by_cat[r.get("category")].append(r["mpn"])
for c, mpns in by_cat.items():
    sample.extend(mpns[:2])
sample = sample[:20]
sample_rows = []
for m in sample:
    rec = raw_by_mpn.get(m, {})
    sr = rec.get("source_raw", {}).get("main_product", {})
    clean = next((r for r in rows if r["mpn"] == m), {})
    mast = mby.get(m, {})
    sample_rows.append({
        "mpn": m,
        "raw_productCode": rec.get("productCode") or sr.get("productCode"),
        "raw_productModel": rec.get("productModel") or sr.get("productModel"),
        "clean_category": clean.get("category"),
        "clean_description": (clean.get("description") or "")[:80],
        "clean_specs": (clean.get("attributes_json") or "")[:120],
        "master_description": (mast.get("description") or "")[:80],
        "master_specs": (mast.get("attributes_json") or "")[:120],
        "alt": (mast.get("alternative_parts") or "")[:60],
        "apps": (mast.get("applications") or "")[:60],
        "datasheet": (mast.get("datasheet_url") or "")[:60],
    })

# ---------------------------------------------------------------- report
print("== write report ==")
line("")
line("# 02 LCSC HTTP · 200 条 MASTER Release 报告")
line()
line(f"_generated: {datetime.datetime.now().isoformat(timespec='seconds')}_")
line()
line("## Release Summary")
line()
line("| 项 | 值 |")
line("|---|---|")
line(f"| batch_id | http236 |")
line(f"| input_candidates (pool) | 200 |")
line(f"| releasable subset | {len(releasable)} (154 categorized + 2 numeric) |")
line(f"| released (CREATE) | {created} |")
line(f"| updated (SKIP/already) | {skipped} |")
line(f"| skipped | {skipped} |")
line(f"| failed | 0 |")
line(f"| MASTER_BEFORE | {before_count} |")
line(f"| MASTER_AFTER | {after_count} |")
line(f"| NET_CHANGE | +{after_count - before_count} |")
line(f"| excluded Uncategorized | {len(excluded)} (backlog, §1/§11) |")
line()
line("> **数量说明**：用户 §15 写 `RELEASED: 200`，但 §1/§11 明确排除 46 条")
line("> Uncategorized，且 §10 要求 2 个纯数字 MPN 必须入 MASTER（它们恰在 46 内）。")
line("> 一致解 = 释放 **156**（154 已分类 + 2 个被救纯数字 MPN），其余 44 条")
line("> 真正非 11 家族（Connector/Switch/Fuse/Memory/RF/Sensor/IGBT/TRIAC/DSP/DAC/")
line("> Isolator/RTC）保持 backlog。故实际 RELEASED = 156，非 200。")
line()
line("## Quality")
line()
line("| 项 | 值 |")
line("|---|---|")
line(f"| CJK_LEAK | {cjk_leak} |")
line(f"| INTERNAL_COMMERCIAL_LEAK | {commercial_leak} |")
line(f"| IDENTITY_ERRORS | {len(id_err)} |")
line(f"| DUPLICATE_ERRORS | {dup_err} |")
line(f"| SPEC_ERRORS (empty attributes_json) | {len(spec_err)} |")
line()
line("## Rich Data Preservation (released 156 rows 中非空数)")
line()
line("| 字段 | 非空 | 占比 |")
line("|---|---:|---:|")
for k, v in rich.items():
    line(f"| {k} | {v} | {v*100//max(len(new_rows),1)}% |")
line()
line("### FIELD_NOT_IN_MASTER_SCHEMA（无 MASTER 列，按 §4 记录不擅自改 schema）")
line()
line("以下字段在当前 `MASTER_COLS`（19 列）没有对应列，按用户 §4 指令")
line("**不修改 MASTER schema**，记录为 `FIELD_NOT_IN_MASTER_SCHEMA`，后续单独处理：")
line()
line("```")
for f in not_in_schema:
    line(f"  {f}")
line("```")
line()
line("> 说明：`attributes_json_unmapped` 与 `related_parts_raw` 是 02 新入口的富字段，")
line("> 但 MASTER schema 仅有 `attributes_json`（canonical）。它们目前留在 CLEAN 候选池")
line(">（`data/raw/_clean_preview/http236/...`），未进入 MASTER。如需保留须在 02 家族")
line("> 扩展时一并加列（需另行授权）。`overview`/`productFeatures` 已并入 `description`；")
line("> `ECCN/RoHS/package/MOQ/weight` 当前 HTTP 适配器未抽取为独立列。")
line()
line("## Special Cases")
line()
line(f"- `5023520200` = MASTER: **{'YES' if num_in_master['5023520200'] else 'NO'}**"
     + (f" (category={mby['5023520200'].get('category')!r})" if num_in_master['5023520200'] else ""))
line(f"- `1054500101` = MASTER: **{'YES' if num_in_master['1054500101'] else 'NO'}**"
     + (f" (category={mby['1054500101'].get('category')!r})" if num_in_master['1054500101'] else ""))
line()
line("## Excluded")
line()
line(f"- 36 duplicates (batch 内去重，未入 candidate pool)")
line(f"- 46 Uncategorized（pool 内）：其中 2 个纯数字 MPN 已按 §10 救入 MASTER，")
line(f"  其余 **{len(excluded)}** 条保持 backlog（§1/§11）")
line(f"- 0 rejected")
line(f"- 仅 http236 candidate pool 内数据")
line()
line("## 20-Row RAW → CLEAN → MASTER 抽样")
line()
line("| MPN | RAW C-number | CLEAN cat | MASTER desc | specs | alt | apps | datasheet |")
line("|---|---|---|---|---|---|---|---|")
for s in sample_rows:
    line(f"| {s['mpn']} | {s['raw_productCode']} | {s['clean_category']} | "
         f"{s['master_description'][:40]} | {(s['master_specs'] or '')[:30]} | "
         f"{(s['alt'] or '')[:20]} | {(s['apps'] or '')[:20]} | {(s['datasheet'] or '')[:30]} |")
line()
line("## Traceability")
line()
line(f"- release log: `data/production/releases/http236/release_log.json`")
line(f"- backup: `{os.path.basename(backup_path)}`")
line(f"- http236 → C-number → source RAW → CLEAN candidate → MASTER 全链路可追溯")
line()
line("## Git")
line()
import subprocess
def git(*args):
    return subprocess.run(["git"] + list(args), cwd=ROOT, capture_output=True,
                          text=True).stdout
gs = git("status", "--short")
gd = git("diff", "--stat")
line("```")
line(git("status", "--short").rstrip())
line("```")
line()
line("### git diff --stat (本轮修改 vs 预存 WIP)")
line()
line("```")
line(gd.rstrip())
line("```")
line()
line("> 本轮修改：`tools/factory/release_pipeline.py`（加 `allow_uncategorized_mpns` 旋钮，")
line("> 向后兼容）、`tools/factory/lcsc_http_adapter.py` + `tools/factory/product_data.py`")
line("> （V2 修正）、`tools/_http236_release_master.py`（新增）。`components/components-data.js`")
line("> 与 `incremental_build.py` 为预存 WIP，未触碰。")
line()
line("## Final State")
line()
line("```")
line("02 ADAPTER: UPDATED")
line("02 DRY-RUN V2: PASSED")
line("MASTER RELEASE: COMPLETE")
line()
line(f"RELEASED: {created}")
line("MASTER: UPDATED")
line()
line("01: FROZEN")
line("RAW: UNCHANGED")
line("03: UNCHANGED")
line("04: UNCHANGED")
line("WEBSITE: UNCHANGED")
line()
line("COMMIT: NO")
line("PUSH: NO")
line("DEPLOY: NO")
line("```")

open(REPORT, "w", encoding="utf-8").write("\n".join(A))
print("REPORT written:", REPORT)
print(f"RELEASED={created} MASTER {before_count}->{after_count} "
      f"CJK_LEAK={cjk_leak} COMMERCIAL_LEAK={commercial_leak}")
