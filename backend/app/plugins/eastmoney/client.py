"""东方财富公开 HTTP 接口 client。

取数纪律（CONTRIBUTING「不伪造数据」）：
  - 行情类接口（push2/push2his）用 curl 子进程取数（urllib 会被东财服务端断连，
    curl 实测可达）；数据中心接口（datacenter-web）httpx 实测可用，无需子进程。
  - 必带 UA + Referer（缺 header 会被风控拒绝）；
  - 失败指数退避 + 随机抖动；空响应/连接失败明确返回 None（不得转成空数据冒充成功）；
  - 东财对短时间高频请求会临时封 IP（实测 6+ 次/几秒 → HTTP 000），调用方必须节流。

能力分层：
  - ``fetch_fflow_daykline``：个股资金流日线（逐股，近 120 交易日）；
  - ``fetch_lhb_day``：龙虎榜明细（**全市场按日一次**，含上榜原因多行）；
  - ``fetch_margin_day``：融资融券个股明细（**全市场按日分页**，约 4500 行/日）。
"""
from __future__ import annotations

import json
import random
import subprocess
import time
from datetime import date

import httpx
import polars as pl

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
FFLOW_DAYKLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"

# fields2 顺序: f51 日期, f52 主力净额, f53 小单, f54 中单, f55 大单, f56 超大单,
#               f57 主力净占比, f58 小单占比, f59 中单占比, f60 大单占比, f61 超大单占比
FFLOW_FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"

# 接口单位与口径（CONTRIBUTING §3.1，已用加总恒等式实测验证，不可凭字段名推断）：
#   - 净额单位为元，正 = 净流入；
#   - 恒等式 f52 == f55 + f56（主力 = 大单 + 超大单），f53+f54+f55+f56 == 0；
#   - 占比为百分数数值（-1.5 表示 -1.5%），非小数制；
#   - 历史深度约 120 个交易日（lmt 传更大值无效），仅覆盖近半年。


def to_secid(symbol: str) -> str:
    """'600000.SH'/'000001.SZ'/'830799.BJ' → 东财 secid '1.600000'/'0.000001'/'0.830799'。"""
    code, _, exchange = symbol.strip().partition(".")
    code = code.strip()
    if exchange.upper() == "SH":
        return f"1.{code}"
    # 深市与北交所在东财均为 0 前缀
    return f"0.{code}"


def parse_klines(klines: list[str], symbol: str) -> pl.DataFrame:
    """把东财 klines 字符串列表解析为资金流帧（原始日期，未做 T-1 平移）。

    纯函数，不依赖网络，可单测。畸形行跳过（不抛错、不造数）。
    """
    rows = []
    for line in klines or []:
        parts = line.split(",")
        if len(parts) < 11:
            continue
        try:
            d = date.fromisoformat(parts[0])
            f52, _f53, _f54, f55, f56, f57, _f58, _f59, f60, _f61 = map(float, parts[1:11])
        except (ValueError, TypeError):
            continue
        rows.append({
            "symbol": symbol,
            "date": d,
            "main_net": f52,        # 元，主力净额 = 大单 + 超大单
            "large_net": f55,       # 元，大单
            "super_large_net": f56,  # 元，超大单
            "small_net": _f53,      # 元，小单
            "main_rate": f57,       # %
            "large_rate": f60,      # %
            "small_rate": _f58,     # %
        })
    return pl.DataFrame(rows, schema={
        "symbol": pl.Utf8, "date": pl.Date,
        "main_net": pl.Float64, "large_net": pl.Float64,
        "super_large_net": pl.Float64, "small_net": pl.Float64,
        "main_rate": pl.Float64, "large_rate": pl.Float64, "small_rate": pl.Float64,
    })


def fetch_fflow_daykline(
    secid: str,
    *,
    lmt: int = 500,
    retries: int = 3,
    timeout: int = 15,
) -> list[str] | None:
    """拉单只标的的资金流日线 klines。

    返回：klines 字符串列表；无数据（退市/无记录）返回 []；
    请求失败（限流/封禁/网络）返回 None —— 调用方必须区分这两种空。
    """
    url = (
        f"{FFLOW_DAYKLINE_URL}?lmt={lmt}&klt=101&secid={secid}"
        f"&fields1=f1,f2,f3,f7&fields2={FFLOW_FIELDS2}"
    )
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            out = subprocess.run(
                ["curl", "-s", "--max-time", str(timeout), "-A", UA,
                 "-H", "Referer: https://quote.eastmoney.com/", url],
                capture_output=True, text=True, timeout=timeout + 5,
            ).stdout.strip()
            if not out:
                raise RuntimeError("空响应(可能限流/封禁)")
            data = json.loads(out).get("data")
            if not data or not data.get("klines"):
                return []
            return data["klines"]
        except Exception as exc:  # noqa: BLE001 — 统一退避重试
            last_err = exc
            if attempt < retries - 1:
                time.sleep((2 ** attempt) + random.uniform(0, 0.5))
    if last_err is not None:
        print(f"[eastmoney] fetch fail secid={secid}: {last_err}", flush=True)
    return None


