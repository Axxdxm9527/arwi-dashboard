# -*- coding: utf-8 -*-
"""ARWI 3.0 数据服务：抓取 → 对齐 → 滚动Z-Score → 合成ARWI → 信号判定。

数据源（国内优先，全部免费公开）：
  - 美元指数 DXY  : 新浪财经 hq.sinajs.cn `DINIW`（实时）+ 自累积历史；
                    Yahoo `DX-Y.NYB` 可选回填（网络可达时）
  - VIX 恐慌指数   : 腾讯行情 web.ifzq.gtimg.cn `us.VIX`（日K）；
                    备用 新浪 `b_VIX`（实时）
  - 10Y名义美债    : FRED `DGS10`（国内可访问；无国内免费替代源）
  - 10Y实际利率    : FRED `DFII10`；备用 `T10YIE` 替代法（名义-盈亏平衡）
  - 伦敦现货黄金   : 新浪外盘期货 `XAU`（全量日K）+ `hf_XAU` 实时
  - 布伦特原油     : 新浪外盘期货 `OIL`（全量日K）+ `hf_OIL` 实时

容灾逻辑：
  1. 每个因子多源链顺序尝试；单请求失败自动重试 3 次（指数退避）。
  2. DFII10 失败 → 用“名义收益率 - T10YIE”替代实际利率。
  3. 仍失败 → 沿用前一日数据并标记“待更新”；连续 3 天失败 → “数据源异常”。
  4. yfinance 仅作可选回填增强：失败静默跳过，不影响国内主链与状态标记。
"""
import io
import json
import logging
import math
import os
import re
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from pandas_datareader import data as pdr

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except Exception:  # noqa: BLE001
    HAS_PLAYWRIGHT = False

try:
    from playwright_stealth import stealth as _pw_stealth
    HAS_STEALTH = True
except Exception:  # noqa: BLE001
    HAS_STEALTH = False

import db as database
from config import (
    CONSECUTIVE_FAIL_LIMIT, EMPTY_RETRY_GUARD, EXTREME_SIGMA, FACTORS,
    FETCH_DAYS_BACKFILL, FETCH_DAYS_INCREMENTAL, FILL_MAX_DAYS, FLAT_EPS,
    GREEN_LOW, HISTORY_POINTS, MIN_HISTORY_DAYS, RED_HIGH, RETRY_BACKOFF,
    RETRY_TIMES, TABLE_METRICS, WEIGHTS, YF_OPTIONAL, CST,
)

