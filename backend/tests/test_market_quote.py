"""港美股实时快照: 腾讯源解析 / 市场时区归属 / 写盘隔离。

不依赖外网: 用真实响应的字段样本(只读抓取一次固化)验证解析契约。
"""
from __future__ import annotations

from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import market_quote as mq
from app.services.quote_freshness import QuoteFreshness


# 真实响应样本(2026-11-23, 字段数 78/73; 时间戳在 [30], 涨跌额/幅 [31]/[32])
def _line(code: str, symbol: str, ts: str, price: str, chg_pct: str) -> str:
    parts = [""] * 35
    parts[0], parts[1], parts[2], parts[3] = "100", "名称", symbol.split(".")[0], price
    parts[4], parts[5] = str(float(price) - 1), price
    parts[6] = "1000"
    parts[30], parts[31], parts[32] = ts, "1.00", chg_pct
    parts[33], parts[34] = str(float(price) + 1), str(float(price) - 2)
    return f'v_{code}="' + "~".join(parts) + '";'


def test_symbol_code_mapping():
    assert mq.to_quote_code("00700.HK") == "hk00700"
    assert mq.to_quote_code("AAPL.US") == "usAAPL"
    assert mq.to_quote_code("600000.SH") is None


def test_parse_hk_snapshot_fields():
    line = _line("hk00700", "00700.HK", "2026/11/23 15:58:00", "344.0", "2.39")
    r = mq.parse_snapshot(line, "hk", "00700.HK")
    assert r is not None
    assert r["symbol"] == "00700.HK"
    assert r["last_price"] == 344.0
    assert r["change_pct"] == pytest.approx(0.0239)  # 百分数 → 小数制
    assert r["timestamp"] == mq.parse_quote_ts("2026/11/23 15:58:00", "hk")


def test_parse_us_snapshot_keeps_requested_symbol():
    # 美股 parts[2] 返回 AAPL.OQ, 必须用请求的 AAPL.US
    line = _line("usAAPL", "AAPL.OQ", "2026-11-23 10:50:19", "350.76", "2.39")
    r = mq.parse_snapshot(line, "us", "AAPL.US")
    assert r and r["symbol"] == "AAPL.US"


def test_us_timestamp_uses_eastern_timezone():
    # 美东 10:50 → epoch 对应美东本地日期 11-23, 不能按北京时区算成别的日子
    ts = mq.parse_quote_ts("2026-11-23 10:50:19", "us")
    assert ts is not None
    assert mq.trading_date_for("us", ts) == date(2026, 11, 23)


def test_market_open_sessions():
    assert mq.is_market_open("hk", now=datetime(2026, 11, 23, 10, 0, tzinfo=CN_TZ))
    assert not mq.is_market_open("hk", now=datetime(2026, 11, 23, 20, 0, tzinfo=CN_TZ))
    assert mq.is_market_open("us", now=datetime(2026, 11, 23, 22, 0, tzinfo=CN_TZ))


def test_build_records_include_quote_ts():
    rec = {
        "symbol": "00700.HK", "last_price": 344.0, "open": 343.0, "high": 345.0,
        "low": 342.0, "volume": 1000.0, "amount": None,
        "timestamp": mq.parse_quote_ts("2026/11/23 15:58:00", "hk"),
    }
    daily = mq.MarketQuoteService._build_daily([rec], date(2026, 11, 23))
    assert "quote_ts" in daily.columns
    assert daily["quote_ts"][0] == rec["timestamp"]
    enr = mq.MarketQuoteService._build_enriched([rec], date(2026, 11, 23))
    assert {"raw_close", "quote_ts"}.issubset(enr.columns)
    assert enr["close"][0] == enr["raw_close"][0]


def _rec(symbol: str, close: float, open_: float, high: float, low: float) -> dict:
    return {
        "symbol": symbol, "last_price": close, "open": open_, "high": high, "low": low,
        "volume": 100.0, "amount": None, "timestamp": 1795640380000,
    }


