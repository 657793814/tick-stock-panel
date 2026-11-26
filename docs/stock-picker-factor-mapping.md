# stock-picker 全因子映射表（阶段 1 交付物）

> 阶段 1 产出：把 stock-picker 全部 19 个因子逐项映射到 tick-stock-panel，并给出配方草案与策略层/数据管线映射。
> **本阶段为纯分析文档，未改动任何代码。** 映射依据为两侧代码实证，均带文件位置。

## 映射分级说明

| 级别 | 含义 | 落地方式 |
| --- | --- | --- |
| **A 现成** | 当前注册表有语义一致的因子 | 直接复用，无需新增定义 |
| **B 可表达** | 当前有基础因子/算子，需组合或新增 virtual/composite | 用 DSL/py_expr 或注册 virtual 因子表达 |
| **C 需数据** | 当前无因子列，依赖另类数据接入 | 走 `ext_data` timeseries 或新增 service 落列 |
| **D 不移植** | 权重为 0 且已失效/被证伪 | 保留说明，不进配方 |

> ⚠️ **口径差异（贯穿全表）**：stock-picker 因子值是 **0-100 评分**（横截面百分位或固定区间映射），当前注册表因子是**原始数值** + `_apply_scoring` 内的 min-max 归一化。因此**权重数值不能直接照搬**——同一"权重"作用在两种口径上产生完全不同的选股结果。下方配方草案是语义对应的**起点**，必须经 OOS 重检后才可生效（见主规划阶段 5）。

---

## 1. 短线因子映射（15 个，方案 G）

权重列来自 stock-picker `config.yml:43-85` / `core/scoring_model.py:80-116`（Σ=1.00）。

| # | stock-picker 因子 | 权重 | 当前对应 | 级别 | 数据通道 / 落地说明 |
| --- | --- | --- | --- | --- | --- |
| 1 | `hot_theme` 题材热度 | **0.55** | 无同名因子列 | **C** | 服务层有 `services/market_mainline.py`（概念粒度日频，有成分漂移前视警告）、`services/sector_rotation.py`（当日分钟）、`services/concept_rotation_analyzer.py`（AI 文本，非数值列）。要变成个股因子列需：概念成分表 join 回个股 + 走 `ext_data` timeseries 落列。**这是最大难点：权重最高且当前无干净的历史个股列。** |
| 2 | `liq_dev` 缩量偏离 | **0.14** | `log_amount`、`amount_ratio_5d`、`amount_mean_20d`（量价组）；流动性组 `amihud_20d` 等 | **B** | 定义 = `log1p(amount)` 相对 60 日中位数的偏离的横截面反向分位。当前有 `log_amount` 基础列，需新增 virtual 因子表达"相对历史中位偏离"，warmup≥60。 |
| 3 | `reversal_20d` 反转 | **0.10** | `momentum_20d`（base）；负向即反转 | **B** | stock-picker 中 `reversal_20d = 100 - momentum`，与 momentum 完全共线（ρ=-1）。用 `momentum_20d` 的**低方向**（`scoring_directions=low`）表达，不要新增独立因子。 |
| 4 | `vol_dev` 波动收敛偏离 | **0.07** | `annual_vol_20d`、`downside_vol_20d`、`vol_regime_5_60` | **B** | 定义 = 20 日收益 std 相对 60 日中位偏离的横截面反向分位。需新增 virtual 因子，warmup≥60。 |
| 5 | `volatility` 波动率 | **0.06** | `annual_vol_20d`（base, `registry.py:139`）、`atr_pct`、`amplitude` | **A** | 直接复用；方向为低波动溢价（`low`）。 |
| 6 | `momentum` 动量 RPS20 | **0.02** | `momentum_20d`（base, `registry.py:99`） | **A** | 直接复用。注意 stock-picker 的 RPS 是候选池横截面百分位，当前是原始收益率，`_apply_scoring` 会 min-max 归一化。 |
| 7 | `technical` 技术形态 | **0.02** | 均线偏离组 10 项 + 趋势组（MACD/KDJ/BOLL）+ 超买超卖组 RSI | **B** | stock-picker 用 `TechnicalScorer` 六维合成（趋势30+乖离20+量能15+支撑10+MACD15+RSI10）。当前可用 composite 因子组合现有虚拟因子近似，或直接分散进 scoring。 |
| 8 | `volume_price` 量价配合 | **0.02** | `vol_price_corr_20d`、`vol_ratio_5d`、`vol_ratio_10d`、`turnover_ratio_5d` | **B** | stock-picker 是**分段峰值型**（量比 1.4 时 85 分，过高过低都降分），非单调。当前因子为单调值，需用 py_expr 分段映射表达，或 composite 近似。 |
| 9 | `capital_flow` 主力资金流 | **0.01** | 无内置因子列 | **C** | `ext_data` timeseries → `ext_<id>_<field>` base 因子（`factors/ext_factors.py:116-143,284-342`）。注意仅 timeseries 模式可回测，snapshot 模式回测帧会跳过。 |
| 10 | `dragon_tiger` 龙虎榜 | **0.01** | 无因子列；`services/dragon_tiger.py` 提供三榜 dict | **C** | 需把机构净买额落成 `(symbol,date,value)` 时序表（按日落 parquet），再走 ext_ base 通道。当前 dict 仅供复盘卡片。 |
| 11 | `north_flow` 北向 | **0.00** | 无 | **D** | stock-picker 已恒返回 50（政策停发），不移植。 |
| 12 | `size` 小市值 | **0.00** | `log_float_mv`（规模组 virtual, `registry.py:238-242`） | **A** | 有现成。权重 0（stock-picker 链路已通待验证），可保留因子但不进配方。 |
| 13 | `liquidity` 原始流动性 | **0.00** | 流动性组 `amihud_20d`/`turnover_z_60d`/`turnover_mean_20d`/`turnover_std_20d` | **A** | 有现成。stock-picker 已证伪（84% 是 size 效应），权重 0，不进配方。 |
| 14 | `valuation_fundamental` 通达信估值基本面 | **0.00** | 财务 7 项（`pb_latest`/`roe_latest`/`revenue_yoy_latest` 等 PIT） | **B** | 当前 PIT 财务已接入且更严格（`backtest/fundamentals.py`），可覆盖大部分语义；PE/股息率当前无对应。权重 0（待 OOS），保留不进配方。 |
| 15 | `event_catalyst` 事件催化 | **0.00** | 无 | **C/D** | 当前无事件因子；stock-picker 依赖问财离线灌库且权重 0 待 OOS。阶段 2 暂不接，留作后续。 |

