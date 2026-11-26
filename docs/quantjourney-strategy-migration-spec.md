# quantjourney-bt 策略迁移规格（tick-stock-panel 策略库）

> 任务：把 quantjourney-bt `strategies/` 的 51 个示例策略改写为 tick-stock-panel Polars 表达式策略，写入 `data/strategies/custom/`。
> 用户选择范围：全部 51 个迁移。26 权重类 + 20 order 类 → Polars 信号策略文件；5 walk-forward 类 → 不作为策略文件，在 `docs/quantjourney-strategy-migration.md` 中作为优化工作流说明。
> 硬约束：AGENTS.md（最小改动、不覆盖工作区已有修改、不虚构测试/审查结果、以实际验证结果为完成标准）+ 团队工作准则（硬约束优先/统一覆盖优先/最小改动/有效性验证闭环）。策略文件只允许 `import polars as pl`；不改动任何已有项目文件；不新增依赖。

## 0. 目标与格式

tick-stock-panel 策略库只接受 Polars 表达式策略（`EXECUTION_BACKEND="polars_expr"`）。每个迁移后的策略文件必须严格符合以下模板（`data/strategies/custom/<stem>.py`，`stem` = 文件名去 `.py`）：

```python
"""策略简短描述（中文）。源: quantjourney-bt <source path>。"""
import polars as pl

META = {
    "id": "<stem>",             # 英文唯一 ID，与文件名 stem 同名
    "name": "<中文策略名>",
    "description": "<一句话逻辑 + 源说明 + 迁移说明(order 语义变更等)>",
    "tags": ["quantjourney-bt", "迁移", "<category>"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5, "price_max": 500,
        "market_cap_min": <值>,          # 策略口径,缺省可用 50e8
        "exclude_st": True, "exclude_new_days": 60,
    },
    "params": [                    # 仅保留用户可能调节的阈值,可选
        {"id": "...", "label": "...", "type": "float",
         "default": <默认>, "min": <下限>, "max": <上限>, "step": <步长>},
    ],
    "scoring": { <column>: 权重, ... },   # 权重和必须 = 1.0
    "scoring_directions": { <col>: "high"/"low", ... },  # 可选
    "order_by": "score", "descending": True, "limit": <N>,
}

EXECUTION_BACKEND = "polars_expr"
ENTRY_SIGNALS = [...]            # 信号列名, 或无
EXIT_SIGNALS = [...]            # 无
STOP_LOSS = -0.05
MAX_HOLD_DAYS = <N>,

RULES = """...原始意图 + 迁移说明..."""

def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    ...   # 返回布尔 pl.Expr, 仅用 pl.col 比较/&/|| 组合
```

## 1. 允许使用的列（tick-stock-panel enriched 列）

仅可引用 `app/indicators/pipeline.py` 的 `ENRICHED_COLUMNS` 中列。可用基列：
`symbol date open high low close volume amount prev_close change_pct change_amount amplitude`
`ma5 ma10 ma20 ma30 ma60 ema5 ema10 ema20 ema30 ema60`
`macd_dif macd_dea macd_hist boll_upper boll_lower kdj_k kdj_d kdj_j`
`atr_14 vol_ma5 vol_ma10 vol_ratio_5d high_60d low_60d`
`momentum_5d momentum_10d momentum_20d momentum_30d momentum_60d`
`annual_vol_20d rsi_6 rsi_14 rsi_24`
`deviate_3d deviate_10d deviate_30d`（可选，若策略涉及）
`name total_shares float_shares`（JOIN 列，可选）

## 2. 指标映射与内联计算

quantjourney 用特征名（如 `SMA_20_close`、`RSI_14`、`Momentum`、`adj_close`），tick-stock-panel 用列名。映射：
- `SMA_<n>` → 优先 `ma<n>`；n 仅 5/10/20/30/60 时直接用对应列，否则用 `pl.col("close").rolling_mean(<n>)` 内联。
- `RSI_<n>` → 优先 `rsi_<n>`；n 非 6/14/24 时用内联 RSI 表达（可优先用 `rsi_14`）。
- `Momentum_<n>` → 优先 `momentum_<n>`；n 非 5/10/20/30/60 时用 `pl.col("close").rolling_mean` 与等权对比的内联，或 `momentum_20d`。
- `adj_close` → `close`。
- `vol_ratio`/成交量类 → `vol_ratio_5d` / `vol_ma5` / `vol_ma10`。
- `annual_vol` → `annual_vol_20d`。
- 优先使用已有等价列，避免重新推导；仅在原策略所需周期无对应列时才内联 `pl.Expr`（如 `pl.col("close").rolling_mean(200)`）。

## 3. 语义保真要求

- 入场条件、信号方向、参数默认值、加权意图必须忠实于 quantjourney 原策略。
- `scoring` 反映原策略的权重结构：等权（active.div(counts)）、风险平价（低波动加权，可简化为语义权重）、动量加权、多因子加权、情绪加权、对冲/比率类（ratio 类可映射为信号评分）等。权重和必须 = 1.0。
- `scoring_directions`：按原策略的排序方向（高/低，如低波动优先、高动量优先、ADX 越高越好）。
- order 类策略（O01–O20）：`filter()` 表达其入场/出场信号逻辑；`RULES` 必须显式记录：原策略的 order 执行语义（MARKET/LIMIT/STOP/STOP_LIMIT/STOP_TRAIL/STOP_TRAIL_LIMIT/BRACKET/OCO、slippage/commission/blotter）由已整合的订单执行引擎（`app/backtest/order_engine.py`）承载，Polars 策略只表达信号。这是**迁移语义变更**，必须在 description/RULES 中说明。
- walk-forward 5 个（W01–W05）：**不要**写成策略文件；在迁移文档中作为优化工作流（walk-forward/优化方案）说明。
- 不要虚构不存在的因子列或算子；不确定的周期用可用列或注释说明。

## 4. 验证（每个子代理完成后）

对每个写出的文件执行：
```
python -c "import ast; ast.parse(open('data/strategies/custom/<stem>.py',encoding='utf-8').read()); print(<stem>,'syntax ok')"
```
并要求：文件只 import polars；`scoring` 权重和 = 1.0（人工校验）；`filter()` 返回布尔 `pl.Expr`。

## 5. 每个子代理的输出

写完指定批次的文件后，返回：
- 写出的文件相对路径清单（绝对路径）
- 每个策略的「源 → id → 逻辑摘要（信号/加权/参数）」映射一行
- 验证结果（syntax ok / scoring 权重和）

工作目录：`/Users/liuzuodong/Documents/workspace/tick-stock-panel`