logging.basicConfig(level=logging.INFO,
                    format="[%(asctime)s] %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("arwi")

FACTOR_KEYS = [f["key"] for f in FACTORS]
STORE_KEYS = ["dxy", "vix", "tnx", "dfii10", "gold", "oil"]

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
SINA_HDR = {**HDR, "Referer": "https://finance.sina.com.cn"}

_lock = threading.Lock()
_substituted = False  # 实际利率是否使用替代法


# ---------------------------------------------------------------------------
# 一、抓取层（国内源优先）
# ---------------------------------------------------------------------------
def _clean_index(s: pd.Series) -> pd.Series:
    """统一索引为归一化的 datetime64（去时区、去时间）。"""
    idx = s.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    s.index = pd.DatetimeIndex(pd.to_datetime(idx)).normalize()
    s = s[~s.index.duplicated(keep="last")]
    return s


def _fill_limited(s: pd.Series, limit=FILL_MAX_DAYS) -> pd.Series:
    """前向填充但限制最大连续填充天数。

    数据源短期滞后（如 FRED 1-2 天）可平滑补齐；若某因子长时间断供，
    超过 limit 天后保持空缺（前端显示 -- / 该因子 z 按中性处理），
    避免“旧值无限期伪装成今日值”造成的假平值。
    """
    out = s.copy()
    prev, run = np.nan, 0
    for i in range(len(out)):
        v = out.iloc[i]
        if pd.isna(v):
            if not pd.isna(prev) and run < limit:
                out.iloc[i] = prev
                run += 1
            else:
                run = limit   # 达到上限：保持 NaN
        else:
            prev, run = v, 0
    return out


def _http_get(url, headers=None, timeout=15, retries=RETRY_TIMES):
    """带状态码日志与重试的 GET，返回 requests.Response。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=headers or HDR, timeout=timeout)
            log.info("GET %s -> %d", url.split("?")[0].rsplit("/", 1)[-1] or url,
                     r.status_code)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("GET %s -> FAILED (attempt %d/%d): %s: %s",
                        url.split("?")[0][:70], attempt, retries,
                        type(e).__name__, e)
            if attempt < retries:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"请求失败: {last_err}")


def fetch_sina_hq_latest():
    """新浪实时行情一次拉取：DINIW(美元指数) / hf_XAU(伦敦金) / hf_OIL(布伦特) / b_VIX。
    返回 {key: value}，缺失键自动跳过。"""
    codes = {"DINIW": "dxy", "hf_XAU": "gold", "hf_OIL": "oil", "b_VIX": "vix"}
    url = "https://hq.sinajs.cn/list=" + ",".join(codes.keys())
    r = _http_get(url, headers=SINA_HDR)
    r.encoding = "gbk"
    out = {}
    for line in r.text.splitlines():
        line = line.strip()
        if not line.startswith("var hq_str_"):
            continue
        head, _, payload = line.partition("=")
        code = head.replace("var hq_str_", "").strip()
        key = codes.get(code)
        if key is None:
            continue
        f = payload.strip().strip('"').split(",")
        if not f or not f[0]:
            continue
        try:
            if code == "DINIW":           # 时间,最新价,...
                out[key] = float(f[1])
            elif code == "b_VIX":          # 名称,最新价,涨跌额,涨跌幅%,...
                out[key] = float(f[1])
            else:                          # hf_ 外盘: 买,卖,最新价,...
                out[key] = float(f[2])
        except (ValueError, IndexError):
            continue
    return out


def fetch_tencent_hf_latest():
    """腾讯实时：hf_XAU / hf_OIL（备用源，与新浪互备）。"""
    codes = {"hf_XAU": "gold", "hf_OIL": "oil"}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes.keys())
    r = _http_get(url)
    r.encoding = "gbk"
    out = {}
    for line in r.text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        name, _, payload = line.partition("=")
        key = codes.get(name.replace("v_", "", 1).strip())
        if key is None:
            continue
        f = payload.strip().strip('"').split(",")
        try:
            out[key] = float(f[0])        # 腾讯 hf_ 首字段为现价
        except (ValueError, IndexError):
            continue
    return out


def fetch_sina_global_fut_kline(symbol):
    """新浪外盘期货日K（全量历史，返回按日期升序的 Series）。"""
    url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
           f"var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine?symbol={symbol}")
    r = _http_get(url, timeout=20)
    txt = r.text
    data = json.loads(txt[txt.find("(") + 1:txt.rfind(")")])
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"sina futures {symbol} 返回空数据")
    s = pd.Series({d["date"]: float(d["close"]) for d in data})
    return _clean_index(s).sort_index()


def fetch_tencent_us_kline(code, days):
    """腾讯美股/指数日K（如 us.VIX）。返回升序 Series。"""
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={code},day,,,{max(days, 60)},qfq")
    j = _http_get(url).json()
    d = (j.get("data") or {}).get(code)
    if not d:
        raise RuntimeError(f"tencent {code} 无数据")
    k = d.get("day") or d.get("qfqday") or []
    if not k:
        raise RuntimeError(f"tencent {code} 日K为空")
    s = pd.Series({row[0]: float(row[2]) for row in k})   # [date, open, close, high, low, vol]
    return _clean_index(s).sort_index().tail(days + 30)


def fetch_fred(series, days):
    """FRED 序列：pandas_datareader 主通道，CSV 直连兜底（国内可访问）。"""
    start = (datetime.now() - timedelta(days=days + 30)).strftime("%Y-%m-%d")
    last_err = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            df = pdr.DataReader(series, "fred", start=start)
            s = _clean_index(df[series].dropna())
            log.info("GET fred/%s -> 200 OK (rows=%d, latest=%s)",
                     series, len(s), s.index[-1].date())
            return s
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("GET fred/%s -> FAILED (pdr, attempt %d/%d): %s: %s",
                        series, attempt, RETRY_TIMES, type(e).__name__, e)
            try:
                url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
                r = requests.get(url, params={"cosd": start}, timeout=20)
                log.info("GET fred/%s -> %d (direct)", series, r.status_code)
                r.raise_for_status()
                raw = pd.read_csv(io.StringIO(r.text), na_values=".")
                raw.columns = [c.strip().strip('"') for c in raw.columns]
                raw["DATE"] = pd.to_datetime(raw["DATE"])
                raw = raw.set_index("DATE")
                s = _clean_index(raw[raw.columns[0]].dropna())
                s.name = series
                if s.empty:
                    raise RuntimeError("empty direct response")
                return s
            except Exception as e2:  # noqa: BLE001
                last_err = e2
                log.warning("GET fred/%s -> FAILED (direct): %s", series, e2)
            if attempt < RETRY_TIMES:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError(f"FRED {series} 连续失败: {last_err}")


def _try_yf_backfill(key, days):
    """可选增强：若 Yahoo 网络可达，用 yfinance 补充历史回填。
    失败静默返回 None（中国大陆网络 Yahoo 被封，属预期）。"""
    if not YF_OPTIONAL:
        return None
    symbols = {"dxy": "DX-Y.NYB", "vix": "^VIX", "tnx": "^TNX",
               "gold": "GC=F", "oil": "BZ=F"}
    symbol = symbols.get(key)
    if not symbol:
        return None
    try:
        import yfinance as yf
        period = "3mo" if days <= 90 else "6mo"
        df = yf.Ticker(symbol).history(period=period, auto_adjust=False)
        if df is None or df.empty:
            return None
        s = _clean_index(df["Close"].dropna())
        log.info("yfinance %s 可选回填成功 (rows=%d)", symbol, len(s))
        return s
    except Exception:  # noqa: BLE001
        log.info("yfinance %s 不可用（可选回填跳过）", symbol)
        return None


# ---------------------------------------------------------------------------
# 二、按因子的多源链抓取
# ---------------------------------------------------------------------------
def _fetch_factor(key, days):
    """按因子多源链抓取，返回 (Series 或 None, note)。主链失败抛异常。"""
    if key == "dxy":
        # ① 优先：Playwright + 真实 Chrome 抓 investing.com 美元指数（≈160 日连续历史）
        if HAS_PLAYWRIGHT:
            try:
                s = _fetch_dxy(days)
                # 用新浪实时值补齐最新一日（investing 延迟约 1 日）
                latest = fetch_sina_hq_latest().get("dxy")
                if latest is not None:
                    s.loc[pd.Timestamp.now().normalize()] = latest
                    s = _clean_index(s).sort_index()
                return s, "investing美元指数(Playwright)"
            except Exception as e:  # noqa: BLE001
                log.warning("investing DXY 浏览器抓取失败：%s，降级新浪实时", e)
        # ② 降级：新浪实时 + 自累积 / yfinance
        latest = fetch_sina_hq_latest().get("dxy")
        if latest is None:
            raise RuntimeError("新浪 DINIW 美元指数实时为空")
        s = pd.Series([latest], index=[pd.Timestamp.now().normalize()])
        s.name = "dxy"
        # 优先用 yfinance 回填历史（网络可达时），否则自累积
        hist = _try_yf_backfill("dxy", days)
        if hist is not None and len(hist) >= MIN_HISTORY_DAYS:
            hist.iloc[-1] = latest
            return hist, "yfinance回填+新浪实时"
        return s, "新浪实时(自累积历史)"
    if key == "vix":
        # ① 优先：Playwright + 真实 Chrome 抓 investing.com VIX（≈160 日连续历史）
        if HAS_PLAYWRIGHT:
            try:
                s = _fetch_vix(days)
                # 用新浪 b_VIX 实时值补齐最新一日
                try:
                    latest = fetch_sina_hq_latest().get("vix")
                    if latest is not None:
                        s.loc[pd.Timestamp.now().normalize()] = latest
                        s = _clean_index(s).sort_index()
                except Exception:  # noqa: BLE001
                    pass
                return s, "investing VIX(Playwright)"
            except Exception as e:  # noqa: BLE001
                log.warning("investing VIX 浏览器抓取失败：%s，降级腾讯", e)
        # ② 降级：腾讯日K + 新浪实时
        try:
            s = fetch_tencent_us_kline("us.VIX", days)
            s.name = "vix"
            # 用新浪 b_VIX 实时值补齐最后一个交易日（K线滞后时今日值保持实时）
            try:
                latest = fetch_sina_hq_latest().get("vix")
                if latest is not None:
                    s.loc[pd.Timestamp.now().normalize()] = latest
                    s = _clean_index(s).sort_index()
            except Exception:  # noqa: BLE001
                pass
            return s, "腾讯us.VIX日K+新浪实时"
        except Exception:  # noqa: BLE001
            v = fetch_sina_hq_latest().get("vix")
            if v is None:
                raise
            s = pd.Series([v], index=[pd.Timestamp.now().normalize()])
            s.name = "vix"
            return s, "新浪b_VIX实时(降级)"
    if key == "tnx":
        s = fetch_fred("DGS10", days)
        s.name = "tnx"
        return s, "FRED DGS10"
    if key == "dfii10":
        s = fetch_fred("DFII10", days)
        s.name = "dfii10"
        return s, "FRED DFII10"
    if key == "gold":
        try:
            s = fetch_sina_global_fut_kline("XAU")
            s.name = "gold"
            latest = fetch_sina_hq_latest().get("gold")
            if latest is not None:
                s.loc[pd.Timestamp.now().normalize()] = latest
            return s.tail(days + 30), "新浪外盘XAU日K"
        except Exception:  # noqa: BLE001
            v = fetch_tencent_hf_latest().get("gold")
            if v is None:
                raise
            s = pd.Series([v], index=[pd.Timestamp.now().normalize()])
            s.name = "gold"
            return s, "腾讯hf_XAU实时(降级)"
    if key == "oil":
        try:
            s = fetch_sina_global_fut_kline("OIL")
            s.name = "oil"
            latest = fetch_sina_hq_latest().get("oil")
            if latest is not None:
                s.loc[pd.Timestamp.now().normalize()] = latest
            return s.tail(days + 30), "新浪外盘OIL日K"
        except Exception:  # noqa: BLE001
            v = fetch_tencent_hf_latest().get("oil")
            if v is None:
                raise
            s = pd.Series([v], index=[pd.Timestamp.now().normalize()])
            s.name = "oil"
            return s, "腾讯hf_OIL实时(降级)"
    raise RuntimeError(f"未知因子 {key}")


# ---------------------------------------------------------------------------
# 三、同步主流程
# ---------------------------------------------------------------------------
# ICE BofA MOVE 指数（债券收益率波动率，债市"VIX"，数值约 80~160）
# 参考页：https://cn.investing.com/indices/ice-bofaml-move
# ⚠️ 注意：腾讯 usMOVE 是美股 MOVE（Corvex Inc.，股价约 12 美元），与本指数无关，勿用。
MOVE_PAGE_URL = "https://cn.investing.com/indices/ice-bofaml-move"
MOVE_INV_HDR = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "domain-id": "www",
    "Referer": "https://cn.investing.com/",
}


def _parse_move_json(j):
    """解析 investing.com 历史数据 JSON（兼容多种字段命名）。返回升序 Series 或 None。"""
    rows = j.get("data") if isinstance(j, dict) else j
    if not isinstance(rows, list):
        return None
    pts = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        dt = row.get("datetime") or row.get("row_date") or row.get("date") or row.get("time")
        val = row.get("value")
        if val is None:
            val = row.get("price_close") or row.get("close") or row.get("price")
        if dt is None or val is None:
            continue
        try:
            if isinstance(dt, (int, float)):
                dt = datetime.utcfromtimestamp(dt).date().isoformat()
            else:
                dt = str(dt)[:10]
            pts.append((dt, float(val)))
        except (ValueError, TypeError):
            continue
    if len(pts) < 2:
        return None
    s = pd.Series({d: v for d, v in pts})
    s.index = pd.to_datetime(s.index).normalize()
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s.name = "move"
    return s


def _fetch_investing_series(pair_id, page_url, name):
    """investing.com 指数历史抓取（基于真实浏览器 + Playwright 绕过 Cloudflare）。
    流程：
      1) Playwright 启动系统 Chrome（真实浏览器指纹 + 反检测注入）
      2) 打开指定指数页面（页面加载会异步触发 historical/chart API）
      3) 拦截 `financialdata/{pair_id}/historical/chart` 响应
      4) 取 close（第 5 列）按时间排序
    数据列含义：[timestamp_ms, open, high, low, close, volume, change_pct]
    返回：pd.Series（pd.Timestamp 索引，与 df.index 对齐），约 160 个日点（≈7 个月）。
    """
    if not HAS_PLAYWRIGHT:
        raise RuntimeError("playwright 未安装，无法用浏览器抓取数据")
    # Chrome / Edge 路径（Windows + Linux 双平台，GitHub Actions 用 Linux）
    chrome_paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    exe = next((p for p in chrome_paths if os.path.exists(p)), None)
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge 浏览器，无法启动 Playwright")

    captured = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=exe, headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
                "--disable-dev-shm-usage",
                "--window-size=1920,1080",
            ],
        )
        try:
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                color_scheme="light",
            )
            # 隐藏 webdriver 标记 + 伪造语言/插件
            ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                window.navigator.chrome = { runtime: {} };
                Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en']});
                Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
                const _q = HTMLAnchorElement.prototype.toString;
                HTMLAnchorElement.prototype.toString = function() { return _q.call(this); };
            """)

            def on_resp(resp):
                if f"financialdata/{pair_id}/historical/chart" in resp.url:
                    try:
                        captured.setdefault("rows", []).append(resp.json())
                    except Exception:
                        pass

            page = ctx.new_page()
            page.on("response", on_resp)
            # 加载页面触发历史 API（图表组件异步加载，约 5-15s）
            page.goto(page_url, timeout=120000, wait_until="domcontentloaded")
            # 等图表历史 API 响应：每 1s 检查一次，最长 40s
            for _ in range(40):
                if captured:
                    break
                page.wait_for_timeout(1000)
            page.wait_for_timeout(3000)   # 缓冲：确保数据完整加载
        finally:
            browser.close()

    if not captured or not captured.get("rows"):
        raise RuntimeError(f"未拦截到 investing {name} 历史数据响应")

    # 取最后一次响应（数据最全）
    last_resp = captured["rows"][-1]
    arr = last_resp.get("data") if isinstance(last_resp, dict) else last_resp
    if not arr:
        raise RuntimeError(f"investing {name} API 返回 data 为空")

    # 数据列: [timestamp_ms, open, high, low, close, volume, change_pct]
    rows = []
    for r in arr:
        try:
            ts_ms = int(r[0])
            # 用 Timestamp 索引（与 df.index 对齐，date 会导致 upsert 覆盖成 None）
            dt = pd.Timestamp(ts_ms, unit="ms", tz="UTC").tz_localize(None).normalize()
            close = float(r[4])
            if close and not math.isnan(close):
                rows.append((dt, close))
        except (IndexError, TypeError, ValueError):
            continue
    if not rows:
        raise RuntimeError(f"{name} 解析后无有效数据")
    s = pd.Series({d: v for d, v in rows}).sort_index()
    s.name = name.lower()
    log.info("investing %s（浏览器抓取）：rows=%d (%s -> %s)",
             name, len(s), s.index.min(), s.index.max())
    return s


