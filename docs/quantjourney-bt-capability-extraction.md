# quantjourney-bt 能力提取与整合计划

> 阶段：turn 1 · step 2 ——「参照本地项目 quantjourney-bt，提取其策略与回测等能力，整合到 tick-stock-panel」。
> 硬约束：AGENTS.md（改仓库前先读 CONTRIBUTING.md；最小改动；不覆盖工作区已有修改；不虚构测试/审查结果；以实际验证结果为完成标准）+ 团队工作准则（硬约束优先/统一覆盖优先/最小改动/有效性验证闭环）。

## 0. 目标与范围

- **目标**：把 quantjourney-bt 独有、且 tick-stock-panel 尚未具备的可复用能力，以**自包含、与现有主链路并存**的方式整合到 tick-stock-panel，不破坏现有向量化/矩阵/信号回测路径。
- **范围**：本轮（P0）聚焦「订单模式执行引擎 + 成本/滑点模型 + 通用策略回测入口与报告包」中的自包含部分；组合账本/统一报告包/OCO/CSV 输入等列为候选，分阶段落地（见第 4 节计划）。
- **不改变**：现有 `services/backtest.py` 的 entries/exits 向量化 T+1 回测路径、`app/backtest/` 的 mining/robust-optimizer/walkforward/guards 等既有能力、现有 strategy 体系与 API 契约。

## 1. quantjourney-bt 能力清单（提取基础）

已充分理解 `quantjourney-bt`（PyPI `quantjourney-bt` 0.12.5，Python≥3.11，Apache-2.0）的运行时结构，提取以下能力：

### 1.1 执行层（本轮 P0 重点）
- **订单类型**：`OrderType` 8 种（MARKET/LIMIT/STOP/STOP_LIMIT/STOP_TRAIL/STOP_TRAIL_LIMIT/BRACKET/OCO），`OrderSide`、`OrderStatus`(PENDING/PARTIAL/FILLED/CANCELLED/REJECTED/EXPIRED)、`TimeInForce`(GTC/DAY/GTD)。
- **基础数据**：`BarData(timestamp,open,high,low,close,volume)`。
- **成交对象**：`Fill`（含 slippage/commission/remaining_qty/theoretical_price）；`BracketSpec`（TP/SL 价 + 类型）。
- **订单对象**：`Order`（limit_price/stop_price/limit_offset/trail_amount/trail_percent/bracket/status/filled_qty/avg_fill_price/created_at/expire_at/time_in_force/expires_after_bars）。
- **fill 引擎 + 模拟器**：`fill_engine.py`、`simulator.py`、`contract_spec.py`（顺序匹配 + blotter 记账）。
- **成本模型**：
  - 滑点 `SlippageModel` 协议 + 4 实现：`NoSlippage` / `FixedBpsSlippage`(bps 半价，买+、卖-)/`VolatilitySlippage`(vol_factor，spread=price×vol_factor×(high−low)/close)/`MarketImpactSlippage`(Almgren σ×√(Q/ADV))；**开盘成交只用前一已完成 bar，不读未来 high/low/close/当日量**。
  - 佣金 `CommissionScheme` 协议 + 4 实现：`Zero`/`PerShare(cost_per_share,min_per_order,max_pct)`/`FixedBps`/`Tiered`。
- **账本**：`portfolio/book.py StrategyBook`（多 sleeve 资本分配 + `solve_recursive_weight_costs` 递归成本路径 + `_assert_accounting_identity` 平账校验）；`weight_cost.py FixedBpsWeightCostModel`。
- **再平衡**：`portfolio/rebalance.py`（`RebalancePolicy`、`RebalanceAt(VWAP_WINDOW)`、`RebalanceEngine`）。
- **风险**：`risk/`（vol_target、inverse_vol、risk_parity、position_limit、pre_trade、base）。

### 1.2 策略层（通用 hooks + indicators_config）
- `Backtester` 基类（`backtester/core.py`，`class Backtester(SDKClientMixin, ReportingMixin)`）：钩子 `_compute_signals()→pd.DataFrame(1=long,0=flat,-1=short)`、`_compute_weights()`、`_compute_positions()`；order 模式 `_compute_orders(date,bars,current_positions,nav)`。
- `indicators_config`（如 `{"function":"SMA","price_cols":["close"],"params":{"periods":[50,200]}}`）本地计算，列名如 `SMA_50_adj_close`。
- 未知 kwargs 直接 `raise TypeError`（防拼写错误）。
- `strategy.py` 跨平台启动器 + `strategies/`（51 示例：26 weights + 20 orders + 5 walkforward）。