# ===== 数据中心 (datacenter-web) =====
#
# 龙虎榜与融资融券同属东财数据中心报表接口, 均为 **全市场按日** 维度:
#   - RPT_DAILYBILLBOARD_DETAILSNEW: 单日约 60~90 行(含可转债/B股, 需过滤),
#     同一标的可因多条「上榜原因」重复出现;
#   - RPTA_WEB_RZRQ_GGMX: 单日约 4500 行(含 ETF/可转债), pageSize 上限 500。
# 两接口都按 TRADE_DATE/DATE 精确过滤, 历史可回溯, 是天然的日频事件因子来源。

DATACENTER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
DATACENTER_REFERER = "https://data.eastmoney.com/"
LHB_REPORT = "RPT_DAILYBILLBOARD_DETAILSNEW"
MARGIN_REPORT = "RPTA_WEB_RZRQ_GGMX"
DATACENTER_PAGE_SIZE = 500
DATACENTER_MAX_PAGES = 40
DATACENTER_PAGE_SLEEP_S = 0.4

# A 股代码首位: 0/3=深市, 6=沪市, 4/8=北交所; 可转债(11x/12x)、ETF(15x/51x)、
# B 股(900) 的首位不在集合内, 天然被排除。
_A_SHARE_PREFIX = frozenset("03684")
_LHB_STOCK_TYPE_PREFIX = "058"  # 058001001=A股主板/创业/科创, 058001002=B股, 060=可转债


def is_a_share_code(code: str) -> bool:
    """6 位 A 股代码判定（排除可转债/ETF/B 股）。"""
    text = (code or "").strip()
    return len(text) == 6 and text.isdigit() and text[0] in _A_SHARE_PREFIX


def normalize_secucode(raw: object) -> str | None:
    """'600519.SH' / '000001.sz' → '600519.SH'; 非 A 股或格式异常返回 None。"""
    text = str(raw or "").strip().upper()
    code, _, market = text.partition(".")
    if market not in {"SH", "SZ", "BJ"} or not is_a_share_code(code):
        return None
    return f"{code}.{market}"


def _pct_of(amount: float | None, base: float | None) -> float | None:
    """amount / base * 100; 任一为空或 base<=0 时返回 None (不造 0)。"""
    if amount is None or base is None or base <= 0:
        return None
    return amount / base * 100.0


