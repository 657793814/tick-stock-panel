# 页面数据来源地图 (Data Source Map)

本文回答一个问题：**每个页面展示的数据，实际来自哪个数据源**。当你在页面上看到
「龙虎榜」「资金流」「融资融券」等字样时，不要按字面推断来源——同名数据在项目里
可能存在**两条独立链路**。改任何消费方之前，先到这里核对真实调用链。

取数约定见 `CONTRIBUTING.md` §4（数据源插件化）。本文只描述现状，不描述设计意图。

## 1. 数据接入的四种通道

| 通道 | 入口 | 路由 |
| --- | --- | --- |
| ① 能力路由 · 行情数据集 | `daily` / `adj_factor` / `realtime` / `minute` / `depth5` / `financial` / `full_minute` | `设置 → 数据源 → 能力路由`，偏好字段路由到 TickFlow 或插件/自定义源 |
| ② 能力路由 · 事件类数据集 | `moneyflow`（个股资金流）/ `lhb`（龙虎榜）/ `margin`（融资融券） | 同一矩阵，`tf_provides=false`（TickFlow 不提供），默认路由 `eastmoney` |
| ③ 扩展数据 (ext_data) | `data/ext_data/` 下用户配置的表和列 | 板块轮动「资金流」等维度按「表id.列名」引用，不写死来源 |
| ④ 专有服务 | 复盘页龙虎榜、盘前风向标（fuyao）；xtick 页 | 不走能力路由，服务内直接调用指定 provider，源不可用时明确降级 |

## 2. 页面 × 数据来源

| 页面 / 卡片 | 后端链路 | 实际来源 | 备注 |
| --- | --- | --- | --- |
| 复盘页「龙虎榜」卡片（全部/机构/游资三榜） | `/api/market-recap/dragon-tiger` → `services/dragon_tiger.py` | **fuyao（同花顺）专有** | 未配置 fuyao 时 `state=source_unavailable`，页面降级提示。**与东方财富插件的龙虎榜是两套数据，互不相通**（见 §3） |
| 复盘页「盘前风向标」卡片 | `/api/market-recap/auction-benchmark` → `services/auction_benchmark.py` | **fuyao 专有** | 竞价筛选名单 + 当日实际表现对照 |
| AI 复盘正文中的龙虎榜摘要 / 风向标摘要 | `services/market_recap.py` | **fuyao 专有** | 复用上两条链路；失败为空串，不阻断复盘主流程 |
| 「东方财富」页（龙虎榜 / 融资融券表） | `/api/eastmoney/*` → `services/eastmoney_sync.py` → `data/financials/{lhb_metrics,margin_metrics}` | **eastmoney 插件** | 手动同步（页面按钮或 `POST /api/eastmoney/sync/{table}`）；落盘后注册为「龙虎榜」「融资融券」因子组，T 日数据 T+1 生效 |
| 个股资金流（ext 因子列） | `scripts/ingest_moneyflow_factors.py` → `provider.get_moneyflow()` → ext_data timeseries | **eastmoney 插件** | 逐标的拉取，T-1 平移防前视；供选股/回测/板块轮动引用 |
| 板块轮动卡「资金流」维度 | `services/sector_rotation.py` 读用户选择的扩展数据列 | **ext_data**（不写死来源） | 候选是全部扩展表数值列；典型填充者是上面的东财 moneyflow 因子列或 xtick，但完全取决于用户选择 |
| xtick 页（情绪 / 板块 / moneyflow 等） | `services/xtick.py` → xtick 插件 | **xtick 插件** | xtick 自带 `moneyflow` 数据集，与东财 moneyflow 是不同来源 |
| 日 K / 分时 / 实时快照 / 五档 / 财务 | 能力路由数据集（通道①） | 见设置页能力路由矩阵 | 默认 TickFlow；路由到插件（如 fuyao 声明 realtime/daily/adj_factor/financial，腾讯声明 minute）时以路由为准 |
| 侧栏指数条 / 市场总览指数 | `services/index_const.py` 固定四只核心指数 | 当前实时路由源 | 产品级固定契约，不走路由矩阵；自定义源经 `get_realtime_indices` 补充协议拉取，失败保留上轮缓存，无数据由本地日 K 兜底 |
| 市场主线（板块/概念识别） | `services/market_mainline.py` | **本地计算**（enriched 涨停梯队 + `ext_gn_ths` 概念成分快照） | 历史回看存在概念归属漂移（MEMBERSHIP_NOTE 随 API 返回） |

## 3. 两个「龙虎榜」的区别

项目里同时存在两套龙虎榜，**不要合并，也不要假设可以互相替代**：

