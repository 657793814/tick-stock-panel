"""东方财富日频事件数据同步（龙虎榜 / 融资融券）。

落盘契约与财务/股东快照保持一致：``data/financials/{table}/part.parquet``，
``announce_date`` 存**交易日**。``backtest/fundamentals.py`` 对日频表按
「交易日 T 的数据 → T+1 生效」精确日对齐（不做前向填充），因此这里不需要
额外做 T-1 平移，滞后由因子侧统一保证。

为什么放 ``data/financials/``：该目录在本项目里实际承担「点时因子快照」的职责
（metrics/holder_metrics 已在此），因子加载器只认这一个根目录；表名独立
（lhb_metrics / margin_metrics），不会与财务表互相覆盖。

节流：东财对高频请求会临时封 IP。龙虎榜 1 请求/日、融资融券约 9 请求/日，
按日之间 sleep ``_DAY_INTERVAL_S``，默认窗口只补最近 20 个交易日；首次回补
更长历史需显式传 start/end，并接受更长的耗时。
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.market_time import cn_today
from app.plugins.eastmoney import client as em_client
from app.services.fs_utils import atomic_write_parquet

logger = logging.getLogger(__name__)

LHB_TABLE = "lhb_metrics"
MARGIN_TABLE = "margin_metrics"
TABLES: dict[str, str] = {
    LHB_TABLE: "龙虎榜",
    MARGIN_TABLE: "融资融券",
}
_DEFAULT_TRADING_DAYS = 20
_DAY_INTERVAL_S = 1.2
_MAX_RANGE_DAYS = 400


def table_path(data_dir: Path, table: str) -> Path:
    return data_dir / "financials" / table / "part.parquet"


def load_table(data_dir: Path, table: str) -> pl.DataFrame:
    path = table_path(data_dir, table)
    if not path.exists():
        return pl.DataFrame()
    try:
        return pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — 读失败按无数据处理, 下次同步重建
        logger.warning("读取 %s 失败: %s", table, exc)
        return pl.DataFrame()


def trading_days(data_dir: Path, start: date, end: date) -> list[date]:
    """区间内交易日: 优先用本地日K分区 (真实交易日), 缺失时退回工作日。"""
    root = data_dir / "kline_daily"
    days: list[date] = []
    if root.exists():
        for child in root.iterdir():
            name = child.name
            if not child.is_dir() or not name.startswith("date="):
                continue
            try:
                day = date.fromisoformat(name[5:])
            except ValueError:
                continue
            if start <= day <= end:
                days.append(day)
    if days:
        return sorted(set(days))
    out: list[date] = []
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def _merge_daily(existing: pl.DataFrame, fetched: pl.DataFrame) -> pl.DataFrame:
    """按 (symbol, announce_date) 去重合并, 新抓的覆盖旧值。"""
    if existing.is_empty():
        return fetched.unique(subset=["symbol", "announce_date"], keep="last").sort(
            ["symbol", "announce_date"]
        )
    if fetched.is_empty():
        return existing
    keep = fetched.select(["symbol", "announce_date"]).unique()
    base = existing.join(keep, on=["symbol", "announce_date"], how="anti")
    merged = pl.concat([base, fetched], how="diagonal_relaxed")
    return merged.unique(subset=["symbol", "announce_date"], keep="last").sort(
        ["symbol", "announce_date"]
    )


def fetch_day(table: str, day: date) -> pl.DataFrame | None:
    """抓单日; None 表示请求失败 (调用方不得当作 '当日无数据')。"""
    if table == LHB_TABLE:
        rows = em_client.fetch_lhb_day(day)
        return None if rows is None else em_client.parse_lhb(rows, day)
    if table == MARGIN_TABLE:
        rows = em_client.fetch_margin_day(day)
        return None if rows is None else em_client.parse_margin(rows, day)
    raise ValueError(f"unknown eastmoney table: {table!r}")


def sync_table(
    data_dir: Path,
    table: str,
    *,
    start: date | None = None,
    end: date | None = None,
    days: int | None = None,
) -> int:
    """增量同步一张日频表, 返回写入后的总行数 (0 = 无新数据/拉取失败)。"""
    if table not in TABLES:
        raise ValueError(f"unknown eastmoney table: {table!r}")

    end = end or cn_today()
    if start is None:
        window = days if days and days > 0 else _DEFAULT_TRADING_DAYS
        start = end - timedelta(days=max(window * 2, 7))
    if (end - start).days > _MAX_RANGE_DAYS:
        start = end - timedelta(days=_MAX_RANGE_DAYS)
    if start > end:
        return 0

    targets = trading_days(data_dir, start, end)
    if days and days > 0:
        targets = targets[-days:]
    if not targets:
        logger.info("%s skipped: %s~%s 无交易日", table, start, end)
        return 0

    frames: list[pl.DataFrame] = []
    failed = 0
    for index, day in enumerate(targets):
        if index:
            time.sleep(_DAY_INTERVAL_S)
        frame = fetch_day(table, day)
        if frame is None:
            failed += 1
            continue
        if not frame.is_empty():
            frames.append(frame)

    existing = load_table(data_dir, table)
    if not frames:
        if failed:
            logger.warning("%s: %d 个交易日全部拉取失败 (疑似限流)", table, failed)
        return len(existing)

    fetched = pl.concat(frames, how="vertical")
    merged = _merge_daily(existing, fetched)
    path = table_path(data_dir, table)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_parquet(merged, path)
    logger.info(
        "%s done: %d 日 (失败 %d) → %d 行 / %d 标的",
        table, len(frames), failed, len(merged),
        merged["symbol"].n_unique() if "symbol" in merged.columns else 0,
    )
    return len(merged)


def table_status(data_dir: Path) -> dict[str, dict]:
    """各表覆盖情况: 行数/标的数/日期范围 (供状态卡展示)。"""
    out: dict[str, dict] = {}
    for table, label in TABLES.items():
        frame = load_table(data_dir, table)
        if frame.is_empty() or "announce_date" not in frame.columns:
            out[table] = {"label": label, "rows": 0, "symbols": 0, "start": None, "end": None}
            continue
        dates = frame["announce_date"].drop_nulls()
        out[table] = {
            "label": label,
            "rows": len(frame),
            "symbols": int(frame["symbol"].n_unique()) if "symbol" in frame.columns else 0,
            "start": str(dates.min()) if len(dates) else None,
            "end": str(dates.max()) if len(dates) else None,
        }
    return out
