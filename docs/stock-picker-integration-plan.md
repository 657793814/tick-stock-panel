# stock-picker 策略能力整合规划 v2（已按决策更新）

> 状态：**已确认，全量开工中**（用户 2026-11-22 确认）。
> 分析对象：`/Users/liuzuodong/Documents/workspace/stock-picker`
> 目标项目：当前仓库 `tick-stock-panel`
> 结论依据：只读分析 + 代码验证，区分「文档描述」与「代码实际」。

---

## 0. 已确认的决策

| 决策点 | 用户答复 | 落地含义 |
| --- | --- | --- |
| 1 整合目标 | **C（两者都要）** | ①选股结果/策略接入看板展示与监控；②把因子与评分方法变成系统可复用能力 |
| 2 工作区改动 | **直接改（不建分支、不提交）** | 在现有工作区改动基础上直接整合，不建二开分支、不提交任何东西 |
| 3 因子范围 | **所有因子** | stock-picker 15 个短线因子 + 4 个长线因子全部纳入映射，含另类数据因子 |
| 4 提交策略 | **不提交** | 本整合产生的改动不提交、不推送 |
| 5 策略层/数据管线 | **纳入整合** | 结论修正为「策略层进策略库可行且推荐；数据管线拆层做插件部分可行」，见 §1 |

**开工方式（最终）**：不建分支、不提交、直接在当前工作区改动；现有 60+ 未提交改动保持原样，整合改动与之共存，靠文件隔离 + 人工区分，全程不执行任何 git 提交/分支操作。

---

## 1. 重新回答第 5 点：策略层和数据管线到底能不能做？

我上一版写「不搬策略层、不搬数据管线」是**最小改动的保守判断，不是技术否定**。按你要求重新核实当前项目的真实扩展点后，结论修正如下。

### 1.1 策略层 → **能做，而且应该做进策略库（L1/L2）**

当前项目的策略库远比 stock-picker 成熟，代码已验证：

| 策略库现有能力 | 真实位置 |
| --- | --- |
| `StrategyEngine` + `StrategyDef`（加载/执行/评分） | `backend/app/strategy/engine.py:190,273` |
| 5 种执行后端：`polars_expr` / `matrix_native` / `python_history_legacy` / `composite` / `minute_filter` | `engine.py:540,557-568` |
| 25 个种子策略全部用 `matrix_native` | `strategy/seeds/*.py`（`EXECUTION_BACKEND = "matrix_native"` ×25） |
| 策略定义格式：META（params/scoring/order_by）+ MATRIX_STRATEGY 类 | `strategy/seeds/active_limit_gene.py:1-80` |
| composite 叠加策略 + `overlay_filter` 硬过滤 | `engine.py:115-146,373-416`、`secondary-development.md:18` |
| 因子一键生成排名策略、策略因子归因 | `secondary-development.md:29` |
| 择时/市场状态 | `api/regime.py`、`services/regime_builder.py`、`backtest/regime_alignment.py` |
| 仓位策略 | `backtest/position_sizing.py`（`PositionSizingPolicy`，已实现） |
| 影子/模拟盘 | `strategy/paper.py`、`strategy/paper_watch.py` |

**结论**：stock-picker 的"策略"完全可以落成当前策略库里的策略文件。但要做**语义映射**，不是复制它的 1533 行 `ShortTermStrategy.run()`：

| stock-picker 概念 | 映射到当前项目 | 分级 |
| --- | --- | --- |
| 权重配方（方案 G / 长线权重） | 策略文件 `META.scoring` | L1 |
| 过滤条件（ST/涨停/成交额/上市天数/板块分散） | `basic_filter` + `overlay_filter` + 现有风控服务 | L1 |
| 市场情绪 5 维 + 动态门槛 65/70/75 | `regime` 模块（择时）+ 策略 params | L1/L2 |
| 组合层风控（相关性/拥挤度/波动率保险丝） | 现有 services + `PositionSizingPolicy` | L2 |
| 专家 Ensemble 第二意见 | 当前无对应；先作为可选 L2 小接口评估 | 可选 |
| 影子推荐 | `paper.py` / `paper_watch.py` | L2 |

短线/长线两套策略用 composite 或直接两个策略文件承载，纳入策略库正常发现、执行、回测、监控。

### 1.2 数据管线 → **能做，但必须拆层，不能整体搬**（部分可行）

当前项目已有插件化数据层，代码已验证：

