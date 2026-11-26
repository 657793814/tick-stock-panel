"""stock-picker 多因子短线策略（量价近似版）

从 stock-picker 方案 G 迁移的短线选股逻辑。当前为「量价近似」：
C 类另类数据因子（hot_theme/capital_flow/dragon_tiger，合计原权重 0.57）
需真实数据源，未到位前其权重按比例让渡给下列可用因子。
见 docs/stock-picker-alt-data-contract.md。
"""
import polars as pl

META = {
    "id": "csp_short_multifactor",
    "name": "stock-picker多因子短线(量价近似)",
    "description": (
        "stock-picker 方案G短线逻辑迁移：缩量偏离+波动收敛+低波动+动量反转，"
        "叠加stock-picker过滤口径；题材/资金流数据到位前为量价近似"
    ),
    "tags": ["stock-picker", "短线", "多因子", "量价"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5,
        "price_max": 200,
        "amount_min": 3e7,        # stock-picker min_volume 3000万
        "exclude_st": True,
        "exclude_new_days": 60,   # stock-picker min_listing_days 60
    },
    "params": [
        {
            "id": "max_chase_pct",
            "label": "追高排除线(%)",
            "type": "float",
            "default": 7.0,
            "min": 3.0,
            "max": 15.0,
            "step": 0.5,
        },
        {
            "id": "min_amount",
            "label": "最小成交额(元)",
            "type": "float",
            "default": 3e7,
            "min": 1e7,
            "max": 1e9,
            "step": 1e7,
        },
    ],
    # 权重和=1.0；C 类因子数据到位后按 alt-data-contract 增补占位权重
    "scoring": {
        "uf_sp_liq_dev": 0.30,       # 缩量偏离（原 liq_dev 0.14，让渡后放大）
        "uf_sp_vol_dev": 0.18,       # 波动收敛（原 vol_dev 0.07）
        "annual_vol_20d": 0.15,      # 低波动溢价（原 volatility 0.06）
        "momentum_20d": 0.22,        # 动量反转合成（原 reversal 0.10 + momentum 0.02）
        "cf_sp_technical": 0.15,     # 技术形态（原 technical 0.02 + volume_price 0.02）
    },
    # 方向以 OOS 复检为准；此处按 stock-picker 语义设定初值
    "scoring_directions": {
        "momentum_20d": "low",       # reversal 主导：低动量/超跌优先
        "annual_vol_20d": "low",     # 低波动溢价
    },
    "order_by": "score",
    "descending": True,
    "limit": 10,                      # stock-picker max_candidates 3，此处放宽供选择
}

EXECUTION_BACKEND = "polars_expr"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.02                     # stock-picker stop_loss -2%
MAX_HOLD_DAYS = 3                     # stock-picker time_stop_days 3

RULES = """
1. 基础过滤：排除ST/退市、上市不足60日、成交额<3000万、股价5~200元。
2. 追高排除：当日涨幅超过 max_chase_pct(默认7%) 的标的剔除。
3. 缩量休整：成交额相对60日中位数偏离越小（缩量）评分越高。
4. 波动收敛：20日波动率相对60日中位数越低评分越高。
5. 反转优先：20日动量越低（超跌）评分越高。
6. 技术形态：均线偏离+MACD+RSI+量能加权合成评分。
7. 题材/资金流：数据到位后通过叠加条件(overlay_filter)补充，当前为量价近似。
"""


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    """stock-picker 短线过滤：排除追高、要求缩量休整的基本形态。"""
    max_chase = float(params.get("max_chase_pct", 7.0)) / 100.0
    min_amount = float(params.get("min_amount", 3e7))
    return (
        (pl.col("change_pct") <= max_chase)
        & (pl.col("amount") >= min_amount)
        & (pl.col("close") > pl.col("ma20") * 0.90)  # 不接深跌破位
    )
