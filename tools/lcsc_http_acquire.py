#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01 采集 · LCSC English HTTP 主采集器 V1.0.1 (礼貌化 / 反封禁 优化)
===============================================================
唯一目标: 从 LCSC 英文 Product Detail Page 采集产品原始信息,
完整、可靠、可追溯地落入 RAW。本阶段不考虑 MASTER/SKU/03/04。

数据流:  LCSC English HTTP -> 01 采集 -> RAW
禁止:    01 -> CLEAN / MASTER / HTML / SKU / PUBLISHED

落盘 (默认 <repo>/data/raw/lcsc_http/):
  <Cxxxx>.json                      结构化 RAW (source_raw / internal_raw / real_time_snapshot)
  _next_data/<Cxxxx>.json           原始 __NEXT_DATA__ payload 备份 (可追溯, 默认开启)
  _catalog_snapshot_<DATE>.json     全局类目树快照 (跨产品去重, 避免每页重复 57KB)
  _runlog_<ts>.jsonl                运行日志 (c_number/url/status/http_status/captured_at/error_type/error_message)
  _manifest_<ts>.json               运行汇总 (total/ok/error/skip + 错误分布 + 耗时)

边界 (严格遵守):
  - 只写 data/raw/ ; 不读/不改 MASTER / CLEAN / attributes_dictionary /
    gen_parts.py / SKU HTML / components / 03 / 04 / sitemap / 网站页面。
  - 不重构旧采集器 harvest_api.py / scrape_lcsc.py / harvest_lcsc_400.py (保留作 fallback)。

反封禁 (2026-09-11 优化, 冻结层 bugfix/retry/并发 桶, 不升 V2):
  - 默认走 Playwright/Edge 真实上下文取数 (真实 TLS/JA3 + 完整头 + cookie 链);
    --no-browser 回退裸 urllib (指纹弱, 仅兜底)。
  - 全局断路器: 连续 5 个 5xx/429 -> 整批冷却 15 分钟 (--cooldown-* 可调)。
  - 错误分类: 429/5xx 退避+冷却; 403 硬封禁 -> 停+告警 (不升级对抗)。
  - 默认 concurrency=1, delay=3s; code 列表固定种子洗牌; 每 N 个插 5-15s 长暂停。
  - 严守 robots.txt: 只打 /en/product-detail/, 不碰 /product-detail-v2/。

用法:
  python lcsc_http_acquire.py C578299
  python lcsc_http_acquire.py C578299 C2596 C4340
  python lcsc_http_acquire.py --codes-file codes.txt
  python lcsc_http_acquire.py --codes-file codes.txt --concurrency 1 --delay 3 --timeout 25 --max-retries 3
  python lcsc_http_acquire.py --codes-file codes.txt --limit 20      # 试采前 20 条
  python lcsc_http_acquire.py --force                               # 忽略 checkpoint 重新采
  python lcsc_http_acquire.py --no-raw-backup                       # 不写 _next_data 备份
  python lcsc_http_acquire.py --out /tmp/preview                    # 指定输出目录 (隔离预览)
  python lcsc_http_acquire.py --no-browser                          # 回退 urllib (指纹弱)
  python lcsc_http_acquire.py --cooldown-threshold 5 --cooldown-sec 900
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import os
import re
import sys
import threading
import time
import urllib.request as urllib_request
import urllib.error as urllib_error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

# ----------------------------------------------------------------------------
# 路径解析: 默认输出目录锚定到仓库 data/raw/lcsc_http (脚本在 <repo>/tools/ 下)
# ----------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
DEFAULT_OUT = os.path.join(REPO_ROOT, "data", "raw", "lcsc_http")

PARSER_NAME = "lcsc_http_acquire"
PARSER_VERSION = "1.0.1"
SUPPLIER = "LCSC"
LOCALE = "en"

# ----------------------------------------------------------------------------
# 网络层默认 UA / 头 (随后由 collector_common 覆盖为 UA 池首项 + 自洽头)
# ----------------------------------------------------------------------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
HDR = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}

# 01 礼貌化共享组件: UA 池 / 自洽头 / 洗牌 / stealth 启动 / 全局断路器
try:
    sys.path.insert(0, SCRIPT_DIR)
    import collector_common as cc
    DEFAULT_UA = cc.UA_POOL[0]
    HDR = cc.build_headers(DEFAULT_UA)
    UA = DEFAULT_UA
