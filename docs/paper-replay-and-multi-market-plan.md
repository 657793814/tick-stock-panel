# 模拟盘成交回放与港美股多市场完善方案

> 状态: 实施中。本文档是 `docs/paper-trading-plan.md` 的增量方案，覆盖两件事：
> 1. 每个模拟账户按固定策略成交，并把买卖点回放到日 K 图上；
> 2. 把 `tickflow-stock-panel` 的港美股（HK/US）能力适配到本仓库。
>
> 遵循 [CONTRIBUTING.md](../CONTRIBUTING.md) 的数据契约、插件化、缓存与测试矩阵要求。
> 本文档区分「已实现」「待实现」，不得把设计示例当成现有 API。

## 1. 目标与非目标

### 1.1 目标

- **账户级固定策略**：一个模拟账户绑定一份冻结的策略参数快照，后续所有自动成交都由该快照驱动，账户之间互不影响。
- **可追溯成交**：每笔买卖都能回答「哪一天、什么价、多少股、哪条策略/规则、什么原因」。
- **K 线回放**：在账户面板内选择标的与日期区间，用日 K + 买卖标记复盘该账户在该标的上的完整操作。
- **港美股可用**：市场切换后，选股、市场总览、市场环境、强度梯队、指数、回测费用、模拟盘撮合都按市场语义工作；A 股默认行为与历史数据完全兼容。

### 1.2 非目标

- 不做实盘通道、融资融券、期权期货。
- 不做 tick 级撮合；模拟盘仍按快照价 / 日线开盘收盘价成交。
- 不重写参考仓库的独立市场目录结构；本仓库继续使用共享日 K / enriched 存储（见 3.2）。
- 不在本次引入港股逐标的 lot size 的完整维表同步；先按市场默认手数 + 标的维表可选字段降级（见 7.3）。

## 2. 现状盘点

### 2.1 已具备

| 能力 | 位置 | 说明 |
|---|---|---|
| 模拟盘账务 | `backend/app/strategy/paper.py` | 多账户、订单、append-only `fills.jsonl`、持仓重放、T+1、费用、涨跌停、除权、盘后结算、净值、回合统计 |
| 模拟盘 API | `backend/app/api/paper.py` | 账户/订单/成交/持仓/净值/统计/对比/自动规则/批量创建 |
| 固定策略执行 | `backend/app/custom/paper_execution/` | 账户绑定冻结快照，逐日产生买卖决策并调用 `paper.create_order`，事件写入 `events` |
| 图表标记 | `frontend/src/components/StockDailyKChart.tsx`、`frontend/src/lib/chart-types.ts` | `ChartMarker` 支持 `buy/sell/neutral`、标签、颜色、上下位置 |
| 市场注册表 | `backend/app/markets.py` | `market_of`、`normalize_symbol`、交易时段、涨跌停、印花税、手数 |
| 市场切换 | `frontend/src/lib/market.tsx`、`Layout.tsx` | `cn/hk/us` 全局上下文与 localStorage 持久化 |
| 港美股维表/日K同步 | `backend/app/jobs/daily_pipeline.py::run_market_sync` | 港美股维表单独存放，日 K 写入共享 `kline_daily`，再按 symbols 重算 enriched |
| 港美股指数 | `backend/app/api/indices_market.py`、`services/index_sync_market.py` | 独立 `kline_index_daily_{market}` |
| 市场门控雏形 | `api/screener.py` | 港美股策略兼容性检查、连板梯队当前返回门控提示 |

### 2.2 主要缺口

1. **共享存储缺少市场隔离**：`ScreenerService`、市场总览、环境、复盘等聚合读的是共享 enriched；若不过滤 symbol 后缀，HK/US 标的会混入 A 股结果，A 股也会混入港美股结果。
2. **指标计算没有 market 语义**：`compute_all` 对全表执行 A 股涨跌停/连板信号；HK/US 行会产生无意义信号。
3. **聚合服务没有 market 参数**：`ScreenerService`、`build_market_overview`、`build_regime_*`、复盘、强度梯队。
4. **前端只完成了市场切换器**：Dashboard、Regime、Screener、Backtest、Review、LimitUpLadder 等页面尚未全部按 `market` 传参或做能力门控。
5. **模拟盘未区分市场**：手数固定 100、涨跌停检查、印花税单边、T+1、基准 `000300.SH` 都是 A 股口径；账户没有 `market` 字段。
6. **没有成交回放 API/UI**：前端只能看成交表，不能把买卖点叠加到 K 线。

