# quantjourney-bt 策略迁移映射清单（P2）

迁移范围：quantjourney-bt 全部 51 个示例（26 weights + 20 orders）迁移为 tick-stock-panel 策略库（`data/strategies/custom/`）的 Polars 信号策略文件；5 个 walk-forward 类**不作为策略文件**，作为优化工作流说明（见 `docs/quantjourney-bt-capability-extraction.md` 与本文件第 4 节）。

迁移规格：`docs/quantjourney-strategy-migration-spec.md`。

生成说明：部分批量改写子代理因上下文耗尽失败（无收尾消息），已按小批次重派或直接落盘；最终以契约校验（仅 import polars、`scoring` 权重和 = 1.0、`filter` 返回布尔 `pl.Expr`）为准。校验结果：**46 个迁移文件全部通过契约校验**。

## 1. 迁移文件清单（源 → id → 逻辑摘要）

### 1.1 weights（26 个，custom_weights_*）

| 源 (quantjourney-bt) | 目标 id | 逻辑摘要（信号 / 加权 / 参数 / 迁移说明） |
|---|---|---|
| example_weights_01_sma_daily | custom_weights_01_sma_daily | 日 SMA(50/200) 多头趋势：close 内联 rolling_mean(50/200) 作入场，scoring momentum_60d 高优先、等权；原 weights 执行由 order_engine 承载。 |
| example_weights_02_monthly_drift_etf | custom_weights_02_monthly_drift_etf | 月频漂移：SMA 趋势等权，scoring momentum_60d 高优先；原 weights 执行由 order_engine 承载。 |
| example_weights_03_weekly_rsi_reversion | custom_weights_03_weekly_rsi_reversion | 周频 RSI 均值回归：RSI 低值优先，scoring rsi_14 低优先、等权；原 weights 执行由 order_engine 承载。 |
| example_weights_04_quarterly_dual_momentum | custom_weights_04_quarterly_dual_momentum | 季度双动量：momentum_60d 正且 rank min top(top_n) 入场，scoring momentum_60d 高优先、等权。 |
| example_weights_05_monthly_inverse_vol | custom_weights_05_monthly_inverse_vol | 月频逆波动率：annual_vol_20d 低值优先近似低波动加权，等权。 |
| example_weights_06_signal_change_defensive | custom_weights_06_signal_change_defensive | 信号变化防御：动量信号切换时防御性再平衡，scoring momentum_60d 高优先、等权。 |
| example_weights_07_intraday_rsi_15m | custom_weights_07_intraday_rsi_15m | 15 分钟 RSI 均值回归：rsi_14 低值优先，scoring rsi_14 低优先、等权。 |
| example_weights_08_intraday_1m_ema_scalp | custom_weights_08_intraday_1m_ema_scalp | 1 分钟 EMA 微仓：close EMA 代理趋势，scoring momentum_60d 高优先、等权。 |
| example_weights_09_intraday_1h_sma_trend | custom_weights_09_intraday_1h_sma_trend | 1 小时 SMA 趋势：close 内联 rolling_mean 趋势等权，scoring momentum_60d 高优先。 |
| example_weights_10_monthly_circuit_breaker | custom_weights_10_monthly_circuit_breaker | SMA(50/200) 趋势入场 + -15% 回撤熔断摊平、10 日冷却，scoring momentum_60d 高优先、等权。 |
| example_weights_11_quarterly_te_cost_gate | custom_weights_11_quarterly_te_cost_gate | 季度 top3 正动量 + 跟踪误差触发(0.06) + 年度换手预算(4x)成本门，scoring momentum_60d 高优先、等权。 |
| example_weights_12_daily_partial_drift | custom_weights_12_daily_partial_drift | 日动量 + 仅对漂移超 10% 持仓部分再平衡（低换手），scoring momentum_60d 高优先、等权。 |
| example_weights_13_pairs_ratio_zscore | custom_weights_13_pairs_ratio_zscore | KO/PEP 对数比 z-score 成对交易；成对跨标的信号受 per-symbol 富化列约束无法直接表达，以 momentum_60d 正为代理候选，待成对通道打通后启用（RULES 注明）。 |
| example_weights_14_pairs_hedge_ratio | custom_weights_14_pairs_hedge_ratio | EWA/EWC 滚动 OLS 对冲比率 z-score 成对交易；同成对受限，以 momentum_60d 正为代理候选，待成对通道打通后启用。 |
| example_weights_15_cross_sectional_momentum | custom_weights_15_cross_sectional_momentum | 252 日动量横截面，做多 top3 做空 bottom3（美元中性），scoring momentum_60d 高优先、等权；做空/美元中性由 order_engine 做空位置表达。 |
| example_weights_16_cross_sectional_reversal | custom_weights_16_cross_sectional_reversal | 21 日收益横截面反转，做多底部做空顶部，scoring momentum_60d 低优先、等权；做空/美元中性由 order_engine 表达。 |
| example_weights_17_vol_target_trend | custom_weights_17_vol_target_trend | SMA(50/200) 趋势 + 目标波动率(0.10)缩放暴露，scoring momentum_60d 高优先、等权。 |
| example_weights_18_vol_target_momentum | custom_weights_18_vol_target_momentum | 动量 top3 + 目标波动率(0.15)缩放暴露，scoring momentum_60d 高优先、等权。 |
| example_weights_19_risk_parity_multiasset | custom_weights_19_risk_parity_multiasset | 暖机后全资产多头，RiskParityModel 等风险贡献(ERC)权重（低波动获更多资本）；以 annual_vol_20d 低值优先近似 ERC，RULES 注明完整 ERC 需协方差数据通道。 |
| example_weights_20_risk_parity_capped | custom_weights_20_risk_parity_capped | ERC 权重 + PositionLimitModel(max_weight=0.25) 位置上限再归一化（风险模型串联）；ERC 近似 + 位置上限规则记录在 RULES。 |
| example_weights_21_bollinger_reversion | custom_weights_21_bollinger_reversion | 布林带均值回归：close 低于 20 窗 mean-2std 下轨入场，scoring close 低值优先、等权。 |
| example_weights_22_macd_trend | custom_weights_22_macd_trend | MACD(EMA12-EMA26, 信号线 EMA9) 高于信号线做多；以 MA 代理 MACD 趋势信号，scoring momentum_60d 高优先、等权。 |
| example_weights_23_fx_time_series_momentum | custom_weights_23_fx_time_series_momentum | FX 6 月动量做多/做空 + 63 日波动率逆序加权；FX 数据通道未提供，以 momentum_60d 正为多头候选 + annual_vol_20d 低值优先近似，RULES 注明 FX 数据依赖。 |
| example_weights_24_fx_cross_sectional_momentum | custom_weights_24_fx_cross_sectional_momentum | 4 个 USD 外汇 3 月收益横截面，做多最强做空最弱（美元中性）；FX 数据通道未提供，以 momentum_60d 排名 top1 为多头候选，RULES 注明横截面/美元中性与 FX 数据依赖。 |
| example_weights_25_continuous_futures_trend | custom_weights_25_continuous_futures_trend | 连续期货 fast(63)/slow(252) MA 趋势 + 63 日波动率逆序加权；期货数据未提供，以 MA 趋势信号为入场 + 动量高优先近似，RULES 注明期货数据依赖。 |
| example_weights_26_adanos_observations | custom_weights_26_adanos_observations | AAPL 持有，可用正向观测 + 正向情绪提及时做多；本地情绪观测数据通道未提供，以 momentum_60d 高优先为情绪信号代理候选，RULES 注明。 |