- `data_providers/base.py:49` `MarketDataProvider` Protocol + `ProviderCapabilities`（instruments/daily/adj_factor/minute/realtime/depth5/financial）
- `plugin.yaml` 声明式插件：`plugins/tdxquant/plugin.yaml`（通达信 TdxAiData，TCP 协议，覆盖 realtime/depth5/daily/adj_factor/minute/full_minute）、`plugins/fuyao/plugin.yaml`（覆盖 daily/minute/realtime/financial + 龙虎榜）
- 另类数据已有服务：`services/dragon_tiger.py`（三榜聚合）、`services/ext_data.py`、`services/sector_rotation.py`、`services/concept_rotation_analyzer.py`、`services/market_mainline.py`、`services/auction_benchmark.py`

按数据类型拆层：

| 数据类型 | 承载方式 | 可行性 |
| --- | --- | --- |
| 标准行情（K线/实时/财务/复权） | 包装成 provider 插件（参照 tdxquant/fuyao） | ✅ 高，有现成契约 |
| 龙虎榜 | 已有 `fuyao.dragon_tiger()` + `services/dragon_tiger.py` | ✅ 已具备 |
| 资金流/题材热度/板块 | 部分已有 services；缺的走 services + custom 路由扩展 | ⚠️ 中，需逐个核对 |
| 北向 | stock-picker 已失效归零 | ❌ 不移植 |
| 历史快照（`factor_daily.db` 2024-2026） | 无法快速补齐 | ⚠️ 只影响回测，不影响实盘选股 |
| Windows 路径 / ASHareHub 配额账本 / 东财限流 | 按插件契约重写适配 | ⚠️ 工程改造量 |

**结论**：数据管线"做进当前项目"可行，但答案是**一个 provider 插件（标准行情）+ 若干 services 扩展（另类数据）**，不是把 `core/data_engine.py`（2807 行）塞进来。历史快照缺失导致回测口径不完整，必须保留警示。

---

## 2. 两项目核心对照（保留）

- stock-picker 不是策略插件系统：2 个硬编码策略、无注册表；真正资产是**多因子评分 + 因子清单治理 + OOS 权重纪律 + 样本偏差处理**。
- 当前评分模型同构：`engine.py:1926-1963 _apply_scoring`（min-max + 权重重新归一化）≈ stock-picker `ScoringModel.rank_stocks`（加权 + 缺失权重让渡）。
- 因子注册表同构：`app/factors/registry.py:34,95` ≈ `core/factor_registry.py`。
- OOS 验证同构：`backtest/walkforward.py` ≈ `core/oos_validator.py`。

---

## 3. 修正后的整合方案（全量、分阶段）

原则：复用现有能力，L1/L2 为主，不建第二套数据/策略/评分/缓存；每阶段独立可验证可回滚；整合改动不提交不推送。

### 阶段 0 — 保持工作区原样（已确认：直接改，不碰 git）
- 不建分支、不提交；整合改动以新增文件为主，尽量避免触碰已有未提交改动的核心文件。

### 阶段 1 — 全因子与配方映射（L1，纯文档产出）
- 建立**全量因子对照表**：15 短线 + 4 长线逐因子 → 当前 `factors/registry.py` 现有因子 / 虚拟因子 / 另类 services / 需新增 / 无对应（北向）。
- 建立**配方映射**：方案 G 权重、长线权重 → 策略文件 META.scoring 草案，**明确标注权重不可直接套用，需重检口径**。
- 产出策略层映射表（§1.1），确认短线/长线用 composite 还是独立策略文件。
- 不写代码。

### 阶段 2 — 补齐因子定义与另类数据（L1/L2）
- 有对应的因子：在 `factors/registry.py` 补 definition（direction/warmup/asset_types/null_policy），复用 `dsl.py` / `scoring_value_expr`（当前已有 70+ 因子，见 `registry.py:97-313`）。
- 另类数据因子：逐个核对 `services/dragon_tiger.py` / `ext_data.py` / `sector_rotation.py` 是否已能提供；能则接，不能则新增 service 或标记缺失并 fail-closed。
- 北向：不移植。
- 验证：`test_factor_registry.py` 快照测试 + 新因子数值断言 + 缺能力 fail-closed 测试。

