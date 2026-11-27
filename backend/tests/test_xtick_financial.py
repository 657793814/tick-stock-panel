"""XTick 财务/股东数据接入测试 (纯离线)。

覆盖:
- /doc/gaap → metrics 的 canonical 字段映射与公告日;
- /doc/capital → shares 的股本列;
- 十大股东/流通股东/股东户数 → holder_metrics 聚合口径;
- 多表财务/股东因子按各自公告日做点时门控。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.backtest.fundamentals import (
    attach_fundamental_factors,
    fundamental_tables,
)
from app.plugins.xtick.holders import build_holder_rows
from app.plugins.xtick.provider import XTickProvider

PANEL_DATES = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8)]


def _panel() -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": ["600519.SH"] * len(PANEL_DATES),
        "date": PANEL_DATES,
        "close": [10.0, 10.0, 10.0, 10.0],
    }).sort(["symbol", "date"])


def _provider() -> XTickProvider:
    provider = XTickProvider()
    provider._client = object()  # type: ignore[assignment] — raw 被整体替换, 不触网
    return provider


def test_financials_metrics_maps_canonical_columns():
    provider = _provider()
    payload = [{
        "code": "600519", "reportDate": "2026-09-30", "publicDate": "2026-10-31",
        "basicEps": 51.53, "bps": 216.95, "revenueInc": 1.8179, "profitInc": -0.2715,
        "roe": 23.7708, "grossProfit": 89.5811, "netProfit": 50.9405,
        "gearRatio": 14.2, "netProfitM": -0.2465,
    }]
    provider.raw = lambda kind, params=None: payload  # type: ignore[method-assign]

    frame = provider.get_financials("metrics", ["600519.SH"], latest_only=True)

    assert frame.height == 1
    row = frame.to_dicts()[0]
    assert row["symbol"] == "600519.SH"
    assert row["period_end"] == "2026-09-30"
    assert row["announce_date"] == "2026-10-31"
    assert row["eps_basic"] == 51.53
    assert row["bps"] == 216.95
    assert row["net_income_yoy"] == -0.2715
    assert row["net_margin"] == 50.9405
    # 语义未确认的字段不得凭名字猜口径透传
    assert "netProfitM" not in row


def test_financials_shares_maps_capital_table():
    provider = _provider()
    payload = [{
        "code": "600519", "reportDate": "2026-09-30", "publicDate": "2026-09-30",
        "totalCapital": 1250080000.0, "flowCapital": 1250080000.0,
        "restrictCapital": 0.0,
    }]
    provider.raw = lambda kind, params=None: payload  # type: ignore[method-assign]

    frame = provider.get_financials("shares", ["600519.SH"], latest_only=True)

    row = frame.to_dicts()[0]
    assert row["float_shares"] == 1250080000.0
    assert row["total_shares"] == 1250080000.0
    assert row["restricted_shares"] == 0.0


def test_financials_unsupported_table_returns_empty():
    provider = _provider()
    assert provider.get_financials("income", ["600519.SH"]).is_empty()


def test_holder_rows_aggregate_ratios_and_changes():
    top = [
        {"reportDate": "2025-12-31", "publicDate": "2026-03-20", "rank": 1, "ratio": 50.0, "reason": "不变"},
        {"reportDate": "2025-12-31", "publicDate": "2026-03-20", "rank": 2, "ratio": 10.0, "reason": "新进"},
        {"reportDate": "2026-03-31", "publicDate": "2026-04-25", "rank": 1, "ratio": 52.0, "reason": "增持"},
        {"reportDate": "2026-03-31", "publicDate": "2026-04-25", "rank": 2, "ratio": 8.0, "reason": "减持"},
    ]
    flow = [
        {"reportDate": "2026-03-31", "publicDate": "2026-04-25", "rank": 1, "ratio": 40.0, "reason": "不变"},
    ]
    nums = [
        {"reportDate": "2025-12-31", "publicDate": "2026-03-20", "holder": 100000},
        {"reportDate": "2026-03-31", "publicDate": "2026-04-25", "holder": 90000},
    ]

    rows = build_holder_rows("600519.SH", top_rows=top, flow_rows=flow, num_rows=nums)
    latest = {row["period_end"]: row for row in rows}["2026-03-31"]

    assert latest["announce_date"] == "2026-04-25"
    assert latest["top10_ratio"] == 60.0
    assert latest["top10_hhi"] == round((0.52 ** 2 + 0.08 ** 2), 6)
    assert latest["top10_new_count"] == 0.0
    assert latest["top10_increase_count"] == 1.0
    assert latest["flow_ratio"] == 40.0
    assert latest["holder_num"] == 90000.0
    assert latest["holder_num_chg"] == -10.0


def test_holder_rows_skip_empty_period():
    assert build_holder_rows("600519.SH", top_rows=[], flow_rows=[], num_rows=[]) == []


def test_multi_table_factors_gate_by_own_announce_date():
    metrics = pl.DataFrame({
        "symbol": ["600519.SH"],
        "announce_date": ["2026-01-05"],
        "roe": [20.0],
        "bps": [5.0],
    })
    holders = pl.DataFrame({
        "symbol": ["600519.SH"],
        "announce_date": ["2026-01-06"],
        "holder_num": [90000.0],
        "top10_ratio": [60.0],
    })

    attached = attach_fundamental_factors(
        _panel(),
        {"metrics": metrics, "holder_metrics": holders},
        ["roe_latest", "holder_num", "top10_hold_ratio"],
    ).sort("date")

    roe = attached["roe_latest"].to_list()
    holder = attached["holder_num"].to_list()
    # 公告当日不可用 (保守 T+1)
    assert roe[0] is None and roe[1] == 20.0
    assert holder[0] is None and holder[1] is None and holder[2] == 90000.0
    assert attached["top10_hold_ratio"].to_list()[2] == 60.0


def test_multi_table_factors_null_when_table_missing():
    metrics = pl.DataFrame({
        "symbol": ["600519.SH"], "announce_date": ["2026-01-05"], "roe": [20.0], "bps": [5.0],
    })
    attached = attach_fundamental_factors(
        _panel(), {"metrics": metrics}, ["roe_latest", "holder_num"],
    )
    assert attached["holder_num"].is_null().all()


def test_fundamental_tables_group_by_source():
    assert fundamental_tables(["roe_latest", "holder_num"]) == {"metrics", "holder_metrics"}
