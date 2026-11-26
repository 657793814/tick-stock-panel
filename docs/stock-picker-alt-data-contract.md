# C 类另类数据因子接入契约（阶段 2 补充说明）

> 阶段 2 中，A 类（现成）与 B 类（`uf_sp_liq_dev`/`uf_sp_vol_dev`/`cf_sp_technical`）已真实落地并测试通过。
> C 类因子的数据本身无法由代码生成，需要真实外部数据源。本文说明接入契约，不伪造数据。
>
> **更新（阶段 9）**：C 类中 `hot_theme`（题材）与 `dragon_tiger`/`institutional`（龙虎榜）已
> 通过真实数据源落地并可回测，详见下方「已落地现状」。`capital_flow`（主力资金流）仍缺真实源。

## 已落地现状（阶段 9，真实数据）

| 因子 | 因子列 | 数据源 | 真实覆盖 | 脚本 |
| :--- | :--- | :--- | :--- | :--- |
| `hot_theme` | `ext_ext_mainline_theme_theme_score` | `data/mainline_history/part.parquet`（本地涨停连板+概念成分算出的题材龙头分） | 241 个生效交易日、1349 只龙头股、3388 个股日；**T-1 滞后防前视** | `scripts/ingest_theme_factors.py` |
| `dragon_tiger` / `institutional` | `ext_ext_dragon_org_org_net_value` | fuyao 历史龙虎榜 org 榜（一年内可按日回补，字段含机构净买/游资净买/热度） | 228 个交易日、15765 行；机构净买有值 62.9% | `scripts/backfill_dragon_tiger_history.py` + `scripts/ingest_dragon_tiger_factors.py` |
| `hot_money` | `ext_ext_dragon_org_hot_money_net_value` | 同上，游资净买 | 有值 51.5% | 同上 |
| `hot_rank` | `ext_ext_dragon_org_hot_rank` | 同上，龙虎榜热度排名（低=热） | 有值 62.7% | 同上 |

- **完整版策略** `data/strategies/custom/csp_short_full.py` 已纳入上述 C 类因子（合计权重 0.40），与量价近似版 `csp_short_multifactor` 并存供 A/B 对比。
- **覆盖率限制**：题材仅龙头股（非全市场）、龙虎榜仅上榜股；缺失走 `_apply_scoring` 权重让渡。这是真实数据覆盖，非伪造。
- **权重仍为语义初值**：C 类数据已到位，但权重未经 OOS 校准（当前仅 13 个月数据，见 `docs/stock-picker-weight-calibration.md`）。
- **资金流数据源已找到（阶段 10 / KHunter 整合）**：参照 KHunter 的资金流能力，确认真实源为 Tushare `moneyflow_ths`（需 token，当前无）或**东财公开接口** `push2his.eastmoney.com/.../fflow/daykline`（免 token、实测可达、120 交易日历史、字段 f52=f55+f56 加总恒等式已验证）。已写 `scripts/ingest_moneyflow_factors.py`（限流友好 + 断点续传 + T-1 滞后）；但验证阶段**东财 IP 被临时封禁（HTTP 000）**，封禁期未写任何资金流数据。脚本就绪，解封后 `--symbols-file <池>` 即可补齐 `capital_flow` 因子。详见 `docs/khunter-integration-plan.md`。
- **ADX regime 能力已落地（阶段 10）**：新增 Wilder ADX(14) + KHunter 三态状态机（`app/khunter_algorithms.py` + `app/services/adx_regime.py`），T-1 因子 `ext_ext_adx_*`（283 日/全市场），策略 `khunter_adx_regime.py`。端到端回测显示择时闸门降低交易数 52%、回撤 -0.32 vs -0.65。

下文「为什么 C 类没有在阶段 2 自动落地」与「接入路径」记录的是阶段 2 时的原始判断与通用通道，仍适用于 `capital_flow` 等待接入的数据源。

## 为什么 C 类没有在阶段 2 自动落地

stock-picker 的 C 类因子依赖以下私有/配额数据源（来自其代码实证）：

