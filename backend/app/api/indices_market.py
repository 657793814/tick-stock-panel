"""港美股主要指数 API（多市场扩展）。

数据来自腾讯免费接口（TickFlow 免费模式无港美股指数），由
services/index_sync_market.sync_market_indices 同步到本地。
"""
from __future__ import annotations

import logging
from datetime import date, time as dt_time, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from app import cache_store
from app.market_time import cn_now
from app.services import index_sync_market

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/indices/market", tags=["indices-market"])

# 本地指数快照盘后才更新, 盘中仍按短 TTL 缓存: 同一份结果在多个页面/客户端
# 间共享, 避免每次打开看板都全量扫 kline_index_daily 分区 (实测 1.2~1.4s CPU)。
_INDEX_QUOTES_NS = "index-quotes"


def _index_quotes_ttl(market: str) -> int:
    """盘中 60s / 休市 600s: 指数快照由定时同步落盘, 同步结束会主动失效。

    本地并无逐笔指数行情, 过短 TTL 只会把同一份扫描结果反复重算
    (冷启动一次 3~13s), 反而推高 CPU。
    """
    now = cn_now()
    if now.weekday() >= 5:
        return 600
    t = now.time()
    if market == "hk":
        open_now = dt_time(9, 30) <= t <= dt_time(16, 10)
    else:
        open_now = t >= dt_time(21, 30) or t <= dt_time(4, 10)
    return 60 if open_now else 600


@router.get("")
def market_indices(request: Request, market: str = Query("hk", description="hk|us")):
    """港美股指数清单 + 最新收盘/涨跌幅。"""
    if market not in ("hk", "us"):
        raise HTTPException(status_code=400, detail="market 必须为 hk|us")
    items = index_sync_market.list_market_indices(market)
    data_dir = request.app.state.repo.store.data_dir
    daily_dir = Path(data_dir) / f"kline_index_daily_{market}"
    out = []
    for item in items:
        symbol = item["symbol"]
        latest = None
        if daily_dir.exists():
            try:
                import polars as pl
                # 实时快照优先（美股指数 latest_{symbol}.parquet 含 change_pct）
                snap_path = daily_dir / f"latest_{symbol}.parquet"
                if snap_path.exists():
                    snap = pl.read_parquet(snap_path)
                    if not snap.is_empty():
                        r = snap.to_dicts()[0]
                        latest = {
                            "date": str(r.get("date", "")),
                            "close": float(r.get("close", 0)),
                            "change_pct": float(r.get("change_pct") or 0),
                            "snapshot": True,
                        }
                if latest is None:
                    lf = pl.scan_parquet((daily_dir / "date=*" / "*.parquet").as_posix())
                    row = lf.filter(pl.col("symbol") == symbol).sort("date", descending=True).head(2).collect()
                    if row.height >= 2:
                        prev = float(row["close"][-1])
                        cur = float(row["close"][0])
                        latest = {
                            "date": str(row["date"][0]),
                            "close": cur,
                            "change_pct": (cur / prev - 1) if prev else 0.0,
                        }
                    elif row.height == 1:
                        latest = {"date": str(row["date"][0]), "close": float(row["close"][0]), "change_pct": 0.0}
            except Exception as e:  # noqa: BLE001
                logger.warning("index %s latest failed: %s", symbol, e)
        out.append({**item, "latest": latest})
    return {"market": market, "items": out}


