# KHunter 能力整合规划

> 分析对象：`/Users/liuzuodong/Documents/workspace/KHunter`（只读分析，未修改）
> 目标项目：当前仓库 `tick-stock-panel`
> 结论依据：两个并行深度探索 + 主上下文逐行代码验证。引用代码区分「文档描述」与「代码实际」。
> 状态：**分析完成，进入实施**。全程不碰 git、不改核心模块、不动用户既有未提交改动。

---

## 1. 为什么要整合 KHunter（与 stock-picker 的差异）

上一轮整合的 stock-picker 提供了「多因子配方 + 影子模式」范式，但其资金流数据不可得。KHunter 补上了这块拼图，且带来三项 stock-picker 没有的**纯算法资产**：

| 能力 | stock-picker | KHunter | 当前项目(tick-stock-panel) |
|---|---|---|---|
| 主力资金流数据 | 无 | ✅ Tushare/东财双源 | ❌ 缺（本轮补） |
| 出货信号否决 | 无 | ✅ 大单流出+小单流入联合判定 | ❌ 缺 |
| 连续净流入天数 | 无 | ✅ 纯算法 | ❌ 缺 |
| ADX 择时状态机 | 无 | ✅ 纯函数零依赖（带迟滞/两日同向/反转日） | ❌ 只有 vol_regime |
| 因子 DSL / 注册表 | 有 | ❌ 无 | ✅ 成熟 |
| StrategyEngine / 回测 / walkforward | 弱 | 有但 244KB 单体耦合 SQLite | ✅ 成熟 |

**核心判断**：KHunter 的评分/策略/回测**主体框架与当前项目高度重复**（且文档与代码大面积失同步，如权重 docstring 35/20/20/25 实为 55/10/10/25），**不整体移植**。真正值得整合的是「资金流数据 + 4 个可独立抽离的纯算法/工程纪律」。

---

## 2. 资金流数据源：真实可用性与方案选择

### 2.1 KHunter 的路线（参照对象）
- 主源：Tushare Pro `moneyflow_ths`（同花顺口径），起点 `2024-12-24`，一次调用返回全市场约 5200 只。
- 字段：`net_amount`(主力净额)、`buy_lg/sm_amount`(大/小单净额)、对应 `_rate`(占比%)、`net_d5_amount`(5日主力净额)。单位万元，正=净流入。
- 鉴权：需 Tushare token（`config/tushare_config.json`，被 gitignore）。

### 2.2 当前环境实测结论
| 路径 | 实测结果 |
|---|---|
| Tushare token | ❌ 当前项目未装 tushare、无 token、无环境变量、KHunter 的 token 文件被 gitignore 不存在 → **不可走** |
| 东财 `push2his/.../fflow/daykline` | ✅ **免 token 可达**，首次请求成功，返回 120 交易日历史（2026-05-28~11-20） |
| 东财字段验证 | ✅ 已用加总恒等式验证：`主力(f52)=大单(f55)+超大单(f56)`、四类净额和为 0、占比字段齐全 |
| 东财限流 | ⚠️ 短时间 6+ 次请求触发 IP 临时封禁（HTTP 000）→ **必须限流友好 + 断点续传** |

### 2.3 最终方案（比 KHunter 路线更适合当前环境）
用东财免 token 的 `fflow/daykline` **逐股拉历史资金流**，落进当前项目的 `ext_data` timeseries 通道（与已落地的龙虎榜/题材因子同一范式，PIT 对齐）。字段映射：

```
f51 日期 → date
f52 主力净额(元) → main_net         (= f55 大单 + f56 超大单)
f53 小单净额(元) → small_net
f55 大单净额(元) → large_net
f56 超大单净额(元) → super_large_net
f57/f58/f60/f61 各类净占比(%) → main_rate / small_rate / large_rate / super_large_rate
```

单位换算：东财返回**元**，KHunter 口径为**万元**，落列保留元（当前项目因子用原始值 + min-max，不做 0-100 分档，见 stock-picker-factor-mapping 的口径警示）。