补充：`limit_up_streak`（连板）与 `vol_surge_5d`（量能激增）在 stock-picker 未进权重表，但当前有等价物——`consecutive_limit_ups`（`pipeline.py:113-122`）+ `limit_up_count_20d/60d`（`registry.py:221-228`）、`vol_ratio_5d/10d`。**无需移植。**

### 短线映射汇总

- **A 现成**：4（`volatility` / `momentum` / `size` / `liquidity`）
- **B 可表达**：5（`liq_dev` / `reversal_20d` / `vol_dev` / `technical` / `volume_price`，其中 reversal 用方向表达）
- **C 需数据**：4（`hot_theme` / `capital_flow` / `dragon_tiger` / `event_catalyst`）
- **D 不移植**：1（`north_flow`）
- 合计 14；第 15 个 `valuation_fundamental` 为 B 类（财务 PIT 近似覆盖，见 §2 长线说明），短线完整 15 项已在 §1 表格逐项列出。
- **关键结论**：权重 0.55 的 `hot_theme` 和 0.14+0.07 的 `liq_dev`/`vol_dev` 合计约 **0.76 的权重落在不干净或无历史列上**。这是整个整合的风险集中点——不能靠"接一下就上"，必须先解决数据可得性。

---

## 2. 长线因子映射（4 个有效权重 + 1 个零权重）

权重来自 `config.yml:172-178` / `scoring_model.py:117-123`（Σ=1.00）。

| 因子 | 权重 | 当前对应 | 级别 | 数据通道 / 落地说明 |
| --- | --- | --- | --- | --- |
| `fundamental` 基本面 | **0.40** | 财务 7 项：`roe_latest` / `net_income_yoy_latest` / `revenue_yoy_latest` / `gross_margin_latest` / `net_margin_latest` / `debt_ratio_latest` / `pb_latest` | **B** | stock-picker 用 ROE 分档 + PE/PB + 市值。当前 PIT 财务覆盖 ROE/增速/负债/毛利率，PE 与市值分档需补（市值可用 `log_float_mv`）。用 composite 或 scoring 多因子加权表达。 |
| `momentum` 中期动量 | **0.25** | `momentum_120d`（virtual, `registry.py:243-252`） | **A** | 直接复用（stock-picker 用 120 日涨幅横截面百分位，当前有 120 日动量虚拟因子）。 |
| `valuation` 估值水位 | **0.20** | `pb_latest`；无 PE/PS/股息率 | **B** | 当前只有 PB。stock-picker 用 PB+PS+股息率（明确不含 PE）。PS/股息率当前无对应，需补数据源或降维只用 PB。 |
| `institutional` 机构动向 | **0.15** | 无因子列；龙虎榜/资金流服务层 | **C** | = 龙虎榜机构净买 + 主力资金净流入。同短线 `dragon_tiger`/`capital_flow` 的 C 类通道。 |
| `north_flow` 北向 | 0.00 | 无 | **D** | 同短线，不移植。 |

