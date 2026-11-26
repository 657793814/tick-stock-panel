"""市场参数校验 + A 股专属能力的 fail-closed 门禁。

为什么非 cn 要明确拒绝而不是回落 A 股: 异动/涨幅轮动/板块轮动都建立在 A 股
专属口径上（涨跌停、连板、同花顺概念与行业分类、龙虎榜）。这些端点此前不接受
market 参数, 港股市场下照样返回 A 股聚合结果 —— 前端会展示「茅台涨停」这类
错市场内容, 比明确报错更容易误导。与 /api/watchlist 的 _validate_market 同口径
（None / 空 / "all" 视为未指定）。
"""
from __future__ import annotations

from fastapi import HTTPException

from app.markets import ALL_MARKETS, MARKET_CN


def normalize_market(market: object, default: str = MARKET_CN) -> str:
    """把 Query 默认值对象 / None / 空串归一化为市场代码。

    测试会直接调用端点函数（不经过 FastAPI 解析），此时 market 是 `Query(...)`
    对象而不是字符串，需要取它的 .default。
    """
    if market is not None and not isinstance(market, str):
        market = getattr(market, "default", None)
    raw = (market or "").strip().lower()
    return raw or default


def require_market(market: object, *, feature: str = "") -> str:
    """校验市场代码合法；非法 400。返回归一化后的市场代码。"""
    m = normalize_market(market)
    if m == "all":
        return m
    if m not in ALL_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"非法市场 '{m}'，支持: {', '.join(ALL_MARKETS)}",
        )
    return m


def require_cn_market(market: object, *, feature: str) -> str:
    """A 股专属能力门禁: 非 cn 直接 409，不静默返回 A 股内容。

    feature 用于组织错误文案（如 "盘中异动"），前端原样展示 detail。
    """
    m = normalize_market(market)
    if m == "all":
        raise HTTPException(
            status_code=409,
            detail=f"{feature}为 A 股专属口径，不支持跨市场（all），请选择具体市场",
        )
    if m not in ALL_MARKETS:
        raise HTTPException(
            status_code=400,
            detail=f"非法市场 '{m}'，支持: {', '.join(ALL_MARKETS)}",
        )
    if m != MARKET_CN:
        label = "港股" if m == "hk" else "美股"
        raise HTTPException(
            status_code=409,
            detail=f"{feature}暂不支持{label}：该功能依赖 A 股专属数据口径（涨跌停/连板/概念行业分类）",
        )
    return m