### 阶段 3 — 策略层落地到策略库（L1/L2）
- 在 `data/strategies/` 新增短线/长线策略文件（META + 矩阵信号 + scoring + overlay_filter），纳入策略库发现与执行。
- 择时接 `regime`，仓位接 `PositionSizingPolicy`，组合风控用现有 services，影子用 `paper_watch`。
- 专家 Ensemble 作为可选评估，默认不启。
- 验证：策略级测试（参数变更、缓存失效、资产切换、重复 ID、加载失败隔离）。

### 阶段 4 — 数据管线插件（L2）
- 标准行情数据源包装为 provider 插件（参照 `plugins/tdxquant`、`plugins/fuyao` 的 `plugin.yaml` + `MarketDataProvider` 契约），清理 Windows 路径/配额账本。
- 另类数据缺口由 services + custom 路由补，不塞进 ProviderCapabilities 标准契约。
- **治理规矩（2026-11-23 起，必守）**：任何外部数据源必须以插件形态接入，service/script 禁止直连外部 HTTP；分类路由（行情类→标准数据集插件、另类数据→非路由 provider 方法 + ext_data timeseries、派生指标→非数据源）与插件范式 checklist 见 `docs/khunter-integration-plan.md` §7。
- 验证：provider 契约测试（capabilities、标准化 schema、失败抛错而非空数据）、插件加载/卸载、回退链。

### 阶段 5 — 评分/回测适配（L2）
- 复用 `_apply_scoring` 承载配方 + 缺失让渡，评估是否需要横截面分位口径（对标 `scoring.py:67-85` 已有 `zscore over("date")`）。
- 回测用 `walkforward.py`，保留"回测≠实盘"警示；权重校准维持双闸门，禁止默认自动写入。
- 验证：回测测试（信号日/成交日/T+1/费用/滑点）+ 因子归因。

### 阶段 6 — 选股结果接入看板（决策 1 的"A 半边"）
- 把策略库策略的选股结果接入现有看板/监控/通知链路，复用现有 API 与前端查询键，不新建平行页面。
- 验证：前后端联调、SSE 告警链路、前端 `pnpm build`。

---

## 4. 数据契约与兼容红线（逐条核对）

- 百分比/小数制跨边界显式转换，禁止"小于 1 就乘 100"启发式（CONTRIBUTING §3.1）。
- 复权价：enriched 用前复权；涨跌停用原始价（§3.2）。
- 交易日/时区：窗口按实际交易日；分钟 K 北京时间墙钟（§3.3）。
- 缺失语义三态：让渡/弃权/如实计入，不能用中性 50 掩盖缺数。
- 评分一致性：历史结果、回测交易、UI 展示用同一候选集与排序方向（§5.3）。
- 改写入路径列全：持久化/内存缓存/generation/SSE/前端 query invalidation（§6.1）。
- 兼容：策略 JSON / 因子定义 / API 字段变化向后兼容，缺字段默认值（§8）。
- 文件安全：增删策略/因子文件 fail-closed、防路径穿越（§5.1、§8）。

---

## 5. 验证矩阵

| 阶段 | 最低验证 |
| --- | --- |
| 1 映射 | 全因子逐项标注代码位置；权重差异说明 |
| 2 因子 | `test_factor_registry.py` + 新因子数值断言 + 缺能力 fail-closed |
| 3 策略 | 参数/缓存/资产切换/重复 ID/加载失败隔离 |
| 4 数据 | provider 契约、标准化 schema、失败抛错、插件加载/卸载、回退链 |
| 5 评分回测 | 信号日/成交日/T+1/费用/滑点 + 因子归因 + `ruff check` |
| 6 看板 | 前后端联调 + SSE 告警链路 + `pnpm build` |
| 全程 | `git diff --check`、`git status`；不做性能主张则不补基准 |

---

## 6. 风险清单

- **口径误用（P1）**：照搬权重/分位口径未重检 → 错误选股结果。缓解：阶段 1 映射 + 阶段 5 双指标验证。
- **数据缺口静默（P1）**：另类因子取不到给中性分 → "失明专家"。缓解：fail-closed + 明确状态。
- **历史快照不可补**：回测口径不完整，禁止把回测收益当实盘预期。
- **升级冲突**：`engine.py` / `scoring.py` / `factors/registry.py` 是高冲突热点。缓解：优先 L1/L2，记录复核点。
- **数据管线工程量大**：provider 插件 + 多个 services 扩展，范围显著大于原方案。缓解：分阶段，每阶段可回滚。

---

## 7. 实施约定（已确认）