def _fetch_move(days):
    """ICE BofA MOVE 指数：investing.com pairId=1164091（≈160 个日点，近 7 个月）。"""
    return _fetch_investing_series(1164091, MOVE_PAGE_URL, "MOVE")


def _fetch_vix(days):
    """VIX 恐慌指数：investing.com pairId=44336（≈160 个日点）。"""
    return _fetch_investing_series(44336, "https://cn.investing.com/indices/volatility-s-p-500", "VIX")


def _fetch_dxy(days):
    """美元指数 DXY：investing.com pairId=942611（≈160 个日点）。"""
    return _fetch_investing_series(942611, "https://cn.investing.com/indices/usdollar", "DXY")


def needs_sync(last):
    """判断是否需要增量拉取（北京时间视角：美盘收盘在次日凌晨4-5点）。"""
    if not last:
        return True
    today = date.today()
    if today.weekday() == 0:      # 周一 → 最新美盘为上周五
        expected = today - timedelta(days=3)
    elif today.weekday() == 6:    # 周日 → 最新美盘为上周五
        expected = today - timedelta(days=2)
    else:                          # 周二~周六 → 前一日
        expected = today - timedelta(days=1)
    return last < expected.isoformat()


def _sync(full, force):
    """核心同步：拉取 → 对齐 → 入库。返回 (ok, message)。"""
    global _substituted
    days = FETCH_DAYS_BACKFILL if full else FETCH_DAYS_INCREMENTAL
    fetched, status, notes = {}, {}, {}

    # 1) 六个因子多源链抓取
    for i, key in enumerate(STORE_KEYS):
        if i > 0:
            time.sleep(1.0)   # 国内接口节奏，避免触发风控
        try:
            s, note = _fetch_factor(key, days)
            if s is None or len(s) == 0:
                raise RuntimeError("空序列")
            fetched[key] = s
            status[key] = "ok"
            notes[key] = note
            database.reset_fail(key)
            log.info("因子 %s <- %s (rows=%d)", key, note, len(s))
        except Exception as e:  # noqa: BLE001
            status[key] = "stale"
            database.bump_fail(key)
            log.error("因子 %s 抓取失败：%s，将沿用前一日数据", key, e)

    # 2) 对齐合并
    df = pd.concat([fetched[k] for k in STORE_KEYS if k in fetched],
                   axis=1, join="outer").sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if df.empty:
        log.error("所有数据源拉取失败，本次同步中止")
        return False, "所有数据源拉取失败"

    # 3) 实际利率替代法：名义收益率 - 盈亏平衡通胀率
    _substituted = database.get_meta("real_rate_substituted", "0") == "1"
    if "dfii10" not in df.columns or df["dfii10"].notna().sum() == 0:
        try:
            t10 = fetch_fred("T10YIE", days)
            if "tnx" in df.columns and len(t10):
                df["dfii10"] = _fill_limited(df["tnx"].reindex(df.index)) - \
                    _fill_limited(t10.reindex(df.index))
                _substituted = True
                database.set_meta("real_rate_substituted", "1")
                status["dfii10"] = "ok"
                log.info("实际利率使用替代法：名义收益率 - T10YIE")
        except Exception as e:  # noqa: BLE001
            database.bump_fail("T10YIE")
            status["dfii10"] = "stale"
            log.error("T10YIE 替代法失败：%s", e)

    # 4) 缺失列用 DB 历史补齐（沿用前一日，标记待更新；限 5 天防假平值）
    for key in STORE_KEYS:
        if key not in df.columns or df[key].notna().sum() == 0:
            db_s = _series_from_db(key)
            if not db_s.empty:
                df[key] = _fill_limited(db_s.reindex(df.index))
                if status.get(key, "ok") == "ok":
                    status[key] = "stale"
            else:
                df[key] = np.nan
        else:
            # 本次抓取范围可能短于 DB 历史（如某源偶发只返回 60 天）：
            # 用 DB 历史合并补齐（本次值优先，历史兜底），避免 upsert 把长历史覆盖成 None
            db_s = _series_from_db(key)
            if not db_s.empty:
                merged = df[key].reindex(df.index).combine_first(db_s.reindex(df.index))
                df[key] = merged

    # 5) 有限前向填充（滞后补齐 ≤5 天）+ 至少 4/6 列有值 + 截断
    #    thresh=4 容忍单因子断供：该因子当日按中性处理、表内 "--"、标记待更新
    for col in df.columns:
        df[col] = _fill_limited(df[col])
    df = df.dropna(thresh=len(STORE_KEYS) - 2).tail(420)
    if df.empty:
        log.error("对齐后无有效数据")
        return False, "对齐后无有效数据"

    # 5.5) MOVE 独立指标（Playwright + 真实浏览器抓 investing.com，绕开 Cloudflare）
    move_series = None
    try:
        move_series = _fetch_move(days)
        if move_series is not None and len(move_series):
            database.reset_fail("move")
    except Exception as e:  # noqa: BLE001
        database.bump_fail("move")
        log.warning("MOVE 浏览器抓取失败：%s，沿用 DB 历史", e)
        move_series = None
    if move_series is None or len(move_series) == 0:
        move_series = _series_from_db("move")
    # 统一索引类型为 Timestamp（与 df.index 一致），否则 upsert 会覆盖为 None
    move_map = {pd.Timestamp(k): v for k, v in move_series.items() if not math.isnan(v)}

    # 5.6) OAS 独立指标（FRED 公开接口：BAMLC0A0CM 投资级 / BAMLH0A0HYM2 高收益，
    #      FRED 单位为百分比，×100 转为 bp；不参与 ARWI 合成与 thresh）
    oas_maps = {}
    for oas_key, fred_series in (("oas_ig", "BAMLC0A0CM"), ("oas_hy", "BAMLH0A0HYM2")):
        try:
            s = fetch_fred(fred_series, days)
            if len(s):
                s = (s * 100.0).round(2)          # % → bp
                s = _clean_index(s)
                s = s[~s.index.duplicated(keep="last")].sort_index()
                oas_maps[oas_key] = {ts: v for ts, v in s.items() if not math.isnan(v)}
                database.reset_fail(oas_key)
                log.info("FRED %s(%s)：rows=%d (%s -> %s)",
                         oas_key, fred_series, len(s),
                         s.index.min().date(), s.index.max().date())
            else:
                raise RuntimeError("空序列")
        except Exception as e:  # noqa: BLE001
            database.bump_fail(oas_key)
            log.error("FRED %s(%s) 拉取失败：%s，沿用前一日数据", oas_key, fred_series, e)
            prev = _series_from_db(oas_key)
            oas_maps[oas_key] = {ts: v for ts, v in prev.items() if not math.isnan(v)}

    # 6) 入库
    last = database.latest_date()
    records, added = [], 0
    row_stale = any(status.get(k) == "stale" for k in STORE_KEYS)
    now_str = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    for ts, row in df.iterrows():
        d = ts.strftime("%Y-%m-%d")
        records.append({
            "date": d, "dxy": row["dxy"], "vix": row["vix"], "tnx": row["tnx"],
            "dfii10": row["dfii10"], "gold": row["gold"], "oil": row["oil"],
            "move": move_map.get(ts),
            "oas_ig": oas_maps.get("oas_ig", {}).get(ts),
            "oas_hy": oas_maps.get("oas_hy", {}).get(ts),
            "status": "stale" if row_stale else "ok", "updated_at": now_str,
        })
        if not last or d > last:
            added += 1
    database.upsert_rows(records)

    # 6.5) 独立全量入库：dxy / vix / move（完整覆盖抓取的全部日期，
    #      不受六因子对齐范围限制；只更新对应列，不触碰其他因子）。
    #      解决 FRED 只回填 47 天时 df 对齐窗口被压缩、长历史被覆盖的问题。
    _indep_map = {
        "dxy": {pd.Timestamp(k): v for k, v in fetched["dxy"].items()} if "dxy" in fetched else None,
        "vix": {pd.Timestamp(k): v for k, v in fetched["vix"].items()} if "vix" in fetched else None,
        "move": move_map,
    }
    for icol, imap in _indep_map.items():
        if not imap:
            continue
        n_changed = 0
        with database.get_conn() as conn:
            for i_ts, i_val in imap.items():
                if i_val is None or (isinstance(i_val, float) and math.isnan(i_val)):
                    continue
                d_str = pd.Timestamp(i_ts).strftime("%Y-%m-%d")
                cur = conn.execute(
                    f"SELECT {icol} FROM daily_metrics WHERE date=?", (d_str,)
                ).fetchone()
                if cur is None or cur[icol] != float(i_val):
                    conn.execute(
                        f"""INSERT INTO daily_metrics (date, {icol}, status, updated_at)
                            VALUES (?, ?, 'ok', ?)
                            ON CONFLICT(date) DO UPDATE SET
                                {icol}=excluded.{icol}, status='ok', updated_at=excluded.updated_at""",
                        (d_str, float(i_val), now_str),
                    )
                    n_changed += 1
        if n_changed:
            log.info("独立入库 %s：%d 个交易日（共 %d 点）", icol, n_changed, len(imap))

    database.prune()
    src_summary = "、".join(f"{k}:{notes[k]}" for k in STORE_KEYS if k in notes) or "全部失败"
    log.info("入库 %d 行（新增 %d），最新日期 %s | 源：%s",
             len(records), added, df.index[-1].strftime("%Y-%m-%d"), src_summary)
    return True, f"已更新至 {df.index[-1].strftime('%Y-%m-%d')}"