except Exception:  # noqa: BLE001
    cc = None
    DEFAULT_UA = UA

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)

# 浏览器模式下, 非 2xx (除 404) 抛此异常走重试/分类逻辑
class HttpStatusError(Exception):
    def __init__(self, code, message=""):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------------------
# 字段分类 (供 RAW 隔离/标记用, 不在 01 阶段做 CJK/语义清洗)
# ----------------------------------------------------------------------------
# 实时业务字段: price / stock /  availability / flash-sale —— 采, 但标记 volatile
REALTIME_KEYS = {
    "stockNumber", "stockSz", "domesticStockVO", "overseasStockVO", "wmStockHk",
    "productPriceList", "productLadderPrice", "reelPrice", "ladderDiscountRate",
    "isRealPrice", "isShowForeignPrice", "isForeignOnsale", "isForeignDisplay",
    "hasThirdPartyStock", "flashSaleProductPO",
}

# 内部/商业字段: 必须隔离到 internal_raw, 与公开数据 (source_raw) 分离
INTERNAL_KEYS = {
    "productCostPricePO", "activityPO", "szlcscActivityPO", "warehouseCode",
    "productBatchCode", "costPrice", "purchasePrice", "productCostPrice",
    "supplierCost", "productPricePO", "profitRate", "grossProfit", "costPricePO",
}


# ----------------------------------------------------------------------------
# 错误类型常量
# ----------------------------------------------------------------------------
class Err:
    HTTP_ERROR = "http_error"                 # 非 200 且非 404 类
    TIMEOUT = "timeout"
    NOT_FOUND_404 = "not_found_404"           # dataIsNull=True 或 http=404 (死链/泛化页)
    NEXT_DATA_MISSING = "next_data_missing"   # 页面无 __NEXT_DATA__ 脚本
    JSON_PARSE = "json_parse"                 # payload 非合法 JSON
    MAIN_PRODUCT_NOT_FOUND = "main_product_not_found"  # pageProps.webData 缺失/非 dict
    C_NUMBER_MISMATCH = "c_number_mismatch"   # webData.productCode != 目标 C-number
    DATA_IS_NULL = "data_is_null"             # dataIsNull=True (泛化/无产品页)
    UNEXPECTED = "unexpected"
    RATE_LIMITED = "rate_limited"             # 429
    SERVER_ERROR = "server_error"             # 5xx
    HARD_BLOCKED = "hard_blocked"             # 403 硬封禁


def _classify_http(code4):
    """返回 (err_type, is_blocking, is_hard_stop)。"""
    if code4 == 429:
        return Err.RATE_LIMITED, True, False
    if code4 == 403:
        return Err.HARD_BLOCKED, False, True
    if code4 == 404:
        return Err.NOT_FOUND_404, False, False
    if code4 is not None and 500 <= code4 < 600:
        return Err.SERVER_ERROR, True, False
    return Err.HTTP_ERROR, False, False


# ----------------------------------------------------------------------------
# 网络抓取
# ----------------------------------------------------------------------------
def build_url(code: str) -> str:
    return f"https://www.lcsc.com/en/product-detail/{code}.html"


def fetch_page(code: str, timeout: float):
    """urllib 兜底路径: 返回 (http_status, html_str)。非 2xx 抛 HTTPError。"""
    url = build_url(code)
    req = urllib_request.Request(url, headers=HDR)
    with urllib_request.urlopen(req, timeout=timeout) as r:
        status = r.getcode()
        raw = r.read()
    return status, raw.decode("utf-8", "replace")


def fetch_page_browser(ctx, code: str, timeout: float):
    """Playwright/Edge 真实上下文取数: 返回 (http_status, html_str)。
    200 / 404 以正常响应返回; 429/403/5xx 抛 HttpStatusError 走重试/分类。
    关键: 浏览器对 4xx/5xx 会令 page.goto 直接抛导航错误 (net::ERR_...),
    不会返回 resp.status, 因此用 response 监听拿到真实状态码再判定。
    仅导航超时 / 网络错误且无 response 时抛普通异常。"""
    url = build_url(code)
    page = ctx.new_page()
    captured = {"status": None}
    def _on_response(response):
        captured["status"] = response.status
    page.on("response", _on_response)
    html_str = ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        html_str = page.content()
    except Exception:  # noqa: BLE001
        # 非 2xx 导致导航错误: 用监听到的状态码判定; 404 仍尝试取页面
        try:
            if captured["status"] == 404:
                html_str = page.content()
        except Exception:  # noqa: BLE001
            pass
    finally:
        page.close()
    status = captured["status"]
    if status is None:
        raise HttpStatusError(0, "no HTTP response captured (timeout/network)")
    if status == 200 or status == 404:
        return status, html_str
    # 429/403/5xx -> 抛异常, 由 acquire_one 重试 + 错误分类 + 全局断路器
    raise HttpStatusError(status, f"HTTP {status}")


