"""港美股实时行情快照（腾讯免费源）。

港美股不走 A 股 QuoteService: 后者定版边界(11:30/15:00)、交易日判定、
指数/ETF 分类与监控评估全是 A 股口径。本模块提供同构的"实时快照 → quote_ts
→ 落盘"能力, 但按市场时区/时段独立运作, 写共享 kline_daily/enriched 时只用
merge-upsert(不覆写 A 股行)。

数据结构与 A 股一致: record["timestamp"](epoch ms) → daily/enriched.quote_ts。
"""
from __future__ import annotations

import logging
import math
import re
import time
import urllib.request
from datetime import date, datetime, time as dt_time, timezone
from zoneinfo import ZoneInfo

import polars as pl

from app.markets import CN_TZ, MARKET_HK, MARKET_US, is_trading_now, market_of

logger = logging.getLogger(__name__)

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
_ENDPOINT = "https://qt.gtimg.cn/q="

_HK_TZ = ZoneInfo("Asia/Hong_Kong")
_US_TZ = ZoneInfo("America/New_York")

# 腾讯源时间戳格式: 港股 YYYY/MM/DD HH:MM:SS, 美股 YYYY-MM-DD HH:MM:SS(美东本地)
_TS_FORMAT = {MARKET_HK: "%Y/%m/%d %H:%M:%S", MARKET_US: "%Y-%m-%d %H:%M:%S"}
_TS_TZ = {MARKET_HK: _HK_TZ, MARKET_US: _US_TZ}


def to_quote_code(symbol: str) -> str | None:
    """00700.HK → hk00700, AAPL.US → usAAPL; 非港美股返回 None。"""
    market = market_of(symbol)
    if market not in (MARKET_HK, MARKET_US):
        return None
    code = symbol.rsplit(".", 1)[0]
    return f"{market}{code}"


def parse_quote_ts(raw: str, market: str) -> int | None:
    """腾讯时间戳字符串 → epoch ms。美股用美东时区(含 DST), 否则会差 12~13h。"""
    fmt = _TS_FORMAT.get(market)
    tz = _TS_TZ.get(market)
    if not fmt or not tz:
        return None
    try:
        return int(datetime.strptime(raw.strip(), fmt).replace(tzinfo=tz).timestamp() * 1000)
    except (ValueError, OverflowError):
        return None


def _f(value: str) -> float | None:
    try:
        v = float(value)
        return v if v == v and abs(v) != float("inf") else None  # noqa: PLR2004 - NaN/Inf 过滤
    except (ValueError, TypeError):
        return None


def parse_snapshot(line: str, market: str, symbol: str) -> dict | None:
    """解析一行 `v_CODE="..."` 为 A 股同构 record。失败返回 None(fail-closed)。"""
    line = line.strip().rstrip(";").strip()
    if "=" not in line:
        return None
    var, payload = line.split("=", 1)
    if not var.strip().startswith("v_"):
        return None
    parts = payload.strip().strip('"').split("~")
    # 时间戳在 [30], 涨跌额/幅在 [31]/[32], 高低在 [33]/[34]: 至少 35 段
    if len(parts) < 35:
        return None
    price = _f(parts[3])
    if price is None or price <= 0:
        return None
    quote_ts = parse_quote_ts(parts[30], market)
    if quote_ts is None:
        return None
    change_pct = _f(parts[32])
    return {
        "symbol": symbol,                 # 用请求的 .HK/.US, 不用 parts[2](美股返回 AAPL.OQ)
        "name": parts[1] or None,
        "last_price": price,
        "prev_close": _f(parts[4]),
        "open": _f(parts[5]) or price,
        "high": _f(parts[33]) or price,
        "low": _f(parts[34]) or price,
        "volume": _f(parts[6]) or 0.0,    # 港美股为股, 不做 A 股 /100 手换算
        "amount": None,
        "change_pct": change_pct / 100.0 if change_pct is not None else None,  # 百分数→小数制
        "change_amount": _f(parts[31]),
        "timestamp": quote_ts,
    }


def fetch_realtime(symbols: list[str], *, timeout: float = 12.0,
                   max_codes: int = 60) -> list[dict]:
    """按批拉取港美股实时快照。columns 与 A 股 provider 契约一致。

    失败/空响应返回 [] (软失败), 由调用方保留上一轮缓存。
    """
    pairs = [(s, to_quote_code(s)) for s in symbols]
    pairs = [(s, c) for s, c in pairs if c]
    if not pairs:
        return []
    records: list[dict] = []
    for i in range(0, len(pairs), max_codes):
        batch = pairs[i:i + max_codes]
        url = _ENDPOINT + ",".join(code for _, code in batch)
        try:
            req = urllib.request.Request(url, headers=UA)
            raw = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", errors="ignore")
        except Exception as e:  # noqa: BLE001 - 免费源 fail-closed
            logger.warning("港美股实时行情拉取失败: %s", e)
            continue
        code_to_symbol = {code: sym for sym, code in batch}
        for line in raw.strip().split(";"):
            m = re.match(r"\s*v_([A-Za-z0-9]+)\s*=", line)
            if not m:
                continue
            symbol = code_to_symbol.get(m.group(1))
            if not symbol:
                continue
            market = market_of(symbol)
            record = parse_snapshot(line, market, symbol)
            if record:
                records.append(record)
    return records