### 长线映射汇总

- **A 现成**：1（`momentum`）
- **B 可表达**：2（`fundamental` / `valuation`）
- **C 需数据**：1（`institutional`）
- 长线整体可行性高于短线：主要落在已接入的 PIT 财务面板上。

---

## 3. 配方草案（META.scoring）

> 以下为**语义对应的起点草案**，权重为 stock-picker 原值映射，**未经 OOS 重检，不可直接生效**。阶段 5 需用 `backtest/walkforward.py` 校准。

### 3.1 短线策略配方草案

```python
# data/strategies/custom/sp_short_multifactor.py（阶段 3 落盘，此处仅为草案）
META = {
    "id": "sp_short_multifactor",
    "name": "stock-picker 多因子短线",
    "scoring": {
        # C 类待数据落地后才生效；缺数据时该因子权重让渡
        "ext_hot_theme_score": 0.55,   # hot_theme — 需 ext_data timeseries
        "liq_dev_60d": 0.14,            # B 类新增 virtual
        "momentum_20d": 0.08,           # reversal_20d(0.10) 用低方向 + momentum(0.02)
        "vol_dev_60d": 0.07,            # B 类新增 virtual
        "annual_vol_20d": 0.06,         # A 现成，低方向
        "volume_price_fit": 0.02,       # B 类分段映射
        "ext_capital_flow": 0.01,       # C 类
        "ext_dragon_tiger_net": 0.01,   # C 类
        "technical_composite": 0.02,    # B 类 composite
        # size/liquidity/valuation_fundamental/event/north 权重 0，不进配方
    },
    "scoring_directions": {
        "momentum_20d": "low",          # 表达 reversal_20d
        "annual_vol_20d": "low",        # 低波动溢价
    },
    "order_by": "score",
    "descending": True,
}
```

**注意**：`reversal_20d` 与 `momentum` 在 stock-picker 是两个独立因子（0.10 + 0.02），但因完全共线，当前合为一个 `momentum_20d` 低方向权重 0.08（0.10-0.02 的对冲需 OOS 验证方向）。这是**口径适配而非照搬**。

### 3.2 长线策略配方草案

```python
META = {
    "id": "sp_long_multifactor",
    "name": "stock-picker 多因子长线",
    "scoring": {
        "roe_latest": 0.20,                 # fundamental 拆分
        "net_income_yoy_latest": 0.10,      # fundamental 拆分
        "revenue_yoy_latest": 0.10,         # fundamental 拆分
        "momentum_120d": 0.25,              # A 现成
        "pb_latest": 0.20,                  # valuation 降维（PS/股息率缺）
        "ext_dragon_tiger_net": 0.08,       # institutional 拆分 — C 类
        "ext_capital_flow": 0.07,           # institutional 拆分 — C 类
    },
    "scoring_directions": {"pb_latest": "low"},
    "order_by": "score",
    "descending": True,
}
```

---

## 4. 策略层映射

stock-picker 的策略是"权重配方 + 流程编排"，当前策略库把流程拆成多个已实现的能力：

| stock-picker 策略能力 | 当前策略库对应 | 落地方式 |
| --- | --- | --- |
| 权重配方（方案 G / 长线） | `META.scoring` + `scoring_directions` | 见 §3 草案 |
| 硬过滤（ST/涨停/上市天数） | `META.basic_filter` + engine 默认过滤（`engine.py:517-525`） | 策略文件声明 |
| 叠加条件硬过滤 | `overlay_filter`（**属于 override 配置，不写进策略文件**，`custom_signals.py:302-341`） | 前端"叠加条件"面板生成，按策略 id 挂 override |
| 市场情绪 5 维 + 动态门槛 65/70/75 | `regime` 模块（`services/regime_builder.py`）+ 策略 params | 阶段 3 接 regime |
| 组合层风控（板块分散/相关性/拥挤度/波动率保险丝） | 现有 services + `PositionSizingPolicy`（`backtest/position_sizing.py`） | 阶段 3/5 |
| 仓位分配 `PortfolioOptimizer` | `PositionSizingPolicy`（已实现，L1 策略文件） | 直接复用 |
| 专家 Ensemble 第二意见 | 当前无对应 | 可选 L2，默认不启 |
| 影子推荐 | `strategy/paper.py` + `paper_watch.py`（已实现"绑定即评估"） | 直接复用 |
| 策略发现/加载 | `StrategyEngine._load_all`（`engine.py:305-339`），扫描 `data/strategies/custom|ai|composite/` | 放对目录即可 |