def _float(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_datacenter(
    report_name: str,
    filter_expr: str,
    *,
    sort_columns: str = "",
    sort_types: str = "",
    page_size: int = DATACENTER_PAGE_SIZE,
    max_pages: int = DATACENTER_MAX_PAGES,
    retries: int = 3,
    timeout: int = 20,
) -> list[dict] | None:
    """按日拉取数据中心报表的全部行（自动翻页）。

    返回：行列表（无数据时为 []）；请求失败（限流/封禁/网络/结构异常）返回 None
    —— 调用方必须区分「真的没有」与「拿不到」。
    """
    rows: list[dict] = []
    page = 1
    headers = {"User-Agent": UA, "Referer": DATACENTER_REFERER, "accept": "application/json"}
    while page <= max_pages:
        params = {
            "reportName": report_name,
            "columns": "ALL",
            "pageNumber": page,
            "pageSize": page_size,
            "filter": filter_expr,
        }
        if sort_columns:
            params["sortColumns"] = sort_columns
            params["sortTypes"] = sort_types or "1"
        payload: dict | None = None
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = httpx.get(
                    DATACENTER_URL, params=params, headers=headers, timeout=timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                payload = resp.json()
                break
            except Exception as exc:  # noqa: BLE001 — 统一退避重试
                last_err = exc
                if attempt < retries - 1:
                    time.sleep((2 ** attempt) + random.uniform(0, 0.5))
        if payload is None:
            print(f"[eastmoney] datacenter fail {report_name} p{page}: {last_err}", flush=True)
            return None

        result = payload.get("result") or {}
        data = result.get("data") or []
        if not isinstance(data, list):
            return None
        rows.extend(item for item in data if isinstance(item, dict))
        pages = result.get("pages")
        try:
            total_pages = int(pages or 0)
        except (TypeError, ValueError):
            total_pages = 0
        if not data or page >= total_pages or page >= max_pages:
            break
        page += 1
        time.sleep(DATACENTER_PAGE_SLEEP_S)
    return rows


def fetch_lhb_day(trade_date: date, *, retries: int = 3) -> list[dict] | None:
    """单日龙虎榜明细（全市场一次请求）。"""
    return fetch_datacenter(
        LHB_REPORT,
        f"(TRADE_DATE='{trade_date.isoformat()}')",
        sort_columns="BILLBOARD_NET_AMT",
        sort_types="-1",
        retries=retries,
    )


def fetch_margin_day(trade_date: date, *, retries: int = 3) -> list[dict] | None:
    """单日融资融券个股明细（全市场，约 9 页）。"""
    return fetch_datacenter(
        MARGIN_REPORT,
        f"(DATE='{trade_date.isoformat()}')",
        sort_columns="SCODE",
        sort_types="1",
        retries=retries,
    )


LHB_SCHEMA = {
    "symbol": pl.Utf8,
    "announce_date": pl.Date,
    "lhb_net_amount": pl.Float64,   # 元, 龙虎榜净买入额
    "lhb_buy_amount": pl.Float64,   # 元, 龙虎榜买入额
    "lhb_sell_amount": pl.Float64,  # 元, 龙虎榜卖出额
    "lhb_amount": pl.Float64,       # 元, 当日成交额
    "lhb_net_ratio": pl.Float64,    # %, 净买入额 / 成交额
    "lhb_on_board": pl.Int8,        # 1 = 当日上榜 (因子侧未上榜按 0)
    "lhb_reason_count": pl.Int32,   # 上榜原因条数
}

MARGIN_SCHEMA = {
    "symbol": pl.Utf8,
    "announce_date": pl.Date,
    "margin_balance": pl.Float64,       # 元, 融资余额
    "margin_short_balance": pl.Float64,  # 元, 融券余额
    "margin_buy_amount": pl.Float64,    # 元, 当日融资买入额
    "margin_net_amount": pl.Float64,    # 元, 当日融资净买入额
    "margin_net_5d": pl.Float64,        # 元, 近 5 日融资净买入额
    "float_mv": pl.Float64,             # 元, 流通市值
    "margin_balance_ratio": pl.Float64,  # %, 融资余额 / 流通市值
    "margin_buy_ratio": pl.Float64,      # %, 融资买入额 / 流通市值
    "margin_net_5d_ratio": pl.Float64,   # %, 近 5 日融资净买入额 / 流通市值
    "margin_short_ratio": pl.Float64,    # %, 融券余额 / 流通市值
}


def parse_lhb(rows: list[dict], trade_date: date) -> pl.DataFrame:
    """龙虎榜明细 → 日频因子帧（纯函数，可单测）。

    同一标的常因多条「上榜原因」重复出现（如「日涨幅 15% 前5」与「连续三日累计
    偏离 20%」），口径取**绝对值最大的一条净买入额**为主口径，同时记录上榜原因
    条数；不做求和（不同口径的成交额窗口不同，求和会重复计算）。
    """
    best: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for row in rows or []:
        if not str(row.get("SECURITY_TYPE_CODE") or "").startswith(_LHB_STOCK_TYPE_PREFIX):
            continue
        symbol = normalize_secucode(row.get("SECUCODE"))
        if symbol is None:
            continue
        counts[symbol] = counts.get(symbol, 0) + 1
        net = _float(row.get("BILLBOARD_NET_AMT"))
        current = best.get(symbol)
        if current is None or abs(net or 0.0) > abs(current["lhb_net_amount"] or 0.0):
            best[symbol] = {
                "symbol": symbol,
                "lhb_net_amount": net,
                "lhb_buy_amount": _float(row.get("BILLBOARD_BUY_AMT")),
                "lhb_sell_amount": _float(row.get("BILLBOARD_SELL_AMT")),
                "lhb_amount": _float(row.get("ACCUM_AMOUNT")),
                "lhb_net_ratio": _float(row.get("DEAL_NET_RATIO")),
            }
    records = [
        {
            **item,
            "announce_date": trade_date,
            "lhb_on_board": 1,
            "lhb_reason_count": counts[item["symbol"]],
        }
        for item in best.values()
    ]
    return pl.DataFrame(records, schema=LHB_SCHEMA)


def parse_margin(rows: list[dict], trade_date: date) -> pl.DataFrame:
    """融资融券明细 → 日频因子帧（纯函数，可单测）。

    比例为自身量纲 (占比 %)，避免净额直接参与截面排序被市值主导：
    融资余额占比取上游 ``RZYEZB``；买入/净买入/融券占比以流通市值 ``SZ`` 现算。
    """
    records: list[dict] = []
    for row in rows or []:
        symbol = normalize_secucode(row.get("SECUCODE"))
        if symbol is None:
            continue
        float_mv = _float(row.get("SZ"))
        buy = _float(row.get("RZMRE"))
        net_5d = _float(row.get("RZJME5D"))
        short_balance = _float(row.get("RQYE"))

        records.append({
            "symbol": symbol,
            "announce_date": trade_date,
            "margin_balance": _float(row.get("RZYE")),
            "margin_short_balance": short_balance,
            "margin_buy_amount": buy,
            "margin_net_amount": _float(row.get("RZJME")),
            "margin_net_5d": net_5d,
            "float_mv": float_mv,
            "margin_balance_ratio": _float(row.get("RZYEZB")),
            "margin_buy_ratio": _pct_of(buy, float_mv),
            "margin_net_5d_ratio": _pct_of(net_5d, float_mv),
            "margin_short_ratio": _pct_of(short_balance, float_mv),
        })
    return pl.DataFrame(records, schema=MARGIN_SCHEMA)
