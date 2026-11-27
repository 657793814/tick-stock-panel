# pynecore-strategies 项目能力分析与整合建议

> 分析对象：`pynecore-strategies`（本地开源项目，TradingView Pine 策略/指标离线回测语料库）
> 分析目的：盘点 pynecore-strategies 的能力，提炼可借鉴或可整合到当前项目 `tick-stock-panel` 的能力与落地路径
> 文档状态：能力分析稿（供评审与后续整合决策参考）
> 生成时间：2026-11-27

## 1. 文档目的与范围

本文档只做**能力盘点与可借鉴性分析**，不包含对 pynecore-strategies 的代码移植或直接改造成本评估。结论以“与 `tick-stock-panel` 现有架构和现状对照”为前提，只给出“哪些能力值得借鉴、以什么方式、按什么优先级落地”，具体改造另行按团队二次开发 SOP 执行。

范围限定：`pynecore-strategies` 主项目（`scripts/`、`assets/`、`tools/`、`config/`、`data/`、`pyproject.toml`、`.mcp.json` 等）与 README 已声明的公开能力。**不包含原始 Pine 脚本语料本身**（`assets/` 的 Pine 源是语料内容，不是可整合的代码库）；只将其作为“可纳入的已验证能力资产”来评估。

## 2. 项目定位

pynecore-strategies 是一个**开源策略/指标语料库**：把 TradingView 开源 Pine Script 策略与指标编译成 **PyneCore（Python）脚本**，可在任何行情数据上离线批量回测，无需 TradingView 账号、浏览器或图表。

- 规模：449 个策略、471 个指标、85 个 Pine 库，共 1,005 个源；其中策略 384 个 bit-exact、65 个 verified；指标 458 个 bit-exact、13 个 verified。
- 验证：经 PyneSys 对照 TradingView 验证（bit-exact：逐 bar IEEE-754 双精度一致；verified：逐 bar/逐笔在发布容差内一致）；295 个 v4、327 个 v5、293 个 v6、5 个更早版本的 Pine 源，由 PyneComp 统一转 v6 再编译，保留原版本默认设置。
- 定位：**为量化研究提供“已验证的外部策略/指标语料 + 离线批量回测通道”**，可喂给特征管线或 LLM  agents。

一句话：**把 TradingView Pine 策略/指标转化为可离线批量回测的 Python 语料，强调版本归一化、逐脚本验证、数据缓存与批量运行。**

## 3. 项目结构概览

```
pynecore-strategies/
  scripts/
    strategy_<name>.py        # 编译后的策略（TradingView 标题 snake_case + 8位脚本ID）
    indicator_<name>.py       # 编译后的指标
    lib/<Publisher>/<Name>/v<N>.py   # 编译后的 Pine 库
    <script>.toml              # 每脚本的输入与策略设置（可改佣金/资金/仓位等）
  assets/
    strategy/<category>/...    # 原始 Pine 源（逐字保留）
    indicator/<category>/...
    lib/...
    compile_state.json         # 编译进度（按源 SHA-256）
  tools/compile_assets.py      # 全量批量编译工具（幂等、限流、断点续跑）
  data/                        # 行情缓存 .ohlcv（append-only，.toml 符号信息）
  config/                      # api.toml、插件设置（可能含密钥，gitignore）
  output/                      # 运行结果（trade/strat/单bar CSV、viz ndjson）
  pyproject.toml               # uv 项目，依赖 pynesys-pynecore[cli,ccxt]
  .mcp.json                    # 支持 MCP 接入 LLM agents
  pyrightconfig.json / .pyne  # 工程标记
  README.md / LICENSE
```

关键实现分层：**语料层（Pine 源 + 编译产物）→ 编译层（PyneComp，版本归一化）→ 运行层（PyneCore 离线回测 + ccxt 数据）→ 配置层（每脚本 .toml 配置与设置覆盖）**。数据与结果均落盘缓存，支持离线批量运行。

## 4. 核心能力盘点

### 4.1 Pine → PyneCore 编译与验证

- **编译**：`pyne compile` 把 Pine 源经 PyneSys 云端编译器编译为 PyneCore Python 脚本，命名规则固定为 `strategy_<name>.py` / `indicator_<name>.py` / `scripts/lib/...`。
- **版本归一化**：PyneComp 统一把 v4/v5 转为 v6，并保留原版本默认设置（如 `margin_long=0`、`close_by_id=True`）。
- **验证体系**：区分 bit-exact（逐 bar IEEE-754 双精度一致）与 verified（逐 bar/逐笔在发布容差内一致）；语料含 65 策略 verified + 13 指标 verified。
- **全量编译保障**：1,005 源全部编译并通过 Python 语法检查；`tools/compile_assets.py` 支持幂等状态、限流停止、断点续跑。