# ----------------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------------
def extract_next_data(html_str: str):
    """从 HTML 提取 __NEXT_DATA__ JSON 对象。失败返回 None。"""
    m = NEXT_DATA_RE.search(html_str)
    if not m:
        return None
    blob = m.group(1)
    blob = html.unescape(blob)  # 还原 &quot; &amp; &lt; &gt; 等
    return blob


def get_main_product(nd: dict, code: str):
    """
    可靠主产品识别:
      1) 直接取 props.pageProps.webData (规范主产品节点)
      2) 校验 webData.productCode == 目标 C-number
      3) 校验 productModel/brandNameEn/wmCatalogNameEn 一致性 (仅告警, 不致命)
    严禁「全局首个 productModel/brandNameEn 匹配」。
    返回 (webData_dict, data_is_null, consistency_notes) 或抛 KeyError/ValueError。
    """
    pp = nd.get("props", {}).get("pageProps", {})
    if not isinstance(pp, dict):
        raise KeyError("pageProps missing")
    data_is_null = bool(pp.get("dataIsNull", False))
    web_data = pp.get("webData")
    if not isinstance(web_data, dict):
        raise KeyError("webData missing/invalid")
    notes = []
    pc = web_data.get("productCode")
    if pc != code:
        raise ValueError(f"productCode mismatch: webData={pc!r} expected={code!r}")
    for k in ("productModel", "brandNameEn", "wmCatalogNameEn"):
        if not web_data.get(k):
            notes.append(f"missing:{k}")
    return web_data, data_is_null, notes


def isolate_internal(node):
    """
    递归扫描 node, 抽出所有 INTERNAL_KEYS 到 isolated 列表 (保留路径与值),
    并从 node 中删除这些键。返回 isolated 列表。对 node 做原地修改 (已 deepcopy)。
    """
    isolated = []

    def walk(o, path):
        if isinstance(o, dict):
            for k in list(o.keys()):
                child_path = f"{path}.{k}" if path else k
                if k in INTERNAL_KEYS:
                    isolated.append({"path": child_path, "key": k, "value": o[k]})
                    del o[k]
                else:
                    walk(o[k], child_path)
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")

    walk(node, "")
    return isolated


# ----------------------------------------------------------------------------
# 落盘
# ----------------------------------------------------------------------------
def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def atomic_write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def write_runlog(path: str, record: dict, lock: threading.Lock) -> None:
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


# 全局 catalog 快照状态 (线程安全)
_catalog_written = False
_catalog_lock = threading.Lock()


def maybe_write_catalog_snapshot(out_dir: str, date_tag: str, catalog_list) -> str:
    """每个采集会话写一次全局类目树快照, 返回引用文件名。跨产品去重。"""
    global _catalog_written
    ref = f"_catalog_snapshot_{date_tag}.json"
    full = os.path.join(out_dir, ref)
    if catalog_list is None:
        return ref
    with _catalog_lock:
        if not _catalog_written and not os.path.exists(full):
            atomic_write_json(full, {
                "captured_at": utc_now(),
                "source": "lcsc",
                "locale": LOCALE,
                "note": "global category tree snapshot; referenced by per-product RAW via catalog_snapshot_ref",
                "catalogList": catalog_list,
            })
            _catalog_written = True
    return ref