## 3. 总体设计

### 3.1 领域边界

```
策略快照 (paper_execution snapshot)
        │ 逐日决策
        ▼
paper.create_order ──► orders/*.json
        │ 撮合
        ▼
fills.jsonl (唯一事实源) ──► positions / nav / rounds
        │
        └──► replay API ──► 前端日K + ChartMarker
```

- 成交台账仍是唯一事实源；回放不新增账务表，只做**读模型**。
- 固定策略语义由 `paper_execution` 的冻结快照保证；回放只展示，不重新计算信号。
- 多市场语义统一从 `app/markets.py` 取，不在各模块重复硬编码后缀。

### 3.2 共享存储下的市场隔离（关键决策）

参考仓库使用 `kline_daily_{market}` / `kline_daily_enriched_{market}` 独立目录。本仓库已有大量 A 股数据与共享存储 WIP，**不迁移目录**，采用「共享物理存储 + 逻辑市场隔离」：

1. **写入**：HK/US 日 K 继续写入 `kline_daily`；`run_market_sync` 只对本次 universe 调 `run_pipeline(symbols=universe, market=market)`，merge-upsert，不触碰 A 股分区。
2. **读取**：所有面向单市场的读路径必须调用 `markets.filter_symbols(df, market)`：
   - `cn` → `.SH/.SZ/.BJ`
   - `hk` → `.HK`
   - `us` → `.US`
3. **缓存**：进程级缓存 key 必须包含 `market`；策略结果缓存文件名也必须按 market 隔离。
4. **指标**：`compute_all(df, market=...)` 先按市场切分；仅 `cn` 执行涨跌停/连板信号，HK/US 输出 `signal_limit_* = False`、`consecutive_limit_* = 0`，保留动量/均线/量比等通用指标。
5. **默认值**：所有 API 的 `market` 默认 `cn`，旧 URL、旧前端、旧测试行为不变。

> 不采用「靠 symbol 后缀在 SQL 里到处 `LIKE`」的分散写法，统一收口到 `markets.py`，便于测试和后续替换存储结构。

## 4. 模拟盘固定策略与回放

### 4.1 账户与策略绑定

现状已经支持「一个账户绑定一份策略快照」。本次补齐：

- 账户新增可选字段 `market`（`cn|hk|us`，默认 `cn`）。
  - 创建账户时可显式指定；未指定时按第一条成交/订单的 symbol 推断并回写。
  - 已有账户无该字段时视为 `cn`，不破坏旧数据。
- 绑定快照时校验策略与账户市场兼容：依赖涨停/连板信号的策略不允许绑定 HK/US 账户（复用 `_market_compatible_strategy` 口径）。
- 账户市场决定：手数、T+N、印花税、涨跌停检查、基准指数、可选交易时段。

### 4.2 回放读模型

新增两个只读端点（薄胶水，逻辑放 `app/strategy/paper.py` 或新 `paper_replay.py`）：

#### `GET /api/paper/replay/symbols?account=<id>`

返回该账户成交过的标的摘要，供回放弹窗左侧列表：

```json
{
  "symbols": [
    {
      "symbol": "00700.HK",
      "name": "腾讯控股",
      "asset_type": "stock",
      "market": "hk",
      "fills": 6,
      "buys": 3,
      "sells": 3,
      "first_date": "2026-10-09",
      "last_date": "2026-11-18",
      "last_side": "sell",
      "realized_pnl": 1280.5
    }
  ]
}
```

- 只统计 `kind=fill`；`corp_action` 单独计数或忽略。
- 名称通过 `repo.get_name_map([...])` 批量取，避免逐标的查询。
- 按 `last_date` 倒序。

#### `GET /api/paper/replay?account=<id>&symbol=<symbol>&start=&end=&limit=`

返回单标的回放数据：

