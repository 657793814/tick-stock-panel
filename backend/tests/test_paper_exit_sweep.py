"""风控出场扫描 (run_exit_sweep) 单测。

迁移的 order 类策略改写成信号策略后 EXIT_SIGNALS=[], 模拟盘只买不卖;
run_exit_sweep 按策略文件里的 STOP_LOSS / MAX_HOLD_DAYS 兜底卖出。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.strategy import paper, paper_auto


STRATEGY_ID = "custom_orders_04_limit_trend_pullback"

# 仓库内真实策略目录 (策略常量读取依赖 data/strategies/custom 布局)
REPO_DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture
def account(tmp_path: Path) -> str:
    acc = "acc_sweep_test"
    root = tmp_path / "paper" / "accounts" / acc
    (root / "auto_rules").mkdir(parents=True)
    (root / "orders").mkdir()
    (root / "fills").mkdir()
    (root / "nav").mkdir()
    (root / "positions.json").write_text(json.dumps({
        "positions": {
            "600000.SH": {
                "asset_type": "stock",
                "qty": 1000,
                "avg_cost": 10.0,
                "available_qty": 1000,
                "lots": [{"date": "2026-11-20", "qty": 1000}],
            },
        }
    }), encoding="utf-8")
    rule = {
        "name": "对比·测试",
        "match_kind": "strategy",
        "match_id": STRATEGY_ID,
        "side": "auto",
        "size_mode": "pct_equity",
        "size_value": 10.0,
        "order_type": "market",
        "cooldown_days": 5,
        "max_positions": 10,
        "enabled": True,
        "id": "arule_test",
        "created_at": "2026-11-20T10:00:00+08:00",
    }
    (root / "auto_rules" / "arule_test.json").write_text(
        json.dumps(rule, ensure_ascii=False), encoding="utf-8")
    # 最小策略桩: 提供 STOP_LOSS / MAX_HOLD_DAYS 常量 (扫描用 AST 读取, 不执行)
    strat_dir = tmp_path / "strategies" / "custom"
    strat_dir.mkdir(parents=True)
    (strat_dir / f"{STRATEGY_ID}.py").write_text(
        f'EXIT_SIGNALS = []\nSTOP_LOSS = -0.05\nMAX_HOLD_DAYS = 252\n',
        encoding="utf-8")
    paper.create_account(
        tmp_path, 1_000_000.0, account_id=acc, name="测试", market="cn",
    )
    return acc


def test_strategy_exit_consts_reads_negative_stop(tmp_path: Path) -> None:
    if not (REPO_DATA_DIR / "strategies" / "custom" / f"{STRATEGY_ID}.py").exists():
        pytest.skip("仓库策略文件不存在")
    stop, hold = paper_auto._strategy_exit_consts(REPO_DATA_DIR, STRATEGY_ID)
    assert stop == -0.05
    assert hold == 252
    # 不存在的策略文件 → (None, None), 扫描跳过
    assert paper_auto._strategy_exit_consts(tmp_path, "not_exist") == (None, None)


def test_exit_sweep_sells_below_stop_loss(tmp_path: Path, account: str) -> None:
    # 9.4 / 10.0 - 1 = -6% ≤ -5% → 触发止损全平
    orders = paper_auto.run_exit_sweep(tmp_path, {"600000.SH": 9.4}, account_id=account)
    assert len(orders) == 1
    assert orders[0]["side"] == "sell"
    assert orders[0]["qty"] == 1000
    assert orders[0]["source"].startswith("auto:sweep:")
    # 市价单在盘中钩子撮合: 以同一快照喂 evaluate_intraday 完成成交
    fills = paper.evaluate_intraday(tmp_path, {"600000.SH": 9.4}, account_id=account)
    assert any(f["side"] == "sell" for f in fills)
    pos = paper.load_positions(tmp_path, account).get("600000.SH")
    assert pos is None or pos["qty"] == 0


def test_exit_sweep_skips_above_stop_loss(tmp_path: Path, account: str) -> None:
    orders = paper_auto.run_exit_sweep(tmp_path, {"600000.SH": 9.9}, account_id=account)
    assert orders == []


def test_exit_sweep_respects_pending_sell(tmp_path: Path, account: str) -> None:
    # 先挂一张卖出单占满可卖额度 → 扫描不应重复下单
    order, err = paper.create_order(
        tmp_path, "600000.SH", "sell",
        account_id=account, qty=1000, order_type="market", ref_price=9.4,
    )
    assert err is None and order is not None
    orders = paper_auto.run_exit_sweep(tmp_path, {"600000.SH": 9.4}, account_id=account)
    assert orders == []