### 4.2 离线批量回测引擎

- **运行方式**：`pyne run <script> <symbol@tf>`，支持任意 ccxt 支持的交易所与周期；可用 `--from/--to` 限定区间。
- **数据**：`.ohlcv` 文件为缓存，后续下载只追加新 bar，运行全程离线读取；多周期脚本通过 `--security` 提供各周期数据文件。
- **输出**：`output/<script>_trade.csv`（每笔进出：时间、价格、合约、盈亏、区间收益与回撤）、`output/<script>_strat.csv`（策略统计：净收益、回撤、盈亏比、胜率、多空拆分）、`output/<script>.csv`（每 bar 一行：OHLCV 与每个 plot 的值）、`output/<script>_viz.ndjson`（`--viz` 的 plot 样式与绘制对象）。
- **配置**：每脚本独立 `.toml`，输入与策略设置分离，可改 `commission_value`、`initial_capital`、`default_qty_value` 等而不改代码；脚本保留作者设定的成本与仓位，不同脚本口径不同。
- **测试保障**：全量编译语法检查 + 策略在 Gate.io BTC/USDT 上按多周期回测（418/418 在三个周期完整完成），只验证可执行性，不重复 TradingView 对照。

### 4.3 策略/指标语料库（scripts/）

- 文件命名即 TradingView 标题（snake_case）+ 8 位脚本 ID，便于溯源；原始 Pine 源在 `assets/` 与之逐字相邻。
- 语料混合 Pine 版本（295 v4 / 327 v5 / 293 v6 / 5 更早），由编译层统一归一化，保证跨版本可编译。
- 覆盖趋势类（MA/Supertrend/Ichimoku/Parabolic SAR 等）、震荡类（RSI/Stochastic/CCI 等）、动量类、波动率类、量能类等常用策略形态，并含 ML 策略、自定义策略等。

### 4.4 数据与配置工程

- **数据缓存**：`data/` 的 `.ohlcv` 为 append-only 缓存，保留符号信息（`.toml`），增量下载、离线运行。
- **编译状态与限流**：`assets/compile_state.json` 按源 SHA-256 记录编译进度；`tools/compile_assets.py` 按 API 限流（每小时/每天配额）停止并继续，下次从同一位置续跑。
- **配置卫生**：`config/` 可能含密钥，gitignore；`pyproject.toml` 用 uv 管理依赖。

### 4.5 批量编译工具（tools/compile_assets.py）

- 把 `assets/` 全量编译到 `scripts/`，命名与上文一致；支持 `--status`（进度与配额，不消耗配额）与默认编译（直到完成或配额耗尽）。
- 工程范式：**幂等（按源指纹）+ 限流可续跑 + 状态持久化**，是语料增量维护的通用模板。

### 4.6 工程配套

- 依赖：Python 3.11+ + uv，安装 `pynesys-pynecore[cli,ccxt]`；测试环境 PyneCore 6.12.1、PyneComp v6.0.79。
- 供 AI agents：提供 `pynecore-skills` 技能（教 Claude Code/Cursor/Codex 使用 PyneCore 语言与 `pyne` CLI）；`.mcp.json` 支持以 MCP 方式接入 LLM agents。

## 5. 与当前项目 tick-stock-panel 的能力对照

`tick-stock-panel` 现状（作为对照基准）：

- **定位**：多市场（A股/港股/美股）智能量化工作台（fork 二开版），Polars 引擎、Python 3.11+、Node 20+、React 18、Docker 单容器自托管。
- **已具备**：策略引擎（自定义策略 + AI 生成）、回测（引擎/订单引擎/持仓/因子/walkforward/分钟回放/稳健优化）、指标流水线（68 列指标与信号）、因子平台（DSL 因子 + 检验组合）、因子挖掘、市场环境/异动监控/监控中心、个股分析、连板梯队、复盘海报、插件化数据源、多市场隔离、开放接口（65 端点 + Token 六档）+ MCP 服务器（12 工具）。
- **当前推进阶段**：quantjourney-bt 能力提取与整合（整合方案已落盘，P0-1 订单模式执行引擎编码中，下一步完成订单引擎模块及相关测试）；另有虚拟账户（模拟盘）设计稿（未实现）、纸面回放与多市场计划。

