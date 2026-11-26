"""Per-symbol quote provenance for API readers; never fetch or mutate market data."""
from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.market_time import CN_TZ, cn_now
from app.markets import MARKET_CN, MARKET_HK, MARKET_US, is_trading_now, market_of
from app.services import trading_day

# 港美股本地时区(美股含 DST)。用于把 quote_ts 归属到正确交易日。
_MARKET_TZ = {
    MARKET_HK: ZoneInfo("Asia/Hong_Kong"),
    MARKET_US: ZoneInfo("America/New_York"),
}


class QuoteFreshness:
    def __init__(self, *, now: datetime | None = None, trading: bool | None = None,
                 enabled: bool = True, paused: bool = False, interval: float = 6) -> None:
        self.now = (now or cn_now()).astimezone(CN_TZ)
        self.trading = trading
        self.enabled, self.paused = enabled, paused
        self.max_age_ms = max(90_000, interval * 3_000)

    @classmethod
    def for_request(cls, request):
        now = cn_now()
        qs = getattr(request.app.state, "quote_service", None)
        return cls(now=now, trading=trading_day.cached_verdict(now),
                   enabled=bool(qs and getattr(qs, "_enabled", False)),
                   paused=bool(qs and getattr(qs, "_paused", False)),
                   interval=getattr(qs, "_interval", 6) if qs else 6)

    def describe(self, row: dict | None, *, historical: bool = False,
                 static_daily: bool = False, market: str | None = None) -> dict:
        """market 传入时按该市场时区/时段判定(港美股), 否则走原 A 股口径。"""
        row = row or {}
        # 港美股: 用市场本地时区解析 quote_ts, 交易时段/休盘按 markets 元数据,
        # 不套 A 股 9:30/11:30/15:00 与 cn_today。A 股(market 缺省/cn)路径不变。
        effective_market = market or market_of(str(row.get("symbol") or ""))
        if effective_market in _MARKET_TZ:
            return self._describe_market(row, effective_market, static_daily=static_daily)
        data_date = str(row.get("date") or "")[:10] or None
        stamp = row.get("quote_ts", row.get("timestamp"))
        quote_dt = None
        try:
            stamp = float(stamp)
            if not math.isfinite(stamp) or stamp <= 0:
                raise ValueError
            quote_dt = datetime.fromtimestamp(stamp / 1000, CN_TZ)
            data_date = quote_dt.date().isoformat()
        except (TypeError, ValueError, OverflowError, OSError):
            stamp = None
        out = {"data_date": data_date, "quote_ts": stamp,
               "checked_at": int(self.now.timestamp() * 1000), "max_age_ms": self.max_age_ms}

        def result(state, message):
            return {**out, "state": state, "message": message}

        try:
            price = float(row.get("close", row.get("last_price")))
            valid = math.isfinite(price) and price > 0
        except (TypeError, ValueError):
            valid = False
        if not valid:
            return result("missing", "暂无可用行情，请检查数据源或稍后重试")
        if quote_dt and quote_dt > self.now.replace(microsecond=0) and (quote_dt - self.now).total_seconds() > 60:
            return result("unknown", "行情时间异常，请检查数据源时间")
        today, clock = self.now.date(), self.now.time()
        trading = self.trading is not False and self.now.weekday() < 5
        if historical or not trading or clock < time(9, 30):
            return result("historical", "历史行情，按实际数据日期显示")
        if data_date is None or data_date < today.isoformat():
            return result("stale", "行情已过期，保留最近有效数据；请检查连接及所选源对该标的的支持范围")
        if data_date > today.isoformat():
            return result("unknown", "行情日期异常，请检查数据源")
        if row.get("prepared_daily"):
            return result("historical", "日K补齐数据，尚未取得实时行情")
        if self.paused:
            return result("paused", "实时行情暂时暂停，保留最近有效数据")
        if not self.enabled:
            return result("disabled", "实时行情未开启，保留最近有效数据")
        if stamp is None:
            # 港美股自选走静态 enriched 日K, 行里本就没有 quote_ts, 并非实时链路
            # 丢时间戳。标为 historical 显示实际数据日期, 而不是 "时间未确认"。
            # 仍保留上面的 stale 判定: 数据日期落后时照常提示过期。
            if static_daily:
                return result("historical", "历史行情，按实际数据日期显示")
            return result("unknown", "当日数据缺少行情时间，无法确认实时性")
        active = clock < time(11, 30) or time(13) <= clock < time(15)
        reference = self.now if active else datetime.combine(today, time(11, 30) if clock < time(13) else time(15), CN_TZ)
        if reference.timestamp() * 1000 - stamp > self.max_age_ms:
            return result("delayed", "行情更新延迟，保留最近有效数据，请检查数据源与连接")
        return result("live", "实时行情") if active else result("historical", "休盘行情，按最后有效时间显示")

    # ── 港美股专用判定(市场时区/时段, 不套 A 股边界) ──────────────────
    def _describe_market(self, row: dict, market: str, *, static_daily: bool) -> dict:
        tz = _MARKET_TZ[market]
        tz_now = self.now.astimezone(tz)
        raw_date = str(row.get("date") or "")[:10] or None
        stamp = row.get("quote_ts", row.get("timestamp"))
        quote_dt = None
        try:
            stamp = float(stamp)
            if not math.isfinite(stamp) or stamp <= 0:
                raise ValueError
            quote_dt = datetime.fromtimestamp(stamp / 1000, tz)
        except (TypeError, ValueError, OverflowError, OSError):
            stamp = None
        data_date = quote_dt.date().isoformat() if quote_dt else raw_date
        out = {"data_date": data_date, "quote_ts": stamp,
               "checked_at": int(self.now.timestamp() * 1000), "max_age_ms": self.max_age_ms}

        def result(state, message):
            return {**out, "state": state, "message": message}

        try:
            price = float(row.get("close", row.get("last_price")))
            if not (math.isfinite(price) and price > 0):
                return result("missing", "暂无可用行情，请检查数据源或稍后重试")
        except (TypeError, ValueError):
            return result("missing", "暂无可用行情，请检查数据源或稍后重试")
        # 未来时间戳(容差 60s)不可信
        if quote_dt and (quote_dt - tz_now).total_seconds() > 60:
            return result("unknown", "行情时间异常，请检查数据源时间")
        if self.paused:
            return result("paused", "实时行情暂时暂停，保留最近有效数据")
        open_now = is_trading_now(market, self.now)
        today_iso = tz_now.date().isoformat()
        if stamp is None:
            # 静态日K(日K同步尚未带 quote_ts): 盘中且日期落后仍报过期,
            # 否则显示数据日期, 而非"时间未确认"。
            if open_now and static_daily and data_date and data_date < today_iso:
                return result("stale", "行情已过期，保留最近有效数据，等待下次同步")
            return result(
                "historical" if static_daily else "unknown",
                "历史行情，按实际数据日期显示" if static_daily else "当日数据缺少行情时间，无法确认实时性",
            )
        if not open_now:
            # 休市: 显示最后有效交易日即可, 即使已跨本地零点(美股/港股次日凌晨)
            # 也不该报"过期" —— 那要等开盘后由盘中分支判定。
            return result("historical", "休盘行情，按最后有效时间显示")
        # 盘中: 数据日期必须是本市场当日, 落后即过期
        today_iso = tz_now.date().isoformat()
        if data_date and data_date < today_iso:
            return result("stale", "行情已过期，保留最近有效数据，等待下次同步")
        if data_date and data_date > today_iso:
            return result("unknown", "行情日期异常，请检查数据源")
        if not self.enabled:
            return result("disabled", "实时行情未开启，保留最近有效数据")
        age_ms = tz_now.timestamp() * 1000 - stamp
        if age_ms > self.max_age_ms:
            return result("delayed", "行情更新延迟，保留最近有效数据，请检查数据源与连接")
        return result("live", "实时行情")
