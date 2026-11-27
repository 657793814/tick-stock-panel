"""XTick 股东结构数据同步 (十大股东 / 十大流通股东 / 股东户数)。

落盘契约: ``data/financials/holder_metrics/part.parquet``, 一行一个
(symbol, period_end), 列由 ``plugins/xtick/holders.py`` 聚合产出。
``backtest/fundamentals.py`` 以 ``announce_date`` 做点时门控并入日频面板,
因此这里必须保留真实公告日, 不得用报告期代替 (否则回测会提前看到数据)。

- 仅 XTick 提供这三个接口; 未启用/未配置 Key 时直接返回 0 行, 不换源。
- 逐股 3 次请求, 全市场首次全量约 30~60 分钟, 由手动同步触发;
  日常增量只补最新期 (start 取近 3 年, 与历史并按 (symbol, period_end) 合并)。
"""
from __future__ import annotations

import contextlib
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_today
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

TABLE = "holder_metrics"
_PATH = ("financials", TABLE, "part.parquet")
_INTERVAL_S = 0.05
_HISTORY_DAYS = 3 * 365


def table_path(data_dir: Path) -> Path:
    return data_dir.joinpath(*_PATH)


def load_holder_metrics(data_dir: Path) -> pl.DataFrame:
    path = table_path(data_dir)
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — 读失败按无数据处理, 下次同步重建
        logger.warning("读取 holder_metrics 失败: %s", exc)
        return pl.DataFrame()


def _default_symbols(data_dir: Path) -> list[str]:
    """优先用标的表, 缺失时退回已有财务指标覆盖的标的。"""
    instruments = data_dir / "instruments" / "instruments.parquet"
    if instruments.exists():
        try:
            frame = pl.read_parquet(instruments)
            if "symbol" in frame.columns:
                if "asset_type" in frame.columns:
                    frame = frame.filter(pl.col("asset_type") == "stock")
                symbols = [str(s) for s in frame["symbol"].drop_nulls().unique().to_list()]
                if symbols:
                    return sorted(symbols)
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 instruments 失败: %s", exc)
    metrics = data_dir / "financials" / "metrics" / "part.parquet"
    if metrics.exists():
        try:
            frame = pl.read_parquet(metrics, columns=["symbol"])
            return sorted({str(s) for s in frame["symbol"].drop_nulls().to_list()})
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 metrics 标的失败: %s", exc)
    return []


def _provider():
    from app.data_providers import custom as custom_sources

    if not custom_sources.is_custom_provider("xtick"):
        return None
    return custom_sources.get_provider("xtick")


def fetch_holder_metrics(
    symbols: list[str],
    *,
    start: str | None = None,
    end: str | None = None,
    provider: Any | None = None,
) -> pl.DataFrame:
    """逐股拉取三个股东接口并聚合成宽表; 单股失败只丢弃该股。"""
    from app.plugins.xtick.holders import build_holder_rows

    own_provider = provider is None
    provider = provider or _provider()
    if provider is None:
        logger.info("holder_metrics skipped: XTick 未启用或未配置 Key")
        return pl.DataFrame()
    if not symbols:
        return pl.DataFrame()

    today = cn_today()
    start = start or (today - timedelta(days=_HISTORY_DAYS)).isoformat()
    end = end or today.isoformat()

    records: list[dict] = []
    total = len(symbols)
    for index, symbol in enumerate(symbols):
        if index:
            time.sleep(_INTERVAL_S)
        params = {"code": str(symbol).split(".")[0], "startDate": start, "endDate": end}
        try:
            top = provider.raw("top_holder", params)
            flow = provider.raw("top_flow_holder", params)
            nums = provider.raw("holder_num", params)
        except Exception as exc:  # noqa: BLE001 — 单股失败不影响整批
            logger.warning("holder_metrics %s 拉取失败: %s", symbol, type(exc).__name__)
            continue
        records.extend(build_holder_rows(
            str(symbol), top_rows=top, flow_rows=flow, num_rows=nums,
        ))
        if (index + 1) % 200 == 0:
            logger.info("holder_metrics 进度 %d/%d", index + 1, total)

    if own_provider:
        with contextlib.suppress(Exception):
            provider.close()
    if not records:
        return pl.DataFrame()
    return pl.DataFrame(records, infer_schema_length=None)


def sync_holder_metrics(
    data_dir: Path,
    symbols: list[str] | None = None,
    *,
    limit: int | None = None,
) -> int:
    """同步股东结构宽表并合并历史; 返回写入行数。"""
    from app.services.financial_sync import _merge_report_history

    target = list(symbols) if symbols else _default_symbols(data_dir)
    if limit is not None and limit > 0:
        target = target[:limit]
    if not target:
        logger.warning("holder_metrics skipped: 无可用标的")
        return 0

    fetched = fetch_holder_metrics(target)
    if fetched.is_empty():
        return 0
    existing = load_holder_metrics(data_dir)
    merged = _merge_report_history(existing, fetched)
    if merged.is_empty():
        return 0
    path = table_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(merged, path)
    logger.info(
        "holder_metrics done: %d symbols -> %d rows", merged["symbol"].n_unique(), len(merged),
    )
    return len(merged)
