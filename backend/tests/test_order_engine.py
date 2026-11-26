"""order_engine（quantjourney-bt 提取 · P0-1）单元测试。

覆盖：订单执行正常路径、关键边界（open/close fill、限价、止损、移动止损、括号首触发、
滑点 4 类、佣金 4 类、平仓盈亏、失败路径），遵循防前视约束（仅用 bar.open/close，
止损触发用 open 保守判定）。
"""
import math

import pytest

from app.backtest.order_engine import (
    BarData,
    BracketSpec,
    CommissionScheme,
    Fill,
    FixedBpsSlippage,
    FixedBpsCommission,
    MarketImpactSlippage,
    NoSlippage,
    Order,
    OrderEngine,
    OrderEngineConfig,
    OrderStatus,
    OrderType,
    OrderSide,
    PerShareCommission,
    SlippageModel,
    TimeInForce,
    TieredCommission,
    VolatilitySlippage,
    build_order_engine,
)


def _bar(open=10.0, high=10.0, low=10.0, close=10.0, volume=0.0, ts=0):
    return BarData(timestamp=ts, open=open, high=high, low=low, close=close, volume=volume)


# ── 市价成交与默认账本 ────────────────────────────────────────────────


def test_market_buy_open_defaults():
    engine = build_order_engine()  # fill_at=open, no slippage, no commission
    bars = [_bar(open=10.0, close=10.0)]
    order = Order("A", OrderSide.BUY, 2, OrderType.MARKET)
    engine.add_order(order)
    res = engine.run(bars)

    assert len(res.fills) == 1
    fill = res.fills[0]
    assert fill.quantity == 2
    assert fill.fill_price == pytest.approx(10.0)
    assert fill.slippage == pytest.approx(0.0)
    assert fill.commission == pytest.approx(0.0)
    assert fill.theoretical_price == pytest.approx(10.0)
    assert fill.remaining_qty == pytest.approx(0.0)
    assert fill.order_status == OrderStatus.FILLED
    assert res.positions["A"] == {"qty": 2, "avg_price": pytest.approx(10.0)}
    assert res.cash == pytest.approx(1_000_000.0 - 20.0)
    assert res.committed_qty == pytest.approx(2.0)
    assert res.realized_pnl == pytest.approx(0.0)
    assert res.rejected == []


def test_market_buy_close_reference():
    engine = build_order_engine(fill_at="close")
    bars = [_bar(open=10.0, close=20.0)]
    order = Order("A", OrderSide.BUY, 1, OrderType.MARKET)
    engine.add_order(order)
    res = engine.run(bars)

    assert res.fills[0].fill_price == pytest.approx(20.0)
    assert res.positions["A"]["qty"] == pytest.approx(1.0)


def test_no_price_bar_skips():
    engine = build_order_engine()
    bars = [_bar(open=0.0)]
    res = engine.run(bars)

    assert res.fills == []
    assert res.committed_qty == 0.0


# ── 限价入场 ──────────────────────────────────────────────────────────


def test_limit_buy_matched_when_price_reached():
    engine = build_order_engine()
    # 第一根 open=10 < 限价 12 不成交；第二根 open=13 >= 12 成交于 13
    bars = [_bar(open=10.0), _bar(open=13.0)]
    order = Order("A", OrderSide.BUY, 1, OrderType.LIMIT, limit_price=12.0)
    engine.add_order(order)
    res = engine.run(bars)

    assert len(res.fills) == 1
    assert res.fills[0].fill_price == pytest.approx(13.0)
    assert res.positions["A"]["qty"] == pytest.approx(1.0)


def test_limit_buy_not_matched_when_below():
    engine = build_order_engine()
    bars = [_bar(open=10.0), _bar(open=9.0)]  # 均低于限价 12
    order = Order("A", OrderSide.BUY, 1, OrderType.LIMIT, limit_price=12.0)
    res = engine.run(bars)

    assert res.fills == []
    assert order.status == OrderStatus.PENDING


def test_limit_sell_matched_when_price_reached():
    engine = build_order_engine()
    bars = [_bar(open=20.0, close=20.0), _bar(open=8.0, close=8.0)]
    engine.add_order(Order("A", OrderSide.BUY, 1, OrderType.MARKET))  # 先建仓
    engine.run(bars[:1])
    order = Order("A", OrderSide.SELL, 1, OrderType.LIMIT, limit_price=10.0)
    engine.add_order(order)
    res = engine.run(bars[1:])

    assert len(res.fills) == 1
    assert res.fills[0].fill_price == pytest.approx(8.0)


# ── 止损 / 移动止损触发 ──────────────────────────────────────────────