def sync_data(force=False):
    """对外同步入口：首次回填全量，之后增量；当日已有数据则不重复拉取。"""
    database.init_db()
    with _lock:
        if database.count_rows() == 0:
            # 空库失败后的防抖：60 秒内不重复全量重试，避免离线时反复请求
            last_attempt = float(database.get_meta("last_full_attempt", 0) or 0)
            if not force and time.time() - last_attempt < EMPTY_RETRY_GUARD:
                return False, "数据源暂不可用，请稍后点击【手动刷新】重试"
            database.set_meta("last_full_attempt", str(time.time()))
            return _sync(full=True, force=force)
        last = database.latest_date()
        if not force and not needs_sync(last):
            log.info("数据已最新（%s），跳过拉取", last)
            return True, "数据已最新，无需更新"
        return _sync(full=False, force=force)


def _series_from_db(key):
    """把 daily_metrics 中的某列转为按日期索引的 Series（用于失败时沿用前值）。"""
    assert key in FACTOR_KEYS + ["tnx", "move", "oas_ig", "oas_hy"], key
    with database.get_conn() as conn:
        rows = conn.execute(
            f"SELECT date, {key} v FROM daily_metrics ORDER BY date"
        ).fetchall()
    data = {r["date"]: r["v"] for r in rows if r["v"] is not None}
    if not data:
        return pd.Series(dtype=float)
    s = pd.Series(data)
    s.index = pd.to_datetime(s.index).normalize()
    return s.sort_index()


