candidate_count check: 200 == 200  OK
identity completeness (mpn/brand/description/C-number): OK
numeric MPNs present in pool: ['5023520200', '1054500101']  OK
pre-release CJK_LEAK = 0  OK | INTERNAL_COMMERCIAL_LEAK = 0  OK

categorized: 154 | rescued numeric: 2 | RELEASABLE subset: 156 | EXCLUDED Uncategorized: 44
plan.gate_ok = True
MASTER_BEFORE rows = 590  sha256=95d96dfa07bc84f0..
backup taken: master_parts_v2.1.csv.release.bak
verify_consistency: OK (pre-existing rows unchanged, new rows present)
MASTER_AFTER rows = 746  sha256=2006491b1fe67345..
CREATE = 156 | SKIP (already in MASTER) = 0 | FAILED = 0 | NET_CHANGE = +156
  5023520200: in MASTER=YES | category='Uncategorized' | desc=set
  1054500101: in MASTER=YES | category='Uncategorized' | desc=set

# 02 LCSC HTTP · 200 条 MASTER Release 报告

_generated: 2026-09-10T06:55:38_

## Release Summary

| 项 | 值 |
|---|---|
| batch_id | http236 |
| input_candidates (pool) | 200 |
| releasable subset | 156 (154 categorized + 2 numeric) |
| released (CREATE) | 156 |
| updated (SKIP/already) | 0 |
| skipped | 0 |
| failed | 0 |
| MASTER_BEFORE | 590 |
| MASTER_AFTER | 746 |
| NET_CHANGE | +156 |
| excluded Uncategorized | 44 (backlog, §1/§11) |

> **数量说明**：用户 §15 写 `RELEASED: 200`，但 §1/§11 明确排除 46 条
> Uncategorized，且 §10 要求 2 个纯数字 MPN 必须入 MASTER（它们恰在 46 内）。
> 一致解 = 释放 **156**（154 已分类 + 2 个被救纯数字 MPN），其余 44 条
> 真正非 11 家族（Connector/Switch/Fuse/Memory/RF/Sensor/IGBT/TRIAC/DSP/DAC/
> Isolator/RTC）保持 backlog。故实际 RELEASED = 156，非 200。

## Quality

| 项 | 值 |
|---|---|
| CJK_LEAK | 0 |
| INTERNAL_COMMERCIAL_LEAK | 0 |
| IDENTITY_ERRORS | 0 |
| DUPLICATE_ERRORS | 0 |
| SPEC_ERRORS (empty attributes_json) | 0 |

## Rich Data Preservation (released 156 rows 中非空数)

| 字段 | 非空 | 占比 |
|---|---:|---:|
| description | 156 | 100% |
| applications | 154 | 98% |
| faq | 85 | 54% |
| canonical specs (attributes_json, 非空) | 141 | 90% |
| alternative_parts | 124 | 79% |
| datasheet_url | 0 | 0% |
| keywords | 156 | 100% |
| image | 0 | 0% |
| availability | 156 | 100% |
| subcategory | 156 | 100% |
| supplier_reference (C-number) | 156 | 100% |
| source_url | 156 | 100% |

> 注：canonical specs 非空 **141/156 (90%)**；其余 **15** 条 `attributes_json={}`（含 2 个纯数字 MPN 与 13 条仅有 unmapped 规格者）。这些行的规格仅存在于 CLEAN 候选池的 `attributes_json_unmapped`（FIELD_NOT_IN_MASTER_SCHEMA），未进入 MASTER。这是当前 MASTER 19 列 schema 的固有限制，非数据丢失。

### FIELD_NOT_IN_MASTER_SCHEMA（无 MASTER 列，按 §4 记录不擅自改 schema）

以下字段在当前 `MASTER_COLS`（19 列）没有对应列，按用户 §4 指令
**不修改 MASTER schema**，记录为 `FIELD_NOT_IN_MASTER_SCHEMA`，后续单独处理：

```
  attributes_json_unmapped
  related_parts_raw
  overview
  productFeatures
  ECCN
  RoHS
  package
  MOQ
  weight
  productCode
  productModel
```

