"""腾讯行情 provider (免 token 公开接口)。

路由数据集 (全部免 token, 公开接口无 SLA):
  - minute:  ifzq mkline, 单标的 m1, 实测约 482 根 (≈2 交易日), 不支持日期范围参数。
    历史深度浅是上游硬限制, 不能当作全量分钟回补源; 逐日增量落盘可累积。
  - realtime: qt.gtimg.cn 批量快照, 单请求 ≤50 只 (按标的列表拉取)。
    上游无法枚举全市场标的 → get_realtime() 不带 symbols 时明确抛错 (fail-closed),
    全市场快照轮询请路由到可枚举的源; 自选补拉 (get_realtime_symbols) 与
    指数 (get_realtime_indices) 正常可用。
  - depth5:  快照自带买卖五档 (索引 9-28), 批量 ≤50 只/请求, 按需拉取。
  - daily:   ifzq fqkline, 单标的 count 模式 (实测 ≤700 根), fq="" 不复权
    与除权因子管线对齐; 行序 [日期, 开, 收, 高, 低, 量(手)], 收在第三位。

口径 (CONTRIBUTING §3):
  - mkline 行序 [时间, 开, 收, 高, 低, 量(手), {}, 额]: 收在第三位, 不可按 OHLC 顺序读;
  - datetime 为北京墙钟 YYYYMMDDHHMM → naive Datetime("us"), 满足分钟时区守卫;
  - volume 单位手 (与日 K 对齐, 实测 vol 求和与日 K 完全一致);
  - 分钟 amount 字段口径未确认, 用 volume×100×close 推算成交额(元), 不伪称上游原值;
    快照成交额为字段 37 (万元→元), 日K 无成交额字段, 同样按量×价推算;
  - 上游只返回池化尾部数据, 按 start/end 窗口过滤; 超出深度的行自然缺失, 不伪造。
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl

from app.data_providers.base import ProviderRequestError
from app.plugins.tencent.client import (
    TencentClient,
    TencentError,
    _to_float,
    from_code,
    minute_datetime,
    to_code,
)

logger = logging.getLogger(__name__)

_DATASETS = ("minute", "realtime", "depth5", "daily")
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
_MINUTE_INTERVAL_S = 0.12   # 公开接口无 SLA, 逐标的保守节流
_MINUTE_OUTPUT_SYMBOLS = 50
_MINUTE_OUTPUT_ROWS = 100_000
_DAILY_INTERVAL_S = 0.12    # 日K同档; 全市场一轮 5600+ 请求, 适合增量/小池子
_DAILY_SLICE_DAYS = 450     # 日K按日历日切片拉取, 每片 ≈300 根, 上限实测 700 根以内
_DAILY_MAX_COUNT = 700      # 实测单次 count=700 正常返回
_DAILY_OUTPUT_SYMBOLS = 50
_DAILY_OUTPUT_ROWS = 50_000
_REALTIME_FIELDS = 39
_QUOTE_CHUNK = 50           # qt.gtimg.cn 单请求代码数上限 (client 内同样截断)
_DEPTH_BID_IDX = ((9, 10), (11, 12), (13, 14), (15, 16), (17, 18))   # 买一~买五 (价, 量)
_DEPTH_ASK_IDX = ((19, 20), (21, 22), (23, 24), (25, 26), (27, 28))  # 卖一~卖五 (价, 量)


@dataclass(frozen=True)
class _TencentConfig:
    datasets: dict[str, int]


def availability() -> tuple[bool, str]:
    """免 token 且无本地依赖: 常驻可用; 取数失败由 provider 自身 fail-closed。"""
    return True, "ok"


def probe_api_key(_api_key: str) -> tuple[bool, str]:
    """兼容设置页通用探测入口: 该源无需 Key。"""
    return True, "该数据源无需 API Key"


def _to_beijing_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None) if value.tzinfo else value


def _minute_rows(
    symbol: str,
    raw: list[list],
    start_dt: datetime | None,
    end_dt: datetime | None,
) -> list[dict]:
    """mkline 原始行 → 内部分钟行。失败不抛, 由调用方对空结果做显式判定。"""
    out: list[dict] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 6:
            continue
        dt = minute_datetime(row[0])
        if dt is None:
            continue
        if start_dt is not None and dt < start_dt:
            continue
        if end_dt is not None and dt > end_dt:
            continue
        close = _to_float(row[2])  # 行序: 时间, 开, 收, 高, 低, 量...
        volume = _to_float(row[5])  # 手
        if close is None or volume is None:
            continue
        high, low = _to_float(row[3]), _to_float(row[4])
        out.append({
            "symbol": symbol,
            "datetime": dt,
            "open": _to_float(row[1]),
            "high": high if high is not None else close,
            "low": low if low is not None else close,
            "close": close,
            "volume": volume,
            "amount": volume * 100.0 * close,  # 手→股×收盘价, 成交额(元)推算
        })
    return out


def _real_row(code: str, fields: list[str], fetched_ms: int) -> dict | None:
    """qt.gtimg.cn 字段 → 项目 realtime 行。索引对照见 client.py 模块注释。"""
    if len(fields) < _REALTIME_FIELDS:
        return None
    mapped = from_code(code)
    if mapped is None:
        return None
    symbol = mapped[0]
    last = _to_float(fields[3])
    prev = _to_float(fields[4])
    change_pct = _to_float(fields[32])
    if change_pct is not None:
        change_pct /= 100.0  # 上游百分数数值 → 项目小数制
    return {
        "symbol": symbol,
        "name": fields[1] or None,
        "last_price": last,
        "prev_close": prev,
        "open": _to_float(fields[5]),
        "high": _to_float(fields[33]),
        "low": _to_float(fields[34]),
        "volume": _to_float(fields[6]),
        "amount": _to_float(fields[37]) * 10_000.0 if _to_float(fields[37]) is not None else None,
        "change_pct": change_pct,
        "change_amount": _to_float(fields[31]),
        "amplitude": None,
        "turnover_rate": _to_float(fields[38]) / 100.0 if _to_float(fields[38]) is not None else None,
        "timestamp": fetched_ms,
        "session": None,
    }


def _quote_timestamp_ms(text: str) -> int | None:
    """快照字段 30 (北京墙钟 YYYYMMDDHHMMSS) → unix 毫秒; 解析失败返回 None。"""
    try:
        dt = datetime.strptime(str(text).strip(), "%Y%m%d%H%M%S")
    except (ValueError, TypeError):
        return None
    return int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp() * 1000)


def _depth_row(code: str, fields: list[str]) -> dict | None:
    """qt.gtimg.cn 快照字段 → 项目五档行 (买 9-18 / 卖 19-28, 量单位手)。"""
    if len(fields) < _REALTIME_FIELDS:
        return None
    mapped = from_code(code)
    if mapped is None:
        return None
    symbol, _asset_type = mapped

    def level(price_idx: int, vol_idx: int) -> tuple[float | None, float | None]:
        return _to_float(fields[price_idx]), _to_float(fields[vol_idx])

    bid = [level(p, v) for p, v in _DEPTH_BID_IDX]
    ask = [level(p, v) for p, v in _DEPTH_ASK_IDX]
    # 买卖两侧价格全为空/0 (如无盘口时段) → 不造数
    if (all(not p for p, _ in bid) and all(not p for p, _ in ask)):
        return None
    return {
        "symbol": symbol,
        "bid_prices": [p for p, _ in bid],
        "bid_volumes": [v for _, v in bid],
        "ask_prices": [p for p, _ in ask],
        "ask_volumes": [v for _, v in ask],
        "timestamp": _quote_timestamp_ms(fields[30]) if len(fields) > 30 else None,
    }


def _daily_rows(
    symbol: str,
    raw: list[list],
    start_d,
    end_d,
) -> list[dict]:
    """fqkline 原始行 → 内部日K行。行序 [日期, 开, 收, 高, 低, 量(手)], 收在第三位。

    上游无成交额字段, 用 volume×100×close 推算(元), 与分钟口径一致, 不伪称上游原值。
    """
    from datetime import date as date_cls

    out: list[dict] = []
    for row in raw:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            d = date_cls.fromisoformat(str(row[0]).strip()[:10])
        except (ValueError, TypeError):
            continue
        if start_d is not None and d < start_d:
            continue
        if end_d is not None and d > end_d:
            continue
        close = _to_float(row[2])
        volume = _to_float(row[5])
        if close is None or volume is None:
            continue
        high, low = _to_float(row[3]), _to_float(row[4])
        out.append({
            "symbol": symbol,
            "date": d,
            "open": _to_float(row[1]),
            "high": high if high is not None else close,
            "low": low if low is not None else close,
            "close": close,
            "volume": volume,
            "amount": volume * 100.0 * close,  # 手→股×收盘价, 成交额(元)推算
        })
    return out


def _daily_slices(start_d, end_d) -> list[tuple]:
    """窗口按 _DAILY_SLICE_DAYS 切片 (升序), 避免单请求超出上游深度上限被静默截断。"""
    from datetime import timedelta

    slices: list[tuple] = []
    cursor = start_d
    while cursor <= end_d:
        slice_end = min(cursor + timedelta(days=_DAILY_SLICE_DAYS - 1), end_d)
        slices.append((cursor, slice_end))
        cursor = slice_end + timedelta(days=1)
    return slices


class TencentProvider:
    """腾讯内置插件 provider。minute/realtime/depth5/daily 走路由, 分时等按需直取。"""

    name = "tencent"
    builtin = True

    def __init__(self) -> None:
        self.config = _TencentConfig({name: 1 for name in _DATASETS})
        self._client: TencentClient | None = None

    def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                self._client.close()
            self._client = None

    def _get_client(self) -> TencentClient:
        if self._client is None:
            self._client = TencentClient()
        return self._client

    def raw(self, kind: str, params: dict[str, Any] | None = None) -> Any:
        """非路由能力直取: quote / minute_kline / day_intraday / daily_kline。"""
        params = params or {}
        client = self._get_client()
        if kind == "quote":
            symbols = params.get("symbols") or []
            return [item for item in (
                _real_row(row["code"], row["fields"], int(time.time() * 1000))
                for row in client.quote(symbols)
            ) if item is not None]
        if kind == "minute_kline":
            return client.minute_kline(
                str(params.get("code", "")), str(params.get("freq", "1m")),
                int(params.get("count", 1000)),
            )
        if kind == "day_intraday":
            return client.day_intraday(str(params.get("code", "")))
        if kind == "daily_kline":
            return client.daily_kline(str(params.get("code", "")), int(params.get("count", 120)))
        raise TencentError(f"未知腾讯接口类型: {kind}")

    # ---- 路由数据集 ----
    def _quote_rows(self, symbols: list[str]) -> list[dict]:
        """批量快照 → realtime 行。client.quote 接收项目 symbol (内部转腾讯代码),
        单请求 ≤50 只由 client 截断, 这里按批节流; 单批失败整批抛错 (fail-closed)。"""
        client = self._get_client()
        targets = [s for s in symbols if s]
        rows: list[dict] = []
        fetched_ms = int(time.time() * 1000)
        for i in range(0, len(targets), _QUOTE_CHUNK):
            batch = targets[i:i + _QUOTE_CHUNK]
            try:
                raw = client.quote(batch)
            except TencentError as exc:
                raise ProviderRequestError(f"腾讯快照请求失败 (批 {i // _QUOTE_CHUNK + 1}): {exc}") from None
            for item in raw:
                row = _real_row(item["code"], item["fields"], fetched_ms)
                if row is not None:
                    rows.append(row)
            if i + _QUOTE_CHUNK < len(targets):
                time.sleep(_MINUTE_INTERVAL_S)
        return rows

    def get_realtime(
        self,
        universes: list[str] | None = None,
        symbols: list[str] | None = None,
    ) -> list[dict]:
        """批量实时快照 (≤50 只/请求)。

        上游无法枚举全市场: 不带 symbols/universes 调用时明确抛错,
        全市场快照轮询请路由到可枚举的源 (fail-closed, 不静默返回空)。
        """
        targets = [s for s in (*(symbols or []), *(universes or [])) if s]
        if not targets:
            raise ProviderRequestError(
                "腾讯源无法枚举全市场标的 (需显式代码列表), 全市场快照请路由到可枚举的源"
            )
        return self._quote_rows(list(dict.fromkeys(targets)))

    def get_realtime_symbols(
        self, symbols: list[str], *, asset_type: str = "stock"
    ) -> list[dict]:
        """自选/补拉协议: 按标的列表批量快照 (quote_service 缺口补拉入口)。"""
        if not symbols:
            return []
        return self._quote_rows(list(dict.fromkeys(symbols)))

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """核心四只指数快照 (qt.gtimg.cn 支持指数代码)。失败返回 None (调用方兜底)。"""
        if not symbols:
            return None
        try:
            return self._quote_rows(list(dict.fromkeys(symbols)))
        except ProviderRequestError as exc:
            logger.warning("腾讯指数快照失败: %s", exc)
            return None

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """五档盘口 (快照自带买卖五档)。单请求 ≤50 只, 逐批节流, 失败 fail-closed。"""
        targets = [s for s in symbols if s]
        if not targets:
            return {}
        client = self._get_client()
        result: dict[str, dict] = {}
        for i in range(0, len(targets), _QUOTE_CHUNK):
            batch = targets[i:i + _QUOTE_CHUNK]
            try:
                raw = client.quote(batch)
            except TencentError as exc:
                raise ProviderRequestError(f"腾讯五档请求失败 (批 {i // _QUOTE_CHUNK + 1}): {exc}") from None
            for item in raw:
                row = _depth_row(item["code"], item["fields"])
                if row is not None:
                    result[row["symbol"]] = {
                        "bid_prices": row["bid_prices"],
                        "bid_volumes": row["bid_volumes"],
                        "ask_prices": row["ask_prices"],
                        "ask_volumes": row["ask_volumes"],
                        "timestamp": row["timestamp"],
                    }
            if i + _QUOTE_CHUNK < len(targets):
                time.sleep(_MINUTE_INTERVAL_S)
        return result

    def get_daily(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        chunks = list(self.iter_daily(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, on_chunk_done=on_chunk_done,
        ))
        parts = [df for df in chunks if not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_DAILY_SCHEMA)

    def iter_daily(
        self,
        symbols: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
        asset_type: str = "stock",
        freq: str = "1d",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        """单标的 count 模式日K (不复权), 窗口按日历日切片, 逐标的节流流式产出。"""
        from datetime import timedelta

        if not symbols or asset_type not in ("stock", "etf", "index"):
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        end_d = (end_time or datetime.now()).date()
        start_d = start_time.date() if start_time is not None else end_d - timedelta(days=365)
        start_d = min(start_d, end_d)
        slices = _daily_slices(start_d, end_d)
        client = self._get_client()
        parts: list[pl.DataFrame] = []
        pending = 0
        for i, symbol in enumerate(symbols):
            code = to_code(symbol, asset_type)
            if code is None:
                if on_chunk_done:
                    on_chunk_done(i + 1, len(symbols))
                continue
            rows: list[dict] = []
            for slice_start, slice_end in slices:
                count = min((slice_end - slice_start).days + 15, _DAILY_MAX_COUNT)
                try:
                    raw = client.daily_kline(code, count, fq="")
                except TencentError as exc:
                    raise ProviderRequestError(f"腾讯日K {symbol} 请求失败: {exc}") from None
                rows.extend(_daily_rows(symbol, raw, slice_start, slice_end))
                time.sleep(_DAILY_INTERVAL_S)
            part = pl.DataFrame(rows, schema=_DAILY_SCHEMA)
            if not part.is_empty():
                parts.append(part)
                pending += part.height
            if len(parts) >= _DAILY_OUTPUT_SYMBOLS or pending >= _DAILY_OUTPUT_ROWS:
                yield pl.concat(parts, how="diagonal_relaxed")
                parts, pending = [], 0
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
        if parts:
            yield pl.concat(parts, how="diagonal_relaxed")

    def get_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> pl.DataFrame:
        chunks = list(self.iter_minute(
            symbols, start_time=start_time, end_time=end_time,
            asset_type=asset_type, freq=freq, on_chunk_done=on_chunk_done,
        ))
        parts = [df for df in chunks if not df.is_empty()]
        return pl.concat(parts, how="diagonal_relaxed") if parts else pl.DataFrame(schema=_MINUTE_SCHEMA)

    def iter_minute(
        self,
        symbols: list[str],
        start_time: datetime | None,
        end_time: datetime | None,
        asset_type: str = "stock",
        freq: str = "1m",
        on_chunk_done: Callable[[int, int], None] | None = None,
    ) -> Iterator[pl.DataFrame]:
        if not symbols or freq != "1m":
            if on_chunk_done:
                on_chunk_done(0, 0)
            return
        client = self._get_client()
        start_dt = _to_beijing_naive(start_time)
        end_dt = _to_beijing_naive(end_time)
        parts: list[pl.DataFrame] = []
        pending = 0
        for i, symbol in enumerate(symbols):
            code = to_code(symbol, asset_type)
            if code is None:
                if on_chunk_done:
                    on_chunk_done(i + 1, len(symbols))
                continue
            try:
                raw = client.minute_kline(code, freq)
            except TencentError as exc:
                raise ProviderRequestError(f"腾讯分钟源 {symbol} 请求失败: {exc}") from None
            rows = _minute_rows(symbol, raw, start_dt, end_dt)
            part = pl.DataFrame(rows, schema=_MINUTE_SCHEMA)
            if parts and pending + part.height > _MINUTE_OUTPUT_ROWS:
                yield pl.concat(parts, how="diagonal_relaxed")
                parts, pending = [], 0
            if not part.is_empty():
                parts.append(part)
                pending += part.height
            if len(parts) >= _MINUTE_OUTPUT_SYMBOLS or pending >= _MINUTE_OUTPUT_ROWS:
                yield pl.concat(parts, how="diagonal_relaxed")
                parts, pending = [], 0
            if on_chunk_done:
                on_chunk_done(i + 1, len(symbols))
            time.sleep(_MINUTE_INTERVAL_S)
        if parts:
            yield pl.concat(parts, how="diagonal_relaxed")