def test_build_daily_clamps_source_ohlc_violation():
    """源快照 low 高于 open/close 时夹取到合法区间, 不把非法行写进日K。

    真实触发: AVAL.US 2026-11-25 实时快照 open=3.26 / high=3.55 / low=3.28 / close=3.54。
    """
    rec = _rec("AVAL.US", 3.54, 3.26, 3.55, 3.28)
    daily = mq.MarketQuoteService._build_daily([rec], date(2026, 11, 25))
    assert daily.height == 1
    row = daily.row(0, named=True)
    assert row["low"] == 3.26  # min(low, open, close)
    assert row["high"] == 3.55
    assert row["low"] <= min(row["open"], row["close"])
    assert row["high"] >= max(row["open"], row["close"])


def test_build_daily_clamps_high_below_close():
    rec = _rec("AAPL.US", 200.0, 190.0, 195.0, 180.0)
    row = mq.MarketQuoteService._build_daily([rec], date(2026, 11, 25)).row(0, named=True)
    assert row["high"] == 200.0


def test_build_daily_drops_non_positive_and_non_finite_rows():
    recs = [
        _rec("BAD1.US", 3.5, 3.4, 3.6, -1.0),          # 负的最低价, 夹取后仍非法
        _rec("BAD2.US", 0.0, 3.4, 3.6, 3.3),           # 收盘价非正
        _rec("BAD3.US", float("nan"), 3.4, 3.6, 3.3),  # 非有限值
        _rec("OK.US", 3.5, 3.4, 3.6, 3.3),
    ]
    daily = mq.MarketQuoteService._build_daily(recs, date(2026, 11, 25))
    assert set(daily["symbol"]) == {"OK.US"}


def test_freshness_live_and_closed():
    ts = mq.parse_quote_ts("2026/11/23 15:58:00", "hk")
    row = {"symbol": "00700.HK", "date": "2026-11-23", "close": 344.0, "quote_ts": ts}
    live = QuoteFreshness(now=datetime(2026, 11, 23, 15, 59, tzinfo=CN_TZ), enabled=True)
    assert live.describe(row, market="hk")["state"] == "live"
    closed = QuoteFreshness(now=datetime(2026, 11, 23, 20, 0, tzinfo=CN_TZ), enabled=True)
    assert closed.describe(row, market="hk")["state"] == "historical"


def test_freshness_static_daily_lags_is_stale():
    qf = QuoteFreshness(now=datetime(2026, 11, 23, 10, 0, tzinfo=CN_TZ), trading=True)
    row = {"symbol": "00700.HK", "date": "2026-11-20", "close": 344.0}
    assert qf.describe(row, market="hk", static_daily=True)["state"] == "stale"


def test_merge_live_preserves_other_market_rows(tmp_path):
    """港美股 merge 写盘不得覆写同分区 A 股行。"""
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    day = date(2026, 11, 23)
    cn = pl.DataFrame({
        "symbol": ["600000.SH"], "date": [day], "close": [10.0],
        "open": [10.0], "high": [10.0], "low": [10.0],
        "volume": [100.0], "amount": [1000.0], "quote_ts": [1795400000000],
    })
    repo.merge_live_daily_asset("stock", cn)
    rec = {
        "symbol": "00700.HK", "last_price": 344.0, "open": 344.0, "high": 344.0,
        "low": 344.0, "volume": 100.0, "amount": None, "timestamp": 1795421308000,
    }
    hk = mq.MarketQuoteService._build_daily([rec], day)
    repo.merge_asset_snapshot_partitions(hk)
    out = pl.read_parquet(tmp_path / "kline_daily" / f"date={day}" / "part.parquet")
    assert set(out["symbol"]) == {"600000.SH", "00700.HK"}


