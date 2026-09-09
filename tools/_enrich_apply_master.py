#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
_enrich_apply_master.py  —  受控写回 LCSC 增强 patch 到生产 MASTER (测试阶段工具)

安全设计
--------
  * 默认 DRY-RUN：只产出差异报告 + 可选 preview CSV，绝不改 MASTER。
  * 真正写回必须显式: --i-accept-write-master YES --write
  * 写回前先按时间戳备份 MASTER 到 data/production/master_parts_v2.1.csv.bak_<ts>
  * 只覆盖 PATCH_COLS 6 列 (attributes_json/applications/alternative_parts/
    description/keywords/faq)，其它列原样保留。
  * 校验：patch 行的 attributes_json 必须是合法 JSON；alternative_parts 不得含自身
    MPN (防御性)；报告逐行变更 (哪些字段被更新、新长度)。

用法
----
  # 1) 先 DRY 看报告 (不改任何文件)
  python tools/_enrich_apply_master.py --patch data/raw/enrich_lcsc_patch_<ts>.csv

  # 2) 确认无误后真正写回 (会先备份)
  python tools/_enrich_apply_master.py --patch data/raw/enrich_lcsc_patch_<ts>.csv \
      --i-accept-write-master YES --write

  # 3) 额外生成 preview CSV (独立文件名, 满足 validate_production_source 门但不部署)
  python tools/_enrich_apply_master.py --patch ... --preview data/production/master_parts_v2.1.preview.csv