对照结论：pynecore-strategies 是**语料库而非应用**，其自身不提供 UI、不运行在 tick-stock-panel 栈上，因此“整合”不是替换引擎，而是**把其“已验证的策略/指标语料 + 离线批量回测通道”作为能力资产，接入 tick-stock-panel 的因子/指标/回测能力**。tick-stock-panel 已有因子平台、指标流水线和回测引擎，因此借鉴应以“**接入已验证语料、复用现有口径与引擎、做再验证**”为原则，避免直接搬运未验证代码与重复建设。

## 6. 可借鉴的能力（按优先级）

以下按“与当前项目契合度 + 落地改动量”排序。具体改造需按团队二次开发 SOP 单独评估。

### P0 —— 优先借鉴：已验证语料接入 + 再验证通道（与 quantjourney-bt 整合路线一致）

1. **“已验证策略/指标语料”作为候选能力资产**
   - 借鉴点：pynecore-strategies 提供 449 策略 / 471 指标中的 **verified（逐 bar/逐笔在容差内与 TradingView 一致）子集**，以及 bit-exact 子集，作为高质量候选。
   - 契合点：与当前 quantjourney-bt“能力提取 + 边界整合”路线完全一致——先提取能力，再在 tick-stock-panel 现有口径下再验证，而非直接移植未验证逻辑。可把 pynecore-strategies 的 verified 子集作为外部策略/指标候选池。
   - 落地建议：建立“外部已验证语料接入 + 再验证”流程：先抽取 verified 子集，用 tick-stock-panel 的指标口径与回测引擎做再验证，通过后才纳入策略/因子候选。

2. **批量回测流水线的“语料级运行”范式**
   - 借鉴点：`pyne run` 逐脚本运行 + 多周期数据文件 + 输出统计 CSV，支持任意交易所与区间。
   - 契合点：与 tick-stock-panel 的**因子挖掘 / 全市场扫描**（mining、factor-platform 的毫秒级扫描、样本外搜索）匹配；可借鉴其“按语料逐项运行、落盘统计、按区间批量跑”的流水线模式，构建外部语料的批量回测通道。
   - 落地建议：把其“逐脚本批量运行 + 统计落盘 + 多区间”范式，适配为 tick-stock-panel 外部策略/指标语料的批量回测流程。

### P1 —— 次优先借鉴：配置与配置的分离 + 幂等编译工程

3. **“配置与代码分离”的语料配置化思路**
   - 借鉴点：每个脚本独立 `.toml`，输入与策略设置（佣金/资金/仓位/成本）分离，可改设置而不改代码；脚本保留作者设定的成本与仓位（口径不同）。
   - 契合点：与 tick-stock-panel 因子 DSL / 策略配置（`config.py`、`features`、`custom_signals`）的“配置与逻辑分离”思路一致，可作为**外部语料配置化的实现参考**。
   - 落地建议：在外部策略/指标接入中，把“每项语料的输入与参数配置单独落盘”作为统一配置规范，避免把参数硬编码进脚本逻辑。

4. **语料增量维护的幂等工程（按源指纹 + 限流续跑 + 状态持久化）**
   - 借鉴点：`compile_state.json` 按源 SHA-256 记录进度，`tools/compile_assets.py` 幂等编译、限流停止、断点续跑。
   - 契合点：可复用为“语料扩充/再编译流程”的通用工程模式，支撑外部语料增量维护。
   - 落地建议：沉淀为外部语料管理流程规范（指纹去重、限流续跑、进度可查），支撑语料持续扩充。

### P2 —— 观察/可选借鉴：Pine 版本归一化、MCP 工具接入、输出格式

5. **Pine 版本归一化思路**
   - 借鉴点：PyneComp 统一 v4/v5→v6 并保留原版本默认设置。
   - 契合点：若未来要引入其他来源的 Pine 策略，可作为“多来源策略统一处理”的版本归一化参考。
   - 落地建议：仅在引入新来源 Pine 策略时参考，作为统一处理的步骤，本次不作为优先项。

6. **输出统计格式参考**
   - 借鉴点：`_trade.csv`（每笔进出）、`_strat.csv`（策略统计：净收益/回撤/盈亏比/胜率/多空拆分）、`<script>.csv`（每 bar 一行）的统计输出结构。
   - 契合点：可作为外部语料回测输出规范的参考，与 tick-stock-panel 现有回测统计/证据输出（`backtest/evidence.py`、`stats_v2.py`、`reporting.py`）对齐。
   - 落地建议：作为输出口径参考，在统一回测统计规范时吸收，不直接引入其依赖生态。