# ----------------------------------------------------------------------------
# 单产品采集 (worker)
# ----------------------------------------------------------------------------
def acquire_one(code: str, out_dir: str, timeout: float, max_retries: int,
                delay: float, backoff_base: float, keep_raw_backup: bool,
                force: bool, runlog_path: str, runlog_lock: threading.Lock,
                date_tag: str, fetch_fn=None, cooldown=None, abort_event=None):
    code = code.strip().upper()
    if not code:
        return
    product_path = os.path.join(out_dir, f"{code}.json")
    url = build_url(code)
    captured_at = utc_now()

    # 硬封禁信号: 上游已置位 -> 直接放弃本产品
    if abort_event is not None and abort_event.is_set():
        return

    # checkpoint / resume
    if not force and os.path.exists(product_path):
        rec = {
            "c_number": code, "url": url, "status": "skip",
            "http_status": None, "captured_at": captured_at,
            "error_type": "checkpoint_exists", "error_message": "already captured",
        }
        write_runlog(runlog_path, rec, runlog_lock)
        return

    # retry / exponential backoff + 错误分类 + 全局断路器
    last_err = None
    http_status = None
    html_str = None
    for attempt in range(1, max_retries + 1):
        try:
            http_status, html_str = (fetch_fn or fetch_page)(code, timeout)
            last_err = None
            if cooldown is not None:
                cooldown.record_success()
            break
        except (urllib_error.HTTPError, HttpStatusError) as e:
            code4 = getattr(e, "code", None)
            et, blocking, hard = _classify_http(code4)
            last_err = e
            if blocking and cooldown is not None:
                if cooldown.record_blocking(code4):
                    print(f"[circuit] 连续 {cooldown.threshold} 个限流/5xx -> "
                          f"整批冷却 {cooldown.cooldown_sec}s")
            if hard:
                rec = {
                    "c_number": code, "url": url, "status": "error",
                    "http_status": code4, "captured_at": captured_at,
                    "error_type": Err.HARD_BLOCKED,
                    "error_message": "HTTP 403 硬封禁 -> 停止整批",
                }
                write_runlog(runlog_path, rec, runlog_lock)
                if abort_event is not None:
                    abort_event.set()
                return
            if attempt < max_retries:
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            rec = {
                "c_number": code, "url": url, "status": "error",
                "http_status": code4, "captured_at": captured_at,
                "error_type": et, "error_message": f"HTTPError: {code4}",
            }
            write_runlog(runlog_path, rec, runlog_lock)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            etype = type(e).__name__
            err_type = Err.TIMEOUT if ("timeout" in etype.lower()
                                      or "Timeout" in str(e)) else Err.HTTP_ERROR
            # 网络/超时疑为网关熔断 -> 计入断路器
            if cooldown is not None:
                if cooldown.record_blocking():
                    print(f"[circuit] 连续 {cooldown.threshold} 个网关/网络错误 -> "
                          f"整批冷却 {cooldown.cooldown_sec}s")
            if attempt < max_retries:
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            rec = {
                "c_number": code, "url": url, "status": "error",
                "http_status": getattr(e, "code", None), "captured_at": captured_at,
                "error_type": err_type, "error_message": f"{etype}: {e}",
            }
            write_runlog(runlog_path, rec, runlog_lock)
            return

    # 非 200 (含 404 / 浏览器返回的 5xx) 处理
    if last_err is None and http_status != 200:
        et, blocking, hard = _classify_http(http_status)
        if blocking and cooldown is not None:
            if cooldown.record_blocking(http_status):
                print(f"[circuit] 连续 {cooldown.threshold} 个限流/5xx -> "
                      f"整批冷却 {cooldown.cooldown_sec}s")
        if hard:
            rec = {
                "c_number": code, "url": url, "status": "error",
                "http_status": http_status, "captured_at": captured_at,
                "error_type": Err.HARD_BLOCKED,
                "error_message": "HTTP 403 硬封禁 -> 停止整批",
            }
            write_runlog(runlog_path, rec, runlog_lock)
            if abort_event is not None:
                abort_event.set()
            return
        rec = {
            "c_number": code, "url": url, "status": "error",
            "http_status": http_status, "captured_at": captured_at,
            "error_type": et, "error_message": f"http_status={http_status}",
        }
        write_runlog(runlog_path, rec, runlog_lock)
        return

    # 解析 __NEXT_DATA__
    try:
        blob = extract_next_data(html_str)
        if blob is None:
            raise ValueError("__NEXT_DATA__ script not found")
        nd = json.loads(blob)
    except Exception as e:  # noqa: BLE001
        rec = {
            "c_number": code, "url": url, "status": "error",
            "http_status": http_status, "captured_at": captured_at,
            "error_type": Err.JSON_PARSE if "JSON" in type(e).__name__
            else Err.NEXT_DATA_MISSING, "error_message": f"{type(e).__name__}: {e}",
        }
        write_runlog(runlog_path, rec, runlog_lock)
        return

    # 主产品识别 + 一致性校验
    try:
        web_data, data_is_null, notes = get_main_product(nd, code)
    except ValueError as e:
        rec = {
            "c_number": code, "url": url, "status": "error",
            "http_status": http_status, "captured_at": captured_at,
            "error_type": Err.C_NUMBER_MISMATCH, "error_message": str(e),
        }
        write_runlog(runlog_path, rec, runlog_lock)
        return
    except KeyError as e:
        rec = {
            "c_number": code, "url": url, "status": "error",
            "http_status": http_status, "captured_at": captured_at,
            "error_type": Err.MAIN_PRODUCT_NOT_FOUND, "error_message": str(e),
        }
        write_runlog(runlog_path, rec, runlog_lock)
        return
    except Exception as e:  # noqa: BLE001
        rec = {
            "c_number": code, "url": url, "status": "error",
            "http_status": http_status, "captured_at": captured_at,
            "error_type": Err.UNEXPECTED, "error_message": f"{type(e).__name__}: {e}",
        }
        write_runlog(runlog_path, rec, runlog_lock)
        return

    if data_is_null:
        rec = {
            "c_number": code, "url": url, "status": "error",
            "http_status": http_status, "captured_at": captured_at,
            "error_type": Err.DATA_IS_NULL,
            "error_message": "dataIsNull=True (generic/non-product page)",
        }
        write_runlog(runlog_path, rec, runlog_lock)
        return

    # 组装 RAW
    pp = nd.get("props", {}).get("pageProps", {})
    overview_data = pp.get("overviewData")
    catalog_list = pp.get("catalogList")
    alt_list = copy.deepcopy(web_data.get("alternatePartList"))

    # 公开主产品 (deepcopy 后剥离内部字段)
    public_main = copy.deepcopy(web_data)
    isolated = isolate_internal(public_main)
    if isinstance(alt_list, list):
        for item in alt_list:
            isolate_internal(item)

    # 实时快照 (从已隔离的 public_main 取, 避免 flashSaleProductPO 内部字段泄漏进 source_raw)
    realtime_fields = {k: public_main.get(k) for k in REALTIME_KEYS if k in public_main}

    catalog_ref = maybe_write_catalog_snapshot(out_dir, date_tag, catalog_list)

    raw = {
        "source": "lcsc",
        "source_url": url,
        "supplier": SUPPLIER,
        "locale": LOCALE,
        "c_number": code,
        "captured_at": captured_at,
        "parser": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "http_status": http_status,
        "data_is_null": data_is_null,
        "catalog_snapshot_ref": catalog_ref,
        "source_raw": {
            "main_product": public_main,
            "overviewData": overview_data,
            "alternatePartList": alt_list,
            "real_time_snapshot": {
                "captured_at": captured_at,
                "note": "volatile business data (price/stock/availability); "
                        "not permanently valid; re-acquire for freshness",
                "fields": realtime_fields,
            },
        },
        "internal_raw": {
            "note": "internal/commercial fields isolated from public data (source_raw); "
                    "do NOT expose publicly",
            "isolated_keys": isolated,
        },
        "consistency": {
            "productCode": web_data.get("productCode"),
            "productModel": web_data.get("productModel"),
            "brandNameEn": web_data.get("brandNameEn"),
            "wmCatalogNameEn": web_data.get("wmCatalogNameEn"),
            "warnings": notes,
        },
    }

    # 原始 payload 备份 (可追溯): 默认开启, 独立文件避免污染结构化 RAW
    if keep_raw_backup:
        backup_path = os.path.join(out_dir, "_next_data", f"{code}.json")
        atomic_write_json(backup_path, {"raw_blob": blob, "parsed": nd})

    # 结构化 RAW 落盘 (原子写)
    atomic_write_json(product_path, raw)

    rec = {
        "c_number": code, "url": url, "status": "ok",
        "http_status": http_status, "captured_at": captured_at,
        "error_type": "", "error_message": "",
        "source_raw_keys": len(public_main),
        "internal_isolated": len(isolated),
        "realtime_fields": len(realtime_fields),
    }
    write_runlog(runlog_path, rec, runlog_lock)
    # 礼貌性 delay (非重试退避)
    if delay > 0:
        time.sleep(delay)


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def read_codes_file(path: str):
    codes = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 支持直接写 C-number 或整 URL
            m = re.search(r"C\d+", line, re.I)
            if m:
                codes.append(m.group(0).upper())
    return codes


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="01 采集 · LCSC English HTTP 主采集器 V1.0.1 "
                    "(LCSC English HTTP -> RAW, 礼貌化/反封禁)")
    ap.add_argument("codes", nargs="*", help="C-numbers, 例如 C578299")
    ap.add_argument("--codes-file", help="每行一个 C-number 的文件")
    ap.add_argument("--out", default=DEFAULT_OUT, help="RAW 输出目录 (默认 <repo>/data/raw/lcsc_http)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="urllib 模式并发线程数 (默认 1; 浏览器模式恒串行)")
    ap.add_argument("--delay", type=float, default=3.0,
                    help="每产品后礼貌性 delay 秒 (2-4s 推荐, 默认 3)")
    ap.add_argument("--timeout", type=float, default=25, help="单请求超时秒")
    ap.add_argument("--max-retries", type=int, default=3, help="失败最大重试次数")
    ap.add_argument("--backoff-base", type=float, default=1.0, help="指数退避基秒")
    ap.add_argument("--limit", type=int, default=0, help="仅采前 N 条 (试采)")
    ap.add_argument("--force", action="store_true", help="忽略 checkpoint 重新采")
    ap.add_argument("--no-raw-backup", action="store_true", help="不写 _next_data 原始备份")
    # 反封禁相关
    ap.add_argument("--browser", dest="browser", action="store_true", default=True,
                    help="用 Playwright/Edge 真实上下文取数 (默认)")
    ap.add_argument("--no-browser", dest="browser", action="store_false",
                    help="回退裸 urllib (指纹弱, 仅兜底)")
    ap.add_argument("--cooldown-sec", type=int, default=900,
                    help="全局断路器冷却秒 (默认 900=15min)")
    ap.add_argument("--cooldown-threshold", type=int, default=5,
                    help="连续限流/5xx 触发冷却的阈值 (默认 5)")
    ap.add_argument("--no-shuffle", action="store_true",
                    help="不打乱 code 顺序 (默认固定种子洗牌)")
    ap.add_argument("--shuffle-seed", type=int, default=20260911,
                    help="洗牌固定种子 (可复现/断点续跑)")
    ap.add_argument("--long-pause-every", type=int, default=20,
                    help="每 N 个产品插入 5-15s 长暂停 (0=关闭, 默认 20)")
    ap.add_argument("--no-warmup", action="store_true",
                    help="浏览器模式跳过 首页/分类 预热漏斗")
    args = ap.parse_args(argv)

    codes = list(args.codes)
    if args.codes_file:
        codes += read_codes_file(args.codes_file)
    # 去重保序
    seen = set()
    uniq = []
    for c in codes:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    codes = uniq

    if args.limit and args.limit > 0:
        codes = codes[:args.limit]

    if not codes:
        ap.error("未提供任何 C-number (positional 或 --codes-file)")

    # P2.I — 固定种子洗牌 (避免严格递增扫描的 bot pattern)
    if not args.no_shuffle and cc is not None:
        codes = cc.shuffled_codes(codes, seed=args.shuffle_seed)

    # P0.A — 全局断路器
    cooldown = (cc.CooldownState(threshold=args.cooldown_threshold,
                                 cooldown_sec=args.cooldown_sec) if cc else None)
    abort_event = threading.Event()

    out_dir = args.out
    os.makedirs(out_dir, exist_ok=True)
    date_tag = datetime.now(timezone.utc).strftime("%Y%m%d")
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    runlog_path = os.path.join(out_dir, f"_runlog_{ts}.jsonl")
    runlog_lock = threading.Lock()

    mode = "browser(Playwright/Edge)" if (args.browser and cc is not None) else "urllib(fallback)"
    print(f"[01-acquire] 模式: {mode} | UA 池: {'on' if cc else 'off'}")
    print(f"[01-acquire] 输出目录: {out_dir}")
    print(f"[01-acquire] 待采 {len(codes)} 个 C-number | concurrency={args.concurrency} "
          f"delay={args.delay}s timeout={args.timeout}s retries={args.max_retries}")
    print(f"[01-acquire] 断路器: threshold={args.cooldown_threshold} cooldown={args.cooldown_sec}s "
          f"| shuffle={'off' if args.no_shuffle else 'on(seed='+str(args.shuffle_seed)+')'} "
          f"| long_pause_every={args.long_pause_every}")
    print(f"[01-acquire] runlog: {runlog_path}")

    start = time.time()
    results = {"ok": 0, "error": 0, "skip": 0}
    err_dist = {}

    def _run_serial(fetch_fn):
        for idx, code in enumerate(codes):
            if abort_event.is_set():
                break
            if cooldown is not None:
                cooldown.wait_if_cooling(print)
            acquire_one(code, out_dir, args.timeout, args.max_retries, args.delay,
                        args.backoff_base, not args.no_raw_backup, args.force,
                        runlog_path, runlog_lock, date_tag,
                        fetch_fn=fetch_fn, cooldown=cooldown, abort_event=abort_event)
            if args.long_pause_every and (idx + 1) % args.long_pause_every == 0:
                time.sleep(random.uniform(5, 15))

    if args.browser and cc is not None:
        handle = None
        try:
            handle = cc.launch_stealth(EDGE, ua=DEFAULT_UA)
            _, _b, ctx = handle
            # P2.G — 访问漏斗 / 会话预热: 先逛首页 + 英文站, 让 LCSC 自然落下 cookie
            if not args.no_warmup:
                warm = ctx.new_page()
                try:
                    warm.goto("https://www.lcsc.com/", wait_until="domcontentloaded",
                              timeout=60000)
                    warm.wait_for_timeout(2500)
                    warm.goto("https://www.lcsc.com/en/", wait_until="domcontentloaded",
                              timeout=60000)
                    warm.wait_for_timeout(1500)
                except Exception as e:  # noqa: BLE001
                    print(f"[warn] 预热漏斗异常(忽略): {e}")
                finally:
                    warm.close()
            _run_serial(lambda c, t: fetch_page_browser(ctx, c, t))
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 浏览器启动失败, 回退 urllib: {e}")
            _run_serial(fetch_page)
        finally:
            if handle is not None:
                cc.close_stealth(handle)
    else:
        # urllib 模式: 串行或线程池
        if args.concurrency <= 1:
            _run_serial(fetch_page)
        else:
            with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
                futures = [
                    ex.submit(acquire_one, code, out_dir, args.timeout, args.max_retries,
                              args.delay, args.backoff_base, not args.no_raw_backup,
                              args.force, runlog_path, runlog_lock, date_tag,
                              fetch_page, cooldown, abort_event)
                    for code in codes
                ]
                for _ in as_completed(futures):
                    pass  # 结果已写入 runlog

    if abort_event.is_set():
        print("[alert] 采集因 HTTP 403 硬封禁提前停止 (未升级对抗)。请人工核查 LCSC 状态后再续跑。")

    # 汇总 (从 runlog 聚合, 单一事实来源)
    if os.path.exists(runlog_path):
        with open(runlog_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                st = rec.get("status", "error")
                results[st] = results.get(st, 0) + 1
                if st == "error":
                    et = rec.get("error_type", "unexpected")
                    err_dist[et] = err_dist.get(et, 0) + 1

    elapsed = time.time() - start
    manifest = {
        "parser": PARSER_NAME,
        "parser_version": PARSER_VERSION,
        "captured_at": utc_now(),
        "out_dir": out_dir,
        "mode": mode,
        "total": len(codes),
        "ok": results.get("ok", 0),
        "error": results.get("error", 0),
        "skip": results.get("skip", 0),
        "error_distribution": err_dist,
        "concurrency": args.concurrency,
        "cooldown_sec": args.cooldown_sec,
        "cooldown_threshold": args.cooldown_threshold,
        "elapsed_sec": round(elapsed, 2),
        "throughput_per_min": round(len(codes) / (elapsed / 60), 2) if elapsed > 0 else 0,
        "runlog": os.path.basename(runlog_path),
    }
    manifest_path = os.path.join(out_dir, f"_manifest_{ts}.json")
    atomic_write_json(manifest_path, manifest)

    print(f"[01-acquire] 完成: ok={manifest['ok']} error={manifest['error']} "
          f"skip={manifest['skip']} | 错误分布={err_dist}")
    print(f"[01-acquire] 耗时 {elapsed:.1f}s | manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
