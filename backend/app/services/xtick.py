"""XTick 专用能力服务 (非路由数据集)。

- tickflow 无竞价/连板/资金流/龙虎榜/新闻等能力, 与 dragon_tiger / news 同样走
  XTick provider, 未启用或未配置 Key 时返回 source_unavailable, 不静默换源。
- Redis 走全局 cache_store: 实时类 30 秒, 当日盘后/热点类 5 分钟, 历史类 7 天。
- 空结果不落缓存, 给上游恢复后的下一次请求重试机会。
- 所有日志只输出 kind 与错误码, 不输出 params/URL/token。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app import cache_store
from app.market_time import cn_today
from app.plugins.xtick.client import XTickError

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "xtick"
CACHE_TTL_SECONDS = 7 * 24 * 3600
INTRADAY_TTL_SECONDS = 30
TODAY_TTL_SECONDS = 5 * 60

XTICK_KINDS = frozenset({
    "stock_info", "calendar", "kline", "kline_minute", "realtime_day",
    "realtime_minute", "depth", "bid_time", "bid_detail", "bid_history",
    "board", "emotion", "moneyflow", "moneyflow_history", "news",
    "time_kline", "bk", "gainian", "lhb", "gaap", "holder_num",
    "top_holder", "top_flow_holder", "amount_stat", "core_time",
    "quant_realtime", "quant_history",
})
REALTIME_KINDS = frozenset({
    "bid_time", "bid_detail", "realtime_day", "realtime_minute", "depth",
    "moneyflow", "news", "time_kline", "quant_realtime", "core_time",
})
TODAY_KINDS = frozenset({"board", "emotion", "lhb", "amount_stat"})

_REQUIRED: dict[str, tuple[str, ...]] = {
    "calendar": ("code", "startDate", "endDate"),
    "kline": ("code", "startDate", "endDate"),
    "kline_minute": ("code",),
    "realtime_day": ("code",),
    "realtime_minute": ("code",),
    "depth": ("code",),
    "bid_detail": ("code", "tradeDate"),
    "bid_history": ("code", "startDate", "endDate"),
    "moneyflow": ("code",),
    "moneyflow_history": ("code", "startDate", "endDate"),
    "board": ("tradeDate",),
    "emotion": ("tradeDate",),
    "news": ("minutes", "tradeDate"),
    "time_kline": ("code",),
    "gainian": ("code",),
    "lhb": ("tradeDate",),
    "gaap": ("code", "startDate", "endDate"),
    "holder_num": ("code", "startDate", "endDate"),
    "top_holder": ("code", "startDate", "endDate"),
    "top_flow_holder": ("code", "startDate", "endDate"),
    "amount_stat": ("tradeDate",),
    "core_time": ("code", "field"),
}


def _provider():
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("xtick"):
        return None
    return custom_sources.get_provider("xtick")


def availability() -> dict:
    provider = _provider()
    return {
        "source": "xtick",
        "display_name": "XTick",
        "available": provider is not None,
        "cache_ttl_days": 7,
        "message": (
            "" if provider is not None
            else "未启用或未配置 XTick 数据源, 请在「设置 → 数据源」配置 API Key"
        ),
    }


def cache_key(kind: str, params: dict) -> str:
    return json.dumps(
        {"kind": kind, "params": params},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )


def cache_ttl(kind: str, params: dict, today: str | None = None) -> int:
    if kind in REALTIME_KINDS:
        return INTRADAY_TTL_SECONDS
    if kind in TODAY_KINDS and str(params.get("tradeDate", "")) == (today or cn_today().isoformat()):
        return TODAY_TTL_SECONDS
    return CACHE_TTL_SECONDS


def _normalize(kind: str, raw: Any) -> dict:
    if isinstance(raw, list):
        return {"items": raw}
    if isinstance(raw, dict):
        return raw
    return {}


def _result(state: str, kind: str, message: str = "", data: dict | None = None,
            ttl: int = CACHE_TTL_SECONDS) -> dict:
    return {
        "state": state,
        "kind": kind,
        "message": message,
        "data": data,
        "cached": False,
        "cache_ttl_seconds": ttl,
    }


def get_data(kind: str, params: dict[str, Any] | None = None) -> dict:
    """取 XTick 专用数据。统一返回 state/data/cached, 不向上抛网络异常。"""
    if kind not in XTICK_KINDS:
        return _result("invalid_request", kind, f"未知 XTick 接口类型: {kind}")
    params = {k: v for k, v in (params or {}).items() if v is not None}
    missing = [name for name in _REQUIRED.get(kind, ()) if not params.get(name)]
    if missing:
        return _result("invalid_request", kind, f"缺少必填参数: {', '.join(missing)}")

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
        raw = provider.raw(kind, params)
    except XTickError as exc:
        logger.warning("XTick 请求失败 (%s, code=%s): %s", kind, exc.code, exc)
        if str(exc.code) == "-4":
            return _result("source_unavailable", kind, "未配置 XTick API Key", ttl=ttl)
        return _result("upstream_error", kind, f"上游请求失败: {exc}", ttl=ttl)
    except Exception as exc:  # noqa: BLE001 — 插件异常必须隔离, 不影响页面其他区域
        logger.warning("XTick 请求异常 (%s): %s", kind, type(exc).__name__)
        return _result("upstream_error", kind, "XTick 数据暂时不可用", ttl=ttl)

    data = _normalize(kind, raw)
    if not data or ("items" in data and not data["items"]):
        # 空结果不落缓存: 给上游恢复后的下一次请求重试机会。
        return _result("no_data", kind, "暂无数据", data, ttl)
    result = _result("ok", kind, data=data, ttl=ttl)
    if ttl:
        cache_store.set_json(CACHE_NAMESPACE, key, result, ttl)
    return result