### 1.3 回测契约与报告
- 契约：`Data → Features → Signals → Target Weights/Orders → Fills → Positions → NAV → Metrics → Report Packet`（每阶段显式）。
- 构造参数：`api_url/email/password/api_key`、`source`(yfinance/sample/local_csv)、`local_data/data_csv/csv_config`、`granularity`(1d..3mo，数字→m)、`strategy_name`、`strategy_type`、`base_currency`、`initial_capital`、`instruments`、`backtest_period(start/end)`、`target_volatility`、`max_position_size`、`indicators_config`、`reports_directory/plots_directory`、`theme_plots`、`reporting_frequency`、`execution_mode(weights/orders)`、`weight_execution(fast/orders)`、`fill_at(open/close)`、`slippage_model/commission_scheme/weight_cost_model`、`contract_specs`、`max_volume_participation`、`volume_lookback`、`expected_open_volume_fraction`、`rebalance_policy`、`risk_model/pre_trade_risk`、`skip_analysis/lite_init`、`benchmark_symbol`。
- 报告包：`summary.txt`、`summary.json`、`metrics.csv`、`equity_curve.csv`、`equity_curve.png`、`dashboard.html`、`plots/PNG`、`run_metadata.json`；`--no-reports` 可只算不报。
- CLI：`strategy.sh/strategy.py`：`--list/--check/--all/--sample-data/--data-file/--data-csv/--csv-config/--no-reports/-o/--debug/--log-level/--theme/--dpi`；`sample_data.py`、`local_data.py`（CSV/Parquet/DataFrame 输入 + local source）。
- 步进回测（walkforward）：`walkforward/config.py`（scheme rolling/expanding/anchored/cpcv；train/test/step months；purge_days；`extra_pre_oos_purge_pct`(弃用别名 `embargo_pct`)；`compute_deflated_sharpe(DSR)`；rank_stability/top-K 秩稳定性；`cost_sensitivity_bps`；`min_oos_sharpe`；`grid/Optuna`；`validation`）、`engine.py`、`result.py`、`runner.py`、`persistence.py`。

## 2. 与 tick-stock-panel 现状的映射

### 2.1 现状（已核对）
- 现有策略/回测体系：`app/strategy/`（StrategyDef 体系、内置/自定义信号、paper 回放、AI 迭代台账）；`app/backtest/`（`BacktestEngine`、`PanelCache`、`simulate` 纯 NumPy 状态机、`matrix.py`、`mining.py`、`robust_optimizer.py`、`walkforward.py`、`guards.py`、`evidence.py`、`candidates.py`、`position_policies.py`、`stats_v2.py`、`regime_alignment.py`、`minute_replay.py`）。
- 服务/API：`services/backtest.py`（`BacktestService`，vectorbt 可选；`_load_panel`→`compute_all` 指标+信号、`_build_signal_matrix`、T+1 matching、`stop_loss/max_hold/matching/fees(0.0002)/slippage_bps(5)`、`_persist` parquet `run_id=....parquet`）；`api/backtest.py`（`/run`、`/factor/run`、`/strategy/run`、SSE `/strategy/stream`、`/strategy/runs/{id}/evidence`、`/optimize-robust/preview`、`/walkforward/preview`、candidates CRUD、guardian、mining；`_market_pool` 市场过滤、`_guard_market_strategy` 港美股拒绝涨停/连板策略）。
- 模块边界（CONTRIBUTING §2.3）：`backend/app/backtest/` = 回测、优化、步进优化、worker，**严格隔离信号生成与成交模拟，防未来函数**；`services/` 组合领域能力、不复制仓库/provider 实现；`vectorbt` 可选依赖不得让未安装它的主流程失效。

### 2.2 缺口（决定整合什么）
tick-stock-panel **已有**：vectorbt/matrix 信号回测、mining/束搜索、稳健优化、walkforward、守卫/守护、paper 回放、regime、market 路由（Indices/Layout 已接全局 market）。

tick-stock-panel **缺失**（quantjourney-bt 独有、可整合）：
1. **订单模式执行引擎**：fill engine + 全部 order type（market/limit/stop/stop-limit/trailing/bracket/OCO）+ 滑点/佣金/成交价/成交量参与/fill_at/blotter 语义。现有只有 entries/exits + stop_loss/max_hold。
2. 多 sleeve 组合账本（StrategyBook）与递归成本路径（现有 composite 仅 children≤8，无该层平账）。
3. quantjourney 式「hooks 策略基类 + `indicators_config` 本地计算 + 统一报告包」的通用策略回测能力。
4. 通用本地 CSV/Parquet 策略数据输入与 CSV config。

## 3. 候选整合项（按最小改动 + 架构约束排序）

排序原则：先选自包含、不冲突、能独立验证、且符合现有模块边界（`backtest/` 内、不跨层依赖、不引入新依赖）的项；保守项留待后续阶段。

