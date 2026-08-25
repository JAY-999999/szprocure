# Data Factory v1 — Generated Page Inspection Report

- **Date:** 2026-08-26
- **Output target:** `_gen_test/` (throwaway dir via `--out`; live site untouched)
- **Source master:** `data/master_parts_v1.csv` (54 rows → 53 product groups)

## 1. Page inventory

| Type | Count | Notes |
|------|-------|-------|
| Product pages (`/products/<slug>/`) | **53** | 0 missing `index.html` |
| Manufacturer pages (`/manufacturers/<slug>/`) | **29** | distinct canonical brands |
| Component category pages (`/components/<top>/`) | **6** | all 6 canonical categories (incl. empty `sensors`) |
| Structured records (`parts.json`) | **53** | mirrors product pages 1:1 |
| Sitemaps | 1 (`sitemap_parts.xml`) + index | |

## 2. Quality checks

- ✅ Every one of the 53 product directories contains a valid `index.html` (**0 missing**).
- ✅ Canonical attribute keys rendered on pages (verified `frequency_hz`,
  `flash_bytes`, `ram_bytes` present in the `STM32F103C8T6` page).
- ✅ Breadcrumb JSON-LD present on product pages.
- ✅ Edge rows handled gracefully — `TEST-MALFORMED` produced
  `/products/testmalformed/index.html` without crashing the batch.
- ✅ No slug collisions / no silent page overwrite (SlugRegistry auto-suffixes
  on conflict; 0 collisions this run).

## 3. Category coverage (target = 5 classes)

| Target class | Fine categories covered | Pages |
|--------------|------------------------|-------|
| MCU | Microcontroller | 12 |
| MOSFET | MOSFET | 10 |
| Passive | Resistor / Capacitor / Inductor | 4 + 6 + 1 = 11 |
| Connector | USB Connectors / Pin Header | 3 + 7 = 10 |
| Module | WiFi / RF / Cellular / GNSS | 4 + 1 + 3 + 2 = 10 |
| **Total** | 11 fine categories | **53** |

All five required classes (MCU / MOSFET / Passive / Connector / Module) are
present with healthy counts.

## 4. Conclusion

✅ 53 SKU pages generated successfully from the 54-row master with full
structural integrity. The **generation stage** of the Data Factory link is
operational. Generated pages use the existing (frozen) SKU template — **no
template/visual changes were made**, per the standing constraint.

> Note: `_gen_test/` is a transient validation artifact and can be deleted; the
> canonical deliverables are the master CSV + the 4 reports. Re-run with:
> `python gen_parts.py --csv data/master_parts_v1.csv --out _gen_test`