# ---------------------------------------------------------------------------
# 四、计算层：滚动 Z-Score → ARWI → 信号
# ---------------------------------------------------------------------------
def _stats(prev_vals):
    vals = [v for v in prev_vals if v is not None and not math.isnan(v)]
    if not vals:
        return 0.0, 0.0
    mean = statistics.fmean(vals)
    std = statistics.stdev(vals) if len(vals) >= 2 else 0.0
    return mean, std


def signal_for(arwi, worsened, improved):
    if arwi > RED_HIGH and worsened >= 3:
        return "red", "高风险"
    if arwi < GREEN_LOW and improved >= 3:
        return "green", "积极"
    return "yellow", "观望"


def compute_arwi_history(rows):
    """对每个交易日（需 ≥11 天）计算滚动 Z-Score 与 ARWI。"""
    out = []
    for i in range(len(rows)):
        if i < MIN_HISTORY_DAYS - 1:
            continue
        today, prev = rows[i], rows[i - (MIN_HISTORY_DAYS - 1):i]
        raw_zs, finals = {}, {}
        for f in FACTORS:
            key = f["key"]
            mean, std = _stats([r.get(key) for r in prev])
            cur = today.get(key)
            z = (cur - mean) / std if (cur is not None and std > 1e-12) else 0.0
            raw_zs[key] = z
            finals[key] = z * f["direction"]
        arwi = sum(finals[k] * WEIGHTS[k] for k in finals)
        worsened = sum(1 for v in finals.values() if v > 0)
        improved = sum(1 for v in finals.values() if v < 0)
        sig, sig_txt = signal_for(arwi, worsened, improved)
        out.append({
            "date": today["date"], "arwi": arwi, "signal": sig,
            "signal_text": sig_txt, "raw_zs": raw_zs, "finals": finals,
            "worsened": worsened, "improved": improved,
        })
    return out