"""
import csv, json, re, sys, os, argparse, shutil, time

PATCH_COLS = ["attributes_json", "applications", "alternative_parts",
              "description", "keywords", "faq"]
MASTER = "data/production/master_parts_v2.1.csv"

# ---------------- CJK / mojibake 归一化 (Plan B #1617, 写回防御) ----------------
# 与 _enrich_lcsc_http.py 同源: 乱码符号卤碌惟掳 -> ±µΩ°, 其余 CJK 剥离。
MOJIBAKE_MAP = {'卤': '±', '碌': 'µ', '惟': 'Ω', '掳': '°', '：': ':'}
_CJK_RE = re.compile(r'[\u3000-\u303F\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\uFF00-\uFFEF]')

def fix_mojibake(text):
    if not text:
        return text
    for bad, good in MOJIBAKE_MAP.items():
        text = text.replace(bad, good)
    return text

def normalize_visible(text):
    if not text:
        return text
    return _CJK_RE.sub('', fix_mojibake(text))

def normalize_data_json(aj):
    """attributes_json: 解析后对字符串值还原符号, 重新 dump; 保留结构。"""
    if not aj:
        return aj
    try:
        obj = json.loads(aj)
    except Exception:
        return aj
    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o]
        if isinstance(o, str):
            return fix_mojibake(o)
        return o
    return json.dumps(walk(obj), ensure_ascii=False)

def norm_mpn(s):
    return re.sub(r'[\s()\-]', '', (s or '').lower())

def load_rows(path):
    with open(path, newline='', encoding='utf-8') as f:
        r = csv.DictReader(f)
        hdr = r.fieldnames
        rows = list(r)
    return hdr, rows

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--master', default=MASTER)
    ap.add_argument('--patch', required=True, help='enrich_lcsc_patch_<ts>.csv')
    ap.add_argument('--i-accept-write-master', default='',
                    help='必须传 YES 才允许 --write')
    ap.add_argument('--write', action='store_true',
                    help='真正写回 MASTER (会先备份); 默认 DRY')
    ap.add_argument('--preview', default='',
                    help='可选: 把合并结果写到该独立文件 (不部署用)')
    ap.add_argument('--report', default='',
                    help='可选: 差异报告 csv 路径 (默认打印到 stdout)')
    args = ap.parse_args()

    if args.write and args.i_accept_write_master != 'YES':
        sys.exit("ERROR: --write 需要 --i-accept-write-master YES 才执行。")
    if not os.path.exists(args.master):
        sys.exit(f"ERROR: MASTER 不存在: {args.master}")
    if not os.path.exists(args.patch):
        sys.exit(f"ERROR: patch 不存在: {args.patch}")

    m_hdr, m_rows = load_rows(args.master)
    missing = [c for c in PATCH_COLS if c not in m_hdr]
    if missing:
        sys.exit(f"ERROR: MASTER 缺少列: {missing}")

    p_hdr, p_rows = load_rows(args.patch)
    p_missing = [c for c in PATCH_COLS if c not in p_hdr]
    if p_missing:
        sys.exit(f"ERROR: patch 缺少列: {p_missing}")

    m_by_mpn = {}
    for i, row in enumerate(m_rows):
        mpn = (row.get('mpn') or '').strip()
        if mpn:
            m_by_mpn.setdefault(mpn, []).append(i)

    change_rows = []
    orphan = 0
    json_bad = 0
    self_alt = 0
    per_col_updates = {c: 0 for c in PATCH_COLS}
    per_col_field_blank = {c: 0 for c in PATCH_COLS}

    for pr in p_rows:
        mpn = (pr.get('mpn') or '').strip()
        if not mpn:
            continue
        idxs = m_by_mpn.get(mpn)
        if not idxs:
            orphan += 1
            continue
        m = m_rows[idxs[0]]  # 取首个匹配

        # 校验 attributes_json 合法
        aj = pr.get('attributes_json', '') or ''
        if aj:
            try:
                json.loads(aj)
            except Exception:
                json_bad += 1
                print(f"[WARN] attributes_json 非法 JSON, 跳过该列: {mpn}")
                aj = None

        # 防御: alternative_parts 不得含自身 MPN
        alts_raw = pr.get('alternative_parts', '') or ''
        if alts_raw:
            alts = [x.strip() for x in alts_raw.split(';') if x.strip()]
            selfn = norm_mpn(mpn)
            filtered = [a for a in alts if norm_mpn(a) != selfn]
            if len(filtered) != len(alts):
                self_alt += 1
            alts_fixed = '; '.join(filtered)
        else:
            alts_fixed = ''

        changed = []
        for col in PATCH_COLS:
            if col == 'attributes_json':
                if aj is None:
                    continue
                # 数据层: 只还原符号, 保留原始中文 (由 allowlist 过滤)
                new_val = normalize_data_json(aj)
            elif col == 'alternative_parts':
                new_val = normalize_visible(alts_fixed)
            else:
                # 可见英文文本: 还原符号 + 剥离残留 CJK
                new_val = normalize_visible(pr.get(col, '') or '')
            if not new_val:
                continue
            old_val = (m.get(col) or '').strip()
            if new_val != old_val:
                m[col] = new_val
                changed.append(col)
                per_col_updates[col] += 1
                if not old_val:
                    per_col_field_blank[col] += 1
        if changed:
            change_rows.append({
                'mpn': mpn,
                'supplier_reference': pr.get('supplier_reference', ''),
                'changed_cols': ';'.join(changed),
                'attrs_keys': (len(json.loads(aj)) if aj else 0),
                'n_alts': (len([x for x in alts_fixed.split(';') if x.strip()]) if alts_fixed else 0),
                'has_faq': int(bool((pr.get('faq') or '').strip())),
                'has_desc': int(bool((pr.get('description') or '').strip())),
            })

    # preview 输出
    if args.preview:
        with open(args.preview, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=m_hdr)
            w.writeheader()
            w.writerows(m_rows)
        print(f"[preview] 已写合并预览 -> {args.preview} ({len(m_rows)} 行)")

    # 报告
    rep_lines = []
    rep_lines.append(f"# LCSC patch 应用报告")
    rep_lines.append(f"MASTER        : {args.master} ({len(m_rows)} 行)")
    rep_lines.append(f"patch         : {args.patch} ({len(p_rows)} 行)")
    rep_lines.append(f"匹配更新      : {len(change_rows)}")
    rep_lines.append(f"孤儿(不在MASTER): {orphan}")
    rep_lines.append(f"attributes_json 非法JSON: {json_bad}")
    rep_lines.append(f"替代料含自身MPN(已过滤): {self_alt}")
    rep_lines.append("--- 每列更新计数 (新值!=旧值) / (旧值为空首次填充) ---")
    for c in PATCH_COLS:
        rep_lines.append(f"  {c:18s}: 更新 {per_col_updates[c]:4d}  (其中首次填充 {per_col_field_blank[c]})")
    rep_lines.append("--- 样本变更 (前 8) ---")
    for r in change_rows[:8]:
        rep_lines.append(f"  {r['mpn']} [{r['changed_cols']}] attrs_keys={r['attrs_keys']} "
                         f"n_alts={r['n_alts']} faq={r['has_faq']} desc={r['has_desc']}")

    report_text = "\n".join(rep_lines)
    if args.report:
        with open(args.report, 'w', encoding='utf-8') as f:
            f.write(report_text + "\n")
        print(f"[report] 已写 -> {args.report}")
    print(report_text)

    if args.write:
        ts = time.strftime('%Y%m%d_%H%M%S')
        bak = args.master + f'.bak_{ts}'
        shutil.copy(args.master, bak)
        with open(args.master, 'w', newline='', encoding='utf-8') as f:
            w = csv.DictWriter(f, fieldnames=m_hdr)
            w.writeheader()
            w.writerows(m_rows)
        print(f"[WRITE] 已写回 MASTER ({len(change_rows)} 行更新), 备份 -> {bak}")
    else:
        print("[DRY] 未写 MASTER。确认无误后加 --i-accept-write-master YES --write")

if __name__ == '__main__':
    main()
