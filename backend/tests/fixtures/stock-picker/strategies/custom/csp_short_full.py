"""stock-picker 多因子短线策略（完整版：含量量价 + C 类另类数据）

与 csp_short_multifactor（量价近似版）并存，供 A/B 对比。

C 类因子已通过真实数据落地（非伪造）：
  - hot_theme → `ext_ext_mainline_theme_theme_score`：mainline_history 题材龙头分，
    T-1 滞后防前视（scripts/ingest_theme_factors.py）。覆盖 1349 只龙头股/241 日。
  - institutional → `ext_ext_dragon_org_org_net_value`：fuyao 历史龙虎榜机构净买，
    一年内逐交易日回补（scripts/backfill_dragon_tiger_history.py +
    ingest_dragon_tiger_factors.py）。
  - hot_money → `ext_ext_dragon_org_hot_money_net_value`：同榜游资净买。

权重纪律：C 类覆盖率稀疏（题材仅龙头股、龙虎榜仅上榜股），未照搬 stock-picker 的
hot_theme 0.55（会导致大面积权重让渡失真），取适度值；缺失一律走 _apply_scoring 的
权重让渡语义。正式权重需 OOS 校准（`scripts/calibrate_scoring_weights.py`，当前数据
不足，见 docs/stock-picker-weight-calibration.md）。
"""
import polars as pl

# 题材分（T-1）与龙虎榜因子列（ext_data timeseries 自动注册名）
THEME = "ext_ext_mainline_theme_theme_score"
ORG_NET = "ext_ext_dragon_org_org_net_value"
HOT_MONEY = "ext_ext_dragon_org_hot_money_net_value"

META = {
    "id": "csp_short_full",
    "name": "stock-picker多因子短线(完整版)",
    "description": (
        "stock-picker 方案G短线完整版：量价 + 题材龙头分(T-1) + 龙虎榜机构/游资净买。"
        "C类覆盖稀疏，缺失走权重让渡；权重为语义初值未经OOS校准。"
    ),
    "tags": ["stock-picker", "短线", "多因子", "另类数据"],
    "asset_types": ["stock"],
    "timeframes": ["1d"],
    "basic_filter": {
        "price_min": 5,
        "price_max": 200,
        "amount_min": 3e7,
        "exclude_st": True,
        "exclude_new_days": 60,
    },
    "params": [
        {"id": "max_chase_pct", "label": "追高排除线(%)", "type": "float",
         "default": 7.0, "min": 3.0, "max": 15.0, "step": 0.5},
        {"id": "min_amount", "label": "最小成交额(元)", "type": "float",
         "default": 3e7, "min": 1e7, "max": 1e9, "step": 1e7},
    ],
    # 权重和 = 1.0。C 类合计 0.40（题材0.25+机构0.10+游资0.05），量价 0.60。
    "scoring": {
        THEME: 0.25,
        ORG_NET: 0.10,
        HOT_MONEY: 0.05,
        "uf_sp_liq_dev": 0.20,
        "uf_sp_vol_dev": 0.12,
        "annual_vol_20d": 0.10,
        "momentum_20d": 0.13,
        "cf_sp_technical": 0.05,
    },
    "scoring_directions": {
        "momentum_20d": "low",
        "annual_vol_20d": "low",
        "uf_sp_liq_dev": "low",
        "uf_sp_vol_dev": "low",
        # 题材分/机构净买/游资净买：高分为好（默认 high，显式声明便于审阅）
        THEME: "high",
        ORG_NET: "high",
        HOT_MONEY: "high",
    },
    "order_by": "score",
    "descending": True,
    "limit": 10,
}

EXECUTION_BACKEND = "polars_expr"
ENTRY_SIGNALS = []
EXIT_SIGNALS = []
STOP_LOSS = -0.02
MAX_HOLD_DAYS = 3

RULES = """
1. 基础过滤：排除ST/退市、上市不足60日、成交额<3000万、股价5~200元。
2. 追高排除：当日涨幅超过 max_chase_pct(默认7%) 的标的剔除。
3. 题材强度(T-1)：前一交易日题材龙头分越高越优先（防前视）。
4. 资金流：龙虎榜机构净买/游资净买越高越好（仅上榜股有值，其余让渡）。
5. 缩量/波动收敛/反转/技术形态：量价底座，同量价近似版。
6. 覆盖率：题材仅龙头股、龙虎榜仅上榜股，多数标的走权重让渡；权重未经OOS校准。
"""


def filter(df: pl.DataFrame, params: dict) -> pl.Expr:
    max_chase = float(params.get("max_chase_pct", 7.0)) / 100.0
    min_amount = float(params.get("min_amount", 3e7))
    return (
        (pl.col("change_pct") <= max_chase)
        & (pl.col("amount") >= min_amount)
        & (pl.col("close") > pl.col("ma20") * 0.90)
    )
