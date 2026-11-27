# 第三方数据源整合:腾讯分 K / XTick / 东方财富

> 🍴 **状态声明**:本文记录本 fork 已落地的三个数据源整合,均与上游仓库无关。
> "实测"结论来自 2026-11-27/28 真实网络请求,上游接口可能变更;
> 接入代码位于 `backend/app/plugins/{tencent,xtick,eastmoney}/`,
> 配套测试为 `backend/tests/test_{tencent,xtick,eastmoney_factors}.py`。
> 升级合并上游时需按 [二次开发基线](./secondary-development.md#0-本仓库二开基线) 复核本文件列出的改动点。

## 目录

- [1. 为什么需要外部数据源](#1-为什么需要外部数据源)
- [2. 腾讯公开行情:补齐分钟 K](#2-腾讯公开行情补齐分钟-k)
- [3. XTick:量化行情 API 整合](#3-xtick量化行情-api-整合)
- [4. 东方财富:日频事件因子](#4-东方财富日频事件因子龙虎榜--融资融券)
- [5. 配置与运维](#5-配置与运维)
- [6. 改动清单与升级复核](#6-改动清单与升级复核)

---

## 1. 为什么需要外部数据源

项目默认数据源为 TickFlow。当前账号为 Free 档,`data/capabilities.json` 的探测日志显示分钟能力全部无权限:

```
kline.minute.by_symbol(无权限) / kline.minute.batch(无权限) /
intraday(无权限) / intraday.batch(无权限) / intraday.universe(无权限)
```

表现是:分时图空白、手动"获取分钟 K"返回 403、分钟策略回测报"回测区间内无分钟K数据"。
`data/kline_minute/` 与 `data/kline_etf_minute/` 均为 0 个 parquet 文件,分钟同步开关默认关闭。

**关键认知**:代码链路是完整的(Provider 契约 → 能力矩阵 → 源路由 → 同步落盘 → 分时图/回测),
缺的是"一个可用的分钟源"。因此三个数据源都以插件形式接入,不改核心链路,
只做" datasets 声明 + provider 实现 + 门禁口径校准"。

数据源插件的通用约定见 [plugin-development.md](./plugin-development.md) 与
[custom-data-source.md](./custom-data-source.md)。

---

## 2. 腾讯公开行情:补齐分钟 K

### 2.1 上游端点实测

腾讯提供无需注册、无需 token 的公开 HTTP GET 端点。实测结果(2026-11-27):

| 端点 | 用途 | 实测结论 |
| :--- | :--- | :--- |
| `https://qt.gtimg.cn/q=sh600519,sz000001,hk00700` | 实时快照 + 五档 | 通。GBK 编码、`~` 分隔、支持批量、含五档盘口和成交额 |
| `https://ifzq.gtimg.cn/appstock/app/kline/mkline?param=sh600519,m1,,,1000` | **真正的分钟 OHLC K 线** | 通。m1/m5/m15/m30/m60 都支持,行序 `[时间,开,收,高,低,量,{},额]` |
| `https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh600519` | 当日分时 | 通。09:30–15:30 共 267 点,价格+累计量额,需差分 |
| `https://web.ifzq.gtimg.cn/appstock/app/day/query?code=sh600519` | 最近 5 日分时 | 通。股票/指数/ETF/港股/北交所都覆盖;`date` 参数无效,不支持批量 |
| `https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,120,qfq` | 前复权日 K | 通。备用直取,不进路由 |

> 注意:`web.ifzq.gtimg.cn` 的 mkline 路径会 301 到 `web3`,代理环境下不稳定;
> 分钟 K 必须使用**无 `web.` 前缀**的 `ifzq.gtimg.cn` 主机。

### 2.2 能力边界(务必先读)

分钟 K 的**历史深度由上游硬限制**,不接受日期范围参数(指定起止日期返回空):

| 周期 | 实测最大根数 | 约覆盖 |
| :--- | :--- | :--- |
| m1 | 482 | ≈2 个交易日 |
| m5 | 320 | ≈7 个交易日 |
| m15 | 320 | ≈1 个月 |
| m30 | 320 | ≈2 个月 |
| m60 | 320 | ≈4 个月 |

因此本源定位为 **近两日分时 + 日常增量积累**,不是长历史回补源:

- ✅ 可解决:当日/近两日分时图、自选迷你分时、盘中信号、逐日盘后增量落盘。
- ⚠️ 不能完全解决:一次性补齐数月 m1 历史。想要长历史仍需 TickFlow Pro/Expert 或 TdxQuant。

### 2.3 字段口径与实现要点

代码位置:`backend/app/plugins/tencent/{client.py,provider.py,plugin.yaml}`。

**client.py**
- 三个端点封装为一个 `httpx.Client` 连接池,超时 10s,异常归一为 `TencentError`。
- `to_code()` / `from_code()` 做项目 symbol 与腾讯代码的双向映射:
  `600519.SH ↔ sh600519`、`000001.SZ ↔ sz000001`、`920002.BJ ↔ bj920002`、`00700.HK ↔ hk00700`;
  裸代码按 6/5/9→sh、0/1/2/3→sz、4/8/920→bj、5 位→hk 推断。
- 实时快照按 GBK 解码,`v_<code>="..."` 正则提取,字段以 `~` 分隔。

**provider.py**
- `datasets: [minute]`。只声明逐标的可全市场覆盖的分钟能力。
  `realtime`/`depth5` 因无法枚举全市场标的,仅通过 `raw()` 按需直取,不进路由。
- 分钟行序 **收在第三位**:`[时间, 开, 收, 高, 低, 量(手), {}, 额]`。
  按 OHLC 顺序读会把收盘价错当成最高价,这是最容易踩的坑。
- `datetime`:北京墙钟 `YYYYMMDDHHMM` → naive `Datetime("us")`,满足
  [分钟时区守卫](../CONTRIBUTING.md)(`minute_normalizer._enforce_minute_beijing_wallclock`)。
- `volume`:单位手,实测逐分钟求和与日 K 成交量完全一致(39806 vs 39808)。
- `amount`:上游额字段口径未经文档确认,按 `volume(手) × 100 × close` 推算成交额(元)。
  实测推算值与 `day/query` 的累计额吻合(491 手 × 100 × 1469.99 = 72,176,509 元)。
- 逐标的请求节流 0.12s;上游失败抛 `ProviderRequestError`,由同步层 fail-closed,
  **不静默回退 TickFlow**(否则用户以为切了源,实际走免费档)。

### 2.4 门禁改造(唯一的核心源码改动)

原 `app/api/kline.py` 的分钟批量门禁只认 TickFlow 档位或全量分钟服务健康,
切到插件后仍会 403。改动:

```python
# 原: if not capset.has(Cap.KLINE_MINUTE_BATCH):
# 改: if not _minute_allowed(capset):
```

`_minute_allowed()` 已是统一判定入口:TickFlow 看档位,插件看 dataset 声明与可用性。
`daily_pipeline.py` 的盘后同步与 `intraday_monitor_support()` 本来就走
`_resolve_minute_provider()`,无需改动。

### 2.5 使用方式

1. 在 **设置 → 数据源 → 分钟K** 选择「腾讯行情」(无需 Key)。
2. 打开分钟同步开关(数据页);盘后管道会自动逐日增量落盘。
3. 分时图、迷你分时、分钟回测自动解锁。近两日数据即时可用,更长历史逐日积累。

### 2.6 验证结果

- `backend/tests/test_tencent.py` **11 例全过**:代码映射、分钟字段顺序与单位、窗口过滤、
  GBK 解析、异常降级、门禁放行。
- 回归矩阵(tencent + xtick + news + cache + fuyao)**201 passed**。
- 端到端落盘:以真实接口 `sync_and_persist_minute(['600519.SH'], days=2)` 写入 482 行 parquet,
  单日读回 241 行(09:30–15:00),字段完整、时间口径正确。
- 容器重启后 `/api/settings/capability-matrix` 的 minute 候选中出现 `tencent: available=true`。

---

## 3. XTick:量化行情 API 整合

### 3.1 来源与能力盘点

本地技能仓库 `/Users/liuzuodong/Documents/workspace/skills/xtick`(commit `05087c5`)提供
同花顺风格的行情/量化 API 封装。盘点发现脚本实际有 **196 个函数**(SKILL.md 只写 90 个):
Market 9、Watch 7、Core 9、Hot 12、Quant 2、Indicator 157。

### 3.2 能力分级决策

| 级别 | 内容 | 决策 |
| :--- | :--- | :--- |
| A 路由数据集 | `getDayKlineRealtime` 全市场实时、`getDeepRealtime` 五档 | ✅ 接入 |
| B 专用能力 | 竞价、连板天梯、市场情绪、资金流、龙虎榜、概念板块、分时/分笔、量化因子 | ✅ 服务层直取 |
| C 暂缓 | 157 个技术指标接口、无文档的 `cdl*`/`ht*`/三角函数接口、PyPI `xtick-skill`、demo 客户端 | ❌ 不引入 |

157 个指标接口不接的原因:项目 `app/indicators/pipeline.py` 已有自研 MA/EMA/MACD/RSI/KDJ/BOLL/ATR,
重复接入会造成口径分裂。

### 3.3 上游限制与实测

- Token 走 URL query string;**HTTPS 返回 404,只能用 HTTP**(`http://api.xtick.top`)。
- 脚本无超时/重试/限流;全部由接入层补齐。
- 用真实 token 探测:股票列表、日 K、分钟 K、实时、深度、竞价、热点、资金流、
  龙虎榜、财务、量化因子全部可用。
- `/doc/core/batchkline` 的成交量/成交额是常规日 K 的 **2 倍**,判定为上游口径异常,
  因此日 K/分钟 K 只做单标的按需查询,**不声明 daily/minute 路由**,避免全市场逐股请求。

### 3.4 实现结构

**后端**
- `backend/app/plugins/xtick/plugin.yaml`:`datasets: [realtime, depth5, financial]`。
- `client.py`:白名单方法映射、token 注入、参数校验、错误脱敏。
- `provider.py`:全市场实时快照(实测 5576 只)、五档盘口、标的维表、设置页试拉;
  失败 fail-closed,不跨源回退。
- `vendor/xtick/scripts/`:固定 skills 仓库 commit `05087c5` 的 5 个 API 模块 + 硬化版
  `XTickUtil`(httpx、超时、状态码、业务错误、gzip/zip、返回类型归一)。
  **安全修复**:关闭 httpx/httpcore 的 INFO 日志,避免含 token 的 URL 进入日志;
  错误文案中的 token 替换为 `[已隐藏]`。
- `services/xtick.py` + `custom/xtick.py`:通过后端扩展注册 `/api/xtick/status`、
  `/api/xtick/data/{kind}`,**不改 `main.py`**。
- 缓存 namespace `xtick`:实时类 30s、当日热点 5min、历史类 7 天;空结果不缓存。

**前端**
- `frontend/src/custom/xtick/extension.tsx` + `Page.tsx`:路由 `/xtick`,导航 `order: 0.6`
  (看板 → 新闻 → XTick → 自选)。
- 8 个标签:竞价、连板天梯、市场情绪、资金流、龙虎榜、资讯、K线/财务、股东结构;
  股东结构页可直接触发「同步为股东结构因子」;页面明确标注"HTTP + query token"的安全边界。
- `api.ts` / `queryKeys.ts` 增加类型、请求封装与查询键。

### 3.5 配置与安全

- Token 写入 `data/user_data/secrets.json` 的 `xtick_api_key`,权限 0600;代码与测试不含真实 token。
- `.env.example` 新增 `XTICK_API_KEY` 与 `XTICK_BASE_URL`(默认 `http://api.xtick.top`)。
- 容器日志中 token 前缀计数为 0。

### 3.6 验证结果

- `backend/tests/test_xtick.py` **14 例全过**。
- 回归矩阵合计 **201 passed**;ruff 全过。
- 前端 `pnpm build` 成功,产物含 `official.xtick`;导航测试 18 passed。
- 容器重启冒烟:`/api/xtick/status` → `available=true`,`/api/xtick/data/board` 返回 426 条,
  `/xtick` → HTTP 200;Redis `tf:xtick:*` TTL 分层正确(当日约 292s,历史约 604424s)。

> 说明:测试中 `2026-11-27` 这类硬编码日期会在跨日后失效(TODAY_TTL 判定),
> 已改为 `cn_today()` 动态取值,避免回归矩阵随时间漂移。

### 3.7 财务与股东:从数据源能力到选股因子

XTick 除行情外还提供基本面数据。实测(2026-11-28,真实 token)确认四类端点可用,
且**每行都带 `reportDate`(报告期)与 `publicDate`(公告日)**,这是做点时因子
(PIT,无未来函数)的关键前提:

| 端点 | 内容 | 实测样本 |
| :--- | :--- | :--- |
| `/doc/gaap` | 核心财务指标,2007 起 | 600519 返回 11 期,含 roe/bps/毛利率/净利率/同比/资产负债率 |
| `/doc/capital` | 历史股本表 | 600519 返回 35 期,totalCapital/flowCapital/restrictCapital |
| `/doc/topholder` | 十大股东 | 600519 返回 100 行(10 期),含 name/num/ratio/reason/rank |
| `/doc/topflowholder` | 十大流通股东 | 与十大股东同构 |
| `/doc/holdernum` | 股东户数,2001 起 | 600519 返回 11 期,holder/holderA/holderFlow |

**接入方式(复用既有财务管线,不新造链路)**

1. `financial` 数据集:`provider.get_financials()` 把 gaap → `metrics`、
   capital → `shares`,映射到项目 canonical 列。之后设置页把「财务数据」切到 XTick,
   现有 `sync_metrics/sync_shares`、`/api/financials/*`、财务页全部照常工作。
2. 股东结构:三个端点聚合为宽表 `data/financials/holder_metrics/part.parquet`
   (`services/holder_sync.py`),一行一个 `(symbol, period_end)`:
   - `top10_ratio` / `top10_top1_ratio` / `top10_hhi` / `top10_new_count` 等;
   - `flow_ratio` / `flow_hhi` 等(十大流通股东);
   - `holder_num` / `holder_num_chg`(股东户数及环比)。
   聚合口径集中在 `plugins/xtick/holders.py` 的纯函数里,便于单测。
3. 因子化:`backtest/fundamentals.py` 的 `FUNDAMENTAL_FACTORS` 增加 `table` 维度,
   新增 6 个「股东结构」因子(`holder_num`、`holder_num_chg`、`top10_hold_ratio`、
   `top10_flow_ratio`、`top10_hhi`、`top10_new_count`),与财务因子共用同一套
   **公告日次一交易日起生效、缺失为 null 不填 0** 的门控规则;因子库、因子检验、
   挖掘、回测、矩阵路径自动可见,无需前端改因子清单。

**口径与边界**

- 三大报表(利润表/资产负债表/现金流量表)上游没有端点,`get_financials` 返回空表,
  由 TickFlow 或扶摇补齐;`_merge_report_history` 按 `(symbol, period_end)` 逐列取
  最新非空值,多源可并集共存,不会互相覆盖。
- 财务接口为**单标的**请求,全市场同步需逐股拉取(已加 50ms 节流);股东同步同样是
  每股 3 次请求,首次全量约 30~60 分钟,因此只做手动触发 + 增量补最新期。
- `netProfit` 实测为净利率(54.35 表示 54.35%),不是净利润金额;语义未确认的字段
  (`netProfitM/netProfitA/turnover/prePay` 等)一律不透传,避免口径猜错。
- `reason`(新进/增持/减持/不变)直接决定新进家数与增减持家数;上游未给"退出"行,
  因此不计算退出家数。
- 股东数据与财务数据公告日可能相差数日,两张表各自独立做 asof join,
  不会因为合并到同一行而提前生效。

**验证**

- `backend/tests/test_xtick_financial.py
backend/tests/test_eastmoney_factors.py` **8 例**:字段映射、股本列、股东聚合、
  多表公告日门控、缺表降级、因子来源分组。
- 真实落盘:600519 + 000001 两只有效标的 → 32 行 `holder_metrics`,
  公告日/报告期/比例/户数环比均与上游一致。
- 既有 `test_fundamental_factors.py` 18 例、`test_financial_shares.py` 等全部通过,
  多表改造未破坏原财务因子行为。

---

## 4. 东方财富:日频事件因子(龙虎榜 / 融资融券)

XTick 补的是"实时/量化"能力,东方财富公开数据中心补的是**日频事件**:
龙虎榜与融资融券都按日全市场披露、可回溯历史,天然适合做截面因子。
个股资金流此前已通过 `scripts/ingest_moneyflow_factors.py` 落 ext_data 因子通道,
本次不重复建设。

### 4.1 接口实测(2026-11-27/28)

| 报表 | 单日规模 | 关键字段 |
| :--- | :--- | :--- |
| `RPT_DAILYBILLBOARD_DETAILSNEW` | 60~90 行(含可转债/B 股) | `BILLBOARD_NET_AMT` / `ACCUM_AMOUNT` / `DEAL_NET_RATIO` / `EXPLANATION` |
| `RPTA_WEB_RZRQ_GGMX` | ~4500 行(含 ETF/可转债),`pageSize` 上限 500 → 约 9 页 | `RZYE` / `RQYE` / `RZMRE` / `RZJME5D` / `SZ` / `RZYEZB` |

- 都走 `datacenter-web.eastmoney.com/api/data/v1/get`,按 `TRADE_DATE` / `DATE` 精确过滤;
  该域名 httpx 实测可达(行情类 push2 接口仍需 curl 子进程)。
- `SECUCODE` 带交易所后缀,辅以代码首位过滤(0/3/6/4/8)排除可转债/ETF/B 股。
- 龙虎榜同一标的可因多条「上榜原因」重复出现(单日 86 行 → 去重后 55 只),
  口径取**绝对值最大的一条**净买入额并记录原因条数;不做求和
  ——不同原因的成交额窗口不同(3 日累计 vs 当日),求和会重复计算。

### 4.2 数据 → 因子

落盘 `data/financials/{lhb_metrics,margin_metrics}/part.parquet`,`announce_date` = 交易日。
`backtest/fundamentals.py` 新增 `freq="daily"` 分支,与财务/股东表共用同一套加载与调用点,
因此因子库、检验、挖掘、回测、矩阵路径自动可见。

| 分组 | 因子 | 口径 |
| :--- | :--- | :--- |
| 龙虎榜 | `lhb_net_ratio` | 净买入额 / 成交额(%) |
| | `lhb_on_board` | 是否上榜(1/0) |
| | `lhb_reason_count` | 上榜原因条数 |
| 融资融券 | `margin_balance_ratio` | 融资余额 / 流通市值(%) |
| | `margin_buy_ratio` | 融资买入额 / 流通市值(%) |
| | `margin_net_5d_ratio` | 5 日融资净买入 / 流通市值(%) |
| | `margin_short_ratio` | 融券余额 / 流通市值(%) |

**日频表与报告期表的差异(关键)**

- 报告期表:公告日次日起**前向填充**到下一期;
- 日频表:只在**次一交易日**生效,**不前向填充**——「没上榜」≠「沿用上次上榜」;
- 只有显式声明 `fill` 的因子才在数据覆盖区间内把空值填 0
  (未上龙虎榜 ⇒ 净买占比 0);两融未覆盖保持 null,
  避免把"非两融标的"当成 0 杠杆。
- 比例口径统一以流通市值归一,避免净额直接参与截面排序被市值主导。

### 4.3 触发与运维

- `/eastmoney` 页:两张表的覆盖状态(行数/标的数/日期范围)+ 同步按钮 + 最新明细;
- `POST /api/eastmoney/sync/{lhb_metrics|margin_metrics}?days=N`:后台线程增量同步,
  默认最近 20 个交易日,上限 400;
- 逐日节流 1.2s:龙虎榜 1 请求/日、两融约 9 请求/日,首次长历史回补需数小时;
- `GET /api/eastmoney/status` / `GET /api/eastmoney/data/{table}` 供页面与脚本消费;
- 因子批量回测在缺表时给出的提示已带具体同步入口
  (`missing_hint()` 按来源表区分 数据页 / XTick 页 / 东方财富页)。

### 4.4 验证

- `backend/tests/test_eastmoney_factors.py` **10 例**:代码过滤、龙虎榜去重口径、
  两融比例、T+1 生效、未上榜填 0、覆盖前 null、矩阵/面板双路径一致、
  增量合并幂等、因子分组、非法表名拒绝。
- 真实落盘 2026-11-26/27:龙虎榜 120 行 / 93 标的,两融 3541 行 / 3541 标的。
- 真实面板抽样:300502.SZ 于 11-27 上榜 → 11-30 因子 `lhb_on_board=1`、
  `lhb_net_ratio=2.33`;两融 11-26 数据在 11-27 生效(`margin_balance_ratio=4.24`)。

### 4.5 展示口径:统一语义词与来源标注

外部字段名(如 `jjzf` / `lhb_net_ratio`)只应出现在接口层,页面一律用项目统一语义词,
并如实标注"这份数据是谁给的"。

**语义词表(单一出处)**

- `frontend/src/lib/field-labels.ts`:原始字段 → 中文标签 + 计量单位
  (`price` / `amount` / `volume` / `ratio` / `percent` / `count` / `date` / `datetime`),
  同时负责数值格式化、对齐与空值语义。口径按字段声明,不做类型猜测:
  - `ratio` = 0.0123 → 1.23%;`percent` = 1.23 → 1.23%。混用会直接产出 123% 的假数据;
  - 同一键在不同接口语义不同的(`jjl` 在竞价是竞价量、在情绪是打板成功率;
    `turnover` 在财务是存货周转率、在行情是换手率),由页面列规范显式覆盖;
  - 未识别字段保留原始键名追加在末尾,上游新增字段不会静默丢失。
- `frontend/src/custom/xtick/columns.ts` / `frontend/src/custom/eastmoney/columns.ts`:
  按接口(或落盘表)声明列顺序与覆盖口径,字段名取自 XTick 官方 `references/apidoc.md`
  与 `plugins/eastmoney/client.py` 的落盘 schema。

**来源标注**

- `frontend/src/lib/source-labels.ts`:插件 id → 展示名;`pull.url` 域名 → 上游展示名。
- `frontend/src/components/SourceTag.tsx`:全站统一徽章「数据来源 · XTick」。
- 后端 `services/dimension_source.py`:概念/行业成分实际来自哪些扩展表、上游是谁
  (`/api/ext-data` 的 `source_label`、`/api/sector-rotation` 的 `sources`),
  页面与轮动卡据此标注"成分来源"。**不写死同花顺** —— 用户把扩展表换成自有源后,
  标签会如实变成"扩展数据";内置预设仍是同花顺(fuyao 指数接口)分类。

> 概念/行业板块当前的主数据源是**同花顺分类**(`ext_gn_ths` / `ext_hy_ths` 内置预设,
> 复用 `FUYAO_API_KEY`)。XTick 也提供概念板块成分(`/api/xtick/data/bk`、`gainian`),
> 但未接入为板块主源:它没有申万/同花顺行业层级,且需要至尊版权限,口径与现有扩展表不一致。

---

## 5. 配置与运维

| 数据源 | Key | 声明数据集 | 路由位置 | 缓存 |
| :--- | :--- | :--- | :--- | :--- |
| 腾讯行情 | 无 | `minute` | 设置 → 数据源 → 分钟K | 无(直取上游,逐日落 parquet) |
| XTick | `xtick_api_key` | `realtime`, `depth5`, `financial` | 设置 → 数据源 / `/xtick` 扩展页 / 财务同步 | `tf:xtick:*` 30s / 5min / 7d |
| TdxQuant | `TDXQUANT_API_KEY` | 六项(含 `minute`, `full_minute`) | 设置 → 数据源 | 按积分计费 |
| 同花顺 (fuyao) | `FUYAO_API_KEY` | `realtime`, `daily`, `adj_factor`, `financial` | 设置 → 数据源 + `/news` 新闻页 | `tf:news:*` |
| 东方财富 | 无 | —(日频事件,不参与能力路由) | `/eastmoney` 页 → 龙虎榜/两融同步 | 落 parquet,无缓存层 |

**选择建议**

- 只要近两日分时和日常增量:选腾讯,零配置零成本。
- 要全市场实时 + 特色量化能力(竞价/连板/资金流):选 XTick,注意 HTTP token 风险。
- 要龙虎榜 / 两融这类日频事件因子:用东方财富,免 token、按日全市场。
- 要长历史分钟回测:配置 TdxQuant(按积分)或升级 TickFlow 档位;
  腾讯可作为低成本日常补充。

**安全提醒**

- 腾讯接口免 token,但为公开接口,无 SLA,可能被限流或变更。
- XTick token 走 HTTP query string,不要在共享网络使用;日志已脱敏,但仍需保管好 `.env` / `secrets.json`。
- 各源的失败都是 fail-closed:源不可用时对应能力显示不可用,**不会静默跨源请求**;
  东财日频事件同步失败时只记录日志,不写入占位行(空表 ≠ 当日无事件)。

---

## 6. 改动清单与升级复核

本 fork 因数据源整合改动/新增的文件:

**新增**
```
backend/app/plugins/tencent/{__init__.py,client.py,provider.py,plugin.yaml}
backend/app/plugins/eastmoney/{client.py,provider.py,plugin.yaml}   # 既有, 本次扩展龙虎榜/两融
backend/app/plugins/xtick/{client.py,plugin.yaml,provider.py,holders.py}
backend/app/plugins/xtick/vendor/xtick/...            # 5 个 API 模块 + 硬化 XTickUtil
backend/app/services/xtick.py
backend/app/services/holder_sync.py
backend/app/services/eastmoney_sync.py
backend/app/services/dimension_source.py            # 概念/行业来源识别 (扩展表 pull.url → 上游名)
backend/app/custom/xtick.py
backend/app/custom/eastmoney.py
backend/tests/test_tencent.py
backend/tests/test_xtick.py
backend/tests/test_xtick_financial.py
backend/tests/test_eastmoney_factors.py
backend/tests/test_dimension_source.py
frontend/src/custom/xtick/{extension.tsx,Page.tsx,columns.ts}
frontend/src/custom/eastmoney/{extension.tsx,Page.tsx,columns.ts}
frontend/src/lib/field-labels.ts                     # 统一字段语义词表 + 格式化
frontend/src/lib/source-labels.ts                    # 数据源展示名 / 上游域名反查
frontend/src/lib/{field-labels,source-labels}.test.ts
frontend/src/components/SourceTag.tsx
docs/third-party-data-source-integration.md           # 本文
```

**修改**
```
backend/app/api/kline.py                 # minute-batch 门禁改用 _minute_allowed
backend/app/backtest/fundamentals.py     # 因子来源表维度 + 日频事件表分支 (freq="daily")
backend/app/backtest/{factor,engine,mining_runtime}.py  # 多表快照加载
backend/app/api/financials.py            # 股东结构查询/同步端点
backend/app/api/ext_data.py              # 扩展表列表返回 source_label (上游展示名)
backend/app/factors/registry.py          # 股东结构因子目录
backend/app/services/sector_rotation.py  # 返回 sources (板块成分来源)
backend/app/services/xtick.py            # status 返回 display_name
backend/app/custom/eastmoney.py          # status: source=id + display_name
backend/tests/test_xtick.py              # 日期改为 cn_today() 动态取值
backend/tests/test_sector_rotation.py    # 板块来源断言
frontend/src/lib/api.ts                  # XTick / 东方财富 类型与请求封装
frontend/src/lib/queryKeys.ts            # XTick / 东方财富 查询键
frontend/src/custom/concept-analysis/Page.tsx   # 成分来源标注 (随扩展表)
frontend/src/custom/industry-analysis/Page.tsx  # 同上
frontend/src/components/SectorRotationCard.tsx  # 成分来源 + 行情基准标注
.env.example                             # XTICK_API_KEY / XTICK_BASE_URL
README.md                                # 文档索引(本文)
data/user_data/secrets.json              # xtick_api_key(运行时数据,勿提交)
```

**上游合并复核清单**

1. `app/api/kline.py` 的门禁改动可能与上游权限模型冲突,升级后确认 `_minute_allowed` 语义未变。
2. `app/main.py` 未改动(后端扩展走 `app/custom/` 自动发现),确认上游仍保留该发现机制。
3. `plugin.yaml` 的 `datasets` 白名单与 `capabilities.py` 的 `CAPABILITY_REGISTRY` 若上游调整,需重新核对声明。
4. `frontend/src/custom/*/extension.tsx` 的 `order` 字段与 `navLayout.ts` 的排序逻辑若上游改动,复核导航顺序。
5. XTick vendored 脚本固定在 commit `05087c5`,上游技能仓库更新后需重新评估是否同步硬化版本。
6. 东财数据中心报表名(`RPT_DAILYBILLBOARD_DETAILSNEW` / `RPTA_WEB_RZRQ_GGMX`)与
   `pageSize` 上限可能调整,变更时同步 `plugins/eastmoney/client.py` 的常量;
   两融单日超过 40 页会被 `DATACENTER_MAX_PAGES` 截断,需复核是否为上游扩表。
