# stock-picker 策略权重 OOS 校准 · 工具与演示结论

> 状态：校准工具已交付并跑通；**演示结论为「不可采纳」**，未改动任何策略文件的权重。
> 关联：`docs/stock-picker-integration-plan.md` 阶段 5、`docs/strategy-iteration.md` §4、`docs/robust-optimizer-plan.md`。

## 1. 为什么要校准（不能照搬权重）

stock-picker 的因子是 **0-100 横截面排名分**，当前项目是**原始值 + min-max 归一化**——同一个数字在两套体系里含义不同，它的「方案 G 权重」不能直接照搬。阶段的语义初值（短线 `uf_sp_liq_dev=0.30` 等）只是占位，正式采纳前必须按项目的双闸门纪律做样本外检验。

## 2. 校准工具

`scripts/calibrate_scoring_weights.py` —— 自包含运行时工具，复用项目已验证构件，不修改核心模块/策略文件/数据。

| 构件 | 来源（复用，不重写） |
| :--- | :--- |
| K 段切窗 `split_validation_windows` | `app/backtest/robust_optimizer.py:38` |
| 稳健分 `R = 0.6×median + 0.4×worst` | `app/backtest/robust_optimizer.py:62` |
| 目标白名单 / 交易数门槛 | `app/backtest/guards.py` |
| 权重覆盖语义 `scoring_replace` | `app/strategy/scoring.py:28` |
| 回测引擎 | `app/backtest/strategy.py` `StrategyBacktestService` |

**双闸门纪律落地**：
1. 区间切 IS（前 80%）/ OOS（封存后 20%）；OOS **绝不参与权重搜索**，只在定型权重上跑一次；
2. IS 内 K 段切窗，对单纯形权重网格（sum=1，步进可配）逐组回测，按稳健分排序；
3. 主目标必须在白名单（`sortino`/`sharpe`/`calmar`/…），最小交易数 fail-closed；
4. 内置**样本充分性判定**：每段可用 <90 天或留出集 <60 天 → 直接判「不可采纳」，不给可信权重。

测试：`backend/tests/test_weight_calibration.py`（6 passed），覆盖单纯形网格完备性/唯一性、稳健分最差惩罚、None 诚实降级、样本充分性判定。

## 3. 演示运行（真实数据）

参数：`csp_short_multifactor`，5 因子，step=0.25（70 组），K=3，目标 sortino，min_trades=5，区间 2025-11-01 ~ 2026-11-20，40 只 A 股，组合仓位 5 持仓/5 日。

结果（报告 `data/backtest_results/weight_calibration_short.json`）：

- 70 组全部有效；IS 最优权重 = `vol_dev 0.5 + momentum 0.25 + technical 0.25`（**缩量因子 liq_dev 权重被归零**）。
- OOS（封存段 2026-09-05~11-20）sortino **-4.72**，IS 中位 -1.27，**退化 3.45**。
- **判定：不可采纳**——样本统计意义不足 + OOS 大幅退化，权重结果仅作工具演示，禁止上线。

### 三个必须正视的信号

1. **所有组合 sortino 均为负**（-1.27 ~ -4.72）：这段区间任何权重都亏。问题在策略假设/数据本身，不在权重微调——这正是阶段 2 警告的「C 类因子（题材/资金流，原权重 0.55）缺失，短线退化为量价近似」的实证。
2. **段冠军不收敛**：段1=`technical` 独大，段2=`momentum+technical`，段3 又掺入 `vol_dev`。按 `robust-optimizer-plan` 的判据，各段冠军散落 = 参数不稳定，任何「最优」都别信。
3. **spread 高达 1.74~7.93**：段间离散是过拟合警报。

## 4. 结论与下一步

- **权重文件不改**：两个策略文件的 scoring 维持语义初值。演示结果不构成修改依据（纪律：样本外只用于判定，且这里连充分性都不满足）。
- **数据 prerequisite**：现有 A 股历史仅 2025-10-20~2026-11-20（约 13 个月 / 284 交易日）。要达到可校准标准需：
  - 每段 IS ≥90 天可用、留出集 ≥60 天 → 总跨度建议 **≥2 年**（最好覆盖一轮完整牛熊）；
  - 补齐 C 类另类数据的历史序列（见 `docs/stock-picker-alt-data-contract.md`），否则校准的是「残缺量价策略」的权重，无意义。
- **重跑命令**（数据达标后）：见脚本文件头 usage；把 `--start/--end` 扩到 ≥2 年、`--step 0.1`、`--k 4`。
- **纪律提醒**：即使达标，校准出的权重也只是「候选采纳 → 进监控观察」，正式替换需人工复核 + 留出集终审只跑一次。

## 5. 本次改动清单

- 新增 `scripts/calibrate_scoring_weights.py`（校准工具）
- 新增 `backend/tests/test_weight_calibration.py`（6 passed）
- 新增 `data/backtest_results/weight_calibration_short.json`（演示报告，运行时数据，gitignore）
- **未改动**：任何策略文件权重、核心回测模块、前端、git（无提交）