def _metric_status(key):
    n = database.get_fail(key)
    if n >= CONSECUTIVE_FAIL_LIMIT:
        return "error"   # 数据源异常
    if n >= 1:
        return "stale"   # 待更新
    return "ok"


# ---------------------------------------------------------------------------
# 五、汇总：生成 /api/data 载荷
# ---------------------------------------------------------------------------
def _trend_word(diff):
    """根据差值给出趋势描述词（ARWI 无量纲，阈值用 0.15/0.3）。"""
    if abs(diff) < 0.15:
        return "基本持平"
    if diff < 0:
        return "显著回落" if diff < -0.3 else "小幅回落"
    return "显著抬升" if diff > 0.3 else "小幅抬升"


def _multi_period_view(arwi, m5, m10, m30, m60):
    """多周期位置综合判断：今日相对 5/10/30/60 日均值处于什么位置。"""
    below = sum(1 for m in (m5, m10, m30, m60) if arwi < m)
    if below == 4:
        return "今日ARWI低于全部四个周期均值，短中长期同步走弱"
    if below == 0:
        return "今日ARWI高于全部四个周期均值，短中长期同步走强"
    if arwi < m5 and arwi < m10 and arwi > m30 and arwi > m60:
        return "短中期回落、长期仍高于60日均值，或属高位回调"
    if arwi > m5 and arwi > m10 and arwi < m30 and arwi < m60:
        return "短期反弹、中期仍低于30日均值，或属低位修复"
    return "多周期信号分化，方向未明，建议等待确认"


def build_summary(t, means, factors):
    arwi = t["arwi"]
    m5, m10, m30, m60 = means["m5"], means["m10"], means["m30"], means["m60"]
    top = sorted(factors, key=lambda f: abs(f["final"]), reverse=True)[:2]
    drivers = "、".join(
        f"{f['name']}{'上行' if f['final'] > 0 else '回落'}（{f['final']:+.1f}σ）"
        for f in top
    )
    if t["signal"] == "red":
        stance = f"当前{t['worsened']}个因子恶化，风险偏好收缩，建议降低风险敞口、增加防御配置。"
    elif t["signal"] == "green":
        stance = f"当前{t['improved']}个因子改善，环境对风险资产友好，建议维持持仓、逢低布局。"
    else:
        stance = "多空因素交织，建议控制仓位、等待方向明朗。"
    note = "（注：实际利率由名义收益率−盈亏平衡通胀率估算）" if _substituted else ""
    return (
        f"今日ARWI为 {arwi:.2f}，较前5日均值（{m5:.2f}）{_trend_word(arwi - m5)}，"
        f"较前10日均值（{m10:.2f}）{_trend_word(arwi - m10)}，"
        f"较前30日均值（{m30:.2f}）{_trend_word(arwi - m30)}，"
        f"较前60日均值（{m60:.2f}）{_trend_word(arwi - m60)}。"
        f"进入「{t['signal_text']}」区间。主要驱动因素为{drivers}。"
        f"{_multi_period_view(arwi, m5, m10, m30, m60)}。{stance}{note}"
    )


