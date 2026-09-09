import csv, os, json, collections
ROOT = r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site"
MASTER = os.path.join(ROOT, "data", "production", "master_parts_v2.1.csv")
BAK = MASTER + ".release.bak"
RAW = os.path.join(ROOT, "data", "raw", "_clean_preview", "http236", "products", "raw", "http236.jsonl")

with open(MASTER, encoding="utf-8", newline="") as f:
    rows = list(csv.DictReader(f))
print("MASTER rows:", len(rows))
mby = {r["mpn"]: r for r in rows}

for m in ("5023520200", "1054500101"):
    r = mby.get(m)
    cat = r.get("category") if r else None
    desc = bool(r and r.get("description"))
    print(f"  {m}: IN={bool(r)} cat={cat!r} desc_set={desc}")

# backup comparison: first 590 rows unchanged
if os.path.exists(BAK):
    with open(BAK, encoding="utf-8", newline="") as f:
        br = list(csv.DictReader(f))
    print("backup rows:", len(br))
    same = all({k: (v or "") for k, v in br[i].items()} == {k: (v or "") for k, v in rows[i].items()}
               for i in range(min(len(br), 590)))
    print("first 590 byte-field-identical to backup:", same)
    print("new tail mpns (last 5):", [r["mpn"] for r in rows[-5:]])

# raw pool structure
print("\n-- raw pool --")
if os.path.exists(RAW):
    recs = [json.loads(l) for l in open(RAW, encoding="utf-8") if l.strip()]
    print("raw pool records:", len(recs))
    r0 = recs[0]
    print("raw record top keys:", sorted(r0.keys()))
    print("has productCode:", "productCode" in r0, "| sample productCode:", r0.get("productCode"))
    sr = r0.get("source_raw", {}).get("main_product", {})
    print("main_product.productCode:", sr.get("productCode"))
else:
    print("RAW pool MISSING:", RAW)