- **开工方式**：直接改，不建分支、不提交、不推送；现有 60+ 未提交改动保持原样。
- **整合改动**：以新增文件为主（映射文档、策略文件、测试），谨慎修改已有核心文件（`engine.py`/`scoring.py`/`registry.py` 属高冲突热点，优先 L1/L2 表达）。
- **推进节奏**：全量 6 阶段，每阶段结束汇报：分级、改动文件、缓存/API/兼容影响、实际执行的测试与结果、剩余风险。
- **完成标准**：以真实测试结果为准，不虚构验证；不做性能主张则不补基准。

> 阶段进度记录见「## 8. 实施进度」。

---

## 8. 实施进度

| 阶段 | 状态 | 交付物 | 验证 |
| --- | --- | --- | --- |
| 1 映射 | ✅ 完成 | `docs/stock-picker-factor-mapping.md`（全 19 因子逐项映射 + 配方草案 + 策略层/数据管线映射） | 纯文档，无代码 |
| 2 因子 | ✅ 完成（可落地部分） | 3 个 B 类因子定义（`data/user_data/custom_factors/`）+ `tests/test_stock_picker_factors.py` + C 类接入契约文档 | 因子注册表 45 passed、新增因子 4 passed |
| 3 策略 | ✅ 完成 | 2 个策略文件（`data/strategies/custom/csp_short_multifactor.py`、`csp_long_multifactor.py`） | 引擎加载 sum=1.0、filter 执行；含阶段3测试 |
| 4 数据 | ✅ 完成（可落地部分） | `scripts/ingest_dragon_tiger_factors.py` + `ext_dragon_org` timeseries 因子（真实落列 62 行）+ 长线纳入机构因子 | 因子注册/PIT对齐验证；含阶段4测试 |
| 5 评分回测 | ✅ 完成 | 复用 `_apply_scoring`（未改核心）+ 验证物化→评分端到端、回测 worker 可见 | 评分端到端非空分数；含阶段5测试 |
| 6 看板 | ✅ 完成（无需改前端） | 策略进入现有策略库列表 API，前端策略页自动可见 | `list_strategies` 可见 + `pnpm build` ✓ |
| 7 权重校准工具 | ✅ 完成 | `scripts/calibrate_scoring_weights.py` + `tests/test_weight_calibration.py` | 6 passed；70 组真实回测跑通 |
| 8 校准演示/评估 | ✅ 完成（结论：不可采纳） | `docs/stock-picker-weight-calibration.md` + `data/backtest_results/weight_calibration_short.json` | OOS sortino -4.72、退化 3.45、样本不足 → 禁止上线；策略权重未改 |
| 9 C类数据真实落地 | ✅ 完成 | 2 个回补/落因子脚本 + 完整版策略 `csp_short_full.py` + 契约文档更新 | 龙虎榜 228 日/15765 行、题材 241 日真实落列，PIT/T-1 验证，119 passed |

### 阶段 1 关键结论
- 19 因子分级：短线 A4/B6/C4/D1（含 valuation_fundamental 归 B）、长线 A1/B2/C1/D1。
- **风险集中**：短线约 0.76 权重落在 C 类（hot_theme 0.55 + 资金流/龙虎榜），当前无干净的历史个股列，需先解决数据落列。
- **口径不可照搬**：0-100 分 vs 原始值 min-max，权重必须经 OOS 重检。
- 阶段 2 优先级：A 类（零风险）→ B 类 → C 类先攻 hot_theme。

### 阶段 2 关键结论
- **已落地**：A 类复用现有因子；B 类新增 3 个因子定义（DSL/composite，L1 配置层，未改核心）：`uf_sp_liq_dev`（缩量偏离）、`uf_sp_vol_dev`（波动收敛偏离）、`cf_sp_technical`（技术形态近似）；北向 D 类排除；连板/量能激增当前已有等价物无需移植。
- **C 类（hot_theme/capital_flow/dragon_tiger/institutional）未自动落地**：数据本身需真实外部源（私密 API/配额/历史快照积累），代码无法生成。已写 `docs/stock-picker-alt-data-contract.md` 说明接入契约（ext_data timeseries / 龙虎榜落列），数据到位后即生效。
- **验证**：`test_factor_registry.py` + `test_factor_dsl.py` 45 passed；新增 `test_stock_picker_factors.py` 4 passed（解析/注册/warmup/物化非空/composite 权重）。
- **风险**：C 类数据缺失时权重让渡会使短线退化为纯量价近似，阶段 5 前不得当完整方案上线。

