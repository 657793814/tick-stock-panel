"""quantjourney-bt 组合层（StrategyBook）自包含移植 —— tick-stock-panel P1-C4。

从 quantjourney-bt 的 ``backtester/portfolio/`` 移植自包含的组合账本栈：

- ``_time``：时间索引对齐辅助。
- ``weight_cost``：``WeightCostModel`` 协议、``WeightCostBreakdown``、
  ``FixedBpsWeightCostModel`` 与 ``solve_recursive_weight_costs`` 递归成本路径。
- ``rebalance``：``RebalanceAt``/``RebalancePolicy``/``RebalancePresets``/``RebalanceEngine``。
- ``book``：``StrategyBook``（多 sleeve 资本分配）、``StrategyBookResult`` 与
  ``_assert_accounting_identity`` 会计恒等式校验。

仅依赖项目既有 pandas/numpy；不改动主链路，作为新增独立组合层能力存在。
"""
from __future__ import annotations

from ._time import normalize_time_index_like, reindex_time_like
from .weight_cost import (
    FixedBpsWeightCostModel,
    WeightCostBreakdown,
    WeightCostModel,
    solve_recursive_weight_costs,
)
from .rebalance import (
    RebalanceAt,
    RebalanceEngine,
    RebalancePolicy,
    RebalancePresets,
)
from .book import StrategyBook, StrategyBookResult

__all__ = [
    "FixedBpsWeightCostModel",
    "RebalanceAt",
    "RebalanceEngine",
    "RebalancePolicy",
    "RebalancePresets",
    "StrategyBook",
    "StrategyBookResult",
    "WeightCostBreakdown",
    "WeightCostModel",
    "solve_recursive_weight_costs",
    "normalize_time_index_like",
    "reindex_time_like",
    "_assert_accounting_identity",
]