**限流纪律**：单股请求间隔 ≥1s、随机 UA、失败指数退避、断点续传（已有日期跳过）、按交易日/按股票池分批。5605 只全市场不现实，优先拉「策略实际会选的股票池」（题材龙头 + 龙虎榜常客，约 1350 只）与近期窗口。

---

## 3. 整合范围（6 项，分「数据/算法/接入」三层）

### 3.1 数据层
- **D1 资金流因子接入**：写 `scripts/ingest_moneyflow_factors.py`（东财逐股拉取 → ext_data timeseries → 因子列）。字段见 §2.3。

### 3.2 算法层（纯函数，零核心改动，放 `backend/app/` 或 `scripts/`）
- **A1 ADX 三态状态机**：移植 `utils/stock_adx_state.py` 的 `step/regime_of/entry_allowed`，纯函数带迟滞(±1)/两日同向/反转日不吃缓冲/高位守门(≥40)。当前项目无 ADX，是真实增量。**不做** KHunter 那套 700 行注释里已废弃的「降温机制」。
- **A2 出货否决 + 主力净额否决**：移植 `_is_distribution_veto` / `_is_main_net_flow_veto` 的三级判定（占比字段优先→成交额回退→纯方向兜底）。纯函数。
- **A3 5 日资金聚合**：移植 `compute_metrics`，含 `complete/missing` 完整性标记（窗口不齐不可判定，fail-closed）。
- **A4 连续净流入天数**：移植 `money_flow_dao.select_continuous_inflow_stocks` 的逐日回溯算法。

### 3.3 接入层
- **I1 策略/评分接入**：资金流因子进 `csp_short_full.py` 配方（C 类最后一块）；ADX/出货否决作为 `overlay_filter` 或独立 regime 信号（不破坏现有策略，另建实验策略或作为可选护栏）。
- **I2 文档与测试**：更新另类数据契约文档；为 4 个算法 + 数据脚本写单测；全量回归。

---

## 4. 明确不做

- ❌ 不移植 KHunter 的 `BacktestEngine`(244KB 单体)、DAO/缓存/状态机、web_server(196KB)——与当前项目重复且强耦合 SQLite。
- ❌ 不移植 5 个 scorer 的阈值打分主体（依赖 Tushare token + SQLite，阈值口径强绑定）。
- ❌ 不移植形态策略集合（仙人指路/多方炮等）——当前 StrategyEngine + 因子 DSL 更强。
- ❌ 不照搬 KHunter 的评分权重（文档与代码不一致，且未 OOS 校准）。
- ❌ 不在封禁期硬拉东财全市场（尊重风控，限流友好）。

---

## 5. 风险与诚实边界

1. **资金流历史深度**：东财该接口 120 交易日（约半年），比 KHunter Tushare 的 2024-12-24 起点短。与本地 K 线区间（2025-10-20 起）部分重叠，够做近期验证，但不足以支撑长周期 OOS（延续上一轮「13个月数据不足」的结论）。
2. **口径差异**：KHunter 明确记载同花顺 `net_d5_amount` 与东财「超大单+大单」数值 99.8% 不同、1/3 符号相反。本方案用东财口径，**不混用**两套，权重需重新校准（当前仍是语义初值，不作上线依据）。
3. **限流不可控**：东财可能随时封禁，拉取脚本必须幂等可续；若某日拉不到，按 fail-closed 留空，绝不伪造。
4. **ADX 路径依赖**：状态机预热要求 ADX 历史起点 ≤ 回测起点，否则同日同值不同档。接入时必须保证预热，否则不可复现。

---

## 6. 实施进度