```json
{
  "account": "acc_x",
  "account_name": "均线策略",
  "symbol": "600519.SH",
  "name": "贵州茅台",
  "asset_type": "stock",
  "market": "cn",
  "start": "2026-01-01",
  "end": "2026-11-20",
  "fills": [
    {
      "seq": 1760000000000,
      "date": "2026-10-09",
      "side": "buy",
      "qty": 100,
      "price": 1680.5,
      "fee": 42.01,
      "order_id": "order_...",
      "source": "strategy-exec:abc123:signal",
      "reason": "signal",
      "reason_label": "策略入场",
      "strategy": { "name": "均线突破", "version": "..." }
    }
  ],
  "markers": [
    {
      "date": "2026-10-09",
      "kind": "buy",
      "label": "B",
      "above": false,
      "price": 1680.5,
      "qty": 100,
      "side": "buy",
      "reason": "signal"
    }
  ],
  "summary": {
    "buys": 3,
    "sells": 3,
    "buy_amount": 504150.0,
    "sell_amount": 521300.0,
    "fees": 260.4,
    "realized_pnl": 1280.5,
    "win_rate": 66.67,
    "avg_holding_days": 8.5,
    "open_qty": 100
  },
  "truncated": false
}
```

约定：

- `symbol` 先经 `markets.normalize_symbol`，再校验属于该账户成交集合，防止越权读其他标的。
- `start/end` 为闭区间；缺省取该标的全部成交日期，前端再决定 K 线窗口。
- `limit` 默认 500、最大 2000；超出置 `truncated=true`，前端提示缩小范围。
- `markers` 由后端生成，前端不重复解析 `source`：
  - `buy` → `kind=buy`，标签 `B`；同一天多笔买可合并为 `B×n`（前端可再做视觉合并）。
  - `sell` → `kind=sell`，标签 `S`。
  - `corp_action` → `kind=neutral`，标签 `除权`，`above=true`。
  - `above`：买入放下方、卖出放上方，避免遮挡蜡烛。
- `reason` 解析：
  - `manual` → `手动`
  - `auto:<rule_id>` → `规则跟单`
  - `strategy-exec:<token>:<reason>` → 按 `reason` 映射：`signal` 入场/出场、`max_hold` 到期、`stop_loss/take_profit/trailing` 风控、`rebalance` 调仓。
  - 解析不到时保留原始 `source`，不臆造。
- 策略名从 `paper_execution` 状态文件读取；文件缺失/损坏时 `strategy=null`，回放仍可用。

### 4.3 回放统计口径

- 只统计所选 `symbol` + 日期区间内的成交。
- `realized_pnl` 用 FIFO 配对（复用 `paper.round_trips` 的算法，按 symbol 过滤后计算），包含买入费、扣除卖出费。
- `open_qty` 为区间结束时仍持有的数量；区间内未平仓部分不计入 `realized_pnl`。
- `win_rate` 分母为已平回合数；无回合时为 `null`，不返回 0 误导。
- 费用展示与账户实际费率一致，不回算历史。

### 4.4 前端回放交互

在 `frontend/src/custom/paper/Page.tsx` 的成交台账/持仓区增加「回放」入口：

1. 点击标的行或「回放」按钮 → 打开 `PaperReplayModal`。
2. 弹窗结构：
   - 顶部：标的名称/代码、市场徽标、日期区间快捷项（全部 / 近 3 月 / 近 1 年）。
   - 主体：`StockDailyKChart`，`markers` 来自回放 API；`assetType` 用返回值。
   - 下方：成交明细表（日期、方向、数量、价格、费用、原因），点击行可高亮对应标记。
   - 右侧/顶部摘要：买入/卖出金额、费用、已实现盈亏、胜率、平均持有天数、期末持仓。
3. K 线数据复用 `api.klineDaily(symbol, days, range)` 与 `QK` 缓存；不新增行情接口。
4. 空状态：该标的无成交、或区间内无行情时给明确文案。
5. 移动端：图表最小高度 320，明细表横向滚动。

### 4.5 回放边界与一致性

- **不重算历史信号**：回放只显示实际成交；策略当时是否触发以台账为准。
- **不修改台账**：回放是只读；修复仍走 `rebuild` / 冲正成交。
- **多账户隔离**：所有查询带 `account`，禁止跨账户聚合（对比页除外）。
- **除权**：`corp_action` 不产生买卖标记，但要在摘要里提示「区间内有 N 次除权调整」。
- **停牌/缺行情**：K 线缺日由图表自然留空；成交表仍展示。
- **港美股**：回放不依赖涨跌停字段；标记只表达买卖。

## 5. 多市场能力补齐

### 5.1 市场工具层（先做）

在 `backend/app/markets.py` 增加：

