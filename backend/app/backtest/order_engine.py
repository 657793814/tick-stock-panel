"""订单模式执行引擎 —— quantjourney-bt 提取 · tick-stock-panel 自包含模块 · P0-1。

本模块从 quantjourney-bt(``backtester/execution/``) 提取策略与回测执行能力，以自包含、
与现有主链路并存的方式落地，**不改动**现有 entries/exits 信号回测（services/backtest.py 的
向量化 T+1 匹配）路径。

覆盖能力（对齐 quantjourney-bt 提取清单 P0-1/B1+B2/C1）：
- 订单类型：MARKET / LIMIT / STOP / STOP_LIMIT / STOP_TRAIL / STOP_TRAIL_LIMIT / BRACKET
  （OCO 为候选，本阶段不实现，见模块末尾说明）。
- 执行语义：fill_at(open/close)、blotter（持仓均价 + 现金 + 平仓盈亏）、成交量参与。
- 成本模型：SlippageModel 协议 + 4 种（NoSlippage / FixedBps / Volatility / MarketImpact）；
  CommissionScheme 协议 + 4 种（Zero / PerShare / FixedBps / Tiered）。

关键约束（遵循 quantjourney-bt 的开盘成交无前视规则）：
- 成交参考价使用 bar 的 open/close，订单匹配在 bar 进度上逐步进行；
- 止损类触发用 bar.open 保守判定，不读取未来 high/low/close；
- 每根 bar 内顺序：先触发（止损/移动止损/括号首触发）→ 再入场（限价→市价）。
- 默认行为：无滑点、无佣金（对应现有回测默认口径可单独用）；新增能力默认关闭、可选。
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

# ── 枚举与基础数据 ──────────────────────────────────────────────────────


class OrderType(Enum):
    """订单类型（与 quantjourney-bt ``backtester/execution/order_types.py`` 对齐）。"""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    STOP_TRAIL = "stop_trail"
    STOP_TRAIL_LIMIT = "stop_trail_limit"
    BRACKET = "bracket"
    # OCO（候选，P0 不实现；见模块说明）
    OCO = "oco"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderStatus(Enum):
    PENDING = "pending"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TimeInForce(Enum):
    """订单有效期（bar 模拟下 DAY 仅首个有效 bar）。"""

    GTC = "GTC"
    DAY = "DAY"
    GTD = "GTD"


@dataclass(frozen=True, slots=True)
class BarData:
    """单根 OHLCV bar，用于成交匹配。"""

    timestamp: object
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# ── 滑点模型 ───────────────────────────────────────────────────────────


@runtime_checkable
class SlippageModel(Protocol):
    """滑点模型协议（quantjourney-bt ``backtester/execution/slippage.py``）。"""

    def compute(
        self, price: float, quantity: float, side: OrderSide, bar: BarData
    ) -> float:
        """返回加滑点后的成交价。"""
        ...


@dataclass(frozen=True, slots=True)
class NoSlippage:
    """无滑点 —— 按理论成交价成交（默认）。"""

    def compute(self, price, quantity, side, bar) -> float:
        return price


@dataclass(frozen=True, slots=True)
class FixedBpsSlippage:
    """固定基点滑点：买 +价、卖 -价。bps 为半价（如 5.0 = 0.05% 单边）。"""

    bps: float = 5.0

    def compute(self, price, quantity, side, bar) -> float:
        spread = abs(price) * self.bps / 10_000
        return price + spread if side == OrderSide.BUY else price - spread


@dataclass(frozen=True, slots=True)
class VolatilitySlippage:
    """波动率滑点：spread = price × vol_factor × (high − low) / close。"""

    vol_factor: float = 0.1

    def compute(self, price, quantity, side, bar) -> float:
        if bar.close == 0:
            return price
        bar_range = abs(bar.high - bar.low) / abs(bar.close)
        spread = abs(price) * self.vol_factor * bar_range
        return price + spread if side == OrderSide.BUY else price - spread


@dataclass(frozen=True, slots=True)
class MarketImpactSlippage:
    """市场冲击滑点（Almgren 风格）：cost ∝ σ × √(Q/ADV)。"""

    sigma_daily: float = 0.02
    adv: float = 1_000_000.0
    eta: float = 0.1

    def compute(self, price, quantity, side, bar) -> float:
        participation = abs(quantity) / max(self.adv, 1.0)
        impact = abs(price) * self.eta * self.sigma_daily * math.sqrt(participation)
        return price + impact if side == OrderSide.BUY else price - impact


# ── 佣金方案 ───────────────────────────────────────────────────────────


@runtime_checkable
class CommissionScheme(Protocol):
    """佣金方案协议（quantjourney-bt ``backtester/execution/commission.py``）。"""

    def compute(
        self, price: float, quantity: float, notional: float
    ) -> float:
        """返回单笔成交的佣金总额。"""
        ...


@dataclass(frozen=True, slots=True)
class ZeroCommission:
    def compute(self, price, quantity, notional) -> float:
        return 0.0


@dataclass(frozen=True, slots=True)
class PerShareCommission:
    """按股固定佣金，含最低/最高上限。"""

    cost_per_share: float = 0.005
    min_per_order: float = 1.0
    max_pct: float = 0.005

    def compute(self, price, quantity, notional) -> float:
        raw = abs(quantity) * self.cost_per_share
        capped = min(raw, abs(notional) * self.max_pct) if self.max_pct else raw
        return max(capped, self.min_per_order)


@dataclass(frozen=True, slots=True)
class FixedBpsCommission:
    """按成交金额固定基点佣金。"""

    bps: float = 1.0
    min_per_order: float = 0.0

    def compute(self, price, quantity, notional) -> float:
        raw = abs(notional) * self.bps / 10_000
        return max(raw, self.min_per_order)


@dataclass(frozen=True, slots=True)
class TieredCommission:
    """按成交量分级的佣金。"""

    tiers: list[tuple[float, float]]
    min_per_order: float = 0.35

    def compute(self, price, quantity, notional) -> float:
        qty_remaining = abs(quantity)
        total = 0.0
        prev_threshold = 0.0
        for threshold, rate in sorted(self.tiers, key=lambda t: t[0]):
            tier_qty = min(qty_remaining, threshold - prev_threshold)
            if tier_qty <= 0:
                break
            total += tier_qty * rate
            qty_remaining -= tier_qty
            prev_threshold = threshold
        return max(total, self.min_per_order)


# ── 订单 / 成交 / 括号 ─────────────────────────────────────────────────


@dataclass
class BracketSpec:
    """括号订单：入场 + 止盈 + 止损。"""

    take_profit_price: float | None = None
    stop_loss_price: float | None = None
    take_profit_pct: float | None = None
    stop_loss_pct: float | None = None
    take_profit_type: OrderType = OrderType.LIMIT
    stop_loss_type: OrderType = OrderType.STOP
    stop_limit_price: float | None = None
    stop_limit_offset: float | None = None
    trail_amount: float | None = None
    trail_percent: float | None = None


@dataclass
class Order:
    """统一订单对象（quantjourney-bt ``order_types.Order``）。"""

    instrument: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    # 价格参数
    limit_price: float | None = None
    stop_price: float | None = None
    limit_offset: float | None = None
    # 移动止损
    trail_amount: float | None = None
    trail_percent: float | None = None
    # 括号
    bracket: BracketSpec | None = None
    # 元数据
    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    created_at: object = None
    time_in_force: TimeInForce = TimeInForce.GTC
    expires_after_bars: int | None = None
    tag: str = ""
    rejection_reason: str | None = None
    # 内部状态（移动止损锚点）
    _trail_anchor: float | None = None

    @property
    def remaining_qty(self) -> float:
        return self.quantity - self.filled_qty

    @property
    def is_active(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)

    def cancel(self) -> None:
        if self.is_active:
            self.status = OrderStatus.CANCELLED

    def __repr__(self) -> str:
        return (
            f"Order({self.order_type.value} {self.side.value} "
            f"{self.remaining_qty:.1f} {self.instrument} "
            f"[{self.status.value}] id={self.order_id[:8]})"
        )


@dataclass
class Fill:
    """一笔成交的结果。"""

    order_id: str
    instrument: str
    side: OrderSide
    quantity: float
    fill_price: float  # 加滑点后的成交价
    slippage: float = 0.0  # 成交价与理论价之差（买正、卖负）
    commission: float = 0.0  # 佣金总额
    timestamp: object = None
    theoretical_price: float | None = None  # 理论成交价（成交前，未加滑点）
    remaining_qty: float = 0.0  # 本次成交后的剩余量
    order_status: OrderStatus = OrderStatus.FILLED


# ── 执行配置与结果 ─────────────────────────────────────────────────────


@dataclass
class OrderEngineConfig:
    """订单引擎配置（对应 quantjourney-bt 的 slippage_model / commission_scheme /
    fill_at / 成交量参与 等构造参数）。"""

    slippage_model: SlippageModel | None = None  # None → NoSlippage
    commission_scheme: CommissionScheme | None = None  # None → ZeroCommission
    fill_at: str = "open"  # "open" | "close"
    time_in_force: TimeInForce = TimeInForce.GTC
    max_positions: int | None = None  # 每标的持仓数量上限
    max_volume_participation: float = 1.0  # 成交量参与度上限（影响市场冲击滑点）
    volume_lookback: float | None = None
    expected_open_volume_fraction: float | None = None

    def __post_init__(self) -> None:
        if self.fill_at not in ("open", "close"):
            raise ValueError("fill_at must be 'open' or 'close'")


@dataclass
class ExecutionResult:
    """执行结果：成交明细 + 交易（平仓）+ 持仓 + 净值 + 拒绝记录。"""

    fills: list[Fill] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    positions: dict[str, dict] = field(default_factory=dict)  # instrument -> {qty, avg_price}
    cash: float = 0.0
    equity: float = 0.0
    realized_pnl: float = 0.0
    committed_qty: float = 0.0
    rejected: list[dict] = field(default_factory=list)
    daily_nav: list[dict] = field(default_factory=list)  # 逐 bar 净值

    def to_dict(self) -> dict:
        import dataclasses

        return dataclasses.asdict(self)


# ── 执行引擎 ──────────────────────────────────────────────────────────


class OrderEngine:
    """blotter 式订单执行引擎：把订单序列按 bar 逐步匹配成交，维护持仓与现金账本。

    与现有回测服务（entries/exits 状态机）为不同执行路径，可并存。
    """

    def __init__(self, config: OrderEngineConfig, cash: float = 1_000_000.0):
        self.config = config
        self.cash = float(cash)
        self.realized_pnl = 0.0
        self.fills: list[Fill] = []
        self.trades: list[dict] = []
        self.daily_nav: list[dict] = []
        self.rejected: list[dict] = []
        self.committed_qty = 0.0
        self._pending: list[Order] = []
        self._positions: dict[str, dict] = {}

        # 成交量参与：仅对 MarketImpact 按 sqrt(participation) 缩放（简化语义）
        self._participation = max(0.0, float(config.max_volume_participation or 1.0))

    # ── 查询 ───────────────────────────────────────────────────────────

    @property
    def positions(self) -> dict[str, dict]:
        return self._positions

    def equity(self) -> float:
        return self.cash + sum(
            (p["qty"] or 0.0) * (p["avg_price"] or 0.0)
            for p in self.positions.values()
        )

    def pending(self) -> list[Order]:
        return list(self._pending)

    def cancel(self, order_id: str) -> None:
        for o in self._pending:
            if o.order_id == order_id:
                o.cancel()
                return

    # ── 下单 ───────────────────────────────────────────────────────────

    def add_order(self, order: Order) -> Order:
        if order.quantity <= 0:
            raise ValueError("quantity must be positive")
        if order.order_type in (OrderType.STOP_TRAIL, OrderType.STOP_TRAIL_LIMIT) and order.side == OrderSide.BUY:
            # 移动止损仅支持 SELL（止盈概念），避免静默错误语义
            raise ValueError("stop_trail 类订单仅支持 SELL 侧")
        if order.order_type == OrderType.BRACKET and not order.bracket:
            raise ValueError("BRACKET 订单必须提供 bracket")
        self._pending.append(order)
        return order

    def run(self, bars: list[BarData]) -> ExecutionResult:
        """按 bar 顺序逐步匹配执行。bars 须按时间升序。"""
        self.fills = []
        self.trades = []
        self.daily_nav = []
        self.rejected = []
        self.committed_qty = 0.0
        self.realized_pnl = 0.0
        for bar in bars:
            if bar.open <= 0:
                continue
            i = len(bars) - 1
            # 每根 bar：先触发（止损/移动止损/括号首触发）→ 再入场（限价→市价）
            for order in list(self._pending):
                # 1) 触发（止损/移动止损/括号首触发）
                trig = None
                if (
                    order.order_type in (
                        OrderType.STOP,
                        OrderType.STOP_LIMIT,
                        OrderType.STOP_TRAIL,
                        OrderType.STOP_TRAIL_LIMIT,
                    )
                    and self._triggered(order, bar)
                ):
                    trig = self._trigger_prices(order, bar)
                elif (
                    order.order_type == OrderType.BRACKET
                    and self._bracket_first_trigger(order, bar) is not None
                ):
                    trig = self._bracket_first_trigger(order, bar)
                if trig is not None:
                    try:
                        self._process_trigger(order, bar, *trig)
                    except ValueError as exc:
                        self.rejected.append(
                            {
                                "order_id": order.order_id,
                                "instrument": order.instrument,
                                "reason": str(exc),
                            }
                        )
                        order.status = OrderStatus.REJECTED
                    continue
                # 2) 入场执行：限价（含 bracket 限价入场）→ 市价
                if (
                    order.order_type in (OrderType.LIMIT, OrderType.BRACKET)
                    and order.limit_price is not None
                    and self._limit_due(order, bar.open)
                ):
                    try:
                        self._process_limit(order, bar)
                    except ValueError as exc:
                        self.rejected.append(
                            {
                                "order_id": order.order_id,
                                "instrument": order.instrument,
                                "reason": str(exc),
                            }
                        )
                        order.status = OrderStatus.REJECTED
                    continue
                if order.order_type in (OrderType.MARKET, OrderType.BRACKET):
                    try:
                        self._process_market(order, bar)
                    except ValueError as exc:
                        self.rejected.append(
                            {
                                "order_id": order.order_id,
                                "instrument": order.instrument,
                                "reason": str(exc),
                            }
                        )
                        order.status = OrderStatus.REJECTED
            # 消耗本 bar 已成交/被拒的订单；仍 PENDING（未达条件）的保留待后续 bar。
            # BRACKET 订单入场后仍需在本单后续 bar 触发止盈/止损，故保留。
            self._pending = [
                o
                for o in self._pending
                if o.status == OrderStatus.PENDING or o.order_type == OrderType.BRACKET
            ]
            self._prune()
            self.daily_nav.append({"bar_index": i, "equity": self.equity()})
        self.committed_qty = sum(f.quantity for f in self.fills)
        return ExecutionResult(
            fills=self.fills,
            trades=self.trades,
            positions=dict(self.positions),
            cash=self.cash,
            equity=self.equity(),
            realized_pnl=self.realized_pnl,
            committed_qty=self.committed_qty,
            rejected=list(self.rejected),
            daily_nav=list(self.daily_nav),
        )

    # ── 匹配规则 ───────────────────────────────────────────────────────

    def _triggered(self, order: Order, bar: BarData) -> bool:
        """止损/移动止损的触发判定，用 bar.open 保守触发（无前视）。"""
        op = bar.open
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            if order.side == OrderSide.BUY:
                return op >= (order.stop_price or float("-inf"))
            return op <= (order.stop_price or float("inf"))
        if order.order_type in (OrderType.STOP_TRAIL, OrderType.STOP_TRAIL_LIMIT):
            if order.side == OrderSide.BUY:  # 不支持，拒绝
                return False
            stop = self._trail_stop(order, bar)
            return stop is not None and op <= stop
        return False

    def _bracket_first_trigger(self, order: Order, bar: BarData) -> tuple[OrderSide, float] | None:
        """括号订单每根 bar 只触发一次：优先止损（更低价先触发），再止盈。

        返回 (side, price) 或 None。
        """
        br = order.bracket
        if br is None:
            return None
        hold = self.positions.get(order.instrument) is not None and (
            self.positions[order.instrument].get("qty") or 0.0
        ) > 0
        if not hold:
            return None
        op = bar.open
        pos_avg = self.positions[order.instrument].get("avg_price") or op
        entry_side = order.side
        # 入场 BUY → 止损 BUY(更低价)、止盈 SELL(更高价)；入场 SELL 反之
        tp_side = OrderSide.SELL if entry_side == OrderSide.BUY else OrderSide.BUY
        sl_side = entry_side

        tp = br.take_profit_price
        tp_pct = br.take_profit_pct
        sl = br.stop_loss_price
        sl_pct = br.stop_loss_pct

        # 止损优先（更低价先触发），其次止盈
        if sl is not None:
            if entry_side == OrderSide.BUY:  # 入场 BUY → 止损 BUY
                if op >= sl:
                    return OrderSide.BUY, sl
            else:  # 入场 SELL → 止损 SELL
                if op <= sl:
                    return OrderSide.SELL, sl
        if tp is not None:
            if entry_side == OrderSide.BUY:  # 入场 BUY → 止盈 SELL
                if op <= tp:
                    return OrderSide.SELL, tp
            else:  # 入场 SELL → 止盈 BUY
                if op >= tp:
                    return OrderSide.BUY, tp
        if sl is None and tp is None:
            if tp_pct is not None:
                tp = pos_avg * (1 - tp_pct)
                if entry_side == OrderSide.BUY:
                    if op <= tp:
                        return OrderSide.SELL, tp
                else:
                    if op >= tp:
                        return OrderSide.BUY, tp
            if sl_pct is not None:
                sl = pos_avg * (1 + sl_pct)
                if entry_side == OrderSide.BUY:
                    if op >= sl:
                        return OrderSide.BUY, sl
                else:
                    if op <= sl:
                        return OrderSide.SELL, sl
        return None

    def _limit_due(self, order: Order, op: float) -> bool:
        """限价单在参考价到达限价时成交（开卖触发用卖出参考，开买用买入参考）。"""
        limit = order.limit_price
        if limit is None:
            return False
        if order.side == OrderSide.BUY:
            return op >= limit
        return op <= limit

    def _trail_stop(self, order: Order, bar: BarData) -> float | None:
        if order._trail_anchor is None:
            # 无显式锚点：用当前持仓均价作为移动止损基准
            pos = self.positions.get(order.instrument)
            if pos is not None and pos.get("qty", 0) > 0:
                order._trail_anchor = pos["avg_price"]
            else:
                return None
        dist = order.trail_amount if order.trail_amount is not None else order.trail_percent * order._trail_anchor
        if dist is None:
            return None
        return order._trail_anchor - dist

    def _trigger_prices(self, order: Order, bar: BarData) -> tuple[OrderSide, float]:
        """返回触发时的 (side, price)。

        普通 STOP / STOP_LIMIT：触发价取 bar.open（市场价，防前视）；
        BRACKET / STOP_TRAIL 等：触发价取固定执行价（止损位/移动止损位/括号止盈止损位）。
        """
        if order.order_type in (OrderType.STOP, OrderType.STOP_LIMIT):
            return order.side, bar.open
        if order.order_type in (OrderType.STOP_TRAIL, OrderType.STOP_TRAIL_LIMIT):
            stop = self._trail_stop(order, bar)
            if stop is None:
                raise ValueError("stop_trail 未设置移动止损锚点")
            return order.side, stop
        raise ValueError("unsupported order type for trigger")

    def _prune(self) -> None:
        if not self._pending:
            self._pending.clear()

    # ── 成交处理（blotter 记账）───────────────────────────────────────

    def _compute_price(
        self, order: Order, base_price: float, quantity: float, bar: BarData
    ) -> float:
        model = self.config.slippage_model
        if model is not None:
            price = float(model.compute(base_price, quantity, order.side, bar))
            # 成交量参与：仅对 MarketImpact 按 sqrt(participation) 缩放（简化语义）
            if model is MarketImpactSlippage and self._participation != 1.0:
                price = price * math.sqrt(self._participation)
        else:
            price = base_price
        return price

    def _apply_fill(
        self,
        order: Order,
        *,
        quantity: float,
        price: float,
        fee: float,
        base_price: float,
        bar: BarData,
        limit_price: float | None = None,
        direction: OrderSide | None = None,
    ) -> None:
        # direction 优先于 order.side：括号/触发成交可能按触发方向反向执行
        side = direction if direction is not None else order.side
        qty = float(quantity)
        notional = qty * price
        slippage = price - base_price
        commission = float(fee)

        # 更新持仓/现金/盈亏
        pos = self.positions.get(order.instrument)
        if side == OrderSide.BUY:
            if pos is None:
                pos = {"qty": 0.0, "avg_price": 0.0}
                self.positions[order.instrument] = pos
            pos["qty"] = pos["qty"] + qty
            if pos["qty"]:
                pos["avg_price"] = (pos["qty"] * pos["avg_price"] + qty * price) / pos["qty"]
            self.cash -= notional + commission
        else:
            if pos is None or pos["qty"] <= 1e-9:
                self.rejected.append(
                    {"order_id": order.order_id, "instrument": order.instrument,
                     "reason": "no_open_position"}
                )
                order.status = OrderStatus.REJECTED
                return
            pos["qty"] -= qty
            if pos["qty"] <= 1e-9:
                # 平仓：按均价计算已实现盈亏
                closed_qty = qty
                realized = closed_qty * (price - (pos["avg_price"] or 0.0)) - commission
                self.realized_pnl += realized
                self.trades.append(
                    {
                        "side": "sell",
                        "instrument": order.instrument,
                        "qty": closed_qty,
                        "price": price,
                        "avg_price": pos["avg_price"],
                        "fee": commission,
                        "realized": realized,
                    }
                )
            else:
                self.trades.append(
                    {
                        "side": "sell",
                        "instrument": order.instrument,
                        "qty": qty,
                        "price": price,
                        "avg_price": pos["avg_price"],
                        "fee": commission,
                        "realized": 0.0,
                    }
                )
            self.cash += notional - commission

        # 校验 max_positions（每标的持仓数量预算）
        if self.config.max_positions is not None and self.config.max_positions < 0:
            raise ValueError("max_positions must be >= 0")
        if self.config.max_positions is not None and pos is not None:
            if pos["qty"] > 0 and pos["qty"] + qty > self.config.max_positions:
                raise ValueError(
                    f"max_positions exceeded for {order.instrument}: "
                    f"{pos['qty']} + {qty} > {self.config.max_positions}"
                )

        # 更新订单状态与成交明细（remaining_qty 为本次成交后的剩余量）
        order.filled_qty = order.filled_qty + qty
        if order.avg_fill_price is None:
            order.avg_fill_price = price
        else:
            order.avg_fill_price = (
                order.avg_fill_price * order.filled_qty + price * qty
            ) / (order.filled_qty + qty)
        order.status = OrderStatus.FILLED

        # 移动止损锚点：SELL 移动止损以本次成交价作为锚
        if (
            order.order_type in (OrderType.STOP_TRAIL, OrderType.STOP_TRAIL_LIMIT)
            and side == OrderSide.SELL
        ):
            order._trail_anchor = price

        self.fills.append(
            Fill(
                order_id=order.order_id,
                instrument=order.instrument,
                side=side,
                quantity=qty,
                fill_price=price,
                slippage=slippage,
                commission=commission,
                timestamp=bar.timestamp if bar else None,
                theoretical_price=base_price,
                remaining_qty=order.remaining_qty,
                order_status=order.status,
            )
        )
        self.committed_qty += qty

    def _process_market(self, order: Order, bar: BarData) -> None:
        fill_price = bar.open if self.config.fill_at == "open" else bar.close
        if fill_price <= 0:
            raise ValueError("fill price must be > 0")
        price = self._compute_price(
            order, fill_price, order.quantity, bar
        )
        fee = self._commission(order, price, order.quantity * price)
        self._apply_fill(
            order,
            quantity=order.quantity,
            price=price,
            fee=fee,
            base_price=fill_price,
            bar=bar,
        )

    def _process_limit(self, order: Order, bar: BarData) -> None:
        op = bar.open
        limit = order.limit_price
        if limit is None:
            raise ValueError("LIMIT / STOP_LIMIT 订单必须设置 limit_price")
        if order.side == OrderSide.BUY and op < limit:
            return  # 未到达限价，继续等待
        if order.side == OrderSide.SELL and op > limit:
            return
        # 限价被触发：以当前 bar.open 为成交基准（防前视，仅用 open）
        price = self._compute_price(
            order, op, order.quantity, bar
        )
        fee = self._commission(order, price, order.quantity * price)
        self._apply_fill(
            order,
            quantity=order.quantity,
            price=price,
            fee=fee,
            base_price=op,
            bar=bar,
        )

    def _process_trigger(self, order: Order, bar: BarData, trigger_side: OrderSide, trigger_price: float) -> None:
        # 移动止损限价偏移（stop_trail_limit）
        if (
            order.order_type == OrderType.STOP_TRAIL_LIMIT
            and order.limit_offset is not None
        ):
            trigger_price = trigger_price - abs(order.limit_offset)
        price = self._compute_price(
            order, trigger_price, order.quantity, bar
        )
        fee = self._commission(order, price, order.quantity * price)
        self._apply_fill(
            order,
            quantity=order.quantity,
            price=price,
            fee=fee,
            base_price=trigger_price,
            bar=bar,
            direction=trigger_side,
        )

    def _commission(self, order: Order, price: float, notional: float) -> float:
        scheme = self.config.commission_scheme
        if scheme is not None:
            return float(scheme.compute(price, order.quantity, notional))
        return 0.0


# ── 构造便捷入口 ───────────────────────────────────────────────────────


def _build_slippage(model_name: str | SlippageModel | None) -> SlippageModel | None:
    if model_name is None:
        return NoSlippage()
    if isinstance(model_name, SlippageModel):
        return model_name
    if model_name in (None, "no_slippage"):
        return NoSlippage()
    if model_name == "fixed_bps":
        return FixedBpsSlippage()
    if model_name == "volatility":
        return VolatilitySlippage()
    if model_name == "market_impact":
        return MarketImpactSlippage()
    raise ValueError(f"未知滑点模型: {model_name!r}")


def _build_commission(scheme_name: str | CommissionScheme | None) -> CommissionScheme | None:
    if scheme_name is None:
        return ZeroCommission()
    if isinstance(scheme_name, CommissionScheme):
        return scheme_name
    if scheme_name in (None, "zero"):
        return ZeroCommission()
    if scheme_name == "per_share":
        return PerShareCommission()
    if scheme_name == "fixed_bps":
        return FixedBpsCommission()
    if scheme_name == "tiered":
        return TieredCommission(tiers=[(float("inf"), 0.001)])
    raise ValueError(f"未知佣金方案: {scheme_name!r}")


def build_order_engine(
    slippage_model: str | SlippageModel | None = None,
    commission_scheme: str | CommissionScheme | None = None,
    *,
    fill_at: str = "open",
    time_in_force: TimeInForce | str | None = None,
    max_positions: int | None = None,
    max_volume_participation: float = 1.0,
    volume_lookback: float | None = None,
    expected_open_volume_fraction: float | None = None,
) -> OrderEngine:
    """构造订单引擎（字符串名 → 模型实例）的便捷入口。

    默认无滑点、无佣金，与现有回测默认口径一致；成本模型/成交量参与作为可选参数启用。
    """
    config = OrderEngineConfig(
        slippage_model=_build_slippage(slippage_model),
        commission_scheme=_build_commission(commission_scheme),
        fill_at=fill_at,
        time_in_force=TimeInForce(time_in_force) if time_in_force else TimeInForce.GTC,
        max_positions=max_positions,
        max_volume_participation=max_volume_participation,
        volume_lookback=volume_lookback,
        expected_open_volume_fraction=expected_open_volume_fraction,
    )
    return OrderEngine(config)


# 说明：OCO（one-cancels-other）为候选能力，本阶段（P0）未实现。
# 实现路径：对每对 OCO 订单维护 pair 引用，任一触发/成交时取消并成交另一笔，
# 与现有 blotter 账本对齐；归入后续 P1/P2。
