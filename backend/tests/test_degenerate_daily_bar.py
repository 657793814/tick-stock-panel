"""源端退化日K(零价字段)的入口规范化。

背景: TickFlow 对极冷门美股会返回 open=low=0、high=close=成交价 的 K 线
(实测 BFJA.US / RCLY.US 2026-11-24, volume=10)。这类行一旦落盘会被自检判成
「非停牌日存在零价」, 锁住 daily_health 并阻断盘后管道/回测; 由于重新取数
返回的还是同一批零价, 整日修补也永远失败。
"""
from datetime import date

import polars as pl

from app.services.kline_sync import _normalize_daily


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows).with_columns(pl.col("date").cast(pl.Date))


def test_zero_open_and_low_backfilled_with_close():
    df = _frame([
        {"symbol": "BFJA.US", "date": date(2026, 11, 24), "open": 0.0, "high": 16.495,
         "low": 0.0, "close": 16.495, "volume": 10.0, "amount": 0.0},
    ])

    out = _normalize_daily(df)

    assert out.height == 1
    row = out.row(0, named=True)
    assert row["open"] == 16.495
    assert row["low"] == 16.495
    assert row["high"] == 16.495
    assert row["close"] == 16.495


def test_row_without_close_is_dropped():
    df = _frame([
        {"symbol": "BAD.US", "date": date(2026, 11, 24), "open": 0.0, "high": 0.0,
         "low": 0.0, "close": 0.0, "volume": 10.0, "amount": 0.0},
        {"symbol": "GOOD.US", "date": date(2026, 11, 24), "open": 10.0, "high": 11.0,
         "low": 9.5, "close": 10.5, "volume": 100.0, "amount": 1000.0},
    ])

    out = _normalize_daily(df)

    assert out["symbol"].to_list() == ["GOOD.US"]


def test_normal_bars_untouched():
    df = _frame([
        {"symbol": "AJG.US", "date": date(2026, 11, 24), "open": 277.14, "high": 287.34,
         "low": 276.06, "close": 286.82, "volume": 1372589.0, "amount": 0.0},
    ])

    out = _normalize_daily(df)

    row = out.row(0, named=True)
    assert (row["open"], row["high"], row["low"], row["close"]) == (277.14, 287.34, 276.06, 286.82)
