import json, re, os
ROOT = r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site"
RAW = os.path.join(ROOT, "data", "raw", "_clean_preview", "http236",
                   "products", "raw", "http236.jsonl")
LOG = os.path.join(ROOT, "data", "production", "releases", "http236", "release_log.json")
REPORT = os.path.join(ROOT, "02_LCSC_HTTP_200_MASTER_RELEASE_REPORT.md")

recs = [json.loads(l) for l in open(RAW, encoding="utf-8") if l.strip()]
raw_map = {r["mpn"]: (r.get("supplier_sku") or "") for r in recs}
print("raw C-number map size:", len(raw_map))

# fix traceability log
log = json.load(open(LOG, encoding="utf-8"))
fixed = 0
for e in log["entries"]:
    c = raw_map.get(e["mpn"]) or e.get("c_number")
    if c:
        e["c_number"] = c
        fixed += 1
    if not e.get("productModel"):
        e["productModel"] = e["mpn"]
json.dump(log, open(LOG, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("release_log c_number fixed:", fixed)

# fix report sample table (replace "| None |" in col2 of sample rows)
rep = open(REPORT, encoding="utf-8").read()
out = []
patched = 0
for ln in rep.split("\n"):
    m = re.match(r"^\|\s*([^|]+?)\s*\|\s*None\s*\|", ln)
    if m:
        mpn = m.group(1).strip()
        cnum = raw_map.get(mpn, "")
        ln = ln.replace("| None |", f"| {cnum} |", 1)
        patched += 1
    out.append(ln)
open(REPORT, "w", encoding="utf-8").write("\n".join(out))
print("report sample rows patched:", patched)