```python
def symbols_of(df: pl.DataFrame, market: str) -> pl.DataFrame
def filter_symbols(df: pl.DataFrame, market: str) -> pl.DataFrame
def market_cache_key(market: str, *parts: str) -> tuple
def benchmark_symbol(market: str) -> str | None
```

- `filter_symbols` 对空表、无 `symbol` 列安全返回。
- `benchmark_symbol`：`cn → 000300.SH`，`hk → HSI`，`us → SPX`。
- 所有聚合入口先过滤再计算。

### 5.2 指标与管道

- `compute_all(df, instruments=None, historical_shares=None, market="cn")`：
  - `market=cn`：保持现有完整链路。
  - `market=hk/us`：只算通用指标与信号；跳过 `compute_limit_signals`；补齐 `signal_limit_up/down=False`、`consecutive_limit_ups/downs=0`，保证下游列存在。
- `run_pipeline(..., market="cn")`：
  - 读取共享 `kline_daily` 后按市场过滤，再计算、写回共享 enriched。
  - `symbols` 仍用于局部重算；`market` 用于过滤与缓存键。
  - 发布锁/心跳沿用现有 `EnrichedPublication`，不新增第二套。
- `run_market_sync` 改为调用 `run_pipeline(symbols=universe, market=market)`，不再依赖「全表重算但不碰 A 股」的隐含假设。
- `ScreenerService(repo, asset_type="stock", market="cn")`：
  - 所有 `_load_enriched_*` 结果先 `filter_symbols`。
  - `latest_date()` 用该市场有数据的日期，而不是全局最新日期（避免 HK/US 未同步时显示 A 股日期）。
  - 历史缓存 key 加 market。

### 5.3 选股与策略

- `POST /api/screener/run`、`GET /api/screener/*` 已接收 `market` 的端点继续透传；未接收的补 `market: str = Query("cn")`。
- 策略兼容性：依赖 `signal_limit_up/down`、`consecutive_limit_*` 的策略在 HK/US 返回 400 或跳过，并给出明确原因（复用现有 `_market_compatible_strategy`）。
- 策略结果缓存：`strategy_cache` 文件名/键加入 market；A 股沿用旧文件名以兼容。
- 批量筛选 `body.market` 必须真正传入 `ScreenerService`（现状已部分读取，需补测试）。

### 5.4 市场总览

- `build_market_overview(repo, quote_service, depth_service, as_of, market="cn")`：
  - `cn` 走现有逻辑。
  - `hk/us` 走 `build_market_overview_market`（移植参考实现）：
    - 无涨停/连板 → `limit` 用 60 日新高/新低；
    - 无概念/行业扩展 → `concept_rank/industry_rank` 为空；
    - 无成交额/换手率 → `amount=0`，`activity` 只保留量比；
    - 指数从 `kline_index_daily_{market}` 读取。
- `GET /api/overview/market?market=cn|hk|us`：缓存键 `market:as_of`。
- 前端 `Dashboard` 按 `useMarket()` 传参；HK/US 隐藏概念/行业区块与涨跌停文案，改用新高/新低。

### 5.5 市场环境（Regime）

- 新增 `build_regime_market(repo, market, start, end)`：
  - 读共享 enriched 后按 market 过滤；
  - 调 `compute_all(df, market=market)`；
  - 4 维评分：赚钱 / 动量（替代投机）/ 抗跌 / 趋势；
  - 输出与 A 股 `RegimeRow` 同构字段，`market` 标识来源。
- 持久化：`data/regime_{market}/part.parquet`，`save/load/coverage` 三件套。
- API：`/api/regime/history|latest|coverage|compute` 全部支持 `market`，默认 `cn`。
- 前端 `Regime` 页按 market 请求；HK/US 隐藏连板/封板率等 A 股专属指标，展示动量档位。

### 5.6 强度梯队（替代连板梯队）

- `GET /api/screener/limit-ladder?market=hk|us` 不再返回「不可用」门控，改为 `_limit_ladder_market`：
  - `boards` = 20 日动量档位：≥25%→5、≥15%→4、≥8%→3、≥3%→2；
  - `status` = `high`（60 日新高）/ `volume`（放量）/ `momentum`（强动量）；
  - `counts.up/down` = 60 日新高/新低数；
  - 返回结构与 A 股梯队一致，前端复用同一组件。
- A 股逻辑不变；`market=cn` 不进入新分支。

### 5.7 复盘 / Recap