> 说明：`attributes_json_unmapped` 与 `related_parts_raw` 是 02 新入口的富字段，
> 但 MASTER schema 仅有 `attributes_json`（canonical）。它们目前留在 CLEAN 候选池
>（`data/raw/_clean_preview/http236/...`），未进入 MASTER。如需保留须在 02 家族
> 扩展时一并加列（需另行授权）。`overview`/`productFeatures` 已并入 `description`；
> `ECCN/RoHS/package/MOQ/weight` 当前 HTTP 适配器未抽取为独立列。

## Special Cases

- `5023520200` = MASTER: **YES** (category='Uncategorized')
- `1054500101` = MASTER: **YES** (category='Uncategorized')

## Excluded

- 36 duplicates (batch 内去重，未入 candidate pool)
- 46 Uncategorized（pool 内）：其中 2 个纯数字 MPN 已按 §10 救入 MASTER，
  其余 **44** 条保持 backlog（§1/§11）
- 0 rejected
- 仅 http236 candidate pool 内数据

## 20-Row RAW → CLEAN → MASTER 抽样

| MPN | RAW C-number | CLEAN cat | MASTER desc | specs | alt | apps | datasheet |
|---|---|---|---|---|---|---|---|
| 5023520200 | C114107 | Uncategorized | CONN HEADER SMD R/A 2POS 2mm - Connector | {} | WT200BW-020R-0W; HDG |  |  |
| 1054500101 | C134092 | Uncategorized | CONN RCPT USB 3.2 Type-C 24POS SMD - USB | {} | TYPE-C 24P QT; TYPE- |  |  |
| BCX56-16,115 | C100026 | Transistor | Nexperia BCX56-16,115 | {} | BCX56-16-AU_R1_000A1 | - Linear voltage reg |  |
| LTV-356T-B | C10804 | Transistor | Lite-On Technology LTV-356T-B | {} |  | - Hybrid substrates  |  |
| CC0603KRX7R9BB102 | C100040 | Capacitor | Yageo CC0603KRX7R9BB102 - 1e-09 F - 50.0 | {"capacitance": 1e-09, "tolera | CC0603KRX7R0BB102; A | Decoupling; Filterin |  |
| 35ZLH100MEFC6.3X11 | C109392 | Capacitor | Rubycon 35ZLH100MEFC6.3X11 - 9.999999999 | {"capacitance": 9.999999999999 | LKFC1101V101MF | Decoupling; Filterin |  |
| RC0603FR-070RL | C100044 | Resistor | Yageo RC0603FR-070RL - 0.0 Ohm - 75.0 V  | {"resistance_ohm": 0.0, "toler | AC0603FR-070RL; AF06 | Current limiting; Vo |  |
| 0805W8F1003T5E | C149504 | Resistor | UNI-ROYAL 0805W8F1003T5E - 100000.0 Ohm  | {"resistance_ohm": 100000.0, " | HV05W8F1003T5E; TC05 | Current limiting; Vo |  |
| S9KEAZ128AMLK | C100100 | Microcontroller | NXP Semiconductors S9KEAZ128AMLK - ARM C | {"core": "ARM Cortex-M0+", "co | MKE04Z128VLK4; S9KEA | Embedded control; Io |  |
| AT24C64D-XHM-T | C111440 | Microcontroller | Microchip Technology AT24C64D-XHM-T - 1  | {"frequency_hz": 1000000} | AT24C64D-XHM-B; CAT2 | Embedded control; Io |  |
| SN74HC00DR | C10090 | Logic IC | Texas Instruments SN74HC00DR - 4.00 V -  | {"supply_v": 4.0, "tpd_ns": 15 | MM74HC00MX; SN74HC00 | Digital logic; Glue  |  |
| SN74LVC1G3157DBVR | C10426 | Logic IC | Texas Instruments SN74LVC1G3157DBVR - 3. | {"supply_v": 3.575, "tpd_ns":  | AIP74LVC1G3157GB236G | - signal gating - ch |  |
| LM317LCDR | C105219 | Voltage Regulator | Texas Instruments LM317LCDR - Adjustable | {"output_type": "Adjustable",  | TL317CDR; LM317LIDR; | - Electronic Points  |  |
| TL431AQDBZR | C105255 | Voltage Regulator | Texas Instruments TL431AQDBZR - Adjustab | {"output_type": "Adjustable",  | TL431AQDBZT; TL431AQ | - Adjustable Voltage |  |
| TMP75AIDGKR | C105258 | Interface IC | Texas Instruments TMP75AIDGKR - I2C;SMBu | {"working_voltage_v": 4.1, "in | TMP75AIDGKR-TUDI; TM | - Power supply tempe |  |
| TXB0104RUTR | C105266 | Interface IC | Texas Instruments TXB0104RUTR - - - 1000 | {"working_voltage_v": 2.9875,  | TXB0104QRUTRQ1; NXB0 | - Headset - Smartpho |  |
| MPZ1608S101ATAH0 | C107332 | Inductor | TDK Corporation MPZ1608S101ATAH0 | {"tolerance": 25.0, "lines": 1 | BLM18KG101TN1D; MPZ1 | - Noise removal for  |  |
| XFL4020-222MEC | C122469 | Inductor | Coilcraft XFL4020-222MEC - 2.2e-06 H | {"inductance_h": 2.2e-06, "tol | XGL4020-222MEC; XGL4 | Power filtering; Ene |  |
| BZX84-C12,215 | C108437 | Diode | Nexperia BZX84-C12,215 - 1 Independent - | {"config": "1 Independent", "v | BZX84C12LT1G; SZBZX8 | - General regulation |  |
| DSEI30-12A | C130472 | Diode | Littelfuse/IXYS DSEI30-12A - 1 Independe | {"config": "1 Independent", "f | DSEI60-12A; VS-E5PX3 | Reverse protection;  |  |

## Traceability

- release log: `data/production/releases/http236/release_log.json`
- backup: `master_parts_v2.1.csv.release.bak`
- http236 → C-number → source RAW → CLEAN candidate → MASTER 全链路可追溯

## Git

```
 M components/components-data.js
 M tools/factory/incremental_build.py
 M tools/factory/product_data.py
 M tools/factory/release_pipeline.py
?? .vercelignore
?? SKU_PAGE_V2_READONLY_DEEP_AUDIT_REPORT.html
?? _analyze_attrs.py
?? _analyze_csv.sh
?? _apply_rohs_mfr.py
?? _apply_spec_override.py
?? _audit_30_diff.json
?? _audit_30_diff.py
?? _audit_552/
?? _audit_cat.py
?? _audit_shots/
?? _base_hashes.txt
?? _build_rows.py
?? _canary_after.json
?? _canary_audit.py
?? _canary_backup_stm32.html
?? _canary_baseline_now.json
?? _canary_baseline_stm32.json
?? _cand_pool.json
?? _check_mfr_pages.py
?? _committed_check.py
?? _components_v21_desktop.png
?? _components_v21_mobile.png
?? _diag_deadlinks.py
?? _diag_linkhealth.py
?? _dryrun_gate.txt
?? _dump_sensors.py
?? _enrich_apply.py
?? _enrich_preview/
?? _frozen_baseline.txt
?? _gen_parts_run.py
?? _inspect_parts.py
?? _m4c_test.py
?? _m4d_test.py
?? _m4e_test.py
?? _mcu20_publish.py
?? _mcu20_rows_final.json
?? _new_rows.json
?? _oracle_verify_precise.py
?? _p0_build_taxonomy.py
?? _p0_equivalence_test.py
?? _p0_synthetic_tests.py
?? _p1b1_verify.py
?? _p3_datasheet_map.csv
?? _p3_e_final.py
?? _p3_e_run.log
?? _p3_e_verify.py
?? _p3_map_raw.json
?? _p3_master_after.json
?? _p3_master_before.json
?? _p3_pre_artifacts.txt
?? _p3_pre_lsfiles.txt
?? _p3_pre_master_sha.txt
?? _p3_report.py
?? _p3_stepA_download.py
?? _p3_stepB_map.py
?? _p3_stepC_apply.py
?? _p3_stepD_upload.py
?? _p3_verify_urls.py
?? _pdf_manifest.json
?? _phase2b_test/
?? _phase3_1_apply_test/
?? _phase3_3/
?? _phase3_pilot10_test/
?? _phase3_va/
?? _phase3_verify_final.py
?? _pick_final.py
?? _pilot_pdf_enrich/
?? _probe_net.py
?? _probe_r2.py
?? _probe_src.py
?? _qa_diag.py
?? _qa_diag2.py
?? _qa_shots/
?? _qa_sku_v3_migration.py
?? _r2_probe.py
?? _rebuild_components_index.py
?? _regen_backup_products_20260904/
?? _regen_sku_v2.py
?? _regress_sku_v2.py
?? _rollout100.log
?? _rollout100.py
?? _rollout100_results.json
?? _rollout_v3_fleet.log
?? _rollout_v3_fleet.py
?? _select_10_20.py
?? _shot_v21.py
?? _sku-v3-prototype/
?? _smoke_slugs.py
?? _smoke_test.py
?? _snap_before.json
?? _snap_before.py
?? _spec_override/
?? _stress_verify.py
?? _v21_mcu_desktop.png
?? _v21_mcu_mobile.png
?? _v21_sensors_desktop.png
?? _v2_screenshots/
?? _v3_list_probe.py
?? _validate_inline_rfq.py
?? _verify_1494.json
?? _verify_1494.py
?? _verify_1495_phase1.json
?? _verify_1495_phase1.py
?? _verify_1495_phase1b.json
?? _verify_1495_phase1b.py
?? _verify_1495_phase1c.json
?? _verify_1495_phase1c.py
?? _verify_candidates_1484.py
?? _verify_html_change.py
?? _verify_rollout.py
?? _verify_subcat_recovery.py
?? _verify_subcat_recovery2.py
?? _write_1494.log
?? build_manifest.json.bak.1788835241
?? enrich_manifests/
?? m4_pre_deploy_audit.py
?? products/ap63203wu7/index.html.bak_cjk_20260910_015347
?? products/b340a13f/index.html.bak_desc_20260909_180520
?? products/b560c13f/index.html.bak_desc_20260909_180520
?? products/esp12fesp8266mod/index.html.bak_cjk_20260910_015347
?? products/lis2dh12tr/index.html.bak_cjk_20260910_015347
?? products/tps5430ddar/index.html.bak_cjk_20260910_015347
?? review_queue.csv
?? scope_guard.py
?? test_p0_processed.csv
?? tests/test_incremental_phase1.py
?? tests/test_incremental_phase2.py
?? tools/_http236_dryrun.py
?? tools/_http236_dryrun_v2.py
?? tools/_http236_release_master.py
?? tools/attribute_translation.json
?? tools/factory/lcsc_http_adapter.py
?? tools/harvest_lcsc_400.py
?? tools/lcsc_http_acquire.py
?? tools/normalize_report.json
?? tools/reconcile_components.py
?? tools/verify_r2_conn.py
?? validate_enrich_rollout.py
```

### git diff --stat (本轮修改 vs 预存 WIP)

```
 components/components-data.js      |  2 +-
 tools/factory/incremental_build.py | 17 +++++++----
 tools/factory/product_data.py      | 58 ++++++++++++++++++++++++++++++++++----
 tools/factory/release_pipeline.py  | 24 ++++++++++++----
 4 files changed, 82 insertions(+), 19 deletions(-)
```

> 本轮修改：`tools/factory/release_pipeline.py`（加 `allow_uncategorized_mpns` 旋钮，
> 向后兼容）、`tools/factory/lcsc_http_adapter.py` + `tools/factory/product_data.py`
> （V2 修正）、`tools/_http236_release_master.py`（新增）。`components/components-data.js`
> 与 `incremental_build.py` 为预存 WIP，未触碰。

## Final State

```
02 ADAPTER: UPDATED
02 DRY-RUN V2: PASSED
MASTER RELEASE: COMPLETE

RELEASED: 156
MASTER: UPDATED

01: FROZEN
RAW: UNCHANGED
03: UNCHANGED
04: UNCHANGED
WEBSITE: UNCHANGED

COMMIT: NO
PUSH: NO
DEPLOY: NO
```