7. **MCP 工具契约接入（.mcp.json）**
   - 借鉴点：pynecore-strategies 支持以 MCP 方式把语料/运行能力交给 LLM agents。
   - 契合点：tick-stock-panel 已有 MCP 服务器（12 工具、Token 权限、SSE 事件流），可借鉴其 MCP 工具契约化作为**外部语料/指标工具的接入方式**。
   - 落地建议：若把外部语料以工具形式接入 tick-stock-panel 的 MCP 服务器，可参照其工具契约化模式，衔接现有权限体系。

## 7. 不建议直接整合 / 需适配的点（风险与边界）

1. **是语料库，不是应用，不能直接替换 tick-stock-panel 引擎**
   - pynecore-strategies 本身不提供 UI、不运行在 tick-stock-panel 的 Polars/Python 回测栈上。其回测依赖 PyneCore/PyneSys 生态与 ccxt，不能直接替换或移植其引擎。

2. **语料规模大，不能整体直接导入**
   - 语料为 1,005 源、449+471+85 规模，其中大量为未验证的社区 Pine 脚本。直接整体导入会造成大量未验证代码与重复建设；应只取 **verified/bit-exact 子集**并做再验证后再纳入。

3. **Pine 语法与 tick-stock-panel 策略 DSL 不直接等价**
   - Pine Script 是第三方语法，与 tick-stock-panel 的策略格式（META/basic_filter/scoring/ENTRY_SIGNALS/filter 等 Polars 表达式策略）不直接等价，引入 Pine 策略需一层“Pine → tick-stock-panel 策略”的映射/转换层，属架构边界，不可直接搬。

4. **依赖生态不同，不能直接引入其依赖面**
   - 其依赖为 `pynesys-pynecore[cli,ccxt]`、uv、Python 3.13 等，与 tick-stock-panel 独立 Python 3.11 + Polars 栈不同，直接引入会扩大依赖与兼容面，需评估兼容性后选择性引入，不能直接全量引入。

5. **收益口径需按统一口径校准**
   - 其 `.toml` 保留作者设定的佣金/仓位，脚本间口径不一致。接入 tick-stock-panel 时，必须按项目统一口径（`commission`、`slippage`、资金、仓位）校准，不能直接沿用其原始口径。

6. **社区 Pine 源质量不作为整合依据**
   - 语料来自 TradingView 开源社区，质量参差；只有经过 bit-exact/verified 验证的子集才适合作为能力资产，其余不纳入。

## 8. 建议落地顺序（pynecore-strategies 相关）

| 优先级 | 内容 | 与当前项目的关联 | 主要产出 |
|---|---|---|---|
| P0 | 已验证语料子集接入 + 再验证通道（复用现有回测/因子/指标口径） | 直接服务 quantjourney-bt 的“能力提取 + 边界整合” | 外部策略/指标候选池 + 再验证流程 |
| P0 | 批量回测流水线（语料级逐项运行 + 统计落盘 + 多区间） | 服务因子挖掘 / 全市场扫描 | 外部语料批量回测通道 |
| P1 | 语料配置化（配置与代码分离）+ 幂等语料管理工程 | 服务外部语料增量维护 | 外部语料配置与维护规范 |
| P2 | Pine 版本归一化、输出统计格式参考、MCP 工具契约接入 | 服务未来多来源 Pine 策略与外部工具接入 | 统一处理/接入规范参考 |

## 9. 结论

pynecore-strategies 的核心能力是**“TradingView Pine 策略/指标的离线批量回测语料 + 版本归一化 + 逐脚本验证 + 数据缓存”**。对 `tick-stock-panel` 而言，最有价值、最契合现有路线的借鉴是：**把其 verified/bit-exact 已验证语料作为候选能力资产，接入现有因子/指标/回测能力并做再验证；复用其“逐脚本批量回测、配置与代码分离、幂等语料管理”的工程范式**，以服务 quantjourney-bt 的“能力提取 + 边界整合”。其 Pine 语法、PyneCore 引擎与依赖面均不直接等价，不能直接移植或整体导入，只能在**语料级、口径级、工程范式级**层面做参考与适配。建议按 P0 → P1 → P2 的顺序，以“接入已验证资产 + 复用现有口径/引擎 + 统一配置与验证规范”为原则落地。