- 港美股复盘不生成 A 股涨停/连板叙事；改为：
  - 指数表现、涨跌家数、强弱分布、新高新低、领涨/领跌、量比活跃榜；
  - 复用 `market_recap` 的流式框架，按 market 选择提示词与数据上下文。
- 若本次不实现完整复盘，至少让 `/api/market-recap` 在 HK/US 返回明确门控，不输出 A 股数据。

### 5.8 回测

- `backtest/engine.py` 费用：
  - 印花税按 `markets.stamp_tax_for(market)`；
  - 港股双边收取，美股不收；
  - 手数按 `markets.lot_size_for(market)`（美股 1，港股暂 100，后续接维表）。
- 标的池按 market 过滤，避免 HK/US 标的进入 A 股回测。
- 依赖涨停/连板信号的策略在 HK/US 回测前置拒绝。

### 5.9 前端市场接线

| 页面/模块 | 改动 |
|---|---|
| `Layout` | 已有切换器；切换后刷新全局 query（保留） |
| `Dashboard` | `market` 传入 overview / indices / breadth；HK/US 隐藏 A 股专属区块 |
| `Screener` | 策略运行、结果、缓存键带 market；不兼容策略置灰并提示 |
| `Regime` | history/latest/coverage/compute 带 market；文案按市场切换 |
| `LimitUpLadder` | HK/US 显示「强度梯队」，列名/状态映射复用 |
| `Review` | 按 market 拉取复盘；无数据时门控 |
| `Backtest` | 标的池、费用、结果文案按 market |
| `Indices` | 已有市场指数页；确保与全局 market 同步 |
| `Paper` | 账户市场徽标、回放入口、港美股费用/手数提示 |
| 概念/行业分析 | HK/US 显示 `MarketNotSupported`（无扩展数据） |

统一约定：所有 queryKey 加 `market` 维度，避免切换市场命中旧缓存。

## 6. API 变更清单

| 方法 | 路径 | 变更 |
|---|---|---|
| GET | `/api/paper/replay/symbols` | 新增 |
| GET | `/api/paper/replay` | 新增 |
| POST | `/api/paper/account` | 新增可选 `market` |
| GET | `/api/paper/overview` | 返回 `market`、按市场基准 |
| GET | `/api/overview/market` | 新增 `market` 参数与 HK/US 分支 |
| GET | `/api/regime/history` | 新增 `market` |
| GET | `/api/regime/latest` | 新增 `market` |
| GET | `/api/regime/coverage` | 新增 `market` |
| POST | `/api/regime/compute` | 新增 `market` |
| GET | `/api/screener/limit-ladder` | HK/US 返回强度梯队 |
| POST | `/api/screener/run` | `market` 透传到服务与缓存 |
| GET | `/api/screener/*` | 需要聚合的端点补 `market` |
| POST | `/api/backtest/*` | 市场过滤与费用 |

所有新参数默认 `cn`，旧调用不受影响。

## 7. 模拟盘多市场撮合适配

### 7.1 账户市场

- `account.json` 新增 `market`；旧账户读取时 `setdefault("market", "cn")`。
- 创建账户 API 增加 `market`，校验 `cn|hk|us`。
- 账户市场与绑定策略市场不一致时拒绝绑定。

### 7.2 手数

- `paper.LOT_SIZE` 保留为 A 股默认，新增：
  - `lot_size_for_account(acc)`：`markets.lot_size_for(acc.market)`；
  - 美股 1 股；港股暂 100（维表若有 `lot_size` 字段则优先）；
  - A 股 100。
- `normalize_qty`、`qty_from_amount`、订单校验改为接收 `lot_size`，默认 100 保持旧行为。
- 港股实际每手股数差异大，UI 在账户市场为 HK 时提示「按 100 股估算，实际以标的每手为准」；后续接维表再收紧。

### 7.3 费用

- `buy_fee/sell_fee` 增加市场参数：
  - `cn`：佣金双边 + 卖出印花税；
  - `hk`：佣金双边 + 买卖双边印花税；
  - `us`：佣金双边，无印花税。
- 账户创建时按市场给默认印花税；用户仍可覆盖。
- 历史台账不重算。

### 7.4 撮合约束

- 涨跌停检查仅 `cn`；HK/US 跳过。
- T+1 仅 `cn`；HK/US 为 T+0，`_available_of` 按账户市场返回全部批次。
- 交易时段：`evaluate_intraday` 的调用方按账户市场判断；美股跨午夜时段沿用 `markets.is_trading_now`。
- 结算日：仍按北京时间交易日；港美股节假日缺行情时按现有顺延规则处理。