def trading_date_for(market: str, quote_ts_ms: int, *,
                     now: datetime | None = None) -> date:
    """按市场本地时区归属交易日。

    美股北京时间 00:00~04:00 仍属美东前一自然日, 不能用 cn_today() 打日期,
    否则跨午夜快照会被写进错误的分区。
    """
    tz = _TS_TZ.get(market, CN_TZ)
    dt = datetime.fromtimestamp(quote_ts_ms / 1000, tz)
    return dt.date()


def is_market_open(market: str, *, now: datetime | None = None) -> bool:
    """按北京时间交易时段 + 简易周末过滤判断是否轮询该市场。"""
    now = now or datetime.now(CN_TZ)
    if now.weekday() >= 5:
        # 美股周五美东盘延续到北京时间周六凌晨, 周六 0~5 点仍应轮询
        if not (market == MARKET_US and now.weekday() == 5 and now.hour < 5):
            return False
    return is_trading_now(market, now)


# ── 服务主体 ────────────────────────────────────────────────────────
import threading  # noqa: E402 - 服务段用, 保持 provider 段依赖精简


class MarketQuoteService:
    """港美股实时快照轮询服务。

    与 A 股 QuoteService 并列、互不侵入: 只处理 .HK/.US, 按市场时区归属交易日,
    通过 merge-upsert 写共享 kline_daily/enriched (不覆写 A 股行)。数据结构与
    A 股一致(quote_ts), 前端复用同一 QuoteFreshness 判定。

    标的池 = 港美股自选 + 该市场维表前 N 只(覆盖无自选时也有数据); 免费源限流,
    N 默认 60, 只拉自选时成本极低。
    """

    DEFAULT_INTERVAL = 15.0          # 免费源节流, 比 A 股 6s 宽松
    MAX_SYMBOLS_PER_MARKET = 60

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._repo = None
        self._app_state = None
        self._interval = self.DEFAULT_INTERVAL
        self._last_status: dict = {}

    def set_repo(self, repo) -> None:
        self._repo = repo

    def set_app_state(self, app_state) -> None:
        self._app_state = app_state

    def start(self) -> bool:
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, name="market-quote-poll", daemon=True,
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        with self._lock:
            self._running = False
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2)

    def status(self) -> dict:
        return dict(self._last_status)

    def refresh_once(self) -> dict:
        """手动触发一轮(供 API/测试用); 返回本轮状态。"""
        return self._poll_once()

    # ── 标的池 ──────────────────────────────────────────────────────
    def _symbols_for(self, market: str) -> list[str]:
        from app.services import watchlist
        from app.markets import market_of

        wanted = [str(r.get("symbol", "")) for r in watchlist.list_symbols()
                  if market_of(str(r.get("symbol", ""))) == market]
        cap = self.MAX_SYMBOLS_PER_MARKET
        # 自选优先, 不足时用维表补足(港美股维表 symbol 已带后缀)
        if self._repo:
            inst = self._repo.get_instruments_market(market)
            if not inst.is_empty() and "symbol" in inst.columns:
                pool = [s for s in inst["symbol"].cast(pl.Utf8).to_list()
                        if market_of(s) == market and s not in wanted]
                wanted += pool[:max(0, cap - len(wanted))]
        return wanted[:cap]

    # ── 轮询 ────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while self._running:
            try:
                # 管道/数据修正期间暂停: 与 A 股 quote_service 同口径防写盘竞态
                qs = getattr(self._app_state, "quote_service", None) if self._app_state else None
                if not (qs and qs.is_paused()):
                    self._poll_once()
            except Exception as e:  # noqa: BLE001 - 轮询线程不得退出
                logger.warning("港美股实时轮询异常: %s", e)
            waited = 0.0
            while self._running and waited < self._interval:
                time.sleep(0.5)
                waited += 0.5

    def _poll_once(self) -> dict:
        status: dict = {"markets": {}, "checked_at": datetime.now(CN_TZ).isoformat()}
        for market in (MARKET_HK, MARKET_US):
            if not is_market_open(market):
                status["markets"][market] = {"open": False}
                continue
            symbols = self._symbols_for(market)
            if not symbols:
                status["markets"][market] = {"open": True, "symbols": 0}
                continue
            records = fetch_realtime(symbols)
            written = self._write_records(market, records) if records else 0
            status["markets"][market] = {
                "open": True, "symbols": len(symbols),
                "received": len(records), "written": written,
            }
        self._last_status = status
        return status

    # ── 写盘(与 A 股同构, merge 不覆写) ─────────────────────────────
    def _write_records(self, market: str, records: list[dict]) -> int:
        if not self._repo or not records:
            return 0
        # 按各市场本地交易日分组(美股跨午夜: 同一轮可能落两个日期)
        groups: dict[date, list[dict]] = {}
        for r in records:
            day = trading_date_for(market, int(r["timestamp"]))
            groups.setdefault(day, []).append(r)
        written = 0
        for day, group in groups.items():
            daily = self._build_daily(group, day)
            if daily.is_empty():
                continue
            try:
                # 只写 kline_daily: 不碰 A 股 _enriched_cache, 也不写 enriched 分区
                # (后者会 bump matrix generation, 让 A 股缓存预热反复失败)。
                self._repo.merge_asset_snapshot_partitions(daily)
                written += daily.height
            except Exception as e:  # noqa: BLE001
                logger.warning("港美股实时快照写盘失败(%s %s): %s", market, day, e)
        if written:
            self._notify(market)
        return written

    @staticmethod
    def _build_daily(records: list[dict], day: date) -> pl.DataFrame:
        """源快照 → 日K行。OHLC 先夹取再校验, 非法行直接丢弃。

        腾讯实时快照偶发自相矛盾 (例如 low=3.28 > open=3.26), 直接信任源会把
        非法行写进 kline_daily, 之后全量自检判 invalid 并阻断下游计算。
        """
        rows = []
        for r in records:
            close = r.get("last_price")
            if not close or close <= 0:
                continue
            open_, high, low = r.get("open") or close, r.get("high") or close, r.get("low") or close
            if not all(math.isfinite(v) for v in (open_, high, low, close)):
                continue
            # 夹取到 open/close 覆盖区间: 源 high 偏低或 low 偏高都不再越界
            high, low = max(high, open_, close), min(low, open_, close)
            if min(open_, high, low, close) <= 0:
                continue
            row = {
                "symbol": r["symbol"], "date": day,
                "open": open_, "high": high, "low": low, "close": close,
                "volume": r.get("volume") or 0.0, "amount": r.get("amount") or 0.0,
                "quote_ts": int(r["timestamp"]),
            }
            rows.append(row)
        if not rows:
            return pl.DataFrame()
        return pl.DataFrame(rows, schema={
            "symbol": pl.Utf8, "date": pl.Date,
            "open": pl.Float64, "high": pl.Float64, "low": pl.Float64, "close": pl.Float64,
            "volume": pl.Float64, "amount": pl.Float64, "quote_ts": pl.Int64,
        }).sort(["symbol", "date"])

    @staticmethod
    def _build_enriched(records: list[dict], day: date) -> pl.DataFrame:
        """港美股 enriched 最小行: 存储列契约内的字段, A 股专属指标留空。"""
        daily = MarketQuoteService._build_daily(records, day)
        if daily.is_empty():
            return daily
        return daily.with_columns([
            pl.col("close").alias("raw_close"),
            pl.col("high").alias("raw_high"),
            pl.col("low").alias("raw_low"),
            pl.lit(0.0, dtype=pl.Float64).alias("turnover_rate"),
            pl.lit(0, dtype=pl.Int64).alias("consecutive_limit_ups"),
            pl.lit(0, dtype=pl.Int64).alias("consecutive_limit_downs"),
        ])

    def _notify(self, market: str) -> None:
        """触发 SSE 让前端即时看到新快照。

        不清 ScreenerService 历史缓存: 港美股自选走磁盘慢路径(60s TTL 自然过期),
        而 clear_history_cache('stock') 是 A 股语义, 会连带让 A 股换代判定失效。
        """
        qs = getattr(self._app_state, "quote_service", None) if self._app_state else None
        if qs and hasattr(qs, "_broadcast_quote_updated"):
            try:
                qs._broadcast_quote_updated()  # noqa: SLF001 - 复用 SSE 广播
            except Exception as e:  # noqa: BLE001
                logger.warning("港美股行情 SSE 通知失败: %s", e)


_service: MarketQuoteService | None = None


def get_service() -> MarketQuoteService:
    global _service  # noqa: PLW0603 - 进程级单例
    if _service is None:
        _service = MarketQuoteService()
    return _service