### 阶段 3-6 关键结论
- **策略层**：2 个策略落地 `data/strategies/custom/`，polars_expr 后端，scoring 权重和=1.0，被主引擎与回测 worker 均正确加载；短线含 B 类新因子，长线含龙虎榜机构因子。
- **数据管线**：写了 `scripts/ingest_dragon_tiger_factors.py`，把 `data/dragon_tiger/date=*.json` 的真实机构净买转成 `ext_dragon_org` timeseries 因子（按日分区、PIT 对齐、可回测）。资金流/题材因子（hot_theme 0.55）仍需真实外部源，未造数据。
- **评分/回测**：确认 `_apply_scoring` + `materialize_scoring_columns` 已内置物化链路（`engine.py:1002,1168`、`backtest/strategy.py:3065`），无需改核心；端到端跑出有效分数。
- **看板**：策略进入现有 `list_strategies()` 列表，前端策略页自动可见，零前端改动；`pnpm build` 通过。
- **全量验证**：相关测试 110 passed（factor registry/store/dsl、ext_factors、strategy registry/scoring、fundamental、新增 stock-picker 测试）；前端 build ✓。
- **测试环境说明**：沙箱下 pytest 需指定独立 `TMPDIR` + `--basetemp`，否则触发 `pytest-of-unknown` 目录冲突（`sitecustomize.py`，与代码无关）。

### 阶段 7-8 权重校准关键结论
- **通道确认**：walkforward/robust_optimizer 只优化策略标量 `params`，不优化 scoring 权重；但项目已有 `scoring_replace` override 通道（`strategy/scoring.py:28`），回测时可完整替换权重——权重搜索无需改策略文件/核心。单次回测 0.08-0.38s，计算可行。
- **工具**：`scripts/calibrate_scoring_weights.py` 遵循双闸门（IS 搜索 / OOS 封存只判一次 / K 段稳健分 / 目标白名单 / 最小交易数 / 样本充分性 fail-closed），复用 `robust_score`、`split_validation_windows`、`trades_ok` 等已验证纯函数，未改核心。
- **演示结论「不可采纳」**：真实数据（仅 13 个月）跑 70 组，全部权重 sortino 为负、段冠军不收敛、spread 1.74-7.93、OOS 退化 3.45 → 样本统计意义不足，禁止上线。**未改动策略文件权重**（演示结果不构成修改依据）。
- **数据 prerequisite**：现有 A 股历史仅 2025-10-20~2026-11-20（284 交易日）。可校准需总跨度 ≥2 年（每段 IS≥90 天、留出集≥60 天）+ 补齐 C 类另类数据历史序列。详见 `docs/stock-picker-weight-calibration.md`。
- **全量验证**：相关测试 116 passed（上一轮 110 + 校准 6），无回归。

### 阶段 9 C 类数据真实落地关键结论
- **题材（hot_theme）**：`data/mainline_history/part.parquet`（242 天本地真实数据）→ `scripts/ingest_theme_factors.py` 聚合成 `(symbol,date,theme_score)` 并 **T-1 平移防前视**，落 `ext_mainline_theme` timeseries（241 日/3388 行，1349 只龙头股）。验证了 T-1 语义真实生效（date=T 显示 T-1 的分数）。
- **龙虎榜（dragon_tiger/institutional）**：发现 fuyao `dragon_tiger_list(date)` 支持**一年内历史日期**（此前只知当日）→ `scripts/backfill_dragon_tiger_history.py` 真实回补 208 天（非交易日/超时跳过，不伪造）→ 扩展 `ingest_dragon_tiger_factors.py` 提取机构净买/游资净买/热度排名，落 `ext_dragon_org` timeseries（228 日/15765 行，机构净买有值 62.9%）。
- **完整版策略** `csp_short_full.py`（8 因子，含 C 类权重 0.40）与量价近似版并存；全部因子注册成功、权重和 1.0。端到端回测两版均跑通，完整版 total_return -0.198 vs 量价版 -0.271（C 类真实改变结果，但区间仍整体亏损，不构成"策略变好"主张）。
- **仍缺**：`capital_flow` 主力资金流（fuyao/tdxquant 无接口、无 akshare），需用户提供数据源走通道 A。
- **验证**：相关测试 119 passed（116 + C 类接入 3），无回归。
