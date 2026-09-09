#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_enrich_lcsc_http.py  —  LCSC 公开英文页纯 HTTP 数据增强 (TEST-ONLY 抓取器)

用途
----
对网站上已上线的 SKU（MASTER 中带合法 supplier_reference C 编号），逐个通过
    https://www.lcsc.com/en/product-detail/<Cxxxx>.html
做一次纯 HTTP 抓取，抽取「图片/价格/库存/PDF 之外」的可见信息，补全到本地数据资产。

抽取字段
--------
  attributes_json   <- 内联 JSON-LD 的 "PropertyValue" 规格 (英文键/值)
  applications      <- <h2>Applications</h2> 下的 <ul><li> 列表 (有则抓, 无则跳过)
  alternative_parts <- "Alternative Parts" 表格里的替代 MPN (锚文本, 排除自身)
  description       <- JSON-LD / Introduction 段落 (清洗掉 From $x / in stock 等)
  keywords          <- 由 mpn/manufacturer/category/package 派生并去重合并
  faq               <- JSON-LD FAQPage 的 Q&A (格式化为 "Q:..?A:..; Q:..?A:..")

明确排除 (身份红线 + 用户约束)
------------------------------
  ✗ 图片 (image)        ✗ 价格 (price)        ✗ 库存 (stock)
  ✗ Datasheet PDF       ✗ 任何 LCSC 购买/现货/交期文案

合规
----
  - 仅访问 www.lcsc.com/en/... 公开产品页 (robots 允许)。
  - wmsc.lcsc.com (LIST API) 有 Disallow:/, 不使用。
  - 真实浏览器 UA + Accept-Language: en; 不附带联系邮箱。
  - 请求间默认 sleep 1.2s, 降低封禁风险。