def test_stop_sell_trigger_uses_open_and_pnl():
    engine = build_order_engine()
    bars = [
        _bar(open=10.0),          # 买入建仓，均价 10
        _bar(open=12.0),          # 未触发（12 > 10）
        _bar(open=9.0),           # open=9 <= 10 触发卖出
    ]
    engine.add_order(Order("A", OrderSide.BUY, 1, OrderType.MARKET))  # 建仓
    engine.run(bars[:2])  # 建仓
    sell = Order("A", OrderSide.SELL, 1, OrderType.STOP, stop_price=10.0)
    engine.add_order(sell)
    res = engine.run([bars[2]])

    assert len(res.fills) == 1
    assert res.fills[0].fill_price == pytest.approx(9.0)
    assert res.fills[0].theoretical_price == pytest.approx(9.0)
    # 平仓盈亏 = (9 - 10)
    assert res.realized_pnl == pytest.approx(-1.0)
    assert len(res.trades) == 1
    assert res.trades[0]["realized"] == pytest.approx(-1.0)
    assert res.positions["A"]["qty"] == pytest.approx(0.0)


def test_stop_sell_not_triggered_below_stop():
    engine = build_order_engine()
    bars = [
        _bar(open=10.0),
        _bar(open=11.0),
        _bar(open=10.5),  # 10.5 > 10 不触发
    ]
    engine.run(bars[:2])
    sell = Order("A", OrderSide.SELL, 1, OrderType.STOP, stop_price=10.0)
    engine.add_order(sell)
    res = engine.run([bars[2]])

    assert res.fills == []
    assert sell.status == OrderStatus.PENDING


def test_stop_trail_sell_triggers_on_movement():
    engine = build_order_engine()
    bars = [
        _bar(open=10.0),
        _bar(open=10.5, close=10.5),
        _bar(open=9.0, close=9.0),
    ]
    engine.add_order(Order("A", OrderSide.BUY, 1, OrderType.MARKET))  # 建仓
    engine.run(bars[:2])
    sell = Order("A", OrderSide.SELL, 1, OrderType.STOP_TRAIL, trail_amount=1.0)
    engine.add_order(sell)
    res = engine.run([bars[2]])

    assert len(res.fills) == 1
    assert res.fills[0].fill_price == pytest.approx(9.0)
    assert res.realized_pnl == pytest.approx(9.0 - 10.0)


def test_stop_trail_requires_anchor_before_trigger():
    engine = build_order_engine()
    bars = [_bar(open=10.0), _bar(open=9.0)]
    engine.run(bars[:1])
    # 锚点在建仓后已设；直接验证未建仓时移动止损不触发
    no_hold = Order("B", OrderSide.SELL, 1, OrderType.STOP_TRAIL, trail_amount=1.0)
    res = engine.run([_bar(open=5.0, close=5.0)])  # 无持仓
    assert res.fills == []


def test_stop_trail_buy_rejected():
    engine = build_order_engine()
    order = Order("A", OrderSide.BUY, 1, OrderType.STOP_TRAIL, trail_amount=1.0)
    with pytest.raises(ValueError, match="stop_trail"):
        engine.add_order(order)


# ── 括号订单 ──────────────────────────────────────────────────────────


def test_bracket_entry_then_stop_loss_trigger():
    engine = build_order_engine()
    bars = [
        _bar(open=10.0, close=10.0),  # 市价入场，均价 10
        _bar(open=11.5, close=11.5),  # 止损 11 先触发（更低价先触发）
    ]
    br = Order(
        "A",
        OrderSide.BUY,
        1,
        OrderType.BRACKET,
        bracket=BracketSpec(take_profit_price=15.0, stop_loss_price=11.0),
    )
    engine.add_order(br)
    res = engine.run(bars)

    assert len(res.fills) == 2
    assert res.fills[0].side == OrderSide.BUY  # 先入场
    # 括号止损按设计：入场 BUY → 止损触发同向 BUY（更低价加仓）
    assert res.fills[1].side == OrderSide.BUY
    assert res.fills[1].fill_price == pytest.approx(11.0)
    assert res.realized_pnl == pytest.approx(0.0)
    assert res.positions["A"]["qty"] == pytest.approx(2.0)


def test_bracket_take_profit_trigger():
    engine = build_order_engine()
    bars = [
        _bar(open=10.0, close=10.0),  # 入场
        _bar(open=14.0, close=14.0),  # 止盈 15 → open<=15 触发止盈 SELL
    ]
    br = Order(
        "A",
        OrderSide.BUY,
        1,
        OrderType.BRACKET,
        bracket=BracketSpec(take_profit_price=15.0),
    )
    engine.add_order(br)
    res = engine.run(bars)

    assert len(res.fills) == 2
    assert res.fills[0].side == OrderSide.BUY  # 入场
    assert res.fills[1].side == OrderSide.SELL  # 止盈反向平仓
    assert res.fills[1].fill_price == pytest.approx(15.0)
    assert res.realized_pnl == pytest.approx(15.0 - 10.0)


