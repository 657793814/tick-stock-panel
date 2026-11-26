"""补齐「有日K但没有 enriched」的标的（多市场扩展）。

症状: 港美股分批同步时, 日K 落盘与 enriched 计算是两步。某一批日K 写完后
enriched 步骤失败/超时/任务被取消, 这批标的就永久只剩日K —— 选股、看板、
异动等读 enriched 的地方对它们全为空, 而数据状态条显示「日K 覆盖 1.1 万只 /
指标 2 千只」。手动「全量重算指标」会把已有指标也重算一遍, 美股要跑很久。

本模块只做差额: 找该市场「日K有、enriched 没有」的标的 → 按市场口径跑
run_pipeline（只对这些标的 merge-upsert, 不触碰 A 股）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

from app.markets import filter_symbols, get_market

logger = logging.getLogger(__name__)

_SCAN_PARTITIONS = 3


def _recent_symbols(base: Path, market: str, limit_parts: int = _SCAN_PARTITIONS) -> list[str]:
    """最近若干日分区里出现过的该市场标的（并集, 去重保序）。"""
    if not base.exists():
        return []
    parts = sorted(
        (p for p in base.glob("date=*") if p.is_dir()),
        key=lambda p: p.name,
        reverse=True,
    )[:limit_parts]
    symbols: list[str] = []
    seen: set[str] = set()
    for part in parts:
        path = part / "part.parquet"
        if not path.exists():
            continue
        try:
            df = pl.read_parquet(path, columns=["symbol"])
        except Exception as e:  # noqa: BLE001 — 单分区损坏不应让整次补齐失败
            logger.warning("读取分区失败 %s: %s", path, e)
            continue
        for symbol in filter_symbols(df, market)["symbol"].to_list():
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    return symbols


def find_missing(repo, market: str) -> list[str]:
    """该市场「最近分区里有日K、但 enriched 缺行」的标的。"""
    get_market(market)  # 非法市场直接 ValueError
    data_dir = Path(repo.store.data_dir)
    daily = _recent_symbols(data_dir / "kline_daily", market)
    if not daily:
        return []
    enriched = set(_recent_symbols(data_dir / "kline_daily_enriched", market))
    return [s for s in daily if s not in enriched]


def backfill(repo, market: str, limit: int = 3000, on_progress=None) -> dict:
    """为差额标的计算 enriched。limit 控制单轮规模（美股全量一次跑太久）。"""
    emit = on_progress or (lambda *a, **k: None)

    emit("backfill_enriched", 3, f"扫描 {market} 日K/指标差额…")
    missing = find_missing(repo, market)
    total = len(missing)
    if total == 0:
        emit("done", 100, f"{market} 日K 与指标已一致, 无需补齐")
        return {"candidates": 0, "computed": 0, "rows": 0, "remaining": 0}
    if limit and total > limit:
        # 已算过的标的排在后面没有意义: 差额本身就是「没算过的」, 直接取前 limit 只。
        batch = missing[:limit]
    else:
        batch = missing

    emit("backfill_enriched", 8,
         f"{market} 待补指标 {total} 只, 本轮处理 {len(batch)} 只")

    from app.indicators.pipeline import run_pipeline

    def _batch_progress(cur: int, tot: int) -> None:
        safe = max(tot, 1)
        emit("backfill_enriched", 8 + int(85 * cur / safe),
             f"计算指标 批次 {cur}/{tot}",
             stage_pct=int(100 * cur / safe), skip_log=True)

    rows = run_pipeline(symbols=batch, market=market, on_batch_done=_batch_progress)
    repo.refresh_cache()
    try:
        from app.services.screener import ScreenerService
        ScreenerService.clear_history_cache()
    except Exception as e:  # noqa: BLE001 — 缓存清理失败不影响已落盘指标
        logger.warning("清理选股缓存失败: %s", e)

    remaining = max(0, total - len(batch))
    emit("done", 100, f"{market} 指标补齐完成, {rows} 行"
         + (f"; 还剩 {remaining} 只, 可再次补一轮" if remaining else ""))
    return {"candidates": total, "computed": len(batch), "rows": rows,
            "remaining": remaining}
