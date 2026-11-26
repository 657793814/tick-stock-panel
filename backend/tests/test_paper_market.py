"""模拟盘多市场撮合测试（方案 §7）：账户 market、手数、费用、T+0、基准。

A 股默认行为必须保持不变；港美股按各自市场语义工作。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.strategy import paper
from app.strategy.paper import (
    buy_fee,
    lot_size_for_account,
    normalize_qty,
    qty_from_amount,
    sell_fee,
)


def test_legacy_account_defaults_to_cn(tmp_path: Path):
    paper.create_account(tmp_path, 1_000_000)
    p = paper._root(tmp_path) / "account.json"
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw.pop("market", None)  # 模拟旧账户无字段
    p.write_text(json.dumps(raw), encoding="utf-8")
    assert paper.get_account(tmp_path)["market"] == "cn"


def test_create_account_with_market_validation(tmp_path: Path):
    for m in ("cn", "hk", "us"):
        acc = paper.create_account(tmp_path, 1_000_000, account_id=m, market=m)
        assert acc["market"] == m
    with pytest.raises(ValueError):
        paper.create_account(tmp_path, 1_000_000, account_id="bad", market="xx")


def test_lot_size_by_market():
    for m, lot in (("cn", 100), ("hk", 100), ("us", 1)):
        assert lot_size_for_account({"market": m}) == lot
    assert lot_size_for_account({}) == 100  # 旧账户


def test_normalize_qty_and_qty_from_amount_respect_lot_size():
    assert normalize_qty(250, lot_size=100) == 200
    assert normalize_qty(250, lot_size=1) == 250
    assert qty_from_amount(1050, 10.0, lot_size=100) == 100
    assert qty_from_amount(25, 10.0, lot_size=1) == 2


@pytest.mark.parametrize(
    ("market", "expect_buy_stamp", "expect_sell_stamp"),
    [
        ("cn", 0.0, 0.001),   # A股卖出单边
        ("hk", 0.001, 0.001),  # 港股双边
        ("us", 0.0, 0.0),      # 美股无印花税
    ],
)
def test_fee_stamp_tax_by_market(market, expect_buy_stamp, expect_sell_stamp):
    stamp = 0.001
    # 100 股 @10 元 = 1000，佣金 max(1000*0.00025, 5)=5
    assert buy_fee(100, 10.0, 0.00025, stamp_tax_pct=stamp, market=market) == round(5.0 + 1000 * expect_buy_stamp, 2)
    assert sell_fee(100, 10.0, 0.00025, stamp, market=market) == round(5.0 + 1000 * expect_sell_stamp, 2)


def test_t_plus_0_available_same_day():
    """港美股 T+0：当日买入即可全额卖出；A股 T+1 仍锁当日批次。"""
    pos = {"qty": 100, "lots": [{"date": "2026-11-21", "qty": 100}]}
    assert paper._available_of(pos, "2026-11-21", market="cn") == 0
    assert paper._available_of(pos, "2026-11-21", market="hk") == 100
    assert paper._available_of(pos, "2026-11-21", market="us") == 100


def test_benchmark_symbol_by_market():
    from app.markets import benchmark_symbol

    assert benchmark_symbol("cn") == "000300.SH"
    assert benchmark_symbol("hk") == "HSI"
    assert benchmark_symbol("us") == "SPX"