### 1.2 orders（20 个，custom_orders_*，order 类改写为信号策略）

| 源 (quantjourney-bt) | 目标 id | 逻辑摘要（信号 / 订单语义变更 / 参数 / 迁移说明） |
|---|---|---|
| example_orders_01_market_sma_cross | custom_orders_01_market_sma_cross | MARKET 模式 SMA(20/50) 交叉（上穿买入、反向卖出）→ SMA 交叉入场信号，等权；原 MARKET 订单、5bps 滑点、PerShareCommission 执行由 order_engine 承载。 |
| example_orders_02_market_rsi_reversion | custom_orders_02_market_rsi_reversion | MARKET 模式 RSI(14)<35 买入、>60 卖出 → RSI 均值回归入场信号，等权；原 MARKET 订单、5bps 滑点、FixedBpsCommission(1bps) 由 order_engine 承载。 |
| example_orders_03_limit_rsi_dip | custom_orders_03_limit_rsi_dip | LIMIT 模式 RSI<40 限价买(close×0.985, 3 bar 到期)、持仓后限价卖(entry×1.03, 3 bar 到期) → RSI 弱入场信号，等权；原 LIMIT 限价订单、slippage/到期由 order_engine 承载。 |
| example_orders_04_limit_trend_pullback | custom_orders_04_limit_trend_pullback | LIMIT 模式 close>SMA(50) 限价买(close×0.99, 2 bar)、趋势失效市场卖、持仓后限价卖(entry×1.04, 5 bar) → SMA 趋势信号入场，等权；原 LIMIT 限价订单、slippage/到期由 order_engine 承载。 |
| example_orders_05_stop_breakout_entry | custom_orders_05_stop_breakout_entry | STOP 模式近 20 日高点上方的买止损单(stop=prior_high×1.002, 5 bar) → 突破信号入场候选，等权；原 STOP 止损订单、slippage/佣金由 order_engine 承载。 |
| example_orders_06_stop_loss_protection | custom_orders_06_stop_loss_protection | MARKET 买入 + STOP 止损(entry×0.95)，随 SMA 趋势信号切换 → SMA 趋势交叉入场信号，等权；原 MARKET/STOP 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_07_stop_limit_breakout | custom_orders_07_stop_limit_breakout | STOP_LIMIT 模式近 20 日高点上方的买止损限价单(stop=prior_high×1.002, limit=stop×1.004, 5 bar) → 突破信号入场候选，等权；原 STOP_LIMIT 止损限价订单、slippage/佣金由 order_engine 承载。 |
| example_orders_08_stop_limit_protection | custom_orders_08_stop_limit_protection | MARKET 买入 + STOP_LIMIT 止损(stop=entry×0.96, limit=entry×0.955)，随 SMA 趋势信号切换 → SMA 趋势交叉入场信号，等权；原 MARKET/STOP_LIMIT 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_09_trailing_stop_trend | custom_orders_09_trailing_stop_trend | MARKET 买入 + STOP_TRAIL 移动止损(trail_percent=0.04)，随 SMA 趋势切换 → SMA 趋势交叉入场信号，等权；原 MARKET/STOP_TRAIL 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_10_trailing_stop_rsi | custom_orders_10_trailing_stop_rsi | MARKET 买入(RSI<38) + STOP_TRAIL 移动止损(trail_percent=0.05)，RSI>65 市场卖 → RSI 均值回归入场信号，等权；原 MARKET/STOP_TRAIL 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_11_trailing_stop_limit | custom_orders_11_trailing_stop_limit | MARKET 买入(SMA 趋势) + STOP_TRAIL_LIMIT 移动止损(限价 trail_amount=close×0.04, limit_offset=close×0.005)，持仓后卖出 → SMA 趋势交叉入场信号，等权；原 MARKET/STOP_TRAIL_LIMIT 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_12_bracket_trend | custom_orders_12_bracket_trend | BRACKET 模式 SMA 趋势 MARKET 入场 + +6% 止盈/-3% 止损括号(OCO) → SMA 趋势信号入场候选，等权；原 BRACKET 括号(OCO) 由 order_engine 承载。 |
| example_orders_13_rsi_both_ways | custom_orders_13_rsi_both_ways | RSI(14)<30 做多买入、>70 做空卖出（双向）MARKET → RSI 双向候选(rsi_14<30 或 >70) 入场候选，等权；原 MARKET 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_14_sma_double_threshold | custom_orders_14_sma_double_threshold | MARKET 买入(close>SMA20 且 close<3×SMA50)、持仓 10 bar 卖出 → SMA 双阈值区间入场信号，等权；原 MARKET 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_15_stop_trend | custom_orders_15_stop_trend | MARKET 买入(SMA 趋势) + STOP 止损(entry×0.95)，持仓 8 bar 后 MARKET 卖出 → SMA 趋势交叉入场信号，等权；原 MARKET/STOP 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_16_limit_trend_enhanced | custom_orders_16_limit_trend_enhanced | MARKET 买入(SMA 趋势) + 逐 bar 限价增强(enter_after)，持仓 12 bar 后卖出（MARKET/LIMIT 混合）→ SMA 趋势交叉入场信号，等权；原 MARKET/LIMIT 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_17_stop_trail_long | custom_orders_17_stop_trail_long | MARKET 买入(SMA 趋势) + STOP_TRAIL 移动止损(trail_amount=close×0.06)，持仓 12 bar 后卖出 → SMA 趋势交叉入场信号，等权；原 MARKET/STOP_TRAIL 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_18_stop_trail_limit_long | custom_orders_18_stop_trail_limit_long | MARKET 买入(SMA 趋势) + STOP_TRAIL_LIMIT 移动止损(限价 trail_amount=close×0.05, limit_offset=close×0.005)，持仓 12 bar 后卖出 → SMA 趋势交叉入场信号，等权；原 MARKET/STOP_TRAIL_LIMIT 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_19_stop_trail_limit_short | custom_orders_19_stop_trail_limit_short | MARKET 卖出(SMA 趋势) + STOP_TRAIL_LIMIT 移动止损，持仓 12 bar 后 MARKET 买入平仓 → SMA 趋势反转(down) 入场候选，等权（反向持仓）；原 MARKET/STOP_TRAIL_LIMIT 订单及滑点/佣金由 order_engine 承载。 |
| example_orders_20_oco_pair | custom_orders_20_oco_pair | OCO 组合价止盈/止损、单腿 MARKET → 成对组合信号受限（per-symbol 富化列约束），以 momentum_60d 正为代理候选；OCO 组合价/触发语义待 order_engine OCO 兼容能力（当前 OCO 候选未实现，待补）承载，RULES 注明。 |

