"""模拟盘总览估值口径 — 回归「盈亏有值但总资产不变」的账务矛盾。

背景: overview 在无盘中快照时曾用 avg_cost 代替最新收盘价, 导致持仓市值恒等于
成本、总资产贴着初始资金, 而已实现盈亏/净值曲线是真实值, 两者互相矛盾。
"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600000.SH"
DAY = date(2026, 11, 25)


def _write_day(tmp_path, close: float) -> None:
    KlineRepository(DataStore(tmp_path)).append_daily(
        pl.DataFrame(
            {
                "symbol": [SYM],
                "date": [DAY],
                "open": [close - 1],
                "high": [close + 1],
                "low": [close - 2],
                "close": [close],
                "volume": [10000.0],
                "amount": [close * 10000.0],
            }
        )
    )


def _stub_account(monkeypatch, cash: float = 1000.0) -> None:
    monkeypatch.setattr(
        paper,
        "get_account",
        lambda *a, **k: {
            "cash": cash,
            "initial_cash": 1000.0,
            "status": "active",
            "market": "cn",
            "name": "t",
        },
    )
    monkeypatch.setattr(
        paper,
        "load_positions",
        lambda *a, **k: {
            SYM: {
                "qty": 100,
                "avg_cost": 10.0,
                "asset_type": "stock",
                "lots": [{"qty": 100, "date": "2026-11-20", "cost": 1000.0}],
            }
        },
    )
    monkeypatch.setattr(paper, "load_nav", lambda *a, **k: [{"date": DAY.isoformat(), "nav": 0}])


def test_overview_marks_positions_to_last_close(tmp_path, monkeypatch):
    _write_day(tmp_path, close=12.0)
    _stub_account(monkeypatch)

    ov = paper.overview(tmp_path)

    assert ov["holdings"][0]["last_price"] == 12.0
    assert ov["market_value"] == 1200.0
    assert ov["total"] == 2200.0
    assert ov["total_pnl"] == 1200.0
    assert ov["estimating"] is False
    assert ov["price_date"] == DAY.isoformat()


def test_overview_prefers_explicit_price_map(tmp_path, monkeypatch):
    """盘中实时快照口径优先: 传入 price_map 时不得回落日线收盘。"""
    _write_day(tmp_path, close=12.0)
    _stub_account(monkeypatch)

    ov = paper.overview(tmp_path, {SYM: 13.5})

    assert ov["holdings"][0]["last_price"] == 13.5
    assert ov["total"] == 1000.0 + 100 * 13.5
    assert ov["estimating"] is True
    assert ov["price_date"] is None