| 阶段 | 状态 | 交付 | 验证 |
|---|---|---|---|
| 0 分析 | ✅ 完成 | 本文档 | 两份探索 + 逐行验证 |
| 1 资金流数据脚本 | ✅ 脚本完成 / ⏸ 数据待拉 | `scripts/ingest_moneyflow_factors.py`（插件消费壳） | 纯函数单测通过（secid/解析/恒等式）；**东财 IP 封禁中，未落真实数据** |
| 1b 资金流插件化 | ✅ 完成 | `backend/app/plugins/eastmoney/`（plugin.yaml + provider + client） | loader 真实注册(available/datasets=[moneyflow])、provider_has_dataset 判定、get_moneyflow 归一化/fail-closed，4 个契约测试通过 |
| 2 算法 A1-A4 | ✅ 完成 | `backend/app/khunter_algorithms.py` | 11 纯函数单测通过 |
| 2b ADX 计算 service | ✅ 完成 | `backend/app/services/adx_regime.py` + `scripts/ingest_adx_factors.py` | Wilder RMA/趋势 ADX 验证通过 |
| 3 接入层 I1-I2 | ✅ 完成 | `data/strategies/custom/khunter_adx_regime.py` + 本文档 + 算法测试 | ADX 因子 488万行落盘/注册/PIT对齐；策略加载/filter/端到端回测通过；全量 **137 passed** |

### 6.1 本轮实际落地成果

1. **ADX regime 能力（完整闭环）**：从无到有，落地 Wilder ADX(14) 计算 + KHunter 三态状态机 + T-1 因子（283 交易日/全市场）+ `khunter_adx_regime` 策略。端到端回测：ADX 策略交易数减半（208 vs 430）、回撤 -0.32 vs -0.65，**择时闸门真实降低了风险暴露**（该区间整体下跌，不追求正收益）。
2. **4 个纯算法 module**：ADX 状态机、出货否决、主力净额否决、5日聚合、连续净流入——全部纯函数、零核心耦合、可单测。
3. **资金流数据脚本就绪**：字段恒等式已验证（f52=f55+f56），限流友好+断点续传，解封即可拉取。

### 6.2 唯一未完成：资金流真实数据落地

- **原因**：东财接口在验证阶段被 IP 临时封禁（HTTP 000，多次请求触发风控）；Tushare 路线无 token 不可用。
- **非阻塞**：取数已封装为正规插件（见 §6.3），解封后执行 `cd backend && uv run --frozen python ../scripts/ingest_moneyflow_factors.py --symbols-file <池>` 即可补齐 `capital_flow` 因子，届时可直接加入 `csp_short_full` 配方。
- **绝不伪造**：封禁期未写入任何资金流数据。

### 6.3 eastmoney 插件（可插拔）

资金流取数已从脚本下沉为正规内置插件，与 tdxquant/fuyao 同范式：

```
backend/app/plugins/eastmoney/
├── plugin.yaml    # name=eastmoney, datasets=[moneyflow], check=availability, 免 token
├── provider.py    # EastmoneyProvider.get_moneyflow(symbols, start, end) 归一化契约
└── client.py      # to_secid / parse_klines / fetch_fflow_daykline(curl+UA+退避节流)
```

- **可插拔路径核实**：`packages.DATASETS` 固定为行情类 7 种（daily/adj_factor/minute/
  full_minute/realtime/depth5/financial）。进一步对照 fuyao provider 的既有约定
  （`dragon_tiger` docstring：**非路由数据集不进 plugin.yaml datasets**），moneyflow
  按同一约定处理：清单 `datasets: []`，provider 以方法形态暴露，消费方直取。
- **路由边界**：moneyflow 不进能力路由（CAPABILITY_REGISTRY 无此能力），消费方经
  `custom.loader.get_provider("eastmoney")` 直取；provider 不声明 daily/realtime 等
  行情数据集 → `provider_has_dataset` 对它们为 False，主流程自动回退 tickflow。
- **契约**：单只失败记日志跳过不中断整批；全部失败抛 `ProviderRequestError`
  （fail-closed）；逐股节流 1.2s；`close()`/`availability()` 齐备（启动自检不做网络请求）。
- **分层正确性**：资金流是另类数据因子，消费方是 ext_data 因子通道（与龙虎榜/题材/
  ADX 一致），不是 kline 管线——插件承载"怎么拉"，脚本承载"怎么落因子"。

## 7. 数据源统一插件化规矩（治理约定，必守）

> **规矩：任何外部数据源必须以插件形态接入；service/script 一律禁止直连外部 HTTP。
> 脚本只许消费 provider（经 `custom.loader.get_provider`）或本地派生数据。**

### 7.1 分类路由（先判定数据类型，再选接入形态）

