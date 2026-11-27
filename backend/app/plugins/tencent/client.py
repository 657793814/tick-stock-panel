"""腾讯公开行情客户端: 三个无需 token 的 HTTP GET 端点。

上游端点 (实测 2026-11-27):
  - qt.gtimg.cn/q=sh600519[,sz000001...]        实时快照+五档, GBK, ~ 分隔, 支持批量
  - ifzq.gtimg.cn/appstock/app/kline/mkline      分钟 K (m1~m60), 真 OHLC
  - web.ifzq.gtimg.cn/appstock/app/day/query     最近 5 日分时 (价格+累计量额)
  - web.ifzq.gtimg.cn/appstock/app/fqkline/get   前复权日 K (备用, 不进路由)

口径要点:
  - mkline 行序为 [时间, 开, 收, 高, 低, 量(手), {}, 额], **收在高/低之前**;
  - mkline 的时间戳为北京墙钟 YYYYMMDDHHMM; 成交量单位为手 (已与日 K 对齐);
  - mkline 的额字段口径未经上游文档确认, 本项目改由 volume×100×close 推算;
  - day/query 的价/量为当日累计值, 需差分才得到每分钟增量; date 参数无效;
  - qt.gtimg.cn 返回 GBK 编码, 字段以 ~ 分隔; 涨跌幅为百分数数值, 项目契约为小数制。

历史深度限制 (上游不接受日期范围参数, 指定起止日期返回空):
  - m1 最多约 482 根 (≈2 个交易日), m5 约 320 根 (≈7 日), m60 约 4 个月;
  - day/query 固定最近 5 个交易日, 不支持批量。
"""
from __future__ import annotations

import logging
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

QUOTE_URL = "https://qt.gtimg.cn/q="
MINUTE_KLINE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
DAY_INTRADAY_URL = "https://web.ifzq.gtimg.cn/appstock/app/day/query"
FQ_KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

_BEIJING = timezone(timedelta(hours=8))
_BATCH_LIMIT = 50
_MAX_MINUTE_BARS = 1000
_QUOTE_RE = re.compile(r'v_([a-z]{2}\d{4,6})="([^"]*)"')
_FREQ_MAP = {"1m": "m1", "5m": "m5", "15m": "m15", "30m": "m30", "60m": "m60"}


class TencentError(RuntimeError):
    """腾讯上游请求或响应异常。"""


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def to_code(symbol: str, asset_type: str = "stock") -> str | None:
    """项目 symbol → 腾讯代码。裸代码按交易所前缀规则推断。"""
    raw = str(symbol or "").strip().upper()
    if not raw:
        return None
    if "." in raw:
        bare, _, market = raw.partition(".")
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj", "HK": "hk"}.get(market)
        if prefix is None or not bare.isdigit():
            return None
        return f"{prefix}{bare}"
    bare = raw
    if not bare.isdigit():
        return None
    if asset_type == "index":
        if bare.startswith(("000", "880")):
            return f"sh{bare}"
        if bare.startswith("399"):
            return f"sz{bare}"
        return None
    if len(bare) == 5:  # 港股裸代码
        return f"hk{bare}"
    if bare.startswith(("6", "5", "9")):
        return f"sh{bare}"
    if bare.startswith(("0", "1", "2", "3")):
        return f"sz{bare}"
    if bare.startswith(("4", "8")) or bare.startswith("920"):
        return f"bj{bare}"
    return None


def from_code(code: str) -> tuple[str, str] | None:
    """腾讯代码 → (项目 symbol, asset_type)。"""
    match = re.fullmatch(r"(sh|sz|bj|hk)(\d{4,6})", str(code or "").strip().lower())
    if match is None:
        return None
    market, bare = match.groups()
    if market == "hk":
        return f"{bare}.HK", "stock"
    if market == "bj":
        return f"{bare}.BJ", "stock"
    suffix = "SH" if market == "sh" else "SZ"
    asset_type = "index" if (suffix == "SH" and bare.startswith(("000", "880"))) or (
        suffix == "SZ" and bare.startswith("399")
    ) else "stock"
    return f"{bare}.{suffix}", asset_type


