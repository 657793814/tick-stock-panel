"""XTick 数据源 provider。

路由数据集:
  - realtime:    全市场实时快照 (/doc/quant/data, 单次请求)
  - depth5:      五档盘口 (/doc/order/deep, 单次最多 50 只)
  - financial:   财务指标(/doc/gaap)与股本(/doc/capital), 单标的逐股拉取
  - daily:       通用K线 /doc/kline/market (单标的, fq=1 不复权, 日期范围)
  - minute:      通用K线 period=1m (单标的, 按日切片)
  - full_minute: 盘中分钟K /doc/order/minute — **批量端点** (≤50 只/请求,
    上游文档宣称支持 all), 全市场一轮 ≈5600/50=112 请求; 行结构按官方
    SDK bean (Minute: time=北京毫秒时间戳, volume=手, amount=元) 解析,
    批量解析 0 行时自动回退单标的 1m K线, 不静默失败
  - moneyflow:   资金流向历史 /doc/hot/historymoney (单标的区间),
    映射到东财 moneyflow 归一 schema (most/big/small → 超大/大/小单,
    中单无对应目标列丢弃, main=超大+大; 占比上游未提供 → 空值)

**口径警示 (未实测项)**: daily/minute/full_minute/moneyflow 的行结构与单位
依据官方 Java SDK bean 与本仓库前端列定义推定; 本仓库维护者此前实测发现
/doc/core/batchkline 成交量与成交额为常规日 K 的 2 倍 (上游口径异常), 该
端点**不使用**。配置 Token 后请先在设置页对上述数据集「试拉」, 与本地日K
对照核验 volume(手)/amount(元) 单位后再用于全市场回填。

口径 (CONTRIBUTING §3):
  - XTick 时间戳为北京日界毫秒 (daily) 或盘中毫秒 (realtime/minute), 直接透传;
  - 量化因子 x006 成交量单位为手, x007 成交额单位为元;
  - x029 涨幅为百分数数值, 本项目 realtime change_pct 契约是小数制, 显式 /100;
  - 接口返回的裸代码补齐交易所后缀; 无法识别的代码丢弃, 不猜测。
"""
from __future__ import annotations

import contextlib
import logging
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.market_time import cn_today
from app.plugins.xtick.client import (
    XTickClient,
    XTickError,
)
from app.plugins.xtick.client import (
    availability as client_availability,
)
from app.plugins.xtick.client import (
    probe_api_key as _probe_api_key,
)

logger = logging.getLogger(__name__)

_DATASETS = ("realtime", "depth5", "financial", "daily", "minute", "full_minute", "moneyflow")
_QUANT_FIELDS = "x001,x002,x003,x004,x005,x006,x007,x008,x009,x010"
_BATCH_LIMIT = 50
_KLINE_INTERVAL_S = 0.08   # K线类单标的请求节流 (与 tdxquant 分钟档一致)
_MINUTE_SCHEMA = {
    "symbol": pl.String, "datetime": pl.Datetime("us"),
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}
_DAILY_SCHEMA = {
    "symbol": pl.String, "date": pl.Date,
    "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
    "volume": pl.Float64, "amount": pl.Float64,
}
_MONEYFLOW_SCHEMA = {
    "symbol": pl.String, "date": pl.Date,
    "main_net": pl.Float64, "large_net": pl.Float64,
    "super_large_net": pl.Float64, "small_net": pl.Float64,
    "main_rate": pl.Float64, "large_rate": pl.Float64, "small_rate": pl.Float64,
}
# moneyflow_history 行的日期字段候选 (上游未在 SDK bean 中固化, 容错解析)
_MF_DATE_KEYS = ("date", "tradeDate", "tradingDay", "time", "day", "updateTime")
_MF_FLOW_KEYS = {  # 东财归一列 ← (买方字段, 卖方字段), 单位元
    "super_large_net": ("buymostamount", "sellmostamount"),
    "large_net": ("buybigamount", "sellbigamount"),
    "small_net": ("buysmallamount", "sellsmallamount"),
}
_MF_DEFAULT_WINDOW_DAYS = 200
# 单标的财务接口, 逐股请求; 节流避免触发上游限流。
_FINANCIAL_INTERVAL_S = 0.05