### 7.5 基准

- `settle_day` 写 `benchmark_close` 时按账户市场取：
  - `cn → 000300.SH`（`kline_index_daily`）
  - `hk → HSI`（`kline_index_daily_hk`）
  - `us → SPX`（`kline_index_daily_us`）
- 基准缺失时省略字段，前端不画基准线。
- 前端净值图基准名称按市场显示，不再硬编码「沪深300」。

## 8. 兼容性与迁移

- **A 股默认**：所有 API 默认 `market=cn`；A 股页面不传参也保持原行为。
- **旧数据**：不移动、不重命名 A 股目录；旧账户无 `market` 视为 `cn`。
- **旧前端**：新增字段均为加法；旧前端忽略 `market`、`markers` 等字段不受影响。
- **缓存**：A 股缓存键/文件名保持兼容；港美股使用独立键。
- **回滚**：多市场功能可通过前端隐藏入口 + 后端 `market=cn` 默认降级；模拟盘回放为只读，删除前端入口即可停用。
- **不做破坏性迁移**：共享存储的市场隔离靠读时过滤，不要求用户重建数据。

## 9. 分阶段实施

### 阶段 1：模拟盘回放（先交付）

1. `paper.py` 增加 `market` 字段读取默认值、回放读模型函数（symbols / fills / markers / summary）。
2. `api/paper.py` 增加两个只读端点与参数校验。
3. `frontend/src/lib/api.ts` 增加类型与 `paperReplaySymbols` / `paperReplay`。
4. `frontend/src/custom/paper/Page.tsx` 增加回放入口与 `PaperReplayModal`。
5. 测试：回放读模型单测 + API 契约测试 + 前端构建。

### 阶段 2：市场隔离地基

1. `markets.py` 增加 `filter_symbols` / `benchmark_symbol` / 缓存键工具。
2. `compute_all` / `run_pipeline` 增加 `market`，HK/US 跳过涨跌停信号。
3. `ScreenerService` 增加 `market`，所有读取路径过滤。
4. `run_market_sync` 传 `market`。
5. 测试：`test_markets.py`、`test_market_routing.py`，重点验证 A 股结果不被 HK/US 污染。

### 阶段 3：聚合能力

1. 市场总览 HK/US 分支。
2. Regime market 构建/持久化/覆盖。
3. 强度梯队 HK/US。
4. 复盘门控或港美股版本。
5. 测试：`test_strength_ladder_market.py`、`test_market_overview_market.py`、`test_regime_market.py`。

### 阶段 4：前端接线与模拟盘多市场

1. Dashboard / Regime / Screener / Ladder / Review / Backtest 按 market 请求。
2. 概念/行业页 HK/US 门控。
3. 模拟盘账户 market、手数、费用、T+0、基准。
4. 测试：前端 `pnpm build`、关键页面本地目测；后端费用/T+0/基准单测。

## 10. 测试矩阵

| 层 | 测试 |
|---|---|
| 回放读模型 | symbol 过滤、日期闭区间、`limit` 截断、`corp_action` 转 neutral、reason 解析、FIFO 区间盈亏、跨账户隔离 |
| 回放 API | 成功响应、未知 symbol、非法 account、空成交、参数边界 |
| 市场工具 | 后缀过滤、空表、无 symbol 列、默认 cn、benchmark 映射 |
| 指标 | HK/US 不产生涨跌停信号；CN 结果与改造前逐位一致 |
| 选股 | CN 结果不含 `.HK/.US`；HK 结果不含 A 股；缓存键隔离 |
| 总览 | HK/US 字段齐全、无 A 股涨停语义、指数读取正确 |
| Regime | HK/US 4 维评分、持久化 upsert、coverage |
| 梯队 | HK/US 动量档位、status 映射、counts 为新高/新低 |
| 模拟盘多市场 | US 1 股、HK 双边印花税、T+0 可卖、CN 涨跌停仍生效、基准映射 |
| 前端 | `pnpm build`；回放弹窗渲染、市场切换 query 刷新 |

## 11. 验证命令

```bash
cd backend && uv run pytest tests/ -q
cd backend && uv run ruff check app tests
cd frontend && pnpm build
git diff --check
```

按改动范围至少运行：

