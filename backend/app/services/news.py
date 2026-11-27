"""新闻资讯服务 (fuyao 专有, 非路由数据集)。

- tickflow 无新闻能力 → 与 dragon_tiger / auction_benchmark 同样走 fuyao provider,
  未启用或未配置 Key 时返回 source_unavailable, 不静默换源、不伪造空资讯。
- Redis 走全局 cache_store (Redis 不可达时自动内存兜底)。详情页与翻页游标
  这类不可变结果缓存 7 天; 首屏快讯/当日异动等易变结果只缓存 10~30 秒,
  避免"7 天前的首屏快讯"冒充最新资讯。
- 时间字段只做类型归一, 不换算单位: 文档未确认 create_time / last_anomaly_time
  的秒/毫秒口径, 原值透传由前端展示, 禁止猜测后乘 1000。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from typing import Any

from app import cache_store
from app.market_time import cn_today
from app.plugins.fuyao.client import FuyaoError

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "news"
CACHE_TTL_SECONDS = 7 * 24 * 3600
FRESH_TTL_SECONDS = 10 * 60
FLASH_FIRST_TTL_SECONDS = 30

NEWS_KINDS = frozenset({
    "article_detail", "events_search", "flash_list", "flash_refresh",
    "notice_detail", "topic_hot_list", "anomaly_analysis", "stock_timeline",
})
# 全量异动查询实测: 文档称最多 30 天, 但多日组合偶发 5003; 按单日拆分可稳定取数。
ANOMALY_MAX_DAYS = 30


def _provider():
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("fuyao"):
        return None
    return custom_sources.get_provider("fuyao")


def availability() -> dict:
    """供前端展示新闻页能力状态。"""
    provider = _provider()
    return {
        "source": "fuyao",
        "available": provider is not None,
        "cache_ttl_days": 7,
        "message": (
            "" if provider is not None
            else "未启用或未配置扶摇数据源，请在「设置 → 数据源」配置 API Key"
        ),
    }


def cache_key(kind: str, params: dict) -> str:
    """键覆盖所有影响结果的入参; 排序保证同参数不同传入顺序命中同一条。"""
    return json.dumps(
        {"kind": kind, "params": params},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


def cache_ttl(kind: str, params: dict, today: date | None = None) -> int:
    """返回秒; 0 表示不缓存。"""
    if kind == "flash_refresh":
        return 0
    if kind in ("article_detail", "notice_detail"):
        return CACHE_TTL_SECONDS
    if kind == "flash_list":
        # seq=0 是第一屏, 会随新快讯变化; 后续 seq 指向固定位置, 可长期缓存。
        return FLASH_FIRST_TTL_SECONDS if str(params.get("seq", "0")) == "0" else CACHE_TTL_SECONDS
    if kind == "topic_hot_list":
        return FRESH_TTL_SECONDS if not params.get("cursor") else CACHE_TTL_SECONDS
    if kind == "stock_timeline":
        return FRESH_TTL_SECONDS if str(params.get("offset", "first")) == "first" else CACHE_TTL_SECONDS
    if kind == "anomaly_analysis":
        try:
            end = date.fromisoformat(str(params.get("end_date", "")))
        except ValueError:
            return FRESH_TTL_SECONDS
        return FRESH_TTL_SECONDS if end >= (today or cn_today()) else CACHE_TTL_SECONDS
    return FRESH_TTL_SECONDS


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _items(value: Any) -> list:
    return value if isinstance(value, list) else []


def _flash_item(row: Any) -> dict:
    item = row if isinstance(row, dict) else {}
    tags = [
        {
            "id": _integer(tag.get("id")) if isinstance(tag, dict) else None,
            "name": _text(tag.get("name")) if isinstance(tag, dict) else None,
        }
        for tag in _items(item.get("tags"))
    ]
    return {
        "id": _text(item.get("id")),
        "seq": _text(item.get("seq")),
        "title": _text(item.get("title")) or "",
        "encoded_url": _text(item.get("encoded_url")) or "",
        "summary": _text(item.get("summary")),
        "tags": tags,
        "type": _integer(item.get("type")),
        "create_time_ms": _integer(item.get("create_time_ms")),
        "update_time_ms": _integer(item.get("update_time_ms")),
        "pic_url": _text(item.get("pic_url")),
    }


def _flash_page(data: dict) -> dict:
    return {"items": [_flash_item(row) for row in _items(data.get("items"))]}


def _article_detail(data: dict) -> dict:
    return {
        "encoded_seq": _text(data.get("encoded_seq")) or "",
        "title": _text(data.get("title")) or "",
        "author": _text(data.get("author")),
        "summary": _text(data.get("summary")),
        "content": _text(data.get("content")) or "",
        "source": _text(data.get("source")),
        "publish_time_ms": _integer(data.get("publish_time_ms")),
        "is_kuaixun": _flag(data.get("is_kuaixun")),
    }


def _notice_detail(data: dict) -> dict:
    summary = data.get("summary")
    summary = summary if isinstance(summary, dict) else None
    interpretation = data.get("monthly_report_interpretation")
    interpretation = interpretation if isinstance(interpretation, dict) else None
    result = {
        "seq": _text(data.get("seq")) or "",
        "guid": _text(data.get("guid")),
        "title": _text(data.get("title")) or "",
        "report_period": _text(data.get("report_period")),
        "summary": None,
        "monthly_report_interpretation": None,
    }
    if summary is not None:
        result["summary"] = {
            "notice_type": _text(summary.get("notice_type")),
            "items": [
                {
                    "title": _text(row.get("title")) if isinstance(row, dict) else None,
                    "content": _text(row.get("content")) if isinstance(row, dict) else None,
                }
                for row in _items(summary.get("items"))
            ],
        }
    if interpretation is not None:
        findings = []
        for finding in _items(interpretation.get("findings")):
            finding = finding if isinstance(finding, dict) else {}
            findings.append({
                "title": _text(finding.get("title")),
                "blocks": [
                    {
                        "sub_title": _text(block.get("sub_title")) if isinstance(block, dict) else None,
                        "content_html": _text(block.get("content_html")) if isinstance(block, dict) else None,
                    }
                    for block in _items(finding.get("blocks"))
                ],
            })
        result["monthly_report_interpretation"] = {
            "notice_type": _text(interpretation.get("notice_type")),
            "has_findings": bool(interpretation.get("has_findings")),
            "findings": findings,
            "summary_html": _text(interpretation.get("summary_html")),
        }
    return result


def _topic_page(data: dict) -> dict:
    items = []
    for row in _items(data.get("items")):
        row = row if isinstance(row, dict) else {}
        items.append({
            "id": _text(row.get("id")),
            "title": _text(row.get("title")) or "",
            "cover": _text(row.get("cover")),
            "url": _text(row.get("url")) or "",
            "type": _text(row.get("type")),
            "seo_key": _text(row.get("seo_key")),
            "propagation_chain": _text(row.get("propagation_chain")),
            "important_flag": _integer(row.get("important_flag")),
            "pinned_flag": _integer(row.get("pinned_flag")),
            "pin_label": _text(row.get("pin_label")),
            "source_created_at": _text(row.get("source_created_at")),
            "source_updated_at": _text(row.get("source_updated_at")),
        })
    return {
        "items": items,
        "next_cursor": _text(data.get("next_cursor")),
        "has_more": bool(data.get("has_more")),
    }


def _anomaly_page(data: dict) -> dict:
    items = []
    for row in _items(data.get("items")):
        row = row if isinstance(row, dict) else {}
        words = row.get("key_word_list")
        items.append({
            "analysis_id": _text(row.get("analysis_id")),
            "domain": _text(row.get("domain")),
            "analysis_content": _text(row.get("analysis_content")),
            "key_word_list": [w for w in words if isinstance(w, str)] if isinstance(words, list) else None,
            "anomaly_type": _text(row.get("anomaly_type")),
            "tag_code": _text(row.get("tag_code")),
            "tag_name": _text(row.get("tag_name")),
            "link_url": _text(row.get("link_url")) or "",
            # 文档未确认单位: 保留上游原值, 不按秒/毫秒猜测换算。
            "create_time": _integer(row.get("create_time")),
            "last_anomaly_time": _integer(row.get("last_anomaly_time")),
            "first_publish_time": _integer(row.get("first_publish_time")),
            "tag_code_update_time": _integer(row.get("tag_code_update_time")),
        })
    return {"items": items, "was_truncated": bool(data.get("was_truncated"))}


def _timeline_page(data: dict) -> dict:
    items = []
    for row in _items(data.get("items")):
        row = row if isinstance(row, dict) else {}
        items.append({
            "id": _text(row.get("id")),
            "type": _integer(row.get("type")),
            "title": _text(row.get("title")) or "",
            "author": _text(row.get("author")),
            "source": _text(row.get("source")),
            "summary": _text(row.get("summary")),
            "publish_time_ms": _integer(row.get("publish_time_ms")),
            "jump_url": _text(row.get("jump_url")) or "",
            "pic_url": _text(row.get("pic_url")),
        })
    return {
        "items": items,
        "has_more": bool(data.get("has_more")),
        "offset": _text(data.get("offset")),
    }


def _event_page(data: dict) -> dict:
    # 事件库字段多且含可选嵌套, 业务字段原样保留; 只保证容器类型稳定。
    return {
        "total": _integer(data.get("total")) or 0,
        "items": _items(data.get("items")),
    }


_NORMALIZERS = {
    "article_detail": _article_detail,
    "events_search": _event_page,
    "flash_list": _flash_page,
    "flash_refresh": _flash_page,
    "notice_detail": _notice_detail,
    "topic_hot_list": _topic_page,
    "anomaly_analysis": _anomaly_page,
    "stock_timeline": _timeline_page,
}


def normalize(kind: str, data: dict) -> dict:
    normalizer = _NORMALIZERS.get(kind)
    return normalizer(data) if normalizer is not None else {}


def _anomaly_by_day(params: dict, provider) -> dict:
    """全量异动跨日查询按单日聚合。

    实测同一日期范围一次请求可能返回 5003, 而逐日请求均可用。逐日请求后按
    analysis_id 去重、按 create_time 倒序合并; 单日失败不拖垮整段查询, 仅标记 partial。
    上游限流(4001)立即上抛, 避免继续请求。
    """
    start = date.fromisoformat(str(params["begin_date"]))
    end = date.fromisoformat(str(params["end_date"]))
    items: list[dict] = []
    seen: set[str] = set()
    truncated = False
    skipped = 0
    current = start
    while current <= end:
        day = current.isoformat()
        day_params = {"begin_date": day, "end_date": day}
        if params.get("thscode"):
            day_params["thscode"] = params["thscode"]
        try:
            raw = provider.news("anomaly_analysis", day_params)
        except FuyaoError as exc:
            if str(exc.code) == "4001":
                raise
            skipped += 1
            current += timedelta(days=1)
            continue
        page = normalize("anomaly_analysis", raw if isinstance(raw, dict) else {})
        for row in page.get("items", []):
            key = str(row.get("analysis_id") or json.dumps(row, sort_keys=True, ensure_ascii=False))
            if key in seen:
                continue
            seen.add(key)
            items.append(row)
        truncated = truncated or bool(page.get("was_truncated"))
        current += timedelta(days=1)
    items.sort(key=lambda row: row.get("create_time") if isinstance(row.get("create_time"), int) else 0,
               reverse=True)
    result = {"items": items, "was_truncated": truncated}
    if skipped:
        result.update({"partial": True, "skipped_days": skipped})
    return result


def _result(state: str, kind: str, message: str | None = None, data: dict | None = None,
            ttl: int = 0, *, cached: bool = False) -> dict:
    return {
        "state": state,
        "kind": kind,
        "message": message,
        "data": data,
        "cached": cached,
        "cache_ttl_seconds": ttl,
    }


def get_news(kind: str, params: dict) -> dict:
    """取新闻数据。统一返回 state/data/cached, 不向上抛网络异常。"""
    if kind not in NEWS_KINDS:
        return _result("invalid_request", kind, f"未知新闻接口类型: {kind}")

    provider = _provider()
    if provider is None:
        return _result("source_unavailable", kind, availability()["message"])

    ttl = cache_ttl(kind, params)
    key = cache_key(kind, params)
    if ttl:
        cached = cache_store.get_json(CACHE_NAMESPACE, key)
        if isinstance(cached, dict):
            return {**cached, "cached": True}

    try:
        if kind == "anomaly_analysis" and not params.get("thscode"):
            start = date.fromisoformat(str(params["begin_date"]))
            end = date.fromisoformat(str(params["end_date"]))
            if (end - start).days >= 1:
                data = _anomaly_by_day(params, provider)
            else:
                raw = provider.news(kind, params)
                data = normalize(kind, raw if isinstance(raw, dict) else {})
        else:
            raw = provider.news(kind, params)
            data = normalize(kind, raw if isinstance(raw, dict) else {})
    except FuyaoError as exc:
        code = str(exc.code)
        logger.warning("扶摇新闻请求失败 (%s): %s", kind, exc)
        if code == "4001":
            return _result("rate_limited", kind, "上游限流，请稍后重试", ttl=ttl)
        if kind == "events_search" and code == "2004":
            return _result("upstream_error", kind,
                           "资讯事件库为同花顺 AI 客户端专用接口，外部 API Key 无法访问", ttl=ttl)
        if kind == "anomaly_analysis" and code == "5003":
            return _result("upstream_error", kind,
                           "该日期范围上游数据源不可用，请缩短范围或改查单日", ttl=ttl)
        return _result("upstream_error", kind, f"上游请求失败: {exc}", ttl=ttl)
    except Exception as exc:  # noqa: BLE001 — 插件异常必须隔离, 不影响页面其他区域
        logger.warning("扶摇新闻请求异常 (%s): %s", kind, exc)
        return _result("upstream_error", kind, "新闻数据暂时不可用", ttl=ttl)

    if data.get("partial"):
        message = f"其中 {data['skipped_days']} 天上游不可用，当前为部分日期结果"
        return _result("ok", kind, message, data, ttl)
    if not data.get("items") and kind not in ("article_detail", "notice_detail"):
        message = (
            "该日期范围未返回异动解读；空结果也可能表示上游服务降级，不能据此判断无异动"
            if kind == "anomaly_analysis" else "暂无数据"
        )
        # 空结果不落缓存: 给上游降级恢复后的下一次请求重试机会。
        return _result("no_data", kind, message, data, ttl)

    result = _result("ok", kind, data=data, ttl=ttl)
    if ttl:
        cache_store.set_json(CACHE_NAMESPACE, key, result, ttl)
    return result