@dataclass(frozen=True)
class _XTickConfig:
    datasets: dict[str, int]


def availability() -> tuple[bool, str]:
    return client_availability()


def probe_api_key(api_key: str) -> tuple[bool, str]:
    return _probe_api_key(api_key)


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bare(code: Any) -> str:
    return str(code or "").strip().upper().split(".")[0]


def _symbol(code: Any, asset_type: str = "stock") -> str | None:
    bare = _bare(code)
    if len(bare) != 6 or not bare.isdigit():
        return None
    if asset_type == "index":
        if bare.startswith("000") or bare.startswith("880"):
            return f"{bare}.SH"
        if bare.startswith("399"):
            return f"{bare}.SZ"
        return None
    if bare.startswith(("6", "5", "9")):
        return f"{bare}.SH"
    if bare.startswith(("0", "1", "2", "3")):
        return f"{bare}.SZ"
    if bare.startswith(("4", "8")) or bare.startswith("920"):
        return f"{bare}.BJ"
    return None


def _type_of(asset_type: str) -> int:
    return {"index": 2, "etf": 4, "stock": 1}.get(asset_type, 1)


def _chunk(values: list[str], size: int = _BATCH_LIMIT) -> list[list[str]]:
    return [values[i:i + size] for i in range(0, len(values), size)]


_BEIJING_TZ = timezone(timedelta(hours=8))  # XTick 时间戳口径: GMT+8
_DT_TEXT_RE = re.compile(
    r"^(\d{4})[-/](\d{2})[-/](\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?"
)


def _naive_of(value: Any) -> datetime | None:
    """XTick 行时间 → 北京墙钟 naive datetime。接受毫秒时间戳或常见字符串格式。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=_BEIJING_TZ).replace(tzinfo=None)
        except (ValueError, OSError, OverflowError):
            return None
    match = _DT_TEXT_RE.match(str(value).strip())
    if match is None:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return datetime(
            int(year), int(month), int(day),
            int(hour or 0), int(minute or 0), int(second or 0),
        )
    except ValueError:
        return None


def _date_of(value: Any) -> date_cls | None:
    naive = _naive_of(value)
    return naive.date() if naive is not None else None


def _date_span(start_d: date_cls, end_d: date_cls) -> list[date_cls]:
    """闭区间内的自然日列表 (升序); 起点晚于终点返回空。"""
    if start_d > end_d:
        return []
    span = (end_d - start_d).days
    return [start_d + timedelta(days=i) for i in range(span + 1)]


def _to_naive(value: datetime | None) -> datetime | None:
    """输入 datetime → 北京墙钟 naive; 已是 naive 原样返回。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(_BEIJING_TZ).replace(tzinfo=None)


def _kline_daily_row(symbol: str, row: Any) -> dict | None:
    """/doc/kline/market 行 → 内部日K行 (volume 手 / amount 元, 官方 bean 口径)。"""
    if not isinstance(row, dict):
        return None
    d = _date_of(row.get("time"))
    close = _to_float(row.get("close"))
    if d is None or close is None:
        return None
    high, low = _to_float(row.get("high")), _to_float(row.get("low"))
    return {
        "symbol": symbol,
        "date": d,
        "open": _to_float(row.get("open")),
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": _to_float(row.get("volume")) or 0.0,
        "amount": _to_float(row.get("amount")),
    }


def _kline_minute_row(symbol: str, row: Any, start_dt, end_dt) -> dict | None:
    """/doc/kline/market period=1m 行 → 内部分钟行 (结构同日K, time 为盘中毫秒)。"""
    if not isinstance(row, dict):
        return None
    dt = _naive_of(row.get("time"))
    close = _to_float(row.get("close"))
    if dt is None or close is None:
        return None
    if start_dt is not None and dt < start_dt:
        return None
    if end_dt is not None and dt > end_dt:
        return None
    high, low = _to_float(row.get("high")), _to_float(row.get("low"))
    return {
        "symbol": symbol,
        "datetime": dt,
        "open": _to_float(row.get("open")),
        "high": high if high is not None else close,
        "low": low if low is not None else close,
        "close": close,
        "volume": _to_float(row.get("volume")) or 0.0,
        "amount": _to_float(row.get("amount")),
    }


