import os, re, csv, glob, collections

REPO = "C:/Users/Administrator.SC-202105071542/Desktop/szprocure-site"
PREVIEW = os.path.join(REPO, "data/founder/preview/products")
COMPS = os.path.join(REPO, "components/index.html")

def slugify(s):
    s = (s or "").strip().lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)
    return s.strip('-')

# ---- published-at-launch target sets ----
master = os.path.join(REPO, "data/founder/master_founder_10.csv")
sku_slugs = set()
mfr_slugs = set()
with open(master, encoding="utf-8-sig", newline="") as f:
    for r in csv.DictReader(f):
        sku_slugs.add((r.get("url_slug") or "").strip())
        mfr_slugs.add(slugify(r.get("manufacturer")))

L1 = {"integrated-circuits", "semiconductor-components", "passive-components",
      "sensors", "connectors", "modules"}

standard = set()
for d in os.listdir(REPO):
    dp = os.path.join(REPO, d)
    if os.path.isdir(dp) and os.path.exists(os.path.join(dp, "index.html")):
        standard.add(d)

def resolve(path):
    p = path.strip().lstrip("/")
    if p == "":
        return "index.html"
    if p.endswith("/"):
        return p + "index.html"
    return p

def exists_at(rel):
    return os.path.exists(os.path.join(REPO, rel))

def classify(href):
    if href.startswith("mailto:") or href.startswith("tel:") or href.startswith("#") \
       or href.startswith("javascript:") or href.startswith("https://wa.me") \
       or href.startswith("https://t.me"):
        return ("SKIP", href, "non-http/anchor")
    if href.startswith("http://") or href.startswith("https://"):
        if "szprocure.com" not in href:
            return ("SKIP", href, "external")
        href = "/" + href.split("szprocure.com", 1)[1].lstrip("/") if "szprocure.com" in href else href
    if not href.startswith("/"):
        return ("SKIP", href, "relative-unexpected")
    path = href.split("#")[0].split("?")[0]
    if path == "":
        return ("OK", "/", "index.html")
    m = re.match(r"^/products/([^/]+)/?$", path)
    if m:
        slug = m.group(1)
        if slug in sku_slugs:
            return ("OK_PROD", path, "in 10-SKU launch set")
        if exists_at(resolve(path)):
            return ("OK", path, "file exists")
        return ("DEAD", path, "product not in 10-SKU Beta set & no file")
    m = re.match(r"^/manufacturers/([^/]+)/?$", path)
    if m:
        slug = m.group(1)
        if slug in mfr_slugs:
            return ("OK_MFR", path, "manufacturer in launch set")
        if exists_at(resolve(path)):
            return ("OK", path, "file exists")
        return ("DEAD", path, "manufacturer not in launch set & no file")
    m = re.match(r"^/components/([^/]+)/?$", path)
    if m:
        cat = m.group(1)
        if cat in L1:
            return ("OK_COMP", path, "component cat in 6-set")
        if exists_at(resolve(path)):
            return ("OK", path, "file exists")
        return ("DEAD", path, "component cat unknown & no file")
    m = re.match(r"^/components/([^/]+)/([^/]+)/?$", path)
    if m:
        # Phase 2.7 (A): L3 subcategory page — lives under
        # data/founder/preview/components/<l2>/<l3>/ during Beta.
        if exists_at(os.path.join("data/founder/preview", resolve(path))):
            return ("OK_COMP", path, "L3 subcategory page (preview)")
        if exists_at(resolve(path)):
            return ("OK_COMP", path, "L3 subcategory page")
        return ("DEAD", path, "L3 subcat unknown & no file")
    rel = resolve(path)
    if exists_at(rel):
        return ("OK", path, rel)
    if path.rstrip("/") in standard:
        return ("OK_PAGE", path, "standard page dir")
    if exists_at(rel.rstrip("/") + "/index.html"):
        return ("OK", path, "dir index")
    return ("DEAD", path, "no file & not in known sets")

pages = sorted(glob.glob(os.path.join(PREVIEW, "*", "index.html")))
# Phase 2.7 (A): also scan the generated component L2/L3 preview pages so the
# L2->L3 and L3->SKU internal-link chains are covered by the audit.
pages += sorted(glob.glob(os.path.join(REPO, "data/founder/preview/components/**", "index.html"), recursive=True))
pages.append(COMPS)

total = 0
dead = []
bytype = collections.Counter()
for pg in pages:
    html = open(pg, encoding="utf-8").read()
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    for h in hrefs:
        c = classify(h)
        bytype[c[0]] += 1
        total += 1
        if c[0] == "DEAD":
            dead.append((os.path.relpath(pg, REPO), h, c[2]))

print("Pages checked:", len(pages))
print("Total internal links:", total)
print("By classification:", dict(bytype))
print("\nDEAD LINKS (must fix before launch):")
if dead:
    for src, h, reason in dead:
        print(f"  [{src}] -> {h}  ({reason})")
else:
    print("  NONE - 0 dead links")
