"""stock-picker 多因子长线策略

从 stock-picker 长线权重迁移：基本面0.40 + 中期动量0.25 + 估值0.20 + 机构动向0.15。
fundamental/valuation 用当前 PIT 财务面板表达；institutional（龙虎榜机构净买+
主力资金）需另类数据，未到位前权重让渡为基本面/动量主导。
见 docs/stock-picker-alt-data-contract.md。
"""
import polars as pl

META = {
    "id": "csp_long_multifactor",
    "name": "stock-picker多因子长线",
    "description": (
        "stock-picker长线逻辑迁移：ROE/成长/估值PIT因子+120日中期动量，"
        "机构动向数据到位前为财报+动量近似"
    ),
    "tags": ["stock-picker", "长线", "多因子", "基本面"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5,
        "price_max": 500,
        "market_cap_min": 50e8,       # 长线偏大盘
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {
            "id": "min_roe",
            "label": "最低ROE(%)",
            "type": "float",
            "default": 5.0,
            "min": 0.0,
            "max": 30.0,
            "step": 1.0,
        },
    ],
    # 权重和=1.0；institutional 用龙虎榜机构净买(ext_dragon_org，真实落列)
    "scoring": {
        "roe_latest": 0.24,                          # fundamental 拆分（原0.40）
        "net_income_yoy_latest": 0.11,               # fundamental 成长
        "revenue_yoy_latest": 0.07,                  # fundamental 成长
        "momentum_120d": 0.25,                       # 中期动量（原0.25）
        "pb_latest": 0.18,                           # 估值（原0.20，无PE/PS降维）
        "ext_ext_dragon_org_org_net_value": 0.15,    # institutional（原0.15，龙虎榜机构净买）
    },
    "scoring_directions": {
        "pb_latest": "low",                  # 低估值优先
    },
    "order_by": "score",
    "descending": True,
    "limit": 5,                              # stock-picker 长线 max_candidates 5
}

EXECUTION_BACKEND = "polars_expr"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.08
MAX_HOLD_DAYS = 120                          # stock-picker hold 3~6 个月

RULES = """
1. 基础过滤：排除ST/退市、上市不足60日、市值50亿以下、股价5~500元。
2. 质量门槛：ROE 不低于 min_roe(默认5%)，缺财务数据的标的剔除。
3. 基本面：ROE + 净利润增速 + 营收增速加权（PIT，按公告日回填，无未来函数）。
4. 中期动量：120日涨幅加权。
5. 估值：PB 越低评分越高（当前无PE/PS/股息率，降维为PB）。
6. 机构动向：龙虎榜机构净买+主力资金，数据到位后补充，当前让渡。
7. 持有周期3~6个月，止损-8%。
"""


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    """stock-picker 长线过滤：要求有财务数据且 ROE 达标。"""
    min_roe = float(params.get("min_roe", 5.0))
    return (
        pl.col("roe_latest").is_not_null()
        & (pl.col("roe_latest") >= min_roe)
    )