@router.get("/kline")
def market_index_kline(
    request: Request,
    market: str = Query("hk"),
    symbol: str = Query(...),
    days: int = Query(250, ge=30, le=800),
):
    """港美股指数日K（含未复权 OHLCV）。"""
    daily_dir = Path(request.app.state.repo.store.data_dir) / f"kline_index_daily_{market}"
    if not daily_dir.exists():
        return {"rows": []}
    import polars as pl
    try:
        lf = pl.scan_parquet((daily_dir / "**" / "*.parquet").as_posix())
        df = (
            lf.filter(pl.col("symbol") == symbol)
            .sort("date", descending=True)
            .head(days)
            .sort("date")
            .collect()
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("index kline failed: %s", e)
        return {"rows": []}
    rows = [
        {"date": str(r["date"]), "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "volume": r["volume"]}
        for r in df.to_dicts()
    ]
    return {"symbol": symbol, "market": market, "rows": rows}


# ================================================================
# 与 A 股 /api/index/* 同构的端点（前端复用 A 股 Indices 页渲染）
# ================================================================

@router.get("/list")
def market_indices_list(request: Request, market: str = Query("hk", description="hk|us")):
    """港美股指数清单（对齐 A 股 /api/index/list 返回格式）。"""
    items = index_sync_market.list_market_indices(market)
    return {"results": items, "count": len(items)}


@router.get("/quotes")
def market_indices_quotes(request: Request, market: str = Query("hk", description="hk|us")):
    """港美股指数最新行情（对齐 A 股 /api/index/quotes 返回格式）。"""
    if market not in ("hk", "us"):
        raise HTTPException(status_code=400, detail="market 必须为 hk|us")
    # market 放在 key 前缀, 同步完成后按前缀精确失效该市场。
    cache_key = f"{market}:{request.app.state.repo.store.data_dir}"
    cached = cache_store.get_json(_INDEX_QUOTES_NS, cache_key)
    if cached is not None:
        return cached
    import polars as pl

    data_dir = request.app.state.repo.store.data_dir
    daily_dir = Path(data_dir) / f"kline_index_daily_{market}"
    items = index_sync_market.list_market_indices(market)
    latest_map: dict[str, dict] = {}
    missing: list[str] = []
    if daily_dir.exists():
        # 快照优先（美股指数 latest_{symbol}.parquet 含 change_pct）
        for item in items:
            symbol = item["symbol"]
            snap = daily_dir / f"latest_{symbol}.parquet"
            if not snap.exists():
                missing.append(symbol)
                continue
            try:
                s = pl.read_parquet(snap)
                if s.is_empty():
                    missing.append(symbol)
                    continue
                r = s.to_dicts()[0]
                latest_map[symbol] = {
                    "date": str(r.get("date", "")),
                    "close": float(r.get("close", 0)),
                    "change_pct": float(r.get("change_pct") or 0),
                    "last_price": float(r.get("close", 0)),
                    "pct": float(r.get("change_pct") or 0),
                }
            except Exception as e:  # noqa: BLE001
                logger.warning("index quote snapshot %s failed: %s", symbol, e)
                missing.append(symbol)
        # 无快照的指数：一次扫描取最近两日（避免每个 symbol 各扫一遍全量分区）
        if missing:
            try:
                # 只需最近两日：只扫最近几个分区，避免全量历史扫描（这是首屏指数条迟迟不出的根因）。
                parts = sorted(daily_dir.glob("date=*"))[-5:]
                files = [p for part in parts for p in part.glob("*.parquet")]
                if not files:
                    raise FileNotFoundError("no parquet partitions")
                lf = pl.scan_parquet([p.as_posix() for p in files])
                df = (
                    lf.filter(pl.col("symbol").is_in(missing))
                    .select(["symbol", "date", "close"])
                    .sort(["symbol", "date"], descending=[False, True])
                    .collect()
                )
                latest_raw: dict[str, list[dict]] = {}
                for r in df.iter_rows(named=True):
                    symbol = str(r["symbol"])
                    rows_sym = latest_raw.setdefault(symbol, [])
                    if len(rows_sym) < 2:
                        rows_sym.append(r)
                for symbol, rows_sym in latest_raw.items():
                    if not rows_sym:
                        continue
                    cur = float(rows_sym[0]["close"])
                    if len(rows_sym) >= 2:
                        prev = float(rows_sym[1]["close"])
                        pct = (cur / prev - 1) if prev else 0.0
                    else:
                        pct = 0.0
                    latest_map[symbol] = {
                        "date": str(rows_sym[0]["date"]),
                        "close": cur,
                        "change_pct": pct,
                        "last_price": cur,
                        "pct": pct,
                    }
                # 极少数停更/新上市的指数不在最近分区，再对剩余 symbol 做一次全量兜底
                still_missing = [s for s in missing if s not in latest_map]
                if still_missing:
                    df_all = (
                        pl.scan_parquet((daily_dir / "date=*" / "*.parquet").as_posix())
                        .filter(pl.col("symbol").is_in(still_missing))
                        .select(["symbol", "date", "close"])
                        .sort(["symbol", "date"], descending=[False, True])
                        .collect()
                    )
                    for r in df_all.iter_rows(named=True):
                        symbol = str(r["symbol"])
                        rows_sym = latest_raw.setdefault(symbol, [])
                        if len(rows_sym) < 2:
                            rows_sym.append(r)
                    for symbol in still_missing:
                        rows_sym = latest_raw.get(symbol) or []
                        if not rows_sym:
                            continue
                        cur = float(rows_sym[0]["close"])
                        prev = float(rows_sym[1]["close"]) if len(rows_sym) >= 2 else 0.0
                        pct = (cur / prev - 1) if prev else 0.0
                        latest_map[symbol] = {
                            "date": str(rows_sym[0]["date"]),
                            "close": cur,
                            "change_pct": pct,
                            "last_price": cur,
                            "pct": pct,
                        }
            except Exception as e:  # noqa: BLE001
                logger.warning("index quotes fallback failed: %s", e)

    rows = [
        {"symbol": item["symbol"], "name": item["name"], **latest_map[item["symbol"]]}
        for item in items
        if item["symbol"] in latest_map
    ]
    result = {"rows": rows, "count": len(rows)}
    cache_store.set_json(_INDEX_QUOTES_NS, cache_key, result, ttl=_index_quotes_ttl(market))
    return result


@router.get("/daily")
def market_indices_daily(
    request: Request,
    market: str = Query("hk"),
    symbol: str = Query(...),
    days: int = Query(180, ge=30, le=800),
    start: str | None = None,
    end: str | None = None,
):
    """港美股指数日K（对齐 A 股 /api/index/daily 返回格式）。"""
    daily_dir = Path(request.app.state.repo.store.data_dir) / f"kline_index_daily_{market}"
    if not daily_dir.exists():
        return {"symbol": symbol, "rows": [], "index_info": None}
    import polars as pl
    try:
        lf = pl.scan_parquet((daily_dir / "date=*" / "*.parquet").as_posix())
        lf = lf.filter(pl.col("symbol") == symbol)
        if start and end:
            lf = lf.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        else:
            lf = lf.sort("date", descending=True).head(days)
        df = lf.sort("date").collect()
    except Exception as e:  # noqa: BLE001
        logger.warning("index daily failed: %s", e)
        return {"symbol": symbol, "rows": [], "index_info": None}
    rows = [
        {"date": str(r["date"]), "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "volume": r["volume"], "amount": 0}
        for r in df.to_dicts()
    ]
    name = next((i["name"] for i in index_sync_market.list_market_indices(market) if i["symbol"] == symbol), symbol)
    return {"symbol": symbol, "rows": rows, "index_info": {"symbol": symbol, "name": name}}


@router.get("/minute")
def market_indices_minute(request: Request, market: str = Query("hk"), symbol: str = Query(...)):
    """港美股指数分时（免费数据源无，返回空）。"""
    return {"symbol": symbol, "rows": [], "index_info": None}


@router.post("/sync")
def market_indices_sync(request: Request, market: str = Query("hk", description="hk|us")):
    """手动同步港美股指数（对齐 A 股 /api/index/sync_daily 语义）。"""
    from app.services.index_sync_market import sync_market_indices

    data_dir = request.app.state.repo.store.data_dir
    n = sync_market_indices(market, Path(data_dir))
    return {"ok": True, "rows": n, "market": market}