def build_payload():
    """组装前端所需全部数据。"""
    rows = database.last_n(HISTORY_POINTS + 60)   # 多取 60 天供长周期均线计算
    if len(rows) < MIN_HISTORY_DAYS:
        return {
            "success": False,
            "error": f"有效交易日数据不足（当前 {len(rows)} 天，需要 ≥ {MIN_HISTORY_DAYS} 天）。"
                     f"首次运行会自动回填历史数据，请稍候或检查网络后点击【手动刷新】。",
        }
    hist = compute_arwi_history(rows)
    if not hist:
        return {"success": False, "error": "无法计算 ARWI：历史数据不足 11 个交易日。"}

    t = hist[-1]
    prev_arwi = [h["arwi"] for h in hist[:-1]]
    # 多周期 ARWI 均值（前 5 / 10 / 30 / 60 日，不含今日）
    def _prev_mean(n):
        vals = prev_arwi[-n:] if n > 0 else []
        return statistics.fmean(vals) if vals else t["arwi"]
    means = {"m5": _prev_mean(5), "m10": _prev_mean(10),
             "m30": _prev_mean(30), "m60": _prev_mean(60)}
    mean10 = means["m10"]
    diff = t["arwi"] - mean10
    direction = "up" if diff > FLAT_EPS else ("down" if diff < -FLAT_EPS else "flat")
    prev_signal = hist[-2]["signal"] if len(hist) >= 2 else t["signal"]

    factors = []
    for f in FACTORS:
        fin = t["finals"][f["key"]]
        factors.append({
            "key": f["key"], "name": f["name"], "label": f["label"],
            "z": round(t["raw_zs"][f["key"]], 3),
            "final": round(fin, 3),
            "weight": WEIGHTS[f["key"]],
            "contribution": round(fin * WEIGHTS[f["key"]], 3),
            "status": _metric_status(f["key"]),
        })

    last_row = rows[-1]
    table = []
    for m in TABLE_METRICS:
        key = m["key"]
        cur = last_row.get(key)
        # 前 5 / 10 / 30 / 60 日均值（不含今日）；日历窗口不足时降级用最近 N 个有效值
        def _hist_mean(n):
            cal = [r.get(key) for r in rows[-(n + 1):-1]]
            cal = [v for v in cal if v is not None and not math.isnan(v)]
            if len(cal) >= max(1, n // 2):    # 日历窗口有 ≥半数有效 → 用日历
                return statistics.fmean(cal), len(cal)
            # 降级：该指标所有历史（不含今日）的最近 n 个有效值
            all_vals = []
            for r in rows[:-1]:
                v = r.get(key)
                if v is not None and not math.isnan(v):
                    all_vals.append(float(v))
            if not all_vals:
                return None, 0
            recent = all_vals[-n:] if len(all_vals) >= n else all_vals
            return statistics.fmean(recent), len(all_vals)
        mean5, n5    = _hist_mean(5)
        mean10, n10  = _hist_mean(10)
        mean30, n30  = _hist_mean(30)
        mean60, n60  = _hist_mean(60)
        _, std10 = _stats([r.get(key) for r in rows[-11:-1]])
        z = (cur - mean10) / std10 if (cur is not None and mean10 is not None and std10 > 1e-12) else 0.0
        if m["is_yield"]:
            dev = (cur - mean10) * 100 if (cur is not None and mean10 is not None) else None   # 基点
        else:
            dev = (cur - mean10) / mean10 * 100 if (cur is not None and mean10) else None       # %
        table.append({
            **m,
            "today": cur,
            "mean5": mean5, "mean10": mean10, "mean30": mean30, "mean60": mean60,
            "n5": n5, "n10": n10, "n30": n30, "n60": n60,
            "deviation": dev, "z": round(z, 3),
            "status": _metric_status(key),
            "substituted": key == "dfii10" and _substituted,
        })

    warnings = [
        f"「{f['name']}」今日Z-Score {f['final']:+.2f}σ，偏离历史均值2倍标准差以上，请注意风险！"
        for f in factors if abs(f["final"]) > EXTREME_SIGMA
    ]
    source_anomaly = any(f["status"] == "error" for f in factors) or \
        _metric_status("tnx") == "error"

    # MOVE 独立指标：最近约 120 个交易日（≈半年）连续数据看板（Playwright 自动抓取）
    move_pts = [(r["date"], r["move"]) for r in rows if r.get("move") is not None]
    move_hist = [{"date": d, "value": round(v, 2)} for d, v in move_pts[-120:]]
    move_latest = move_hist[-1]["value"] if move_hist else None
    move_window = [v for _, v in move_pts[-120:]]
    move_mean = round(statistics.fmean(move_window), 2) if len(move_window) >= 5 else None
    move_high = round(max(move_window), 2) if move_window else None
    move_status = "ok" if len(move_pts) >= 20 else "manual"   # 自动抓取成功显示 ok，数据不足显示手动

    return {
        "success": True,
        "as_of": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "data_date": last_row["date"],
        "arwi": round(t["arwi"], 2),
        "arwi_raw": round(t["arwi"], 4),
        "mean10_arwi": round(mean10, 2),
        "direction": direction,
        "signal": t["signal"],
        "signal_text": t["signal_text"],
        "prev_signal": prev_signal,
        "signal_changed": t["signal"] != prev_signal,
        "worsened": t["worsened"],
        "improved": t["improved"],
        "neutral": 5 - t["worsened"] - t["improved"],
        "factors": factors,
        "table": table,
        "history": [
            {"date": h["date"], "arwi": round(h["arwi"], 3), "signal": h["signal"]}
            for h in hist[-HISTORY_POINTS:]
        ],
        "warnings": warnings,
        "source_anomaly": source_anomaly,
        "substituted_real_rate": _substituted,
        "summary": build_summary(t, means, factors),
        "move": {
            "latest": move_latest,
            "mean60": move_mean,
            "high60": move_high,
            "status": move_status,
            "history": move_hist,
        },
    }


# ---------------------------------------------------------------------------
# 六、全球宏观风险仪表盘（VIX / MOVE / IG-OAS / HY-OAS）
#    阈值表与 Z-Score 规则严格按需求给定，不可自行修改。
#    OAS 为 CDX 的现货替代指标：BAMLC0A0CM（投资级）/ BAMLH0A0HYM2（高收益），
#    沿用原 CDX 的 bp 阈值进行风险等级判断。
# ---------------------------------------------------------------------------
MACRO_BANDS = {   # 各指标阈值分档：[平静上限, 中性上限, 警惕上限, 危机上限]（超过危机上限=极端危机）
    "vix":    {"limits": [15, 25, 35, 45],   "levels": ["平静", "中性", "警惕", "危机", "极端危机"]},
    "move":   {"limits": [80, 120, 160, 220], "levels": ["平静", "中性", "警惕", "危机", "极端危机"]},
    "oas_ig": {"limits": [60, 100, 150, 250], "levels": ["平静", "中性", "警惕", "危机", "极端危机"]},
    "oas_hy": {"limits": [350, 500, 700, 1000], "levels": ["平静", "中性", "警惕", "危机", "极端危机"]},
}

MACRO_META = {
    "vix":    {"name": "VIX 恐慌指数",      "unit": "",    "src": "腾讯 us.VIX 日K / 新浪 b_VIX 实时", "manual": False},
    "move":   {"name": "MOVE 债市波动",      "unit": "",    "src": "美债利率波动率，利率市场恐慌（国债价格、利率剧烈波动）（数据源：cn.investing.com/indices/ice-bofaml-move）", "manual": False},
    "oas_ig": {"name": "IG OAS 利差",       "unit": "bp",  "src": "FRED BAMLC0A0CM（ICE-BofA 投资级 OAS）", "manual": False},
    "oas_hy": {"name": "HY OAS 利差",       "unit": "bp",  "src": "FRED BAMLH0A0HYM2（ICE-BofA 高收益 OAS）", "manual": False},
}


def _band_of(key, value):
    """按阈值表判定档位（平静/中性/警惕/危机/极端危机）。"""
    if value is None:
        return None
    limits = MACRO_BANDS[key]["limits"]
    levels = MACRO_BANDS[key]["levels"]
    for i, up in enumerate(limits):
        if value < up:
            return levels[i]
    return levels[-1]


def _z_alert(z):
    """Z 值警报规则（严格按需求）。"""
    if z is None:
        return None
    if z > 3:
        return {"level": "extreme", "text": "极端事件警报", "icon": "🔴"}
    if z > 2:
        return {"level": "abnormal", "text": "显著异常警报", "icon": "🟠"}
    if z > 1:
        return {"level": "off", "text": "偏离常态", "icon": "🟡"}
    return {"level": "normal", "text": "正常波动范围", "icon": "🟢"}


def _macro_z(series):
    """20 个交易日滚动 Z-Score：Z=(当前值-20日均值)/20日标准差。
    series 需为升序数值列表（最近 20 个交易日）。"""
    window = series[-20:]
    if len(window) < 3:      # 至少 3 个点才能算出有意义的均值/标准差
        return None, None, None
    mean = statistics.fmean(window)
    std = statistics.stdev(window) if len(window) > 1 else 0.0
    if std < 1e-12:
        return 0.0, mean, std
    z = (window[-1] - mean) / std
    return z, mean, std


def build_macro_risk_payload():
    """组装全球宏观风险仪表盘数据。"""
    rows = database.last_n(25)          # 取最近 25 个交易日（足够 20 日窗口）
    res = {
        "success": True, "metrics": {}, "system": None,
        "as_of": datetime.now(CST).strftime("%Y-%m-%d %H:%M"),
        "disclaimer": ("⚠️说明：OAS：市场上真实交易的企业债券，收益率高出无风险美债的差价。"
                       "本仪表盘使用现货债券OAS利差和美债已实现波动率做代理；"
                       "在流动性危机阶段，代理指标会和原版衍生品指数出现基差偏离；"
                       "数据仅供宏观研究，不构成投资建议。"),
    }

    # 各指标序列（最近 20 个非空值，升序）
    series = {k: [] for k in MACRO_BANDS}
    last_dates = {}
    for r in rows:
        for k in MACRO_BANDS:
            v = r.get(k)
            if v is not None:
                series[k].append(float(v))
                last_dates[k] = r["date"]     # 该指标最近一个有值的日期

    z_over_2 = 0
    evaluated = 0
    for key in MACRO_BANDS:
        vals = series[key]
        z, mean20, std20 = _macro_z(vals)
        cur = vals[-1] if vals else None
        band = _band_of(key, cur)
        alert = _z_alert(z)
        if z is not None:
            evaluated += 1
            if z > 2:
                z_over_2 += 1
        res["metrics"][key] = {
            "key": key,
            "name": MACRO_META[key]["name"],
            "unit": MACRO_META[key]["unit"],
            "src": MACRO_META[key]["src"],
            "manual": MACRO_META[key]["manual"],
            "current": round(cur, 2) if cur is not None else None,
            "mean20": round(mean20, 2) if mean20 is not None else None,
            "std20": round(std20, 2) if std20 is not None else None,
            "z": round(z, 2) if z is not None else None,
            "z_alert": alert,
            "band": band,
            "points": len(vals),
            "history": [round(v, 2) for v in vals[-20:]],
            "last_date": last_dates.get(key),
            "has_data": len(vals) >= 3,
        }

    # 系统风险综合判定：统计 Z>2 的指标数量
    if evaluated == 0:
        res["system"] = {"level": "none", "text": "暂无足够数据", "z_over_2": 0, "evaluated": 0}
    elif z_over_2 >= 3:
        res["system"] = {"level": "red", "text": "系统性风险预警", "z_over_2": z_over_2, "evaluated": evaluated}
    elif z_over_2 >= 1:
        res["system"] = {"level": "yellow", "text": "局部风险信号", "z_over_2": z_over_2, "evaluated": evaluated}
    else:
        res["system"] = {"level": "green", "text": "整体风险处于正常区间", "z_over_2": 0, "evaluated": evaluated}

    return res