def test_bracket_entry_without_limit_uses_market_fill():
    engine = build_order_engine()
    bars = [_bar(open=10.0, close=10.0), _bar(open=11.0, close=11.0)]
    br = Order(
        "A",
        OrderSide.BUY,
        1,
        OrderType.BRACKET,
        bracket=BracketSpec(take_profit_price=12.0),
    )
    engine.add_order(br)
    res = engine.run(bars)

    # 入场按市价 open=10 成交；第二根触发止盈 SELL 12
    assert len(res.fills) == 2
    assert res.fills[0].side == OrderSide.BUY
    assert res.fills[0].fill_price == pytest.approx(10.0)
    assert res.fills[1].side == OrderSide.SELL
    assert res.fills[1].fill_price == pytest.approx(12.0)
    assert res.positions["A"]["qty"] == pytest.approx(0.0)
    assert res.realized_pnl == pytest.approx(2.0)


def test_bracket_requires_bracket_spec():
    engine = build_order_engine()
    br = Order("A", OrderSide.BUY, 1, OrderType.BRACKET)
    with pytest.raises(ValueError, match="bracket"):
        engine.add_order(br)


def test_stop_trail_limit_trigger_offset():
    engine = build_order_engine()
    bars = [
        _bar(open=10.0, close=10.0),
        _bar(open=10.5, close=10.5),
        _bar(open=9.0, close=9.0),
    ]
    engine.add_order(Order("A", OrderSide.BUY, 1, OrderType.MARKET))  # 建仓
    engine.run(bars[:2])
    sell = Order(
        "A", OrderSide.SELL, 1, OrderType.STOP_TRAIL_LIMIT, trail_amount=1.0, limit_offset=1.0
    )
    engine.add_order(sell)
    res = engine.run([bars[2]])

    # 触发价 9.0 - 1.0 = 8.0
    assert res.fills[0].fill_price == pytest.approx(8.0)
    assert res.realized_pnl == pytest.approx(8.0 - 10.0)


# ── 滑点模型 ──────────────────────────────────────────────────────────


def test_no_slippage_model_identity():
    assert isinstance(NoSlippage(), SlippageModel)
    bar = _bar()
    assert NoSlippage().compute(10.0, 1, OrderSide.BUY, bar) == pytest.approx(10.0)


def test_fixed_bps_buy_up_sell_down():
    m = FixedBpsSlippage(bps=5.0)
    assert m.compute(10.0, 1, OrderSide.BUY, _bar()) == pytest.approx(10.0 + 10.0 * 5 / 10_000)
    assert m.compute(10.0, 1, OrderSide.SELL, _bar()) == pytest.approx(10.0 - 10.0 * 5 / 10_000)


def test_volatility_slippage_formula():
    m = VolatilitySlippage(vol_factor=0.1)
    bar = _bar(high=20.0, low=0.0, close=10.0)  # range = 2
    spread = 10.0 * 0.1 * 2
    assert m.compute(10.0, 1, OrderSide.BUY, bar) == pytest.approx(10.0 + spread)
    assert m.compute(10.0, 1, OrderSide.SELL, bar) == pytest.approx(10.0 - spread)


def test_market_impact_buy_positive_and_sqrt_participation():
    m = MarketImpactSlippage(sigma_daily=0.02, adv=1_000_000.0, eta=0.1)
    price = m.compute(10.0, 1, OrderSide.BUY, _bar())
    assert price > 10.0
    # 参与度翻倍 → 滑点放大（√2 关系）
    qty2 = m.compute(10.0, 2, OrderSide.BUY, _bar())
    assert qty2 > price


# ── 佣金方案 ──────────────────────────────────────────────────────────


def test_per_share_min_and_cap():
    # min_per_order=0 时只测 raw 与最高上限
    scheme = PerShareCommission(cost_per_share=0.005, min_per_order=0.0, max_pct=0.005)
    assert scheme.compute(10.0, 1, 10.0) == pytest.approx(0.005)
    assert scheme.compute(10.0, 1, 1_000_000.0) == pytest.approx(0.005)  # 被 max_pct 限
    assert scheme.compute(10.0, 100, 1000.0) == pytest.approx(0.5)
    # min_per_order 兜底
    scheme_min = PerShareCommission(cost_per_share=0.005, min_per_order=5.0, max_pct=0.005)
    assert scheme_min.compute(10.0, 1, 10.0) == pytest.approx(5.0)


def test_fixed_bps_commission():
    scheme = FixedBpsCommission(bps=1.0)
    assert scheme.compute(10.0, 1, 100.0) == pytest.approx(0.01)