| | 复盘页龙虎榜（fuyao） | 东方财富页龙虎榜（eastmoney） |
| --- | --- | --- |
| 粒度 | 三榜视图：全部 / 机构（含机构 4 字段）/ 游资席位表 | 按日全市场上榜明细（同一标的多条上榜原因取净额绝对值最大一条） |
| 缓存 | 按日落 JSON（`data/dragon_tiger/`） | Parquet（`data/financials/lhb_metrics/`） |
| 用途 | 复盘叙事、AI 复盘上下文 | 因子组（T+1 生效）、页面表格 |
| 取数形态 | 复盘卡片一次取齐 | 单日 1 请求，手动同步，逐日节流 1.2s |
| 未配置时 | `source_unavailable` 明确降级 | 页面提示点「同步」拉取 |

两者字段结构、榜单口径、缓存方式都不同。若未来要把 fuyao 龙虎榜接入 `lhb` 能力路由
作为候选源，需先统一数据集契约（当前 fuyao 侧是专有服务形态，未声明 `lhb` 数据集）。

## 4. 腾讯行情插件的现状

`backend/app/plugins/tencent/` 已内置，免注册免 token，路由数据集为
**minute / realtime / depth5 / daily**：

- **minute**：ifzq mkline，单标的 m1 约 482 根 ≈ 2 交易日，逐日增量落盘可累积历史；
- **realtime / depth5**：qt.gtimg.cn 批量快照（≤50 只/请求，快照自带买卖五档）。
  上游**无法枚举全市场标的**——`get_realtime()` 不带标的列表时明确抛错（fail-closed），
  全市场快照轮询请路由到可枚举的源；自选补拉（`get_realtime_symbols`）、
  指数快照（`get_realtime_indices`）与五档按需拉取正常可用；
- **daily**：ifzq fqkline 不复权（与除权因子管线口径对齐），单标的 count 模式
  （实测 ≤700 根，窗口按 450 日历日切片），全市场一轮 5600+ 请求，适合增量与小池子；
- 分时/前复权日 K 仍为 `raw()` 按需直取。
- 公开接口无 SLA，逐批/逐标的节流，失败 fail-closed 不静默跨源。

## 5. 事件类能力的路由注册（2026-11-28 起）

`moneyflow` / `lhb` / `margin` 已注册进能力路由矩阵（`data_providers/capabilities.py`，
声明 `tf_provides: false`）：

- 设置页「能力路由」新增三张卡片（资金流 / 龙虎榜 / 两融），默认路由 eastmoney，
  候选/置灰/门控语义与行情能力一致；
- TickFlow 不提供这三类数据，任何档位都不会出现在候选里；偏好非法值由 preferences
  getter 回退 `eastmoney`（不是 tickflow）；
- **资金流候选源**：eastmoney（provider 方法 `get_moneyflow`）与 xtick
  （`/doc/hot/historymoney`，most/big/small → 超大/大/小单，占比为空；
  行结构未实测，解析 fail-closed）；
- **龙虎榜**：xTick 上游是席位明细口径（buyTop5/sellTop5），与 `lhb_metrics`
  因子表口径不同，未注册路由；待统一契约后再接入；
- 取数形态差异（moneyflow 逐标的 / lhb·margin 按日全市场）见
  `docs/plugin-development.md` 事件数据集契约。

## 6. xTick 路由数据集（2026-11-28 起）

`plugins/xtick` 的 datasets 扩展为 `[realtime, depth5, financial, daily, minute,
full_minute, moneyflow]`：

| 数据集 | 端点与形态 | 实测状态 |
| --- | --- | --- |
| realtime | `/doc/quant/data` 全市场单请求 | ✅ 已实测 |
| depth5 | `/doc/order/deep` ≤50 只/批 | ✅ 已实测 |
| financial | `/doc/gaap` + `/doc/capital` 逐标的 | ✅ 已实测 |
| daily | `/doc/kline/market` fq=1 不复权，单标的日期范围 | ⚠️ 行结构/单位按官方 SDK bean 推定，**未实测** |
| minute | 同端点 period=1m，单标的按日 | ⚠️ 未实测 |
| full_minute | `/doc/order/minute` **批量** ≤50 只/请求（官方文档宣称支持 all），全市场一轮 ≈112 请求；批量解析 0 行自动回退单标的 1m K线 | ⚠️ 未实测 |
| moneyflow | `/doc/hot/historymoney` 单标的区间，映射东财 schema | ⚠️ 未实测 |

⚠️ 维护者此前实测发现 `/doc/core/batchkline` 成交量与成交额为常规日 K 的 2 倍
（上游口径异常），该端点**不使用**。daily/minute/full_minute/moneyflow 的
volume(手)/amount(元) 口径依据官方 Java SDK `Minute` bean 与前端列定义推定；
**配置 Token 后先在设置页对上述数据集「试拉」，与本地日K对照核验单位后再用于
全市场回填。**

## 7. 改动核对清单

新增页面或数据消费方时：

1. 在本文 §2 表格登记：页面、后端链路、实际来源、降级行为。
2. 若引用的是通道①②，必须通过能力路由矩阵（`usable`）做门控，不硬编码源名。
3. 若属于通道④专有服务，必须给出源不可用时的明确降级态（参照复盘页 `source_unavailable`）。
4. 同名数据出现多条链路时，在文中显式写清区别（参照 §3 两个龙虎榜）。
