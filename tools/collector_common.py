#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
01 采集器 · 共享 礼貌化 / 反封禁 组件
=====================================
被 lcsc_http_acquire.py 与 harvest_api.py 共用。承载:
  - UA_POOL        : 少量真实、当下版本浏览器 UA (与 viewport/locale 自洽)
  - build_headers  : 构造自洽请求头 (Accept-Language / Sec-CH-UA / Referer ...)
  - shuffled_codes : 固定种子可复现洗牌 (避免 C000001->C000002 严格递增的 bot pattern)
  - launch_stealth : Playwright/Edge 真实上下文启动 (隐藏自动化痕迹)
  - close_stealth  : 关闭 stealth 上下文
  - CooldownState  : 全局断路器 (连续 K 个 5xx/429 -> 整批冷却 T 分钟)

冻结层约束: 本模块属 01 采集器「bugfix/retry/并发」桶, 不升 V2。
设计文档: .workbuddy/memory/SZProcure-01-Collector-Politeness-Optimization.md
"""
from __future__ import annotations

import os
import random
import threading
import time

try:
    from playwright.sync_api import sync_playwright
    _HAVE_PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    _HAVE_PLAYWRIGHT = False

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# ---------------------------------------------------------------------------
# P1.E — UA 池 (少量真实、当下版本; 全部桌面 Windows, 与 viewport 1366x900 /
#        locale en-US 自洽; 不要混 Mobile UA + Desktop viewport)
# ---------------------------------------------------------------------------
# 20~50 个真实、当下版本浏览器 UA (Chrome/Edge/Safari/Firefox, 桌面 Win+macOS 混合),
# 与 viewport/locale 自洽; 每次请求随机抽一个, 打破恒定 UA 的 bot 特征。
UA_POOL = [
    # Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Edge (Windows, Chromium)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # Safari (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
    # Firefox (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Firefox (macOS)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:129.0) Gecko/20100101 Firefox/129.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
]

# Sec-CH-UA 客户端提示: 仅 Chromium 系需要, 按 UA 中 Chrome 主版本动态生成 (须自洽)
def _sec_ch_ua(chrome_ver: int) -> str:
    return (f'"Chromium";v="{chrome_ver}", '
            f'"Google Chrome";v="{chrome_ver}", '
            f'"Not-A.Brand";v="99"')


# 伪装「从搜索引擎点击进来」的 Referer 来路池 (避免自引用暴露 bot)
REFERER_POOL = [
    "https://www.google.com/",
    "https://www.google.com/search?q=lcsc+electronic+components",
    "https://www.bing.com/search?q=lcsc+components",
    "https://www.google.com.hk/",
]


def random_ua() -> str:
    """从 UA 池随机抽一个, 用于每次请求轮换指纹。"""
    return random.choice(UA_POOL)


def _chrome_major(ua: str) -> int:
    import re
    m = re.search(r"Chrome/(\d+)\.", ua or "")
    return int(m.group(1)) if m else 124


def build_headers(ua: str, referer: str = None) -> dict:
    """构造自洽请求头。仅 Chromium 系补 Sec-CH-UA; Referer 默认随机伪装搜索来路。"""
    chrome = _chrome_major(ua) if "Chrome/" in ua else None
    headers = {
        "User-Agent": ua,
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Referer": referer or random.choice(REFERER_POOL),
        "Upgrade-Insecure-Requests": "1",
    }
    if chrome is not None:
        headers["Sec-CH-UA"] = _sec_ch_ua(chrome)
        headers["Sec-CH-UA-Mobile"] = "?0"
        headers["Sec-CH-UA-Platform"] = '"macOS"' if "Mac" in ua else '"Windows"'
    return headers


# ---------------------------------------------------------------------------
# P2.I — 固定种子洗牌 (可复现; 断点续跑时顺序一致, 支持多机同序)
# ---------------------------------------------------------------------------
def shuffled_codes(codes, seed: int = 20260911):
    out = list(codes)
    rnd = random.Random(seed)
    rnd.shuffle(out)
    return out


# ---------------------------------------------------------------------------
# P1.F — Playwright / Edge stealth 启动 (隐藏自动化痕迹)
# ---------------------------------------------------------------------------
def launch_stealth(executable_path: str = EDGE, headless: bool = True,
                   locale: str = "en-US", viewport: dict = None,
                   ua: str = None, proxy: dict = None):
    """
    启动一个隐藏自动化痕迹的 Edge 上下文。
    返回 (pw, browser, context); 调用方用 close_stealth(handle) 关闭。
    navigator.webdriver 被置为 undefined; --disable-blink-features 关闭
    AutomationControlled 标志, 降低 headless 典型泄漏。
    """
    if not _HAVE_PLAYWRIGHT:
        raise RuntimeError("playwright 不可用; 请改用 urllib 兜底 (--no-browser)")
    if viewport is None:
        viewport = {"width": 1366, "height": 900}
    if ua is None:
        ua = UA_POOL[0]
    pw = sync_playwright().start()
    launch_kwargs = dict(
        executable_path=executable_path,
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
        ],
    )
    if proxy:
        launch_kwargs["proxy"] = proxy
    b = pw.chromium.launch(**launch_kwargs)
    ctx = b.new_context(user_agent=ua, locale=locale, viewport=viewport)
    ctx.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return pw, b, ctx


def close_stealth(handle):
    """安全关闭 launch_stealth 返回的 (pw, browser, context)。"""
    if not handle:
        return
    pw, b, ctx = handle
    for closer, name in ((ctx.close, "ctx"), (b.close, "browser"), (pw.stop, "pw")):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# P1.G — SOCKS5 / 静态 IP 出口 (匿名: 不暴露真实本机 IP)
# ---------------------------------------------------------------------------
def load_proxy() -> str | None:
    """读取代理 URL: 优先环境变量 LCSC_PROXY, 否则 tools/.lcsc_proxy (gitignored)。"""
    env = os.environ.get("LCSC_PROXY")
    if env:
        return env.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, ".lcsc_proxy")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:  # noqa: BLE001
            return None
    return None


def parse_proxy(proxy_url: str | None) -> dict | None:
    """转为 Playwright launch proxy 字典; 无则返回 None。

    Chromium 的 SOCKS5 认证需 username/password 单独字段, 不能只嵌在 server URL
    (否则 SOCKS 握手被拒 -> ERR_SOCKS_CONNECTION_FAILED)。故此处把 userinfo 拆出。
    """
    if not proxy_url:
        return None
    from urllib.parse import urlsplit
    sp = urlsplit(proxy_url)
    server = f"{sp.scheme}://{sp.hostname}"
    if sp.port:
        server += f":{sp.port}"
    d = {"server": server}
    if sp.username:
        d["username"] = sp.username
    if sp.password:
        d["password"] = sp.password
    return d


def verify_egress_ip(ctx, expected: str | None = None) -> str | None:
    """preflight: 经已代理 ctx 访问 ipinfo.io, 打印出口 IP, 确认静态 IP 生效。"""
    try:
        page = ctx.new_page()
        try:
            page.goto("https://ipinfo.io/ip", wait_until="domcontentloaded", timeout=20000)
            ip = (page.inner_text("body") or "").strip()
        finally:
            page.close()
        tag = "OK" if (not expected or expected in ip) else "WARN(非期望IP)"
        print(f"[egress] 出口IP={ip} (期望静态IP={expected}) [{tag}]")
        return ip
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 出口IP自检失败: {e}")
        return None


# ---------------------------------------------------------------------------
# P0.A — 全局断路器 (连续 K 个 5xx/429 -> 整批冷却 T 分钟)
# ---------------------------------------------------------------------------
class CooldownState:
    """
    线程安全。记录「应触发冷却」的错误 (5xx / 429 / 网关网络错误),
    连续达到 threshold 即让整批挂起 cooldown_sec 秒。
    now_fn 可注入假时钟以便单元测试 (默认 time.time)。
    """

    def __init__(self, threshold: int = 5, cooldown_sec: int = 900, now_fn=None):
        self.threshold = threshold
        self.cooldown_sec = cooldown_sec
        self._now = now_fn or time.time
        self._lock = threading.Lock()
        self._consecutive = 0
        self._cooling_until = 0.0
        self.triggered = False

    def record_blocking(self, status=None):
        """记录一个限流/网关错误。返回 True 表示刚刚进入冷却 (或已在冷却中)。"""
        with self._lock:
            if self._now() < self._cooling_until:
                return True
            self._consecutive += 1
            if self._consecutive >= self.threshold:
                self._cooling_until = self._now() + self.cooldown_sec
                self._consecutive = 0
                self.triggered = True
                return True
            return False

    def record_success(self):
        with self._lock:
            if self._consecutive > 0:
                self._consecutive = 0

    def is_cooling(self) -> bool:
        with self._lock:
            return self._now() < self._cooling_until

    def remaining_sec(self) -> float:
        with self._lock:
            rem = self._cooling_until - self._now()
            return rem if rem > 0 else 0.0

    def wait_if_cooling(self, log=print):
        """若在冷却中, 阻塞等待直到冷却结束。返回实际等待秒数。"""
        wait = self.remaining_sec()
        if wait > 0:
            log(f"[cooldown] 网关熔断冷却中, 剩余 {wait:.0f}s, 整批挂起...")
            time.sleep(wait)
            return wait
        return 0.0
