"""P1-C4 组合账本（StrategyBook）自包含移植测试。

覆盖：
- WeightCostModel 协议、FixedBpsWeightCostModel 的隐含交易成本与递归成本路径。
- RebalanceEngine 在简单日历策略下的向量化路径（实际权重 + rebalance 标志）。
- StrategyBook：多 sleeve 资本分配、sleeve_nav 对齐、实际分配与会计恒等式校验（正常 + 异常路径）。
- 会计恒等式失败必须抛 AssertionError（直接调用 _assert_accounting_identity）。

仅依赖项目既有 pandas/numpy。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest.portfolio_book import (
    FixedBpsWeightCostModel,
    RebalanceAt,
    RebalanceEngine,
    RebalancePolicy,
    StrategyBook,
    solve_recursive_weight_costs,
)


def _index(n: int = 5) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(
        [pd.Timestamp("2024-01-01") + pd.Timedelta(days=i) for i in range(n)]
    )


def _synthetic_sleeves(n: int = 6) -> dict[str, pd.Series]:
    """构造多 sleeve NAV：价格单调上涨，制造可复算的净值与组合收益。"""
    idx = _index(n)
    sleeves: dict[str, pd.Series] = {}
    for i, name in enumerate(("s1", "s2", "s3", "s4", "s5", "s6")):
        nav = 1_000_000.0 * (1 + 0.01 * i)
        sleeves[name] = pd.Series(
            [nav + (1.0 * i) * 0.0 for _ in idx], index=idx, name=name
        )
    # 第二 sleeve 略有不同，确保对齐后对齐恒等可复算
    sleeves["s2"] = pd.Series(
        [1_000_000.0 * 1.01 + 2 * 0.0 * 0 + (i - 0.5) * 1.0 for i in range(n)],
        index=idx,
        name="s2",
    )
    return sleeves


def test_rebalance_engine_simple_calendar():
    """简单日历策略：首个 bar 必再平衡，其余按日历，输出实际权重与标志。"""
    policy = RebalancePolicy(frequency="D")
    engine = RebalanceEngine(policy, periods_per_year=252)
    idx = _index(5)
    w = pd.DataFrame(
        np.array([[0.4, 0.3], [0.5, 0.5], [0.55, 0.45], [0.56, 0.44], [0.57, 0.43]]),
        index=idx,
        columns=["A", "B"],
        dtype=float,
    )
    r = pd.DataFrame(
        np.array([[0.01, 0.01], [0.02, 0.02], [0.01, 0.01], [0.02, 0.02], [0.01, 0.01]]),
        index=idx,
        columns=["A", "B"],
        dtype=float,
    )
    actual, flags = engine.run(w, r)
    assert actual.shape == w.shape
    assert flags.shape == (len(idx),)
    assert flags.iloc[0]  # 首个 bar 必再平衡
    assert engine.stats["rebalance_count"] >= 1


def test_rebalance_engine_unsupported_field_warns_once():
    """非 CLOSE 的 rebalance_at 触发一次性提示（既有保护）。"""
    import warnings

    policy = RebalancePolicy(rebalance_at=RebalanceAt.OPEN)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        engine = RebalanceEngine(policy, periods_per_year=252, warn_unsupported=True)
    assert any("not honored" in str(w.message).lower() for w in caught)


def test_solve_recursive_weight_costs_empty_index():
    """空权重路径：返回初始资本，不抛错。"""
    idx = pd.DatetimeIndex([])
    w = pd.DataFrame(index=idx, columns=["A", "B"])
    nav = pd.Series([], index=idx)
    gross = pd.Series([], index=idx)
    result = solve_recursive_weight_costs(
        actual_weights=w,
        prices=pd.DataFrame(index=idx, columns=["A", "B"]),
        gross_returns=gross,
        initial_capital=100_000.0,
        rebalance_flags=pd.Series(dtype=bool, index=idx),
        cost_model=FixedBpsWeightCostModel(total_bps=0.0),
    )
    nav, returns, breakdown = result
    assert nav.shape == (0,)


def test_fixed_bps_cost_model_bps_scaling():
    """total_bps 线性缩放：同一交易序列下成本与 total_bps 成正比。

    首个 bar 再平衡持有 1.0 单位 A，产生一次 full re-entry 交易；
    无论具体名义交易值多少，成本 = 交易值 × total_bps/10000，
    因此总成本与 total_bps 成严格正比。
    """
    idx = _index(3)
    w = pd.DataFrame(
        np.array([[1.0, 0.0], [1.0, 0.0], [0.0, 1.0]]),
        index=idx,
        columns=["A", "B"],
        dtype=float,
    )
    pr = pd.DataFrame({"A": [100.0] * 3, "B": [100.0] * 3}, index=idx)
    nav = pd.Series([100_000.0] * 3, index=idx)
    flags = pd.Series([True, True, True], index=idx, dtype=bool)
    base = FixedBpsWeightCostModel(total_bps=1.0)
    breakdown1 = base.compute(actual_weights=w, prices=pr, nav=nav, rebalance_flags=flags)
    scaled = FixedBpsWeightCostModel(total_bps=10.0)
    breakdown10 = scaled.compute(actual_weights=w, prices=pr, nav=nav, rebalance_flags=flags)
    assert np.isclose(breakdown1.total_cost.iloc[0], breakdown10.total_cost.iloc[0] / 10.0)
    assert np.isclose(breakdown10.total_cost.iloc[0], breakdown1.total_cost.iloc[0] * 10.0)


def test_fixed_bps_min_trade_value_filters_small():
    """min_trade_value 过滤后的交易不计成本：交易名义值 < min 时成本为 0。"""
    model = FixedBpsWeightCostModel(total_bps=1.0, min_trade_value=100.0)
    idx = _index(2)
    w = pd.DataFrame(
        np.array([[0.0001, 0.0001], [0.0001, 0.0001]]),
        index=idx,
        columns=["A", "B"],
        dtype=float,
    )
    pr = pd.DataFrame({"A": [100.0] * 2, "B": [100.0] * 2}, index=idx)
    nav = pd.Series([100_000.0] * 2, index=idx)
    flags = pd.Series([True, False], index=idx, dtype=bool)
    breakdown = model.compute(actual_weights=w, prices=pr, nav=nav, rebalance_flags=flags)
    # 名义交易值 = 0.1×100 = 10 < 100 → 被过滤，成本为 0
    assert np.isclose(breakdown.total_cost.iloc[0], 0.0)


def _book_sleeves(n: int = 6, capital: float = 1_000_000.0):
    idx = _index(n)
    sleeves = _synthetic_sleeves(n)
    sleeves["s3"] = pd.Series([capital * 0.6] * n, index=idx, name="s3")
    return sleeves, idx


def test_strategy_book_run_returns_ledger():
    """StrategyBook 跑通：产出净值、收益、实际分配、sleeve_nav 等账本字段。"""
    sleeves, idx = _book_sleeves()
    # 静态分配：s1~s6 等权
    allocations = {name: 1.0 / len(sleeves) for name in sleeves}
    book = StrategyBook(
        sleeves=sleeves,
        allocations=allocations,
        initial_capital=1_000_000.0,
        series_kind="nav",
    )
    result = book.run()
    assert result.sleeve_nav.shape == (len(idx), len(sleeves))
    assert result.actual_allocations.shape == (len(idx), len(sleeves))
    assert result.sleeve_values.shape == (len(idx), len(sleeves))
    assert result.cash.shape == (len(idx),)
    assert result.turnover.shape == (len(idx),)
    assert result.costs.shape == (len(idx),)
    assert result.stats["sleeve_count"] == len(sleeves)
    assert result.stats["period_count"] == len(idx)


def test_strategy_book_accounting_identity_holds():
    """正常跑通的账本满足会计恒等式：cash + Σsleeve_values == nav。"""
    sleeves, idx = _book_sleeves(8)
    allocations = {name: 1.0 / len(sleeves) for name in sleeves}
    book = StrategyBook(
        sleeves=sleeves,
        allocations=allocations,
        initial_capital=1_000_000.0,
        series_kind="nav",
    )
    result = book.run()
    nav = result.nav.to_numpy(float)
    cash = result.cash.to_numpy(float)
    sleeve_values = result.sleeve_values.to_numpy(float)
    reconciled = cash + sleeve_values.sum(axis=1)
    np.testing.assert_allclose(nav, reconciled, rtol=1e-10, atol=1e-8)


def test_strategy_book_returns_kind():
    """series_kind='returns'：净值按 (1+ret).cumprod() 归一化为 NAV。"""
    idx = _index(4)
    returns = pd.DataFrame(
        np.array([[0.01, -0.005, 0.0], [0.01, 0.01, 0.0], [0.02, 0.02, 0.0], [0.0, 0.0, 0.0]]),
        index=idx,
        columns=["a", "b", "c"],
        dtype=float,
    )
    sleeves = {
        "a": pd.Series([0.01, 0.01, 0.02, 0.0], index=idx, name="a"),
        "b": pd.Series([-0.005, 0.01, 0.02, 0.0], index=idx, name="b"),
        "c": pd.Series([0.0, 0.0, 0.0, 0.0], index=idx, name="c"),
    }
    book = StrategyBook(
        sleeves=sleeves,
        allocations={"a": 0.5, "b": 0.5, "c": 0.0},
        initial_capital=100_000.0,
        series_kind="returns",
    )
    result = book.run()
    assert result.sleeve_nav.iloc[0, 0] > 0.0


def test_strategy_book_bad_allocations_rejected():
    """未知 sleeve 必须在运行分配时抛 ValueError。"""
    sleeves, _ = _book_sleeves(4)
    allocations = {"unknown": 0.5}
    book = StrategyBook(
        sleeves=sleeves,
        allocations=allocations,
        initial_capital=1_000_000.0,
        series_kind="nav",
    )
    with pytest.raises(ValueError):
        book.run()


def test_strategy_book_non_positive_capital_rejected():
    sleeves, _ = _book_sleeves(4)
    with pytest.raises(ValueError):
        StrategyBook(
            sleeves=sleeves,
            allocations={"a": 1.0, "b": 0.0},
            initial_capital=0.0,
            series_kind="nav",
        )


def test_assert_accounting_identity_raises_on_mismatch():
    """直接校验：导航与现金+持仓不等时抛 AssertionError。"""
    idx = _index(2)
    nav = pd.Series([150.0, 150.0], index=idx)
    cash = pd.Series([50.0, 50.0], index=idx)
    sleeve_values = pd.DataFrame([[50.0, 0.0], [50.0, 0.0]], index=idx)
    with pytest.raises(AssertionError):
        StrategyBook._assert_accounting_identity(nav, cash, sleeve_values)
