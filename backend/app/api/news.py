"""新闻资讯 API — 资讯/快讯/公告/专题/异动解读/个股分时新闻。

路由前缀: /api/news。HTTP 层只做参数校验和转发, 归一化与 Redis 缓存在 services.news。
上游未配置或失败时返回 200 + state, 与 dragon-tiger 等 fuyao 专有接口保持一致,
由前端显示明确的降级提示。
"""
from __future__ import annotations

from datetime import date as date_cls

from fastapi import APIRouter, HTTPException, Query

from app.services import news as news_service

router = APIRouter(prefix="/api/news", tags=["news"])

_CLIENT_TYPES = {"WEB", "WEB_ENCRYPTED", "PC", "PC_LIST"}
_SEARCH_PARAMS = {"event_name", "event_summary"}


def _validate_date(value: str, field: str) -> date_cls:
    try:
        return date_cls.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(400, f"{field} 格式应为 YYYY-MM-DD, 收到: {value}") from exc


@router.get("/status")
def news_status() -> dict:
    """新闻能力状态与缓存口径。"""
    return news_service.availability()


@router.get("/articles/detail")
def article_detail(
    client_type: str = Query(default="WEB", description="WEB / WEB_ENCRYPTED / PC / PC_LIST"),
    encoded_seq: str = Query(..., min_length=1, max_length=256, description="密文资讯标识"),
) -> dict:
    """资讯详情: 正文为 HTML, 展示时按内容安全策略处理。"""
    kind = client_type.strip().upper()
    if kind not in _CLIENT_TYPES:
        raise HTTPException(400, f"client_type 仅支持 {', '.join(sorted(_CLIENT_TYPES))}")
    return news_service.get_news("article_detail", {"client_type": kind, "encoded_seq": encoded_seq})


@router.get("/events")
def event_search(
    query: str | None = Query(default=None, max_length=200, description="标题或正文关键词"),
    search_param: str = Query(default="event_name", description="event_name / event_summary"),
    standard_start_time: str | None = Query(default=None, description="标准时间下界 YYYY-MM-DD"),
    standard_end_time: str | None = Query(default=None, description="标准时间上界 YYYY-MM-DD"),
    event_grading_score: str | None = Query(default=None, description="逗号分隔评级, 如 A,B"),
    related_industry_code: str | None = Query(default=None, description="逗号分隔行业代码"),
    related_concept_code: str | None = Query(default=None, description="逗号分隔概念代码"),
    size: int = Query(default=20, ge=1, le=100),
) -> dict:
    """资讯事件库检索。上游声明为客户端内接口, 不可用时返回 upstream_error。"""
    param = search_param.strip().lower()
    if param not in _SEARCH_PARAMS:
        raise HTTPException(400, "search_param 仅支持 event_name / event_summary")
    for field, value in (("standard_start_time", standard_start_time), ("standard_end_time", standard_end_time)):
        if value:
            _validate_date(value, field)
    params = {
        "query": query, "search_param": param,
        "standard_start_time": standard_start_time, "standard_end_time": standard_end_time,
        "event_grading_score": event_grading_score,
        "related_industry_code": related_industry_code, "related_concept_code": related_concept_code,
        "size": size,
    }
    return news_service.get_news("events_search", {k: v for k, v in params.items() if v is not None})


@router.get("/flash/list")
def flash_list(
    tag_id: int = Query(..., ge=1, description="快讯栏目 ID"),
    seq: str = Query(default="0", description="首屏传字符串 0, 后续原样回传"),
) -> dict:
    """快讯列表。"""
    return news_service.get_news("flash_list", {"tag_id": tag_id, "seq": seq})


@router.get("/flash/refresh")
def flash_refresh(
    tag_id: int = Query(..., ge=1, description="快讯栏目 ID"),
    seq: str = Query(..., min_length=1, description="客户端已知的最新 seq"),
    create_time_ms: int = Query(..., ge=0, description="已知快讯创建时间(毫秒, 能被 1000 整除)"),
) -> dict:
    """快讯刷新。该接口不进缓存, 否则新快讯会被旧结果挡住。"""
    if create_time_ms % 1000 != 0:
        raise HTTPException(400, "create_time_ms 必须能被 1000 整除")
    return news_service.get_news("flash_refresh", {
        "tag_id": tag_id, "seq": seq, "create_time_ms": create_time_ms,
    })


@router.get("/notices/detail")
def notice_detail(
    seq: str = Query(..., min_length=1, max_length=256, description="公告序号"),
) -> dict:
    """公告详情与结构化解读。"""
    return news_service.get_news("notice_detail", {"seq": seq})


@router.get("/topics/hot")
def topic_hot_list(
    tab_id: int = Query(..., ge=1, description="热门专题栏目 ID"),
    cursor: str | None = Query(default=None, description="分页游标, 后续原样回传"),
    limit: int = Query(default=10, ge=1, le=10, description="每页条数 1-10"),
    important_only: bool = Query(default=False, description="只看重要内容"),
) -> dict:
    """热门深度专题。"""
    params: dict = {"tab_id": tab_id, "limit": limit, "important_only": important_only}
    if cursor:
        params["cursor"] = cursor
    return news_service.get_news("topic_hot_list", params)


@router.get("/anomaly/analysis")
def anomaly_analysis(
    begin_date: str = Query(..., description="开始日期 YYYY-MM-DD"),
    end_date: str = Query(..., description="结束日期 YYYY-MM-DD"),
    thscode: str | None = Query(default=None, description="同花顺标的编码; 省略查全量"),
) -> dict:
    """日期范围异动解读。上游超范围会截取, was_truncated 标记截断。"""
    start = _validate_date(begin_date, "begin_date")
    end = _validate_date(end_date, "end_date")
    if start > end:
        raise HTTPException(400, "begin_date 不能晚于 end_date")
    if not thscode and (end - start).days + 1 > news_service.ANOMALY_MAX_DAYS:
        raise HTTPException(400, f"全量异动查询最多 {news_service.ANOMALY_MAX_DAYS} 天；指定 thscode 可查更长范围")
    params = {"begin_date": begin_date, "end_date": end_date}
    if thscode:
        params["thscode"] = thscode
    return news_service.get_news("anomaly_analysis", params)


@router.get("/stock/timeline")
def stock_timeline(
    thscode: str = Query(..., min_length=1, max_length=32, description="完整同花顺代码, 如 300033.SZ"),
    offset: str = Query(default="first", description="首屏 first, 后续原样回传"),
    size: int | None = Query(default=None, ge=1, le=100, description="每页条数 1-100"),
) -> dict:
    """个股分时新闻。jump_url 保留上游原值, 不保证可用于资讯详情查询。"""
    params: dict = {"thscode": thscode, "offset": offset}
    if size is not None:
        params["size"] = size
    return news_service.get_news("stock_timeline", params)