输出 (默认 DRY, 不写 MASTER)
----------------------------
  <out>/enrich_lcsc_raw_<ts>.csv      原始抽取 (每条 SKU 一行, 全字段)
  <out>/enrich_lcsc_patch_<ts>.csv   合并后的 MASTER-ready 行 (供 #1610 应用)
  <out>/enrich_lcsc_manifest_<ts>.json 汇总统计

写 MASTER 受控
--------------
  默认只产出 patch CSV, 绝不改写 MASTER。只有显式:
      --apply-master --i-accept-write-master YES
  才会把 patch 写回 MASTER (且仍保留备份)。测试阶段不要用此开关。

用法
----
  # 仅针对 6 个样本做测试抽取 (推荐先跑这个)
  python tools/_enrich_lcsc_http.py --refs C8734,C113767,C347475,C2128,C60490,C1591

  # 跑全部合法 SKU (谨慎, 耗时较长)
  python tools/_enrich_lcsc_http.py --limit 0
"""
import re, json, csv, time, sys, os, argparse

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HDR = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

# ---------------- 网络 ----------------
def fetch_page(code, timeout=25):
    url = f"https://www.lcsc.com/en/product-detail/{code}.html"
    import urllib.request as u
    req = u.Request(url, headers=HDR)
    with u.urlopen(req, timeout=timeout) as resp:
        return resp.getcode(), resp.read().decode("utf-8", "replace")

# ---------------- 抽取 ----------------
def extract_property_values(html):
    """内联 JSON-LD 的 PropertyValue 规格 -> [(name, value)]"""
    pairs = []
    for m in re.finditer(r'"@type":"PropertyValue","name":"([^"]*)","value":"([^"]*)"', html):
        k = m.group(1).strip()
        v = m.group(2).strip()
        if k and v:
            pairs.append((k, v))
    # 去重 (同名保留首个)
    seen = set(); out = []
    for k, v in pairs:
        if k.lower() not in seen:
            seen.add(k.lower()); out.append((k, v))
    return out

def extract_applications(html):
    """<h2>Applications</h2> 之后的 <ul><li> 列表"""
    apps = []
    m = re.search(r'<h2[^>]*>Applications</h2>(.*?)(?:<h2|</section)', html, re.S)
    if not m:
        return apps
    for li in re.findall(r'<li[^>]*>(.*?)</li>', m.group(1), re.S):
        t = re.sub(r'<[^>]+>', '', li)
        t = re.sub(r'\s+', ' ', t).strip()
        if t:
            apps.append(t)
    return apps

def _norm_mpn(s):
    return re.sub(r'[\s()\-]', '', (s or '').lower())

def extract_alt_mpns(html, self_code, self_mpn=''):
    """Alternative Parts 表格里的替代 MPN (锚文本), 排除自身编号与自身 MPN 文本"""
    alts = []
    if 'Alternative Parts</h2>' in html:
        seg = html[html.find('Alternative Parts</h2>'):]
        end = seg.find('</table>')
        scope = seg[:end + 8] if end != -1 else seg
    else:
        scope = html
    seen = set()
    self_norm = _norm_mpn(self_mpn)
    for code, text in re.findall(
        r'<a[^>]+href="[^"]*/product-detail/(C\d+)\.html[^"]*"[^>]*>(.*?)</a>', scope, re.S):
        code = code.strip()
        if code == self_code or code in seen:
            continue
        mpn = re.sub(r'<[^>]+>', '', text)
        mpn = re.sub(r'\s+', ' ', mpn).strip()
        if not mpn:
            continue
        # 排除与自身 MPN 文本相同 (忽略大小写/空格/括号) 的行
        if self_norm and _norm_mpn(mpn) == self_norm:
            continue
        seen.add(code)
        alts.append(mpn)
    return alts

def sanitize_desc(t):
    """清洗掉 LCSC 的价格/库存/交期噪声"""
    if not t:
        return ""
    t = re.sub(r'From\s*\$\s*[\d.,]+', '', t, flags=re.I)
    t = re.sub(r',?\s*[\d,]+\s*in stock', '', t, flags=re.I)
    t = re.sub(r'in stock', '', t, flags=re.I)
    t = re.sub(r',?\s*[\d,]+\s*available', '', t, flags=re.I)
    t = re.sub(r'\s+', ' ', t).strip()
    return t.strip(' ,;')

def extract_description(html):
    """JSON-LD description -> Introduction 段落 -> meta description (清洗)"""
    # 1) JSON-LD
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        items = data.get('@graph') if isinstance(data, dict) and '@graph' in data else (
            [data] if isinstance(data, dict) else [])
        for it in items:
            if isinstance(it, dict) and it.get('@type') == 'Product':
                d = it.get('description')
                if d and isinstance(d, str):
                    s = sanitize_desc(d)
                    if s:
                        return s
    # 2) Introduction / Description 章节
    for tag in ('Introduction', 'Description'):
        m = re.search(r'<h2[^>]*>' + tag + r'</h2>(.*?)(?:<h2|</section)', html, re.S)
        if m:
            txt = re.sub(r'<[^>]+>', ' ', m.group(1))
            txt = sanitize_desc(txt)
            if txt:
                return txt
    # 3) meta description (清洗)
    dm = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', html)
    if dm:
        return sanitize_desc(dm.group(1))
    return ""

def extract_faq(html):
    """JSON-LD FAQPage -> [(question, answer)]"""
    faqs = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        items = data.get('@graph') if isinstance(data, dict) and '@graph' in data else (
            [data] if isinstance(data, dict) else [])
        for it in items:
            if isinstance(it, dict) and it.get('@type') == 'FAQPage':
                for q in it.get('mainEntity', []):
                    qtext = q.get('name', '')
                    atext = q.get('acceptedAnswer', {}).get('text', '')
                    if qtext and atext:
                        faqs.append((re.sub(r'<[^>]+>', '', qtext).strip(),
                                     re.sub(r'<[^>]+>', '', atext).strip()))
    return faqs

def extract_lcsc_mfr(html):
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            data = json.loads(m.group(1))
        except Exception:
            continue
        items = data.get('@graph') if isinstance(data, dict) and '@graph' in data else (
            [data] if isinstance(data, dict) else [])
        for it in items:
            if isinstance(it, dict) and it.get('@type') == 'Product':
                b = it.get('brand')
                if isinstance(b, dict) and b.get('name'):
                    return b.get('name')
                mp = it.get('manufacturer')
                if isinstance(mp, dict) and mp.get('name'):
                    return mp.get('name')
    return ""

# ---------------- 合并 ----------------
def merge_attributes(existing_json, pairs):
    """保留策展键, 仅追加 LCSC 独有键 (按 lower 去重)"""
    cur = {}
    if existing_json:
        try:
            cur = json.loads(existing_json)
        except Exception:
            cur = {}
    if not isinstance(cur, dict):
        cur = {}
    keys_lower = {k.lower() for k in cur}
    for k, v in pairs:
        if k.lower() not in keys_lower:
            cur[k] = v
            keys_lower.add(k.lower())
    return cur

def merge_semicolon(existing, new_items):
    """合并 ; 分隔字段, 去重 (保留顺序)"""
    seen = set(); out = []
    for x in (split_semi(existing) + list(new_items)):
        x = x.strip()
        if x and x.lower() not in seen:
            seen.add(x.lower()); out.append(x)
    return '; '.join(out)

def split_semi(s):
    return [x.strip() for x in re.split(r'[;]', s or '') if x.strip()]

def faq_to_col(pairs):
    return '; '.join(f"Q: {q}?A: {a}" for q, a in pairs)

# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default='data/production/master_parts_v2.1.csv')
    ap.add_argument('--out', default='data/raw')
    ap.add_argument('--refs', default='', help='逗号分隔的 supplier_reference 白名单 (测试用)')
    ap.add_argument('--limit', type=int, default=0, help='最多处理多少行合法 SKU (0=全部)')
    ap.add_argument('--delay', type=float, default=1.2, help='请求间隔秒')
    ap.add_argument('--apply-master', action='store_true', help='写回 MASTER (需配合下方确认)')
    ap.add_argument('--i-accept-write-master', default='', help='必须传 YES 才允许写 MASTER')
    args = ap.parse_args()

    if args.apply_master and args.i_accept_write_master != 'YES':
        sys.exit("ERROR: --apply-master 需要 --i-accept-write-master YES 才执行。测试阶段请勿写 MASTER。")

    os.makedirs(args.out, exist_ok=True)
    ts = time.strftime('%Y%m%d_%H%M%S')

    # 读取 MASTER, 收集合法目标
    with open(args.master, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    valid = [r for r in rows if re.match(r'^C\d+$', (r.get('supplier_reference') or '').strip())]
    if args.refs:
        want = {x.strip() for x in args.refs.split(',') if x.strip()}
        targets = [r for r in valid if r.get('supplier_reference') in want]
    else:
        targets = valid
    if args.limit:
        targets = targets[:args.limit]

    print(f"[info] MASTER rows={len(rows)} valid(C#)={len(valid)} targets={len(targets)}")

    raw_path = os.path.join(args.out, f'enrich_lcsc_raw_{ts}.csv')
    patch_path = os.path.join(args.out, f'enrich_lcsc_patch_{ts}.csv')
    mani_path = os.path.join(args.out, f'enrich_lcsc_manifest_{ts}.json')

    raw_fields = ['mpn', 'supplier_reference', 'http_status', 'n_attrs', 'n_apps',
                  'n_alts', 'n_faq', 'lcsc_manufacturer',
                  'raw_attributes_json', 'raw_applications', 'raw_alternative_parts',
                  'raw_description', 'raw_keywords', 'raw_faq']
    patch_fields = ['mpn', 'supplier_reference', 'attributes_json', 'applications',
                    'alternative_parts', 'description', 'keywords', 'faq']

    raw_rows = []
    patch_rows = []
    mani = {'timestamp': ts, 'targets': len(targets), 'ok': 0, 'fail': 0,
            'per_row': []}

    for r in targets:
        mpn = r.get('mpn', '')
        code = (r.get('supplier_reference') or '').strip()
        rec = {k: '' for k in raw_fields}
        rec['mpn'] = mpn
        rec['supplier_reference'] = code
        try:
            status, html = fetch_page(code)
            rec['http_status'] = status
            if status != 200:
                raise RuntimeError(f'HTTP {status}')
            pvs = extract_property_values(html)
            apps = extract_applications(html)
            alts = extract_alt_mpns(html, code, mpn)
            desc = extract_description(html)
            faqs = extract_faq(html)
            mfr = extract_lcsc_mfr(html)

            rec['n_attrs'] = len(pvs)
            rec['n_apps'] = len(apps)
            rec['n_alts'] = len(alts)
            rec['n_faq'] = len(faqs)
            rec['lcsc_manufacturer'] = mfr
            rec['raw_attributes_json'] = json.dumps(pvs, ensure_ascii=False)
            rec['raw_applications'] = '; '.join(apps)
            rec['raw_alternative_parts'] = '; '.join(alts)
            rec['raw_description'] = desc
            rec['raw_faq'] = faq_to_col(faqs)

            # 派生 keywords (mpn/manufacturer/category/package)
            attr_map = dict(pvs)
            derived = [mpn, r.get('manufacturer', ''), r.get('category', '')]
            if r.get('subcategory'):
                derived.append(r.get('subcategory'))
            if 'Package' in attr_map:
                derived.append(attr_map['Package'])
            rec['raw_keywords'] = '; '.join([d for d in derived if d])

            # ---- 合并到 MASTER-ready 行 ----
            merged_attrs = merge_attributes(r.get('attributes_json', ''), pvs)
            merged_apps = (r.get('applications', '').strip()
                           or merge_semicolon('', apps))
            merged_alts = (r.get('alternative_parts', '').strip()
                           or merge_semicolon('', alts))
            merged_desc = (r.get('description', '').strip()
                           or desc)
            merged_kw = merge_semicolon(r.get('keywords', ''), derived)
            merged_faq = (r.get('faq', '').strip()
                          or faq_to_col(faqs))

            patch_rows.append({
                'mpn': mpn,
                'supplier_reference': code,
                'attributes_json': json.dumps(merged_attrs, ensure_ascii=False),
                'applications': merged_apps,
                'alternative_parts': merged_alts,
                'description': merged_desc,
                'keywords': merged_kw,
                'faq': merged_faq,
            })
            mani['ok'] += 1
            mani['per_row'].append({'mpn': mpn, 'ref': code, 'status': 'ok',
                                    'n_attrs': len(pvs), 'n_apps': len(apps),
                                    'n_alts': len(alts), 'n_faq': len(faqs)})
            print(f"[ok]   {mpn} ({code}) attrs={len(pvs)} apps={len(apps)} "
                  f"alts={len(alts)} faq={len(faqs)} mfr={mfr}")
        except Exception as e:
            mani['fail'] += 1
            mani['per_row'].append({'mpn': mpn, 'ref': code, 'status': 'fail', 'err': str(e)})
            print(f"[FAIL] {mpn} ({code}): {e}")
        time.sleep(args.delay)

    with open(raw_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=raw_fields)
        w.writeheader(); w.writerows(raw_rows)
    with open(patch_path, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=patch_fields)
        w.writeheader(); w.writerows(patch_rows)
    with open(mani_path, 'w', encoding='utf-8') as f:
        json.dump(mani, f, ensure_ascii=False, indent=2)

    print(f"\n[done] ok={mani['ok']} fail={mani['fail']}")
    print(f"  raw   : {raw_path}")
    print(f"  patch : {patch_path}")
    print(f"  manifest: {mani_path}")
    if not args.apply_master:
        print("[note] DRY 模式: 未写 MASTER。需要应用请用 --apply-master --i-accept-write-master YES")


if __name__ == '__main__':
    main()