def test_snapshot_write_never_touches_a_share_enriched_cache(tmp_path):
    """回归: 港美股实时写盘不得冲掉 A 股自选内存缓存(曾致自选行全 null)。"""
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    day = date(2026, 11, 23)
    # 模拟 A 股全市场 enriched 内存缓存(自选读的就是它)
    cn_cache = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ"], "date": [day, day],
        "close": [10.0, 12.0], "raw_close": [10.0, 12.0],
        "turnover_rate": [1.0, 2.0],
    })
    from app.indicators.pipeline import ENRICHED_STORAGE_COLS
    cn_cache = cn_cache.select([c for c in ENRICHED_STORAGE_COLS if c in cn_cache.columns])
    repo.merge_live_enriched_asset("stock", cn_cache)
    before = repo._enriched_cache.clone()
    assert before.height == 2

    rec = {
        "symbol": "00700.HK", "last_price": 344.0, "open": 344.0, "high": 344.0,
        "low": 344.0, "volume": 100.0, "amount": None, "timestamp": 1795421308000,
    }
    hk = mq.MarketQuoteService._build_daily([rec], day)
    repo.merge_asset_snapshot_partitions(hk)

    assert repo._enriched_cache.equals(before), "A 股 enriched 缓存被跨市场写盘污染"
    # 关键: 不写 enriched 分区 —— 否则会 bump matrix generation, 让 A 股缓存
    # 预热在 compute 窗口中途判换代而反复丢弃(曾致 A 股自选全 null)。
    enr_path = tmp_path / "kline_daily_enriched" / f"date={day}" / "part.parquet"
    if enr_path.exists():
        assert "00700.HK" not in set(pl.read_parquet(enr_path)["symbol"])
    # 但 kline_daily 必须写入, 供自选叠加 quote_ts/实时价
    assert "00700.HK" in set(pl.read_parquet(
        tmp_path / "kline_daily" / f"date={day}" / "part.parquet"
    )["symbol"])


def test_a_share_live_flush_keeps_hk_us_rows(tmp_path):
    """A 股实时 flush 不得抹掉同分区港美股行(共享 kline_daily)。"""
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    day = date(2026, 11, 25)
    hk = mq.MarketQuoteService._build_daily([_rec("00700.HK", 344.0, 343.0, 345.0, 342.0)], day)
    repo.merge_asset_snapshot_partitions(hk)

    def _cn(close: float) -> pl.DataFrame:
        return pl.DataFrame({
            "symbol": ["600000.SH"], "date": [day], "open": [10.0], "high": [10.0],
            "low": [10.0], "close": [close], "volume": [100], "amount": [1000.0],
            "quote_ts": [1795640000000],
        })

    repo.flush_live_daily(_cn(10.5))
    repo.flush_live_daily(_cn(10.8))

    out = pl.read_parquet(tmp_path / "kline_daily" / f"date={day}" / "part.parquet")
    assert set(out["symbol"]) == {"600000.SH", "00700.HK"}
    assert out.filter(pl.col("symbol") == "600000.SH")["close"][0] == 10.8
    assert out.filter(pl.col("symbol") == "00700.HK")["close"][0] == 344.0


def test_snapshot_merge_aligns_partition_storage_dtypes(tmp_path):
    """快照 merge 必须按存储契约归一 dtype, 避免存量整型量价把分区漂成 Int64。"""
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    day = date(2026, 11, 25)
    path = tmp_path / "kline_daily" / f"date={day}" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["600000.SH"], "date": [day], "open": [10.0], "high": [10.0],
        "low": [10.0], "close": [10.0], "volume": [100], "amount": [1000],
        "quote_ts": [1795640000000],
    }).with_columns(pl.col("volume").cast(pl.Int64), pl.col("amount").cast(pl.Int64)).write_parquet(path)

    hk = mq.MarketQuoteService._build_daily(
        [_rec("00700.HK", 344.0, 343.0, 345.0, 342.0)], day,
    ).with_columns(pl.col("volume").cast(pl.Int64))
    repo.merge_asset_snapshot_partitions(hk)

    out = pl.read_parquet(path)
    assert out.schema["volume"] == pl.Float64
    assert out.schema["amount"] == pl.Float64
    assert set(out["symbol"]) == {"600000.SH", "00700.HK"}