## 2. 验证结果（每个文件契约校验）

- 方法：`import polars as pl` 仅且只；`scoring` 权重和 = 1.0；`filter(df, params) -> 布尔 pl.Expr`。
- 结果：**custom_weights_01–26（26 个）+ custom_orders_01–20（20 个）= 46 个迁移文件全部通过契约校验**。
- 校验脚本：`/tmp/verify_strategies.py`（模板见迁移规格第 4 节，运行：`./backend/.venv/bin/python /tmp/verify_strategies.py`）。

## 3. 迁移限制（写入各文件 RULES，待数据通道打通后启用真实信号）

- 成对类（W13/W14、O20）：受 per-symbol 富化列无成对列约束，采用代理信号 + RULES 说明。
- FX/连续期货（W23/W24/W25）：富化列无 FX/期货数据通道，采用动量/趋势代理 + RULES 注明数据依赖。
- 情绪类（W26）：本地观测情绪数据通道未提供，以动量代理 + RULES 注明。
- ERC/风险模型类（W19/W20）：完整先验协方差 ERC 需协方差数据通道，当前以 annual_vol_20d 低值优先近似，RULES 注明。

## 4. walk-forward 5 个（不作为策略文件）

quantjourney-bt 5 个 walk-forward 示例（示例 walkforward 类）**不迁移为策略文件**，作为优化工作流说明（walk-forward / 稳健优化方案），详见：
- `docs/quantjourney-bt-capability-extraction.md`（能力清单与 walkforward 契约对齐，C5 已完成）。
- `backend/app/backtest/walkforward.py`（新增 quantjourney 契约段：WalkForwardConfig 契约字段、Fold/QJFold 数据契约、fold_scheme_factory、purge/DSR/top-K/组合指标/绿黄红契约，测试 `test_walkforward_contract.py` 通过）。

## 5. 待补项（后续有界落地）

- order_engine OCO 兼容能力（W20/O20 涉及的 OCO 组合价/触发，当前 OCO 候选未实现，待补）。
- 成对/FX/情绪/ERC 真实信号（待对应数据通道打通后启用代理之外的真实信号）。