def _unwrap_rows(payload: Any) -> list:
    """批量端点响应 → 行列表。容忍裸 list 与 {"data": [...]} 两种包裹。"""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("data", "list", "rows", "result"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return inner
    return []


def _moneyflow_row(symbol: str, row: Any) -> dict | None:
    """moneyflow_history 行 → 东财归一 moneyflow 行; 日期或金额字段缺失返回 None。"""
    if not isinstance(row, dict):
        return None
    d = None
    for key in _MF_DATE_KEYS:
        d = _date_of(row.get(key))
        if d is not None:
            break
    if d is None:
        return None
    record: dict[str, Any] = {"symbol": symbol, "date": d}
    for target, (buy_key, sell_key) in _MF_FLOW_KEYS.items():
        buy, sell = _to_float(row.get(buy_key)), _to_float(row.get(sell_key))
        if buy is None and sell is None:
            return None  # 关键金额字段缺失 → 行结构不符合预期
        record[target] = (buy or 0.0) - (sell or 0.0)
    record["main_net"] = (record["super_large_net"] or 0.0) + (record["large_net"] or 0.0)
    record["main_rate"] = None   # 上游未提供占比, 不启发式补全
    record["large_rate"] = None
    record["small_rate"] = None
    return record


# XTick 财务指标 (/doc/gaap) → 项目 canonical 列 (TickFlow 口径, 由
# backtest/fundamentals.FUNDAMENTAL_FACTORS 与前端财务页消费)。
# 仅映射语义已由上游字段名+实测样本确认的指标; 其余字段不猜口径, 不透传。
_METRICS_FIELD_MAP = {
    "basicEps": "eps_basic",
    "bps": "bps",
    "revenueInc": "revenue_yoy",        # 营业收入同比 (%)
    "profitInc": "net_income_yoy",      # 净利润同比 (%)
    "roe": "roe",                       # 净资产收益率 (%)
    "grossProfit": "gross_margin",      # 销售毛利率 (%)
    "netProfit": "net_margin",          # 销售净利率 (%)
    "gearRatio": "debt_to_asset_ratio",  # 资产负债率 (%)
}
_GAAP_HISTORY_START = "2007-01-01"      # 上游 gaap 数据起点
_CAPITAL_HISTORY_START = "2015-01-01"   # 股本表按需回补起点


def _realtime_row(row: dict, fetched_ms: int, asset_type: str = "stock") -> dict | None:
    symbol = _symbol(row.get("code"), asset_type)
    if symbol is None:
        return None
    last = _to_float(row.get("last_price"))
    prev = _to_float(row.get("prev_close"))
    change = _to_float(row.get("change_amount"))
    if change is None and last is not None and prev is not None:
        change = last - prev
    change_pct = _to_float(row.get("change_pct"))
    if change_pct is not None:
        change_pct /= 100.0  # XTick 为百分数数值 (x029: -1.77), 项目契约为小数制
    elif change is not None and prev not in (None, 0):
        change_pct = change / prev
    return {
        "symbol": symbol,
        "name": row.get("name"),
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(row.get("open")),
        "high": _to_float(row.get("high")),
        "low": _to_float(row.get("low")),
        "volume": _to_float(row.get("volume")),  # XTick 单位为手, 与项目契约一致
        "amount": _to_float(row.get("amount")),
        "change_pct": change_pct,
        "change_amount": change,
        "amplitude": None,  # 单位口径未在本接口确认, 不启发式补全
        "turnover_rate": None,
        "timestamp": fetched_ms,
        "session": None,
    }


class XTickProvider:
    """XTick 内置插件 provider。"""

    name = "xtick"
    builtin = True

    def __init__(self) -> None:
        self.config = _XTickConfig({name: 1 for name in _DATASETS})
        self._client: XTickClient | None = None

    def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> XTickClient:
        if self._client is None:
            self._client = XTickClient()
        return self._client

    def raw(self, kind: str, params: dict[str, Any] | None = None) -> Any:
        """专用能力直取; params 不含 token, 由 client 注入。"""
        return self._get_client().raw(kind, params)

    # ---- 路由数据集 ----
    def get_realtime(self) -> list[dict]:
        """全市场实时快照。失败软返回 [], 不阻断轮询线程。"""
        try:
            payload = self.raw("quant_realtime", {"type": 1, "field": _QUANT_FIELDS})
        except XTickError as exc:
            logger.warning("XTick 实时快照失败: %s", exc)
            return []
        columns = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(columns, dict):
            logger.warning("XTick 实时快照结构异常: data 不是对象")
            return []
        codes = columns.get("code") or []
        if not isinstance(codes, list):
            return []
        fetched_ms = int(time.time() * 1000)

        def at(field: str, index: int) -> Any:
            values = columns.get(field)
            return values[index] if isinstance(values, list) and index < len(values) else None

        records: list[dict] = []
        for index, code in enumerate(codes):
            row = {
                "code": code,
                "last_price": at("x002", index),
                "prev_close": at("x001", index),
                "open": at("x003", index),
                "high": at("x004", index),
                "low": at("x005", index),
                "volume": at("x006", index),
                "amount": at("x007", index),
                "change_amount": at("x008", index),
                "change_pct": None,
            }
            record = _realtime_row(row, fetched_ms)
            if record is not None:
                records.append(record)
        if not records and codes:
            logger.warning("XTick 实时快照 %d 行均无法识别代码, 疑似接口结构变化", len(codes))
        return records

    def _realtime_by_symbols(
        self, symbols: list[str], asset_type: str
    ) -> list[dict] | None:
        wanted = [_bare(s) for s in symbols if _bare(s)]
        if not wanted:
            return []
        requested_type = 2 if asset_type == "index" else _type_of(asset_type)
        rows: list[dict] = []
        try:
            for batch in _chunk(wanted):
                payload = self.raw("realtime_day", {"type": requested_type, "code": ",".join(batch)})
                rows.extend(payload if isinstance(payload, list) else [])
        except XTickError as exc:
            logger.warning("XTick 补拉 %s 实时行情失败: %s", asset_type, exc)
            return None
        fetched_ms = int(time.time() * 1000)
        records = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            record = _realtime_row(row, fetched_ms, asset_type)
            if record is not None:
                records.append(record)
        return records

    def get_realtime_symbols(
        self, symbols: list[str], *, asset_type: str = "stock"
    ) -> list[dict] | None:
        return self._realtime_by_symbols(symbols, asset_type)

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        return self._realtime_by_symbols(symbols, "index")

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        wanted = [_bare(s) for s in symbols if _bare(s)]
        if not wanted:
            return {}
        result: dict[str, dict] = {}
        try:
            for batch in _chunk(wanted):
                payload = self.raw("depth", {"type": 1, "code": ",".join(batch)})
                rows = payload if isinstance(payload, list) else []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    symbol = _symbol(row.get("code"))
                    if symbol is None:
                        continue
                    result[symbol] = {
                        "bid_prices": [_to_float(row.get(f"bp{i}")) for i in range(1, 6)],
                        "bid_volumes": [_to_float(row.get(f"bv{i}")) for i in range(1, 6)],
                        "ask_prices": [_to_float(row.get(f"sp{i}")) for i in range(1, 6)],
                        "ask_volumes": [_to_float(row.get(f"sv{i}")) for i in range(1, 6)],
                        "timestamp": row.get("time"),
                    }
        except XTickError as exc:
            logger.warning("XTick 五档盘口失败: %s", exc)
            raise ProviderRequestError(f"XTick 五档盘口失败: {exc}") from None
        return result

    # ---- K 线路由数据集 (daily / minute / full_minute, 通用K线 + 批量分钟) ----

    def _kline_daily_parts(
        self, symbol: str, start_d: date_cls, end_d: date_cls, asset_type: str,
    ) -> list[dict]:
        payload = self.raw("kline", {
            "type": _type_of(asset_type), "code": _bare(symbol), "fq": 1,
            "period": "1d", "startDate": start_d.isoformat(), "endDate": end_d.isoformat(),
        })
        items = payload if isinstance(payload, list) else []
        return [row for row in (_kline_daily_row(symbol, item) for item in items) if row is not None]

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """单标的通用K线日K (fq=1 不复权, 日期范围), 逐标的节流。"""
        end_d = (end_time or datetime.now()).date()
        start_d = start_time.date() if start_time else end_d - timedelta(days=365)
        start_d = min(start_d, end_d)
        parts: list[pl.DataFrame] = []
        for i, symbol in enumerate(symbols):
            if not _bare(symbol):
                continue
            try:
                rows = self._kline_daily_parts(symbol, start_d, end_d, asset_type)
            except XTickError as exc:
                raise ProviderRequestError(f"XTick 日K {symbol} 请求失败: {exc}") from None
            if rows:
                parts.append(pl.DataFrame(rows, schema=_DAILY_SCHEMA))
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
            time.sleep(_KLINE_INTERVAL_S)
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_DAILY_SCHEMA)

    def _kline_minute_day(self, symbol: str, day: date_cls, asset_type: str) -> list[dict]:
        payload = self.raw("kline", {
            "type": _type_of(asset_type), "code": _bare(symbol), "fq": 1,
            "period": "1m", "startDate": day.isoformat(), "endDate": day.isoformat(),
        })
        items = payload if isinstance(payload, list) else []
        return [row for row in (_kline_minute_row(symbol, item, None, None) for item in items) if row is not None]

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        """单标的 1 分钟K (通用K线按日切片), 逐标的×逐日节流。仅支持 freq=1m。"""
        if freq != "1m":
            if on_chunk_done:
                on_chunk_done(0, 0)
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        end_dt = _to_naive(end_time) or datetime.now()
        start_dt = _to_naive(start_time) or (end_dt - timedelta(days=7))
        start_dt = min(start_dt, end_dt)
        days = _date_span(start_dt.date(), end_dt.date())
        parts: list[pl.DataFrame] = []
        done = 0
        total = max(len(symbols) * max(len(days), 1), 1)
        for symbol in symbols:
            if not _bare(symbol):
                continue
            rows: list[dict] = []
            for day in days:
                try:
                    rows.extend(self._kline_minute_day(symbol, day, asset_type))
                except XTickError as exc:
                    raise ProviderRequestError(f"XTick 分钟K {symbol} {day} 请求失败: {exc}") from None
                time.sleep(_KLINE_INTERVAL_S)
            if rows:
                parts.append(pl.DataFrame(rows, schema=_MINUTE_SCHEMA))
            done += max(len(days), 1)
            if on_chunk_done:
                on_chunk_done(done, total)
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_MINUTE_SCHEMA)

    def get_intraday_batch(
        self, symbols: list[str], count: int = 300, asset_type: str = "stock",
    ) -> pl.DataFrame:
        """全量分钟修复轮: 批量分钟端点 /doc/order/minute (≤50 只/请求) 优先。

        行结构按官方 SDK bean (Minute: time=北京毫秒, volume=手, amount=元)
        解析, 只保留当日行; 批量解析 0 行 (结构异常) 时自动回退单标的
        1m K线, 不静默失败。count 语义同 tdxquant: 上游按当日全量返回,
        不按 count 截断。
        """
        if not symbols or asset_type != "stock":
            return pl.DataFrame(schema=_MINUTE_SCHEMA)
        today = cn_today()
        day_start = datetime(today.year, today.month, today.day)
        day_end = day_start + timedelta(days=1)
        client_rows: list[dict] = []
        batch_ok = False
        try:
            wanted = [s for s in symbols if _bare(s)]
            for batch in _chunk(wanted):
                payload = self.raw("realtime_minute", {"type": 1, "code": ",".join(batch)})
                client_rows.extend(_unwrap_rows(payload))
                time.sleep(_KLINE_INTERVAL_S)
            batch_ok = True
        except XTickError as exc:
            logger.warning("XTick 批量分钟失败, 回退单标的 1m K线: %s", exc)
        rows: list[dict] = []
        for item in client_rows:
            if not isinstance(item, dict):
                continue
            symbol = _symbol(str(item.get("code") or ""), "stock")
            if symbol is None:
                continue  # 无法识别的代码丢弃, 不猜测 (口径: CONTRIBUTING §3)
            row = _kline_minute_row(symbol, item, day_start, day_end)
            if row is not None:
                rows.append(row)
        if not rows:
            if batch_ok and client_rows:
                logger.warning(
                    "XTick 批量分钟 %d 行均无法解析 (疑似结构变化), 回退单标的 1m K线",
                    len(client_rows),
                )
            rows = self._intraday_batch_fallback(symbols, day_start, day_end)
        return pl.DataFrame(rows, schema=_MINUTE_SCHEMA)

    def _intraday_batch_fallback(
        self, symbols: list[str], day_start: datetime, day_end: datetime,
    ) -> list[dict]:
        """批量分钟不可用时的修复轮回退: 单标的 1m K线当日窗口。"""
        today = day_start.date()
        rows: list[dict] = []
        for symbol in symbols:
            if not _bare(symbol):
                continue
            try:
                for day_row in self._kline_minute_day(symbol, today, "stock"):
                    dt = day_row["datetime"]
                    if day_start <= dt < day_end:
                        rows.append(day_row)
            except XTickError as exc:
                logger.warning("XTick 修复轮回退 %s 拉取失败: %s", symbol, exc)
                continue
            time.sleep(_KLINE_INTERVAL_S)
        return rows

    def get_moneyflow(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> pl.DataFrame:
        """资金流向历史 (单标的区间) → 东财归一 moneyflow schema。

        映射: most→超大单, big→大单, small→小单 (中单无目标列, 丢弃);
        main = 超大 + 大; 上游未提供占比 → 空值。行结构与日期字段名未实测,
        解析失败按 fail-closed 抛错, 不产出错误 schema 的因子。
        """
        end_d = (end_time or datetime.now()).date()
        start_d = (start_time.date() if start_time else end_d - timedelta(days=_MF_DEFAULT_WINDOW_DAYS))
        start_d, end_d = min(start_d, end_d), max(start_d, end_d)
        parts: list[pl.DataFrame] = []
        for symbol in symbols:
            bare = _bare(symbol)
            if not bare:
                continue
            try:
                payload = self.raw("moneyflow_history", {
                    "code": bare, "startDate": start_d.isoformat(), "endDate": end_d.isoformat(),
                })
            except XTickError as exc:
                raise ProviderRequestError(f"XTick 资金流 {symbol} 请求失败: {exc}") from None
            rows_in = _unwrap_rows(payload)
            rows = [row for row in (_moneyflow_row(symbol, item) for item in rows_in) if row is not None]
            if rows_in and not rows:
                raise ProviderRequestError(
                    f"XTick 资金流 {symbol} 返回 {len(rows_in)} 行均无法解析 (疑似结构变化), fail-closed"
                )
            if rows:
                parts.append(pl.DataFrame(rows, schema=_MONEYFLOW_SCHEMA))
            time.sleep(_KLINE_INTERVAL_S)
        if not parts:
            return pl.DataFrame(schema=_MONEYFLOW_SCHEMA)
        return pl.concat(parts, how="diagonal_relaxed").sort(["symbol", "date"])

    # ---- 财务数据 (financial 数据集, 单标的逐股拉取) ----
    def get_financials(
        self,
        table: str,
        symbols: list[str],
        latest_only: bool = True,
    ) -> pl.DataFrame:
        """拉取财务指标/股本, 输出项目 canonical 列。

        - metrics ← /doc/gaap (reportDate/publicDate + 核心指标);
        - shares  ← /doc/capital (总股本/流通股本/限售股本);
        - 三大报表上游无对应端点, 返回空表, 不伪造。
        """
        if table not in ("metrics", "shares"):
            logger.info("XTick 无 %s 表端点, 跳过 (已有数据保留)", table)
            return pl.DataFrame()
        wanted = [s for s in symbols if _bare(s)]
        if not wanted:
            return pl.DataFrame()

        today = cn_today()
        if latest_only:
            # 覆盖最近 1~3 个报告期, 与 sync 的「最新期」语义一致
            start = (today - timedelta(days=400 if table == "metrics" else 800)).isoformat()
        else:
            start = _GAAP_HISTORY_START if table == "metrics" else _CAPITAL_HISTORY_START
        end = today.isoformat()
        kind = "gaap" if table == "metrics" else "capital"

        rows_out: list[dict] = []
        for index, symbol in enumerate(wanted):
            if index:
                time.sleep(_FINANCIAL_INTERVAL_S)
            try:
                raw = self.raw(kind, {
                    "code": _bare(symbol), "startDate": start, "endDate": end,
                })
            except XTickError as exc:
                logger.warning("XTick 财务 %s %s 失败: %s", table, symbol, exc)
                continue
            for row in raw if isinstance(raw, list) else []:
                if not isinstance(row, dict):
                    continue
                period_end = str(row.get("reportDate") or "")[:10]
                if not period_end:
                    continue
                announce = str(row.get("publicDate") or "")[:10]
                record: dict[str, Any] = {
                    "symbol": symbol,
                    "period_end": period_end,
                    "announce_date": announce or None,
                }
                if table == "metrics":
                    for source, target in _METRICS_FIELD_MAP.items():
                        record[target] = _to_float(row.get(source))
                else:
                    record["total_shares"] = _to_float(row.get("totalCapital"))
                    record["float_shares"] = _to_float(row.get("flowCapital"))
                    record["restricted_shares"] = _to_float(row.get("restrictCapital"))
                rows_out.append(record)
        # 稀疏列 (银行/券商缺毛利率等) 全空时默认采样会推断为 Null, 后续数值写入失败
        return pl.DataFrame(rows_out, infer_schema_length=None) if rows_out else pl.DataFrame()

    def get_instruments(self, asset_type: str = "stock") -> list[dict]:
        symbol_map = {
            "stock": "all", "index": "index", "etf": "etf",
        }.get(asset_type, "all")
        try:
            rows = self.raw("stock_info", {"symbol": symbol_map})
        except XTickError as exc:
            logger.warning("XTick 标的列表失败: %s", exc)
            return []
        result: list[dict] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            code = row.get("code")
            symbol = _symbol(code, asset_type if asset_type == "index" else "stock")
            if symbol is None:
                continue
            result.append({
                "symbol": symbol,
                "name": row.get("name"),
                "code": _bare(code),
                "exchange": symbol.rsplit(".", 1)[-1],
                "asset_type": asset_type,
                "source": self.name,
            })
        return result

    def test_dataset(self, dataset: str, symbols: list[str] | None = None) -> dict:
        """设置页「试拉」入口。"""
        from datetime import timedelta as _td

        try:
            if dataset == "realtime":
                rows = self.get_realtime()
            elif dataset == "depth5":
                sample = symbols or ["000001"]
                rows = list(self.get_depth_batch(sample).items())
            elif dataset == "daily":
                sample = symbols or ["000001"]
                end = datetime.now()
                df = self.get_daily(sample[:3], end - _td(days=30), end)
                rows = df.to_dicts()
            elif dataset == "minute":
                sample = symbols or ["000001"]
                end = datetime.now()
                df = self.get_minute(sample[:3], end - _td(days=3), end)
                rows = df.to_dicts()
            elif dataset == "full_minute":
                df = self.get_intraday_batch((symbols or ["000001"])[:10])
                rows = df.to_dicts()
            elif dataset == "moneyflow":
                sample = symbols or ["000001"]
                df = self.get_moneyflow(sample[:3])
                rows = df.to_dicts()
            else:
                return {
                    "provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [],
                    "error": "XTick 未声明该路由数据集, 将回退原数据源",
                }
        except ProviderRequestError as exc:
            return {
                "provider": self.name, "dataset": dataset, "rows": 0,
                "columns": [], "preview": [], "error": str(exc),
            }
        return {
            "provider": self.name,
            "dataset": dataset,
            "rows": len(rows),
            "columns": sorted({k for row in rows[:20] for k in row}) if rows else [],
            "preview": rows[:5],
        }
