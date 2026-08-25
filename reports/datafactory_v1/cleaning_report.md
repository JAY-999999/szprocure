# Data Factory v1 — Cleaning Report

- Raw files processed : 3
- Rows in (non-empty MPN) : 54
- Master rows out : 54
- Attributes parsed (free-text) : 53
- Attribute keys legacy-normalized : 148
- Attributes malformed (kept raw) : 1
- Unknown attribute keys (needs_review) : 1
- Unknown brands (needs_review) : 3
- Missing manufacturers (needs_review) : 1
- Rows flagged needs_review : 6

## Cleaning-stage review queue

| MPN | Brand | Reason |
|-----|-------|--------|
| USBA-F-CUI | CUI | unknown_manufacturer |
| ATGM336H | MysterySemi | unknown_manufacturer |
| TEST-EMPTY-MFR |  | missing_manufacturer |
| TEST-UNK-BRAND | ZZZFakeChipCorp | unknown_manufacturer |
| TEST-UNK-ATTR | STMicroelectronics | unknown_attribute_key=Weird Quantum Metric |
| TEST-MALFORMED | Yageo | malformed_attributes |