def minute_datetime(text: str) -> datetime | None:
    """腾讯北京墙钟时间戳 → naive datetime (YYYYMMDDHHMM 或 YYYYMMDDHHMMSS)。"""
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) < 12:
        return None
    try:
        return datetime.strptime(digits[:12], "%Y%m%d%H%M")
    except ValueError:
        return None


def quote_timestamp(text: str) -> int:
    """qt.gtimg.cn 的 YYYYMMDDHHMMSS → epoch 毫秒 (北京时间)。"""
    digits = re.sub(r"\D", "", str(text or ""))
    if len(digits) < 14:
        return int(time.time() * 1000)
    try:
        return int(datetime.strptime(digits[:14], "%Y%m%d%H%M%S").replace(tzinfo=_BEIJING).timestamp() * 1000)
    except ValueError:
        return int(time.time() * 1000)


class TencentClient:
    """轻量客户端: 复用一个 httpx 连接池, 所有异常归一为 TencentError。"""

    def __init__(self, timeout: float = 10.0) -> None:
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)

    def close(self) -> None:
        self._http.close()

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._http.get(url, params=params)
        except httpx.HTTPError as exc:
            raise TencentError(f"腾讯接口网络异常: {type(exc).__name__}") from None
        if response.status_code >= 400:
            raise TencentError(f"腾讯接口 HTTP {response.status_code}")
        return response

    def quote(self, symbols: list[str]) -> list[dict]:
        """批量实时快照。symbols 为项目 symbol, 上游代码非法时跳过。"""
        codes = [code for s in symbols if (code := to_code(s)) is not None]
        if not codes:
            return []
        codes = codes[:_BATCH_LIMIT]
        response = self._get(QUOTE_URL + ",".join(codes))
        text = response.content.decode("gbk", errors="replace")
        rows: list[dict] = []
        for match in _QUOTE_RE.finditer(text):
            code, payload = match.groups()
            fields = payload.split("~")
            if len(fields) < 39:
                logger.warning("腾讯实时快照字段数异常: code=%s n=%d", code, len(fields))
                continue
            rows.append({"code": code, "fields": fields})
        return rows

    def minute_kline(self, code: str, freq: str = "1m", count: int = _MAX_MINUTE_BARS) -> list[list]:
        """分钟 K 原始数据行。count 上限由上游决定 (m1 实测约 482 根)。"""
        period = _FREQ_MAP.get(freq, "m1")
        params = {"param": f"{code},{period},,,{count}"}
        payload = self._get(MINUTE_KLINE_URL, params=params).json()
        if payload.get("code") != 0:
            raise TencentError(f"腾讯分钟K返回错误: {payload.get('msg') or payload.get('code')}")
        node = (payload.get("data") or {}).get(code) or {}
        rows = node.get(period)
        return rows if isinstance(rows, list) else []

    def day_intraday(self, code: str) -> list[dict]:
        """最近 5 个交易日分时。每项 {date: YYYYMMDD, data: [原始行, ...]}。"""
        payload = self._get(DAY_INTRADAY_URL, params={"code": code}).json()
        if payload.get("code") != 0:
            raise TencentError(f"腾讯分时返回错误: {payload.get('msg') or payload.get('code')}")
        node = (payload.get("data") or {}).get(code) or {}
        days = node.get("data")
        return days if isinstance(days, list) else []

    def daily_kline(self, code: str, count: int = 120, fq: str = "qfq") -> list[list]:
        """日 K。fq="qfq" 前复权 (备用直取), fq="" 不复权 (路由口径, 与除权因子管线对齐)。"""
        suffix = "qfq" if fq else ""
        params = {"param": f"{code},day,,,{count},{suffix}"}
        payload = self._get(FQ_KLINE_URL, params=params).json()
        if payload.get("code") != 0:
            raise TencentError(f"腾讯日K返回错误: {payload.get('msg') or payload.get('code')}")
        node = (payload.get("data") or {}).get(code) or {}
        rows = node.get("day") if not suffix else (node.get("qfqday") or node.get("day"))
        return rows if isinstance(rows, list) else []