**策略文件规范**（来自 `strategy-guide.md` + 实证 seed）：
- 放 `data/strategies/custom/`（本次落盘位置），文件名 = `META.id`。
- `META` 必填：`id/name/asset_types/timeframes/params/scoring/order_by/descending/limit`。
- 按 `EXECUTION_BACKEND` 二选一定义入口：`polars_expr` 只定义 `filter(df, params)`；`matrix_native` 提供 `MATRIX_STRATEGY`。不得同时定义。
- `scoring` 非空权重和须 = 1.0；只能用真实数值字段或受控虚拟字段，不能用信号/条件名。
- 安全约束：仅允许 `import polars as pl`、`from datetime import ...`；禁 `open/exec/eval/os/sys/subprocess`。

---

## 5. 数据管线映射

| stock-picker 数据源 | 当前项目对应 | 可行性 |
| --- | --- | --- |
| mootdx K线 / 腾讯实时快照 / 新浪 / baostock | 现有 provider（`data_providers/`）+ 插件 `plugins/tdxquant`（通达信 TdxAiData，TCP）、`plugins/fuyao`；`MarketDataProvider` 契约（`base.py:49-99`） | ✅ 标准行情已覆盖，无需新建 |
| 东财板块 / 同花顺强势股 / AShareHub 概念（题材三源） | `services/sector_rotation.py`、`market_mainline.py`、`ext_presets.py` 的 `ext_gn_ths`/`ext_hy_ths`（snapshot） | ⚠️ 有服务但非个股历史列；hot_theme 需自建 timeseries 通道 |
| 资金流三源（ASHareHub/akshare/同花顺） | 无内置；走 `ext_data` timeseries | ⚠️ 需接入 + 落历史 |
| 龙虎榜（东财 datacenter） | `services/dragon_tiger.py` + `plugins/fuyao.dragon_tiger()` | ⚠️ 有当日 dict，需落按日 parquet 才能回测 |
| 通达信基本面 / 问财事件（离线灌库） | `financial_sync` → `data/financials/metrics/part.parquet`（PIT 面板，已接入） | ✅ 财务已覆盖；事件无对应 |
| 北向 | 无 | ❌ 不移植（已失效） |

**provider 插件契约**（`data_providers/base.py:23-31,49-99`）：`ProviderCapabilities`（instruments/daily/adj_factor/minute/realtime/depth5/financial）+ `MarketDataProvider` 方法族；插件声明用 `plugin.yaml`（`plugins/tdxquant/plugin.yaml` 范例：`name/runtime/entry/check/datasets/api_key_env/description`）。失败必须抛 `ProviderRequestError`，不得返回空数据伪装成功。

**结论**：阶段 4 的重点不是"接一个新行情源"（标准行情已够），而是**把另类数据（题材/资金流/龙虎榜）落成可回测的 timeseries 因子列**——这是 `hot_theme`(0.55) 能否落地的前提。

---

## 6. 关键风险与决策点（阶段 1 结论）

1. **风险集中：0.76 权重落在 C 类因子上**（hot_theme 0.55 + capital_flow/dragon_tiger/institutional）。当前无干净的历史个股列，需先解决数据落列，否则短线策略的评分大部分靠"缺失权重让渡"兜底，语义失真。
2. **口径不可照搬**：0-100 分 vs 原始值，权重重检是硬要求，不是可选项。
3. **共线处理**：reversal_20d 与 momentum 完全共线，当前用方向表达合并，需 OOS 验证。
4. **前视偏差**：`market_mainline` 概念成分来自快照（今日成分回看历史），若用于 hot_theme 回测会引入未来函数。
5. **snapshot 模式陷阱**：`ext_data` 的 snapshot 模式只在当日帧可见，回测帧刻意跳过——hot_theme/capital_flow 必须用 timeseries 模式。

**建议的阶段 2 优先级**：
- 先做 A 类（4 短线 + 1 长线，零风险）与 B 类（liq_dev/vol_dev/reversal/valuation/fundamental）。
- C 类中**先攻 hot_theme**（权重 0.55，决定短线策略成败），再 capital_flow / dragon_tiger。
- D 类明确排除。

> 本阶段交付：全因子映射表 + 配方草案 + 策略层/数据管线映射。**下一步（阶段 2）：按上述优先级落地因子定义与另类数据。**