| 因子 | stock-picker 数据源 | 当前项目状态 |
| --- | --- | --- |
| `hot_theme` 题材热度 | 同花顺强势股 HTTP + 东财板块 + AShareHub 概念 | 当前有 `ext_gn_ths`/`ext_hy_ths`（概念/行业成分，**snapshot 模式，不可回测**）、`market_mainline`（概念粒度日频，有成分漂移前视警告）。**无干净的历史个股题材分列。** |
| `capital_flow` 主力资金流 | AShareHub moneyflow / akshare 大单 / 同花顺资金流排行 | `fuyao` provider **无资金流接口**（client.py 方法清单实证）；需用户提供数据源。 |
| `dragon_tiger` / `institutional` | 东财 datacenter 龙虎榜 | `services/dragon_tiger.py` + `fuyao.dragon_tiger()` 有**当日 dict**（机构净买额），非 `(symbol,date,value)` 历史列；需落成按日 parquet 才能回测。 |

这些数据不是算法问题，是**数据可得性问题**：要么需要可用的 API/凭证，要么需要历史数据的持续积累（`factor_daily.db` 那种 2024-2026 快照无法补造）。

## 接入路径（供后续用户提供数据源后执行）

**数据源插件化规矩（2026-11-23 起，必守）**：外部数据源必须先做成插件
（`backend/app/plugins/<name>/`，非路由数据集参照 fuyao.dragon_tiger 约定以
provider 方法形态暴露），取数一律经 `custom.loader.get_provider`，service/script
禁止直连外部 HTTP。分类路由与范式 checklist 见 `docs/khunter-integration-plan.md` §7。

当前项目提供两条标准通道，均无需改核心代码：

### 通道 A：`ext_data` timeseries（推荐，可回测）

1. 在「设置 → 数据 → 扩展数据」新建配置，`mode=timeseries`（按 `date=*` 分区，PIT 对齐，历史帧无未来函数）。
2. 字段映射为数值列（如 `net_mf_amount`、`org_net_buy`、`theme_score`），列名自动生成 `ext_<config_id>_<field>`（`factors/ext_factors.py:52-59,116-143`）。
3. 数值字段自动注册为 `base` 因子，可直接进 `META.scoring` 与 DSL 公式。
4. **注意**：`mode=snapshot` 只注入当日帧，回测帧刻意跳过（`ext_factors.py:329-336`），资金流/题材必须用它 timeseries 模式。

配置结构参考 `data/ext_data/ext_gn_ths/config.json` 与 `backend/app/services/ext_presets.py`（内置预设的 URL/鉴权/两级拉取配方）。

### 通道 B：龙虎榜历史落列

`services/dragon_tiger.py` 已每日落 `data/dragon_tiger/date=YYYY-MM-DD.json`（仅拉取过的日期）。要因子化需要：
1. 把历史 JSON 的机构净买额聚合成 `(symbol, date, org_net_buy)` 面板，落 `data/ext_data/<id>/timeseries/date=*/part.parquet`。
2. 之后走通道 A 自动成为因子列。
3. **前视偏差警告**：只有实际拉取过的日期才有数据，早期日期缺失 → 回测时该因子在早期为 null，权重让渡。这与 stock-picker 的 `factor_daily.db` 覆盖率问题一致。

## 落地清单（数据到位后）

| 因子 | 期望列名 | 方向 | 配方用途 |
| --- | --- | --- | --- |
| `hot_theme` | `ext_<id>_theme_score` | high | 短线权重 0.55（最关键） |
| `capital_flow` | `ext_<id>_net_mf_amount` | high | 短线 0.01 + 长线 institutional |
| `dragon_tiger` | `ext_<id>_org_net_buy` | high | 短线 0.01 + 长线 institutional 0.15 |

## 数据缺失时的行为（已实现，无需改代码）

当前 `_apply_scoring`（`engine.py:1941`）的"缺失权重让渡"语义已保证：某因子列缺失/不可计算时，其权重按比例分摊给可用因子；全部不可用时得分 50。因此在 C 类数据到位前，阶段 3 的策略文件可先落地，C 类因子权重会自动让渡——**但语义会失真**（短线 0.55 权重让渡后，策略实际退化为纯量价）。

**结论**：C 类不是"做不了"，是"需要数据"。阶段 3/5 会在配方中保留这些因子占位，待数据到位后即生效；在此之前，必须明确标注策略处于"量价近似"状态，不得当作完整方案上线。
