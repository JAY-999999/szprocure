# Data Factory v1 — Validation Report

- **Date:** 2026-08-26
- **Scope:** Small-loop validation of the data production link
  `Raw → Cleaning → Master DB → Validation → gen_parts.py → SKU Page`.
  This is a **link test, not a scale test**: 50 real SKU (立创 25 + 华强/汉芯 25)
  + 4 deliberate edge rows = 54 processed.
- **Live site:** untouched. Generation ran with `--out _gen_test` (throwaway dir).

## 1. Pipeline throughput

| Stage | Input | Output |
|-------|-------|--------|
| Raw supplier CSV (LCSC 25 / HQEW 25 / Edge 4) | 54 rows | 54 rows |
| Cleaning (`tools/clean_factory.py`) | 54 raw | `data/master_parts_v1.csv` (54 master rows) |
| `gen_parts.py` validation + generation | 54 parts | 53 product groups (1 merged duplicate) |

## 2. Cleaning stage (single source of truth = `gen_parts.py` normalizers)

`clean_factory.py` reuses `LEGACY_ATTR_MAP`, `load_attr_allowlist`,
`load_mfr_canonical` / `canonicalize_brand`, `resolve_cat`, `slugify` so the
cleaning logic can never diverge from the frozen generator.

- Free-text `attributes` parsed: 50 rows carried a `Key: value; …` block; 4 edge
  rows exercised special paths.
- Legacy attribute keys normalized to canonical §4 keys, e.g.
  `Clock Speed → frequency_hz`, `Program Memory`/`Flash Memory → flash_bytes`,
  `SRAM Size → ram_bytes`, `Drain Source Voltage → vds_v`,
  `On Resistance → rds_on_mohm`, `Number of Positions → positions`,
  `Output Power → output_power_dbm`, `Sensitivity → sensitivity_dbm`.
- Malformed attributes: **1** (kept raw string, flagged).
- Unknown attribute keys (outside §4 allowlist): **1** (`Weird Quantum Metric`).
- Unknown brands (not in `mfr_canonical.csv`): **3** (CUI, MysterySemi, ZZZFakeChipCorp).
- Missing manufacturer: **1**.
- Rows flagged `needs_review`: **6**.

## 3. `gen_parts.py` validation (re-runs canonicalization + allowlist + review routing)

- Loaded `mfr_canonical` (238 aliases) + attributes allowlist (60 keys).
- 54 parts → **53 groups** (1 merged duplicate: `RC0805JR-0710KL` present in
  both LCSC and HQEW → collapsed to one `/products/rc0805jr0710kl/` page, as
  designed — cross-source same-MPN is expected, not a collision).
- Slug collisions: **0** (no URL overwrite risk).
- P0-2 unmapped manufacturer (`needs_review`): **3**.
- P0-3 unknown attribute key (`needs_review`): **1**.
- Malformed `attributes_json` (valid JSON but not an object): **1**.
- Total review-queue items: **6**.

## 4. Review queue (`gen_parts.py` official output)

| MPN | Brand | Reason | Detail |
|-----|-------|--------|--------|
| USBA-F-CUI | CUI | unknown_manufacturer | raw=CUI |
| ATGM336H | MysterySemi | unknown_manufacturer | raw=MysterySemi |
| TEST-UNK-BRAND | ZZZFakeChipCorp | unknown_manufacturer | raw=ZZZFakeChipCorp |
| TEST-EMPTY-MFR | (empty) | missing_manufacturer | empty manufacturer |
| TEST-UNK-ATTR | STMicroelectronics | unknown_attribute_key | key=Weird Quantum Metric |
| TEST-MALFORMED | Yageo | malformed_attributes | attributes_json is not an object |

## 5. Conclusion

✅ The data production link is verified end-to-end. Multi-source supplier exports
with messy free-text attributes are cleaned into a canonical 16-column master;
brand canonicalization, attribute legacy-normalization, allowlist validation,
and review-routing all fire correctly and **idempotently** (cleaning-stage flags
== gen_parts-stage flags). **6 review items were caught — none silently
ingested.** No slug collisions, no generation crashes.

Ready to scale to the 1000-SKU / 200k closure once the user approves.