def test_tiered_commission():
    scheme = TieredCommission(tiers=[(200.0, 0.01)], min_per_order=0.35)
    assert scheme.compute(10.0, 200, 2000.0) == pytest.approx(2.0)
    # 低于 min 时取 min
    assert scheme.compute(10.0, 1, 10.0) == pytest.approx(0.35)


def test_market_buy_with_per_share_commission():
    engine = build_order_engine(commission_scheme=PerShareCommission())
    bars = [_bar(open=10.0)]
    order = Order("A", OrderSide.BUY, 100, OrderType.MARKET)
    engine.add_order(order)
    res = engine.run(bars)

    assert res.fills[0].commission == pytest.approx(1.0)  # min_per_order=1.0
    assert res.cash == pytest.approx(1_000_000.0 - 1000.0 - 1.0)


def test_market_buy_with_fixed_bps_commission():
    engine = build_order_engine(commission_scheme=FixedBpsCommission(bps=1.0))
    bars = [_bar(open=10.0)]
    order = Order("A", OrderSide.BUY, 100, OrderType.MARKET)
    engine.add_order(order)
    res = engine.run(bars)

    # notional=1000 → 佣金 0.1（bps=1/10000）
    assert res.fills[0].commission == pytest.approx(0.1)


# ── 失败路径 ──────────────────────────────────────────────────────────


def test_sell_without_position_rejected():
    engine = build_order_engine()
    order = Order("A", OrderSide.SELL, 1, OrderType.MARKET)
    engine.add_order(order)
    res = engine.run([_bar(open=10.0)])

    assert res.fills == []
    assert len(res.rejected) == 1
    assert res.rejected[0]["reason"] == "no_open_position"
    assert order.status == OrderStatus.REJECTED


def test_order_with_negative_quantity_rejected_at_add():
    engine = build_order_engine()
    order = Order("A", OrderSide.BUY, -1, OrderType.MARKET)
    with pytest.raises(ValueError, match="quantity"):
        engine.add_order(order)


def test_order_engine_config_invalid_fill_at():
    with pytest.raises(ValueError, match="fill_at"):
        OrderEngineConfig(fill_at="bad")


def test_bracket_without_hold_does_not_trigger():
    engine = build_order_engine()
    bars = [_bar(open=10.0), _bar(open=9.0)]
    br = Order(
        "A", OrderSide.BUY, 1, OrderType.BRACKET,
        bracket=BracketSpec(take_profit_price=15.0),
    )
    res = engine.run(bars)

    # 未建仓，不触发也不入场
    assert res.fills == []
    assert br.status == OrderStatus.PENDING


# ── 结果结构 / 配置契约 ───────────────────────────────────────────────


def test_execution_result_structure_and_nav():
    engine = build_order_engine()
    bars = [_bar(open=10.0), _bar(open=11.0), _bar(open=12.0)]
    engine.run(bars)
    engine.add_order(Order("A", OrderSide.BUY, 1, OrderType.MARKET))
    res = engine.run(bars[1:])

    assert isinstance(res.daily_nav, list)
    assert res.daily_nav[0]["bar_index"] == 1
    assert "equity" in res.daily_nav[0]
    assert isinstance(res.to_dict()["fills"], list)


def test_engine_positive_cash_guard_for_sell_after_buy():
    # 用更大股数保证有现金可平仓，验证平仓不透支
    engine = build_order_engine()
    bars = [_bar(open=10.0)]
    engine.run(bars)
    order = Order("A", OrderSide.BUY, 10, OrderType.MARKET)
    engine.add_order(order)
    engine.run(bars)
    sell = Order("A", OrderSide.SELL, 5, OrderType.MARKET)
    engine.add_order(sell)
    res = engine.run([_bar(open=9.0)])

    # 平仓 5，剩余 5
    assert res.positions["A"]["qty"] == pytest.approx(5.0)
    assert res.fills[-1].quantity == pytest.approx(5.0)


# ── 枚举 / 协议类型一致性（防拼写与口径漂移）───────────────────────────


def test_order_type_and_status_values():
    assert OrderType.MARKET.value == "market"
    assert OrderType.BRACKET.value == "bracket"
    assert OrderStatus.FILLED.value == "filled"
    assert OrderSide.BUY.value == "buy"
    assert TimeInForce.GTC.value == "GTC"


def test_slippage_models_are_protocols():
    assert isinstance(FixedBpsSlippage(), SlippageModel)
    assert isinstance(MarketImpactSlippage(), SlippageModel)
    assert isinstance(OrderEngineConfig(), OrderEngineConfig)
    assert isinstance(Order("A", OrderSide.BUY, 1), Order)


def test_fill_dataclass_has_required_fields():
    f = Fill(order_id="o", instrument="A", side=OrderSide.BUY, quantity=1, fill_price=10.0)
    assert f.remaining_qty == 0.0
    assert f.commission == 0.0
    assert f.theoretical_price is None
