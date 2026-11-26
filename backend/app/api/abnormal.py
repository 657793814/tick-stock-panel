"""异动监控 API — 竞价/盘中/偏移三类异动。

- /intraday: 盘中量价信号聚合 (enriched 当日信号列, 零新增采集)
- /overview: 偏移异动边缘总览 (交易所异动规则口径的接近度)
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.api.market_guard import require_cn_market
from app.services.abnormal_moves import build_intraday, build_overview

router = APIRouter(prefix="/api/abnormal", tags=["abnormal"])


@router.get("/intraday")
def abnormal_intraday(
    request: Request,
    limit: int = Query(500, ge=1, le=2000),
    market: str = Query("cn", description="逻辑市场: 仅支持 cn（A 股专属口径）"),
):
    """盘中异动: 涨停/炸板/跌停翘板/跌停/新高/新低/放量 信号命中行。

    港美股无涨跌停/连板, 该端点非 cn 直接 409（不静默返回 A 股内容）。
    """
    require_cn_market(market, feature="盘中异动")
    repo = request.app.state.repo
    return build_intraday(repo, limit=limit)


@router.get("/overview")
def abnormal_overview(
    request: Request,
    min_closeness: float = Query(0.5, ge=0.0, le=1.0),
    limit: int = Query(200, ge=1, le=1000),
    market: str = Query("cn", description="逻辑市场: 仅支持 cn（A 股专属口径）"),
):
    """异动边缘总览: 规则表 + 各窗口实时偏离 + 接近度排序。

    min_closeness: 0.5=观察 / 0.7=边缘 / 1.0=已触发。
    """
    require_cn_market(market, feature="异动边缘总览")
    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    return build_overview(repo, quote_service, min_closeness=min_closeness, limit=limit)
