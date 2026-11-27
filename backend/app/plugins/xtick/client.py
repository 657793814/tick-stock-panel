"""XTick 上游客户端: 白名单调用 vendored 参数映射脚本。

上游脚本只有参数拼装, 没有超时/状态码/业务错误/凭据保护; HTTP 部分由 vendored
XTickUtil 统一硬化。本层只负责:
  - Key 读取与 base URL 注入;
  - 接口白名单, 阻止任意路径拼接;
  - 参数校验与错误文案脱敏。

注意: 上游仅提供 HTTP (HTTPS 返回 404), token 走 query string。
不得把 params / URL 打进日志或异常文案。
"""
from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_VENDOR = Path(__file__).resolve().parent / "vendor"
if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

from xtick.scripts.api import (  # noqa: E402
    XTickCoreApi,
    XTickHotApi,
    XTickMarketApi,
    XTickQuantApi,
    XTickWatchApi,
)
from xtick.scripts.Config import Config  # noqa: E402
from xtick.scripts.util.XTickUtil import XTickError  # noqa: E402

API_KEY_ENV = "XTICK_API_KEY"
SECRETS_FIELD = "xtick_api_key"
BASE_URL_ENV = "XTICK_BASE_URL"
DEFAULT_BASE_URL = "http://api.xtick.top"


def get_api_key() -> str:
    from app import secrets_store

    return secrets_store.get_env_backed_secret(SECRETS_FIELD, API_KEY_ENV)


def availability() -> tuple[bool, str]:
    if not get_api_key():
        return False, f"缺少 API Key(可在设置页填写,或配置 {API_KEY_ENV})"
    return True, "ok"


def probe_api_key(api_key: str) -> tuple[bool, str]:
    """先探后存: 使用候选 Key 拉一次股票列表, 不落盘、不回显 Key。"""
    key = (api_key or "").strip()
    if not key:
        return False, "API Key 为空"
    try:
        client = XTickClient(api_key=key)
        rows = client.raw("stock_info", {"symbol": "sz"})
        return (True, "ok") if isinstance(rows, list) else (False, "上游返回格式异常")
    except XTickError as exc:
        return False, f"Key 无效或网络失败: {exc}"
    except Exception as exc:  # noqa: BLE001 — 探测失败必须给出文案, 不泄露凭据
        return False, f"Key 探测失败: {type(exc).__name__}"


# 只暴露文档明确且本项目实际使用的接口; 157 个技术指标接口不纳入。
_RAW_METHODS: dict[str, Callable[..., Any]] = {
    "stock_info": XTickMarketApi.getStockInfo,
    "calendar": XTickMarketApi.getCalendar,
    "kline": XTickMarketApi.getKlineMarket,
    "kline_minute": XTickMarketApi.getKlineMinute,
    "holder_num": XTickMarketApi.getHolderNum,
    "gaap": XTickMarketApi.getCoreIndicator,
    "top_holder": XTickMarketApi.getTopHolder,
    "top_flow_holder": XTickMarketApi.getTopFlowHolder,
    "capital": XTickMarketApi.getCapital,
    "realtime_day": XTickWatchApi.getDayKlineRealtime,
    "realtime_minute": XTickWatchApi.getMinuteKlineRealtime,
    "depth": XTickWatchApi.getDeepRealtime,
    "depth_history": XTickWatchApi.getDeepHistory,
    "amount_stat": XTickWatchApi.getAmountStat,
    "fq_kline": XTickWatchApi.getFqKlineUpdate,
    "lhb": XTickWatchApi.getLhbHistory,
    "bid_time": XTickCoreApi.getCoreBidTime,
    "core_time": XTickCoreApi.getCoreTime,
    "chuquan": XTickCoreApi.getCoreChuQuan,
    "tingpai": XTickCoreApi.getCoreTingpai,
    "st": XTickCoreApi.getCoreST,
    "price_limit": XTickCoreApi.getCoreHistoryPrice,
    "fenbi": XTickCoreApi.getCoreFenbi,
    "fenjia": XTickCoreApi.getCoreFenjia,
    "batch_kline": XTickCoreApi.getBatchKline,
    "board": XTickHotApi.getLianbanTianti,
    "emotion": XTickHotApi.getMarketEmotion,
    "moneyflow": XTickHotApi.getMoneyFlow,
    "moneyflow_history": XTickHotApi.getMoneyFlowHistory,
    "bid_history": XTickHotApi.getBidHistory,
    "bid_detail": XTickHotApi.getBidDetail,
    "news": XTickHotApi.getNews,
    "time_kline": XTickHotApi.getTimeKline,
    "bk": XTickHotApi.getConceptStocks,
    "gainian": XTickHotApi.getStockConcepts,
    "day_update": XTickHotApi.getDayUpdate,
    "time_fenbi": XTickHotApi.getTimeFenbi,
    "quant_realtime": XTickQuantApi.getQuantDataRealtime,
    "quant_history": XTickQuantApi.getQuantDataHistory,
}

SUPPORTED_KINDS = frozenset(_RAW_METHODS)


class XTickClient:
    """轻量客户端; 无长连接池, 异常统一为 XTickError。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.api_key = (api_key if api_key is not None else get_api_key()).strip()
        self.base_url = (base_url or os.getenv(BASE_URL_ENV) or DEFAULT_BASE_URL).rstrip("/")
        Config.SERVER_URL = self.base_url

    def raw(self, kind: str, params: dict[str, Any] | None = None) -> Any:
        fn = _RAW_METHODS.get(kind)
        if fn is None:
            raise XTickError(-3, f"未知 XTick 接口类型: {kind}")
        if not self.api_key:
            raise XTickError(-4, "未配置 XTick API Key")
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            return fn(**clean, token=self.api_key)
        except TypeError as exc:
            raise XTickError(-3, f"XTick {kind} 参数不匹配: {exc}") from None
        except XTickError:
            raise

    def close(self) -> None:
        return None
