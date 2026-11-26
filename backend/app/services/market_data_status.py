"""按市场统计本地数据画像（多市场扩展）。

A 股与港美股共用 kline_daily / kline_daily_enriched 物理目录，读侧按 symbol
后缀切分市场（见 app.markets.filter_symbols）。本模块只回答一个问题：
「这个市场本地到底有没有数据、到哪一天了」，供前端在切到 hk/us 且为空时
给出可操作的同步入口，而不是留一片空白。

设计取舍:
  - 只扫最近 N 个日分区（默认 12），不扫全历史。日分区按日期命名，港股/
    美股交易日与 A 股不同，最新分区未必包含该市场数据，故向前多看几天。
  - 维表直接读 instruments_index_{market}/instruments.parquet（cn 走共享池）。
  - 不缓存: 调用方（市场状态条）本身低频，且同步后要立刻看到变化。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.markets import ALL_MARKETS, filter_symbols, get_market

logger = logging.getLogger(__name__)

_SCAN_PARTITIONS = 12


def _date_partitions(base: Path, limit: int) -> list[Path]:
    if not base.exists():
        return []
    parts = sorted(
        (p for p in base.glob("date=*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )
    return parts[:limit]


def _scan_market_frame(base: Path, market: str, columns: list[str]) -> pl.DataFrame:
    """读最近若干日分区，只保留该市场的行。"""
    frames: list[pl.DataFrame] = []
    for part in _date_partitions(base, _SCAN_PARTITIONS):
        files = list(part.glob("*.parquet"))
        if not files:
            continue
        try:
            df = pl.read_parquet(files[0], columns=columns)
        except Exception as e:  # noqa: BLE001 — 单分区损坏不应让整页报错
            logger.warning("市场状态读取分区失败 %s: %s", part, e)
            continue
        frames.append(df)
    if not frames:
        return pl.DataFrame()
    return filter_symbols(pl.concat(frames, how="diagonal_relaxed"), market)


def market_data_status(data_dir: Path | str, market: str) -> dict:
    """返回该市场的本地数据画像。"""
    get_market(market)  # 非法市场直接 ValueError，由 API 层转 400
    base = Path(data_dir)

    # 维表位置与 repository.get_instruments_market 同口径:
    #   cn → instruments/instruments.parquet（A 股共享池）
    #   hk/us → instruments_index_{market}/instruments.parquet（单独存放）
    if market == "cn":
        inst_path = base / "instruments" / "instruments.parquet"
    else:
        inst_path = base / f"instruments_index_{market}" / "instruments.parquet"
    instruments = 0
    if inst_path.exists():
        try:
            instruments = pl.read_parquet(inst_path, columns=["symbol"]).height
        except Exception as e:  # noqa: BLE001
            logger.warning("市场维表读取失败 %s: %s", inst_path, e)

    daily = _scan_market_frame(base / "kline_daily", market, ["symbol", "date"])
    symbols = 0
    latest: str | None = None
    if not daily.is_empty():
        symbols = daily["symbol"].n_unique()
        if "date" in daily.columns:
            try:
                value = daily["date"].max()
                latest = value.isoformat() if hasattr(value, "isoformat") else str(value)
            except Exception:  # noqa: BLE001
                latest = None

    enriched = _scan_market_frame(
        base / "kline_daily_enriched", market, ["symbol", "date"],
    )
    enriched_symbols = 0 if enriched.is_empty() else enriched["symbol"].n_unique()

    return {
        "market": market,
        "label": get_market(market).label,
        "instruments": instruments,
        "symbols": symbols,
        "enriched_symbols": enriched_symbols,
        "latest_date": latest,
        "has_data": symbols > 0,
        "supported_markets": ALL_MARKETS,
    }