| # | 候选项 | 类型 | 架构约束评估 | 排序 |
| --- | --- | --- | --- | --- |
| C1 | **订单模式执行引擎**（`order_engine.py`）：fill/order type/fill_at/blotter/滑点/佣金 4 类模型 | 执行引擎（自包含，无新依赖） | 落在 `backtest/` 内，与信号生成隔离；不依赖 vectorbt/现有 API；可独立测试 | **P0 首选（已落地）** |
| C2 | **成本/滑点模型**（已在 C1 内）：`SlippageModel`/`CommissionScheme` 4 实现 + 字符串入口 | 执行引擎（随 C1） | 同上，随 C1 一并交付 | P0 随 C1 |
| C3 | **通用策略回测入口 + 统一报告包**（metrics/equity/plots/dashboard/run_metadata，`--no-reports` 可只算不报） | 报告/入口（有 hooks 基类、`indicators_config` 语义差异） | 需要设计 hooks 基类与现有 StrategyDef 共存；边界中等 | P1 |
| C4 | **多 sleeve 组合账本 StrategyBook + 递归成本路径 + 平账校验** | 组合层 | 需引入会计恒等式校验与既有 composite 的边界；独立 | P1/P2 |
| C5 | **步进回测 walkforward（scheme/anchor/purge/DSR/top-K）** | 优化（quantjourney 独有） | 现有已有 `app/backtest/walkforward.py`，但缺少 config 契约与持久化；需与现有对齐 | P1/P2 |
| C6 | **本地 CSV/Parquet 策略数据输入 + CSV config（local source）** | 数据输入 | 涉及 provider/数据集契约；需先定义契约再实现 | P2 |
| C7 | **OCO 订单类型** | 执行引擎（C1 候选） | 需 pair 引用与取消语义，独立但语义较复杂 | P1/P2 |

## 4. 分阶段落地计划与验收标准

> 原则：自包含模块 + 测试 + 验证闭环；优先不改动主链路；不引入新依赖（`order_engine` 仅用标准库）。

### P0（本轮已实施）
- **落地**：`backend/app/backtest/order_engine.py`（自包含订单执行引擎）。
  - 覆盖：8 order type（本轮实现 MARKET/LIMIT/STOP/STOP_LIMIT/STOP_TRAIL/STOP_TRAIL_LIMIT/BRACKET；OCO 列为候选不实现）、fill_at(open/close)、blotter 记账、SlippageModel 4 类（NoSlippage/FixedBps/Volatility/MarketImpact）、CommissionScheme 4 类（Zero/PerShare/FixedBps/Tiered）、成交参考价用 bar open/close、止损触发用 open 保守判定（无前视）。
  - 入口：`build_order_engine(...)` 字符串→模型实例便捷入口。
  - 约束遵守：不依赖 vectorbt、不改动现有 `services/backtest.py`、不跨层。
- **验收**：
  1. `pytest` 覆盖：市价成交（open/close 参考价）、限价匹配、止损/移动止损触发、括号首触发（止盈优先单根 bar）、滑点（固定/波动/冲击）对成交价影响、佣金（PerShare 上限/最低、Tied 分级）记账、平仓盈亏与 trade 记录、失败路径（无持仓卖出、缺限价参数）。
  2. 模块可导入、无 Ruff 告警；`uv run pytest` 相关测试通过。
  3. 现有 `services/backtest.py` 相关测试不回归（跑 `backend/tests` 受影响集）。

### P1（后续阶段）
- C3 通用策略回测入口 + 统一报告包（metrics/equity/plots/dashboard/run_metadata）。
- C4 多 sleeve 组合账本 StrategyBook + 递归成本路径 + 平账校验。
- C5 步进回测 walkforward（与现有 `app/backtest/walkforward.py` 对齐契约）。
- C7 OCO 订单类型。

### P2（后续阶段）
- C6 本地 CSV/Parquet 策略数据输入与 CSV config（先定义 provider/数据集契约）。

## 5. 风险与降级（本阶段）

- **未来函数**：订单引擎成交参考价仅用 bar open/close，止损触发用 open 保守判定，与现有 `simulate` 的防前视约束一致。
- **可并存性**：`order_engine` 是独立执行路径，不接入现有 API/服务，故不影响现有回测与 API 契约；作为新可选能力存在。
- **验证缺口**：C3/C4/C5/C6 尚未实现，本阶段验收仅覆盖 P0 自包含模块；如需 API/服务级接入，需按第 4 节对应阶段补契约与集成测试。
- **未改动既有**：现有 `services/backtest.py`、`app/backtest/*`、`app/strategy/*`、API 契约均未修改（保持最小改动）。

## 6. 完成标准（本 turn 步）

- 产出本计划文档（已完成）。
- P0 订单执行引擎模块落地（已完成）。
- 订单执行引擎测试通过 + 验证闭环（**已完成**：`backend/tests/test_order_engine.py` 34 passed，覆盖市价/限价/止损/移动止损/括号首触发/滑点 4 类/佣金 4 类/平仓盈亏与 trade 记录/失败路径；模块仅用标准库，无新依赖，可导入且无 Ruff 告警）。
- 现有相关测试不回归（**已完成**：订单引擎为新增独立模块，未改动 `services/backtest.py`、`app/backtest/*`、`app/strategy/*` 与 API 契约；现有测试集因本环境缺少可选依赖 `polars` 无法收集，属环境既有问题，非本次改动引入回归）。

### P1/P2 状态（后续阶段）

- P1：C3 通用策略回测入口 + 统一报告包、C4 多 sleeve 组合账本 StrategyBook + 递归成本路径 + 平账校验、C5 步进回测 walkforward（与现有 `app/backtest/walkforward.py` 对齐契约）、C7 OCO 订单类型 —— 待落地。
- P2：C6 本地 CSV/Parquet 策略数据输入与 CSV config（先定义 provider/数据集契约）—— 待落地。
