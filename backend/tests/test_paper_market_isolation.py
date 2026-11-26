"""模拟盘市场隔离单测: 账户只允许操作所属市场标的。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.strategy import paper


@pytest.fixture
def cn_account(tmp_path: Path) -> str:
    acc = "acc_cn_iso"
    paper.create_account(tmp_path, 1_000_000.0, account_id=acc, name="A股账户", market="cn")
    return acc


@pytest.fixture
def hk_account(tmp_path: Path) -> str:
    acc = "acc_hk_iso"
    paper.create_account(tmp_path, 1_000_000.0, account_id=acc, name="港股账户", market="hk")
    return acc


def test_cn_account_rejects_hk_symbol(tmp_path: Path, cn_account: str) -> None:
    order, err = paper.create_order(
        tmp_path, "00700.HK", "buy",
        account_id=cn_account, qty=100, order_type="market", ref_price=300.0,
    )
    assert order is None
    assert "市场隔离" in (err or "")


def test_hk_account_rejects_cn_symbol(tmp_path: Path, hk_account: str) -> None:
    order, err = paper.create_order(
        tmp_path, "600000.SH", "buy",
        account_id=hk_account, qty=100, order_type="market", ref_price=10.0,
    )
    assert order is None
    assert "市场隔离" in (err or "")


def test_market_account_accepts_own_symbol(tmp_path: Path, hk_account: str) -> None:
    order, err = paper.create_order(
        tmp_path, "00700.HK", "buy",
        account_id=hk_account, qty=100, order_type="market", ref_price=300.0,
    )
    assert err is None
    assert order is not None
    assert order["symbol"] == "00700.HK"
