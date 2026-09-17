#!/usr/bin/env python3
"""从 4960 scale500 RAW 批量下载 PDF —— 复用 datasheet.py 的硬化下载器。

与 01 基础数据采集「同一套方式」:
  * 静态 IP SOCKS 代理出口 (lcsc_http_acquire.configure_urllib_proxy)
  * 启动单次出口 IP 自检, 不符即 fail-closed 禁止下载 (绝不直连本地真实 IP)
  * 串行 workers=1 + 随机礼貌延迟 + 429/5xx 断路冷却 + 403 整批硬停
  * 原子落盘 (temp + validate + os.replace); 内容寻址去重

输入: data/raw/lcsc_http_scale500/C*.json 里的 source_raw.main_product.pdfUrl
输出: SZ_POOL_ROOT/datasheets/pdf/<aa>/<sha256>.pdf (内容寻址) + ledger/index

用法 (在 tools/ 目录):
  python -m factory.download_pdfs_from_scale500 [--limit N] [--batch NAME]
"""
import os
import sys
import glob
import json
import argparse
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.dirname(HERE)
for p in (TOOLS, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from . import datasheet as ds  # noqa: E402  (包相对导入, 配合 python -m factory.*)

SCALE500_DIR = r"C:\Users\Administrator.SC-202105071542\Desktop\szprocure-site\data\raw\lcsc_http_scale500"
DEFAULT_BATCH = "scale500_pdfs"

# --- 单实例锁: 防止重复双击 _run_pdf.cmd 起多个进程抢同一 ledger (曾出 7 进程并发) ---
# 用 PID 文件锁 + 进程存活检测: 即使进程被杀留有残留锁文件, 新进程检测到旧 PID 已死即接管, 不会死锁.
import subprocess
import atexit
_LOCK_PATH = os.path.join(HERE, ".pdf_download.lock")


def _pid_alive(pid):
    if not pid:
        return False
    try:
        out = subprocess.check_output(
            ["tasklist", "/fi", f"pid eq {pid}"], text=True, errors="ignore")
        return str(pid) in out
    except Exception:
        return False


def _release_lock():
    try:
        if os.path.exists(_LOCK_PATH):
            os.remove(_LOCK_PATH)
    except Exception:
        pass


def _acquire_single_instance():
    me = os.getpid()
    # 原子创建锁文件 (O_EXCL): 同一瞬间只会有一个进程成功, 其余必进 FileExistsError
    try:
        fd = os.open(_LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            with open(_LOCK_PATH, encoding="utf-8") as f:
                old = int((f.read() or "0").strip() or 0)
        except Exception:
            old = 0
        if old and _pid_alive(old):
            print(f"[pdf] 已有另一个 PDF 下载进程在运行 (PID {old}), "
                  f"本次自动退出 (避免多进程抢 ledger / 触发封禁)", flush=True)
            sys.exit(1)
        # 否则视为残留死锁, 清掉后重试一次
        try:
            os.remove(_LOCK_PATH)
        except Exception:
            pass
        return _acquire_single_instance()
    # 立即无缓冲写 PID 并 fsync 落盘, 关闭竞态窗口 (避免他进程读到空文件误判为残留)
    try:
        os.write(fd, str(me).encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    atexit.register(_release_lock)
    return True


def _pdf_url_of(d):
    mp = (d.get("source_raw") or {}).get("main_product") or {}
    return (mp.get("pdfUrl") or "").strip()


def build_rows(input_dir=SCALE500_DIR):
    rows = []
    for f in sorted(glob.glob(os.path.join(input_dir, "C*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        cnum = d.get("c_number") or (d.get("source_raw") or {}).get("main_product", {}).get("productCode")
        if not cnum:
            continue
        rows.append({"mpn": str(cnum), "_source_datasheet_url": _pdf_url_of(d)})
    return rows


def _on_record(rec):
    st = rec.get("status")
    mpn = rec.get("mpn")
    if st in ds.DONE_STATES:
        print(f"[pdf][OK]   {mpn} -> {st}", flush=True)
    elif st == ds.FAILED:
        print(f"[pdf][FAIL] {mpn} -> {rec.get('error_code')}", flush=True)
    elif st == ds.SKIPPED:
        print(f"[pdf][SKIP] {mpn} (无 pdfUrl)", flush=True)


def run_pdf_download(input_dir=SCALE500_DIR, batch=DEFAULT_BATCH, limit=None):
    """从 input_dir 下 C*.json 抽 pdfUrl, 经 datasheet.py 硬化下载器落盘 PDF。

    复用: 单实例锁 / 静态 IP fail-closed / 串行礼貌延迟 / 内容寻址去重。
    供 CLI (`python -m factory.download_pdfs_from_scale500`) 与 01 采集系统
    (`lcsc_http_acquire.py --mode pdf|both`) 共用, 行为完全一致。
    返回 datasheet.download_batch 的结果对象。
    """
    _acquire_single_instance()  # 单实例锁: 防止与 CLI / 其它 01-pdf 进程抢 ledger
    rows = build_rows(input_dir=input_dir)
    with_url = sum(1 for r in rows if r["_source_datasheet_url"])
    print(f"[pdf] 候选总数 {len(rows)} | 有 pdfUrl {with_url} | "
          f"跳过(无url) {len(rows) - with_url}", flush=True)
    print(f"[pdf] discover(batch={batch}) ...", flush=True)
    ds.discover(batch, rows=rows)  # 写 ledger 到 SZ_POOL_ROOT
    print(f"[pdf] 开始 download_batch (workers={ds.DEFAULT_WORKERS}, "
          f"礼貌延迟 {ds.SUCCESS_DELAY_MIN}-{ds.SUCCESS_DELAY_MAX}s) ...", flush=True)
    t0 = time.time()
    res = ds.download_batch(batch, limit=limit, on_record=_on_record)
    res.duration = time.time() - t0
    print(f"[pdf] 完成: {json.dumps(res.as_dict(), ensure_ascii=False)}", flush=True)
    return res


def main():
    ap = argparse.ArgumentParser(description="scale500 RAW -> PDF 批量下载")
    ap.add_argument("--limit", type=int, default=None,
                    help="仅下载前 N 个候选 (冒烟测试, ledger 仍记全量)")
    ap.add_argument("--batch", default=DEFAULT_BATCH)
    ap.add_argument("--input-dir", default=SCALE500_DIR,
                    help="RAW JSON 目录 (默认 data/raw/lcsc_http_scale500)。"
                         "本批 01 采集输出在 D 盘时用此参数指向该目录。")
    a = ap.parse_args()
    run_pdf_download(input_dir=a.input_dir, batch=a.batch, limit=a.limit)


if __name__ == "__main__":
    main()