```bash
cd backend && uv run pytest tests/test_paper_replay.py tests/test_markets.py tests/test_market_routing.py -q
```

修复 bug 先写复现测试再修（先失败后通过）。

## 12. 风险与待确认

1. **共享存储污染**：最大风险。所有聚合入口必须过滤；新增读路径时必须在 code review 检查 `market`。
2. **港股手数**：固定 100 只是近似；若产品要求精确，需要同步标的 `lot_size` 维表。
3. **港美股节假日**：北京时间交易日与本地市场交易日不完全一致；结算顺延规则需在真实数据上验证。
4. **美股跨午夜**：盘中撮合与「当日」归属需明确按北京时间还是美东时间；当前沿用北京时间口径，需在 UI 标注。
5. **策略兼容性**：涨停类策略在 HK/US 必须明确拒绝，不能静默产生空结果。
6. **复盘数据量**：回放默认限制 500 笔；大账户需分页或按标的查询。

## 13. 交付标准

- 模拟盘账户可绑定固定策略，成交台账可回放到日 K，买卖点、原因、费用、盈亏可追溯。
- 切换 HK/US 后，选股、总览、环境、强度梯队、指数、回测、模拟盘按市场语义工作，不混入 A 股数据。
- A 股默认行为、历史数据、旧 API 调用保持兼容。
- 新增/修改逻辑有对应测试；后端 pytest、ruff、前端 build、`git diff --check` 全部通过。

## 14. 实施记录（当前分支）

已完成：

- **模拟盘回放**：`fills.jsonl` 读模型 + `/api/paper/replay` + 前端回放弹窗（买卖点标记由后端生成）。
- **市场地基**：`app/markets.py` 作为单一事实来源；`compute_all(market=)`；`ScreenerService(market=)`
  的 enriched 过滤、缓存键与 `latest_date`；`run_market_sync` 传 market。
- **缓存隔离**：`strategy_cache` 按 market 分文件（cn 沿用旧文件名），`run_all` 任务 key 首位为 market，
  分钟摘要按 market 过滤。
- **聚合**：HK/US 市场总览、Regime（`data/regime_{market}/part.parquet`）、强度梯队、复盘门控。
- **模拟盘撮合**：账户 market、手数（美股 1 股）、印花税（港股双边）、T+0、基准指数映射。
- **前端**：全局 market 接入 Dashboard / Regime / 强度梯队 / 选股 / 回测 / 模拟盘 / 复盘与概念行业门控，
  queryKey 全量带 market 维度。
- **回测**：`market` 进入 `/run`、`/strategy/run`、SSE `/strategy/stream` 与优化/滚动端点；
  标的池按市场校验（混市场 400）、涨停/连板类策略在 HK/US 前置拒绝、印花税按市场覆盖
  （cn 保持策略自定义值不变）。

### 14.1 港美股数据接入（2026-11-21 补齐）

前面各节完成的是「读侧隔离 + 接口路由」，**没有写侧的接线**：本地只有 A 股数据，切到
港股/美股后所有页面为空。定位到四处断点并逐一修掉：

| # | 断点 | 表现 | 修复 |
|---|------|------|------|
| 1 | `pipeline/run-market` 需要 `instruments_index_{market}` 维表，但 `instrument_sync.sync_market_instruments` 无任何调用方 | 恒返回「无标的」，0 行 | `run_market_sync` Step 0 维表缺失时自动同步，并记录 `sync_instruments` 阶段 |
| 2 | `sync_market_instruments` 内没有 `tf = get_client()` | `NameError('tf')` 被自身 `except` 吞掉 → 恒 0 只标的、无任何日志 | 函数内取客户端 |
| 3 | `repository.get_instruments_market` 用未导入的 `MARKET_CN` | 调用即 `NameError` | 局部导入常量 |
| 4 | `run_pipeline(market=)` 只在两处 `_compute_storage_batches` 之外丢失 market（主分支 `compute_enriched` 未传） | 港美股按 A 股口径算指标 | 主分支与除权分支均透传 market；维表按市场读取（`instruments_index_{market}`） |

同时补了三处语义缺口：

- **换手率**：A 股的 `turnover_rate` 是 `compute_limit_signals` 的副产物（该函数在港美股被整体跳过），
  导致港美股换手率恒 null、按换手率筛股的策略一只也筛不出。新增 `_compute_market_turnover`，
  按「volume 单位是股（港美股）/手（A 股）」分别换算，公式 `volume × shares_per_unit / float_shares × 100`。
