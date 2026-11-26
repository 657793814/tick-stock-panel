"""模拟盘补结算: 漏结算的交易日要能回放, 否则顺延单永远挂着「待成交」。

现实触发链 (2026-11-24/25): 盘后管道只在当天调一次 settle_day。11-24 管道没跑、
11-25 管道在日K阶段失败 → 两天的顺延单 (next_open) 一张都没撮合, 页面上 180 张
订单全是 pending。补结算按「上次定版日 + 1」逐日回放, 且不能把周末/节假日算进
顺延次数 (MAX_POSTPONE_DAYS), 否则会误判过期。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.strategy import paper
from app.tickflow.repository import DataStore, KlineRepository

SYM = "600519.SH"
D0 = date(2026, 11, 20)   # 下单日 (收盘后)
D1 = date(2026, 11, 23)   # 次日开盘价成交
D2 = date(2026, 11, 24)   # 漏结算的一天
D3 = date(2026, 11, 25)   # 另一个漏结算的一天
BARS = [
    (date(2026, 11, 19), 9.8, 9.9),
    (D0, 10.0, 10.1),
    (D1, 10.2, 10.4),
    (D2, 10.5, 10.6),
    (D3, 10.7, 11.0),
]


def _write_bars(data_dir: Path) -> None:
    rows = pl.DataFrame({
        "symbol": [SYM] * len(BARS),
        "date": [r[0] for r in BARS],
        "open": [r[1] for r in BARS],
        "high": [max(r[1], r[2]) for r in BARS],
        "low": [min(r[1], r[2]) for r in BARS],
        "close": [r[2] for r in BARS],
        "volume": [10000.0] * len(BARS),
        "amount": [r[2] * 10000.0 for r in BARS],
    })
    KlineRepository(DataStore(data_dir)).append_daily(rows)


def _freeze(monkeypatch, day: date, hhmm: time) -> None:
    now = datetime.combine(day, hhmm, tzinfo=CN_TZ)
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper, "cn_today", lambda: now.date())


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    _write_bars(tmp_path)
    return tmp_path


def _pending(data_dir: Path) -> list[dict]:
    return [o for o in paper.load_orders(data_dir) if o["status"] == "pending"]


def test_catchup_settles_every_missed_trading_day(data_dir, monkeypatch):
    """越过多日直接结算到 D3: 单子必须按 D1 开盘价成交, 不能等到 D3 的开盘。"""
    _freeze(monkeypatch, D0, time(20, 0))
    paper.create_account(data_dir, 1_000_000)
    _, err = paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.1)
    assert err is None

    # 只结算到 D3 (D1/D2 都漏了)
    summary = paper.settle_pending_days(data_dir, D3.isoformat())

    assert summary["settled_days"] == [D0.isoformat(), D1.isoformat(), D2.isoformat(), D3.isoformat()]
    assert summary["filled"] == 1
    assert not _pending(data_dir)
    fills = [f for f in paper.load_fills(data_dir) if f.get("side") == "buy"]
    assert len(fills) == 1
    assert fills[0]["date"] == D1.isoformat()
    # 成交价 = 当日 raw_open 10.2 + 5bps 滑点 (默认 slippage_bps)
    assert fills[0]["price"] == pytest.approx(10.2 * (1 + paper.DEFAULT_SLIPPAGE_BPS / 10000), abs=1e-6)


def test_catchup_skips_days_without_daily_partition(data_dir, monkeypatch):
    """没有日K分区的日子 (周末/节假日) 不参与撮合, 否则顺延次数会被空耗到过期。"""
    _freeze(monkeypatch, D0, time(20, 0))
    paper.create_account(data_dir, 1_000_000)
    paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.1)

    # 删掉 D1 分区, 模拟当天没有行情
    import shutil
    shutil.rmtree(data_dir / "kline_daily" / f"date={D1.isoformat()}")

    summary = paper.settle_pending_days(data_dir, D3.isoformat())

    assert D1.isoformat() not in summary["settled_days"]
    order = paper.load_orders(data_dir)[0]
    assert order["status"] == "filled"
    # 未因缺行情被计顺延/过期
    assert order.get("postponed", 0) == 0


def test_catchup_is_idempotent(data_dir, monkeypatch):
    """重放同一段日期不得重复成交 (订单状态 + 台账守卫)。"""
    _freeze(monkeypatch, D0, time(20, 0))
    paper.create_account(data_dir, 1_000_000)
    paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.1)

    first = paper.settle_pending_days(data_dir, D3.isoformat())
    buys_after_first = len([f for f in paper.load_fills(data_dir) if f.get("side") == "buy"])
    second = paper.settle_pending_days(data_dir, D3.isoformat())

    assert first["filled"] == 1 and buys_after_first == 1
    assert second["filled"] == 0
    assert len([f for f in paper.load_fills(data_dir) if f.get("side") == "buy"]) == 1


def test_catchup_lookback_is_bounded(data_dir, monkeypatch):
    """超长停机不会一次回放几周 —— 回看窗口有上限 (默认 15 天)。

    窗口外的日期不重放; 单子按窗口内最早的可结算日成交 (宁可晚一天, 也不留死单)。
    """
    _freeze(monkeypatch, D0, time(20, 0))
    paper.create_account(data_dir, 1_000_000)
    paper.create_order(data_dir, SYM, "buy", qty=100, order_type="next_open", ref_price=10.1)

    summary = paper.settle_pending_days(data_dir, D3.isoformat(), max_lookback_days=1)

    # 只回看到 end-1 天, D0/D1 不重放
    assert summary["settled_days"] == [D2.isoformat(), D3.isoformat()]
    assert not _pending(data_dir)
    fill = [f for f in paper.load_fills(data_dir) if f.get("side") == "buy"][0]
    assert fill["date"] == D2.isoformat()  # 窗口内最早交易日开盘价 10.5


def test_settle_endpoint_backfills_pending_orders(tmp_path, monkeypatch):
    """POST /api/paper/settle 应把漏结算的顺延单补成交 (缺省全部账户)。"""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import paper as paper_api

    _write_bars(tmp_path)
    _freeze(monkeypatch, D0, time(20, 0))
    monkeypatch.setattr(paper_api, "cn_today", lambda: D3)
    paper.create_account(tmp_path, 1_000_000, account_id="acc_test")
    _, err = paper.create_order(tmp_path, SYM, "buy", qty=100, order_type="next_open",
                               ref_price=10.1, account_id="acc_test")
    assert err is None

    app = FastAPI()
    app.include_router(paper_api.router)
    app.state.repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        resolve_asset_type=lambda s: "stock",
    )
    client = TestClient(app)

    r = client.post("/api/paper/settle")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["filled"] == 1
    assert body["accounts"][0]["settled_days"] == [
        D0.isoformat(), D1.isoformat(), D2.isoformat(), D3.isoformat(),
    ]
    filled = client.get("/api/paper/orders?account=acc_test&status=filled").json()["orders"]
    assert len(filled) == 1
    assert client.get("/api/paper/orders?account=acc_test&status=pending").json()["orders"] == []