| 数据类型 | 判定特征 | 接入形态 | 现例 |
|---|---|---|---|
| 行情类 | K线/快照/财务/复权/分钟，喂 kline 主链路 | 标准 7 数据集插件 + 能力路由（CAPABILITY_REGISTRY） | fuyao、tdxquant；tickflow 为核心基线（非插件，by design） |
| 另类数据 | 资金流/龙虎榜/题材/榜单，喂 ext_data 因子通道 | **非路由 provider 方法**（不进 plugin.yaml datasets，参照 fuyao.dragon_tiger 约定）+ ingest 脚本落 timeseries（T-1 防前视） | fuyao.dragon_tiger、eastmoney.get_moneyflow |
| 派生指标 | 从本地 K 线/已有数据计算（ADX 等） | **不是数据源**，不做成插件：service 计算 + ingest 脚本落列 | services/adx_regime.py |
| 通知/依赖外联 | webhook（飞书/企微）、pypi 镜像 | 不属于数据源，排除在外 | webhook_adapter.py |

### 7.2 插件范式 checklist（新建数据源必查）

1. 目录 `backend/app/plugins/<name>/`：`plugin.yaml` + `provider.py` + `client.py`；
2. plugin.yaml：`name/display_name/runtime/entry/check` + `datasets`（**只列可路由数据集**，
   非路由数据集留空，方法形态暴露）；需要 Key 时声明 `api_key_env` + `probe_api_key`；
3. provider：`name`/`builtin=True`/`config.datasets`（与清单一致）/`close()`/模块级
   `availability()`（启动自检**不做网络请求**）；
4. client：curl 子进程优先（urllib 会被部分源断连）、必带 UA/Referer、失败指数退避+
   随机抖动、逐请求节流；**区分「无数据空」与「请求失败 None」**，绝不把失败转成空数据；
5. fail-closed：单标的失败记日志跳过不中断整批；全部失败抛 `ProviderRequestError`；
6. 消费方式：路由能力走 `provider_has_dataset`/preferences；非路由数据一律
   `loader.get_provider(name).<method>()`，脚本不得 import 插件 client 模块。

### 7.3 全量数据源清单与现状（2026-11-23 盘点）

| 数据源 | 形态 | 状态 |
|---|---|---|
| tickflow | 核心默认 provider | ✅ 基线（不插件化，兜底回退目标） |
| fuyao（同花顺 API） | 内置插件 | ✅ realtime/daily/adj_factor/financial 可路由 + dragon_tiger/short_term_benchmark 非路由 |
| tdxquant | 内置插件 | ✅ |
| eastmoney（东财公开接口） | 内置插件 | ✅ get_moneyflow 非路由（资金流数据待东财解封拉取） |
| 用户 YAML HTTP 源（`data/data_sources/*.yaml`） | 机制现成 | ✅ 可插拔（当前无实例） |
| 外装插件包（`data/plugins/`） | 机制现成 | ✅ 可插拔（当前无实例） |
| ext_data pull 配方（ext_presets + config.json） | 配方驱动拉取通道 | ✅ 属 ext_data 体系既有形态，保持不变 |
| mainline_history（题材）/ ADX | 本地派生数据 | ✅ 非数据源，service+脚本形态 |
| **腾讯 ifzq/qt.gtimg + 东财 kline（index_sync_market.py）** | **service 直连，绕过插件** | ⚠️ 待迁移——该文件是用户未提交的多市场扩展进行中工作，**基线化后再迁**为指数行情插件，现在不动 |

### 7.4 落实记录

- `scripts/backfill_dragon_tiger_history.py` 已从直用 `FuyaoClient` 改为
  `loader.get_provider("fuyao").dragon_tiger(...)`（provider 是唯一取数入口，
  生命周期归 loader 所有，脚本不 close）；
- eastmoney 插件清单从 `datasets: [moneyflow]` 修正为 `datasets: []`（对齐非路由约定），
  契约测试同步更新（loader 注册/provider_has_dataset/get_moneyflow 归一化+fail-closed）；
- 后续新数据源（如资金流替代源、宏观数据等）按 §7.2 checklist 落插件，不接受例外。