- **enriched 行数口径**：局部重算原先把「合写后的整个分区行数」当成本次产出（同步 20 只港股报 130 万行），
  改为只计本次产出行。
- **覆盖进度**：`run_market_sync(limit=N)` 优先同步「从未同步过」的标的（账本 `data/.market_sync_{market}.json`），
  反复点「更新」可以分次把覆盖面铺满，而不是每次都拉同一批；结果里回传 `coverage{synced, instruments}`。

对外可见的三处入口：

- `POST /api/pipeline/run-market?market=hk|us&limit=N` — 维表 → 日K → enriched 全链路（limit 为分批上限）。
- `GET /api/data/market-status?market=` — 该市场维表行数、日K覆盖标的数、最新交易日（`data/status` 是全市场合计，无法回答「港股为什么是空的」）。
- 前端 `MarketDataBanner`（市场切换器下方）— 有数据时显示「日K 覆盖 x / y 只 · 最新日期」，无数据时直接给「同步港股/美股数据」按钮并跟随进度；`POST /api/kline/rebuild_enriched?market=` 支持按市场重算指标。

实测（TickFlow 免费档）：港股维表 2,895 只、美股 12,485 只；港股全量日K 29 个批次约 30 分钟，
美股约 125 个批次（2 小时以上），因此美股建议分批同步。

已知限制（fail-closed，不做静默近似）：

- HK/US 暂不支持「全市场」回测，需显式指定标的池（回测面板加载器仍以 A 股 enriched 全表为底）。
- 港股每手股数按 100 估算，精确值待接标的维表。
- Regime 的「情绪周期」页为 A 股涨停/连板口径，HK/US 不展示。
- 港美股换手率按**当前**流通股本折算（A 股的股本历史表不含港美股），历史日期是近似值；
  维表缺 `float_shares` 的标的（港股约 3%）换手率仍为 null。
- 港美股与 A 股共用 `kline_daily` / `kline_daily_enriched` 物理分区，靠读侧按 symbol 后缀切分；
  新增「整表扫描」的消费方必须先用 `markets.filter_symbols` 过滤，否则会混入其它市场。

### 自选 / 搜索的多市场（2026-11-21）

「切到港股却搜不到小米」暴露的是同一条链路上的三处 A 股写死：

| 位置 | 原行为 | 现行为 |
|---|---|---|
| `GET /api/kline/instruments/search` | 只查 `get_instruments_asset()`（A股+ETF） | 新增 `market` 参数；非 cn 查 `instruments_index_{market}`，结果带 `market` 字段；维表缺失返回空而不是回落 A 股 |
| `GET /api/watchlist` | 返回全部 symbol | 可选 `market` 过滤（省略保持旧行为）；`/enriched` 按市场分流 |
| `GET /api/watchlist/enriched` | 只从 A 股内存快照取数，港美股行指标全 null | 新增 `market`：cn 走原路径；hk/us 走 `ScreenerService(market)` + 市场维表，列契约对齐（涨跌停价/连板为 null） |
| `POST /api/kline/instruments/names` | 只查 A 股名称表 | 按 symbol 后缀分市场取名 |

配套修改：

- `repository.get_instruments_market` / 新增 `get_market_name_map`：港美股维表与名称表加进程内 memo
  （原先每次搜索都 `read_parquet` 1.2 万行），随 `refresh_cache` 失效，同步后立刻可见。
- 自选「加入以来涨跌幅」基准价：A 股走 enriched 内存历史，港美股改为只读加入日分区，
  且**逐日回落到最近一个交易日**（周末/假期加入不再恒为 null）。
- `quote_demand` 的 A 股实时订阅只取 cn 自选（港美股没有实时行情，混入只会挂空订阅）。
- 前端：`Watchlist` 接入 `useMarket`，搜索/行情查询带 market 与 market 维度的 queryKey；
  清单仍缓存全市场，展示层按市场切分（mutation 返回的是全量名单，避免切市场后互相覆盖）。

已知限制：

- 港美股自选没有实时行情/分时，只有日K 收盘价（换手率、均线等日频指标正常）。
- `POST /api/watchlist/prepare` 对港股返回 `no_data`（走的是 A 股数据准备链路），后续可改为直接用本地日K。
- `ext_columns`（行业评分/资金流等扩展表）为 A 股口径，港美股自选忽略该参数。

