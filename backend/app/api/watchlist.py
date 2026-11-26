"""自选 API。"""
from __future__ import annotations

import logging
import math
import time
from datetime import UTC, date, datetime, timedelta
from typing import Callable, Literal

import anyio
import polars as pl
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field

from app.db_safe import quote_ident
from app.market_time import CN_TZ, cn_today
from app.price_limits import (
    polars_is_risk_warning_name,
    polars_limit_price,
    polars_price_limit_pct,
)
from app.services import watchlist
from app.services.watchlist_csv import import_watchlist_codes, import_watchlist_csv
from app.services.watchlist_ocr import import_watchlist_image
from app.services.watchlist_ocr.provider import get_ocr_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_MAX_IMPORT_IMAGE_BYTES = 12 * 1024 * 1024  # 12MB
_IMPORT_IMAGE_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
    "image/bmp",
    "image/gif",
}
# OCR 独立并发上限：避免多张大图同时解码 + 多 Tesseract 子进程
_OCR_LIMITER = anyio.CapacityLimiter(2)
# CSV/TXT 导入：文本远小于截图，上限 5MB 足够
_MAX_IMPORT_CSV_BYTES = 5 * 1024 * 1024
_IMPORT_CSV_TYPES = {
    "text/csv",
    "text/plain",
    "application/csv",
}
# 上传分块读取粒度 (与 ext_data 上传一致)
_UPLOAD_CHUNK_BYTES = 1024 * 1024


async def _read_upload_capped(file: UploadFile, max_bytes: int, too_large: str) -> bytes:
    """分块读取上传内容, 累计超过 max_bytes 立即拒绝(400), 返回完整字节。

    与 ext_data._write_upload_capped 同类保护: 一次性 `await file.read()` 会先把整个
    文件读入内存再比较长度, 上限在那之后才生效, 一个远超上限的上传照样把进程内存
    顶满; 分块读取在越过上限的那一块就停止, 内存占用不超过上限 + 一块。
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(400, too_large)
        chunks.append(chunk)
    return b"".join(chunks)


class AddRequest(BaseModel):
    symbol: str
    note: str = ""
    group_id: str | None = None


class BatchAddRequest(BaseModel):
    symbols: list[str]
    note: str = ""
    group_id: str | None = None
    group_ids: list[str] | None = None


class GroupNameRequest(BaseModel):
    name: str
    color: str | None = None


class GroupReorderRequest(BaseModel):
    ordered_ids: list[str]


class GroupTransferRequest(BaseModel):
    target_group_id: str
    symbols: list[str] = Field(min_length=1)
    action: Literal["move", "copy"]


class GroupAssignRequest(BaseModel):
    group_id: str | None = None


class ImportCodesRequest(BaseModel):
    text: str


def _with_names(rows: list[dict], request: Request) -> list[dict]:
    if not rows:
        return rows
    try:
        # 按 symbol 后缀分市场取名称: A 股走 instruments 池, 港美股查各自维表。
        # 自选可跨市场, 只用 A 股名称表会让港股行 name 恒为 null。
        from app.markets import market_of

        repo = request.app.state.repo
        symbols = [r.get("symbol") for r in rows]
        by_market: dict[str, set[str]] = {}
        for s in symbols:
            by_market.setdefault(market_of(str(s)), set()).add(str(s))
        name_by_symbol: dict[str, str] = {}
        for m, syms in by_market.items():
            name_by_symbol.update(
                {k: v for k, v in repo.get_market_name_map(m).items() if k in syms}
            )
        if not name_by_symbol:
            return rows
        return [{**row, "name": name_by_symbol.get(row.get("symbol"))} for row in rows]
    except Exception as e:  # noqa: BLE001
        logger.debug("attach watchlist names failed: %s", e)
        return rows


def _validate_market(market: str | None) -> str | None:
    """自选的市场过滤参数: None = 不过滤 (旧调用方), 其余必须是注册表里的市场。

    测试会直接调用端点函数 (不经过 FastAPI 解析), 此时 market 是 Query 默认值对象,
    而不是 str —— 取它的 default, 与真实请求的行为保持一致。
    """
    if market is not None and not isinstance(market, str):
        market = getattr(market, "default", None)
    if market in (None, "", "all"):
        return None
    from app.markets import ALL_MARKETS

    if market not in ALL_MARKETS:
        raise HTTPException(400, f"非法市场 '{market}', 支持: {', '.join(ALL_MARKETS)}")
    return market


@router.get("")
def list_all(request: Request, market: str | None = Query(None, description="按市场过滤: cn/hk/us")):
    """自选列表。market 省略时返回全部（旧行为），前端按当前市场传参做隔离。"""
    m = _validate_market(market)
    rows = watchlist.list_symbols()
    if m is not None:
        from app.markets import market_of

        rows = [r for r in rows if market_of(str(r.get("symbol", ""))) == m]
    return {"symbols": _with_names(rows, request), "market": m}


@router.post("")
def add_one(req: AddRequest, request: Request):
    try:
        rows = watchlist.add(req.symbol, req.note, req.group_id)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.post("/batch")
def add_batch(req: BatchAddRequest, request: Request):
    try:
        rows, added = watchlist.add_batch(
            req.symbols,
            req.note,
            group_id=req.group_id,
            group_ids=req.group_ids,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request), "added": added}


@router.get("/groups")
def list_groups():
    return {"groups": watchlist.list_groups()}


class PrepareRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=50)


@router.post("/prepare")
def prepare_data(body: PrepareRequest, request: Request):
    """显式提交自选缺数补齐；普通列表读取不创建后台取数任务。"""
    from app.services import daily_bootstrap

    selected = list(dict.fromkeys(s.strip() for s in body.symbols))
    saved = {entry["symbol"] for entry in watchlist.list_symbols()}
    if any(symbol not in saved for symbol in selected):
        raise HTTPException(400, "仅支持补齐已加入自选的标的")
    qs = getattr(request.app.state, "quote_service", None)
    return {"results": daily_bootstrap.prepare(
        request.app.state.repo, request.app.state.capabilities, selected, cn_today(),
        on_updated=qs.notify_data_updated if qs else None,
    )}


@router.post("/groups")
def create_group(req: GroupNameRequest):
    try:
        groups, group = watchlist.create_group(req.name, req.color)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups, "group": group}


@router.put("/groups/reorder")
def reorder_groups(req: GroupReorderRequest):
    """重排分组前后顺序 (json 数组顺序即定义顺序, 侧边栏/标签栏/分组视图共用)。"""
    try:
        groups = watchlist.reorder_groups(req.ordered_ids)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups}


@router.put("/groups/{group_id}")
def rename_group(group_id: str, req: GroupNameRequest):
    try:
        groups = watchlist.rename_group(group_id, req.name, req.color)
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"groups": groups}


@router.delete("/groups/{group_id}")
def delete_group(group_id: str, request: Request):
    try:
        groups, rows = watchlist.delete_group(group_id)
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    return {"groups": groups, "symbols": _with_names(rows, request)}


@router.post("/groups/{group_id}/clear")
def clear_group(group_id: str, request: Request):
    """清空分组成员:把该分组内所有股票转为未分组,保留分组定义。"""
    try:
        rows = watchlist.clear_group(group_id)
    except KeyError as e:
        raise HTTPException(404, "自选分组不存在") from e
    return {"symbols": _with_names(rows, request)}


@router.get("/ocr-status")
def ocr_status():
    """当前 OCR 引擎是否可用（前端可据此提示安装依赖）。"""
    provider = get_ocr_provider()
    return {"provider": provider.name, "available": provider.available()}


@router.post("/import-image")
async def import_from_image(request: Request, file: UploadFile = File(...)):
    """从自选截图识别股票代码，返回候选列表（不自动写入自选）。"""
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = (file.filename or "").lower()
    # 严格白名单：不接受任意 image/*（如 image/svg+xml）
    ok_type = content_type in _IMPORT_IMAGE_TYPES
    ok_ext = filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"))
    if not ok_type and not ok_ext:
        raise HTTPException(400, "仅支持 JPG / PNG / WebP / BMP / GIF 图片")

    data = await _read_upload_capped(file, _MAX_IMPORT_IMAGE_BYTES, "图片过大（上限 12MB）")
    if not data:
        raise HTTPException(400, "空文件")

    existing = {r["symbol"] for r in watchlist.list_symbols()}
    data_dir = request.app.state.repo.store.data_dir
    try:
        # OCR 为同步 CPU/子进程；独立 limiter 限制并发，避免卡住事件循环（行情 SSE 等）
        result = await anyio.to_thread.run_sync(
            lambda: import_watchlist_image(data, data_dir, existing_symbols=existing),
            limiter=_OCR_LIMITER,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("watchlist import-image failed")
        raise HTTPException(500, f"识别失败: {e}") from e

    # 响应不回传整段 raw_text（可能很长）；调试时可开 query，这里默认省略
    result.pop("raw_text", None)
    return result


def _run_candidate_import(parse: Callable[[], dict], empty_msg: str) -> dict:
    """执行候选解析：ValueError→400、其他→500、空候选→400、剥离 raw_text。"""
    try:
        result = parse()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        logger.exception("watchlist import failed")
        raise HTTPException(500, f"解析失败: {e}") from e
    if not result["candidates"]:
        raise HTTPException(400, empty_msg)
    result.pop("raw_text", None)
    return result


@router.post("/import-csv")
async def import_from_csv(request: Request, file: UploadFile = File(...)):
    """从 CSV / TXT 导入自选候选列表（不自动写入自选）。

    兼容同花顺/东财/通达信导出（逗号或 Tab 分隔、UTF-8 或 GBK 编码）。目标分组
    在候选确认时由前端传入 batch 接口，本端点只做解析与主数据校验。
    """
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    filename = (file.filename or "").lower()
    ok_type = content_type in _IMPORT_CSV_TYPES
    ok_ext = filename.endswith((".csv", ".txt"))
    if not ok_type and not ok_ext:
        raise HTTPException(400, "仅支持 CSV / TXT 文件")

    data = await _read_upload_capped(file, _MAX_IMPORT_CSV_BYTES, "文件过大（上限 5MB）")
    if not data:
        raise HTTPException(400, "空文件")

    data_dir = request.app.state.repo.store.data_dir
    # 解码与自选/instruments parquet 读取为同步 CPU/IO，挪线程池避免卡事件循环
    return await anyio.to_thread.run_sync(
        lambda: _run_candidate_import(
            lambda: import_watchlist_csv(
                data,
                data_dir,
                existing_symbols={r["symbol"] for r in watchlist.list_symbols()},
            ),
            "文件中未识别到股票代码或名称",
        )
    )


@router.post("/import-codes")
def import_from_codes(req: ImportCodesRequest, request: Request):
    """从粘贴的证券代码导入自选候选列表（不自动写入自选）。"""
    text = req.text.strip()
    if not text:
        raise HTTPException(400, "请输入要导入的股票代码")

    existing = {r["symbol"] for r in watchlist.list_symbols()}
    data_dir = request.app.state.repo.store.data_dir
    return _run_candidate_import(
        lambda: import_watchlist_codes(text, data_dir, existing_symbols=existing),
        "未识别到股票代码",
    )


@router.post("/{symbol}/top")
def move_one_to_top(symbol: str, request: Request):
    rows = watchlist.move_to_top(symbol)
    return {"symbols": _with_names(rows, request)}


@router.put("/{symbol}/group")
def assign_group(symbol: str, req: GroupAssignRequest, request: Request):
    """互斥设定分组(仅保留此组; None=移出全部分组)。多组操作用 members 端点。"""
    try:
        rows = watchlist.set_group(symbol, req.group_id)
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.post("/groups/{group_id}/transfer")
def transfer_group_members(group_id: str, req: GroupTransferRequest, request: Request):
    try:
        rows, changed = watchlist.transfer_members(group_id, req.target_group_id, req.symbols, req.action)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request), "changed": changed}


@router.post("/groups/{group_id}/members/{symbol}")
def add_member(group_id: str, symbol: str, request: Request):
    """把标的加入分组(多组成员关系: 不影响其他分组)。"""
    try:
        rows = watchlist.add_to_group(symbol, group_id)
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.delete("/groups/{group_id}/members/{symbol}")
def remove_member(group_id: str, symbol: str, request: Request):
    """把标的移出分组(仅摘本组标签; 标的仍在自选, 可能落入未分组)。"""
    try:
        rows = watchlist.remove_from_group(symbol, group_id)
    except KeyError as e:
        raise HTTPException(404, "自选标的不存在") from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"symbols": _with_names(rows, request)}


@router.delete("/{symbol}")
def remove_one(symbol: str, request: Request):
    rows = watchlist.remove(symbol)
    return {"symbols": _with_names(rows, request)}


@router.delete("")
def clear_all():
    """清空自选列表。"""
    count = watchlist.clear()
    return {"removed": count}


# 自选页需要的列
_WATCHLIST_COLS = [
    "symbol", "date", "close", "open", "high", "low", "prev_close", "volume", "quote_ts",
    "change_pct", "change_amount", "amount",
    "turnover_rate",
    "amplitude", "annual_vol_20d",
    "vol_ratio_5d",
    "ma5", "ma10", "ma20", "ma60",
    "vol_ma5", "vol_ma10",
    "high_60d", "low_60d",
    "rsi_6", "rsi_14", "rsi_24",
    "macd_dif", "macd_dea", "macd_hist",
    "kdj_k", "kdj_d", "kdj_j",
    "boll_upper", "boll_lower",
    "atr_14",
    "momentum_5d", "momentum_10d", "momentum_20d", "momentum_30d", "momentum_60d",
    "deviate_3d", "deviate_10d", "deviate_30d",
    "consecutive_limit_ups", "consecutive_limit_downs",
    "signal_limit_up", "signal_limit_down", "signal_volume_surge",
    "signal_ma_golden_5_20", "signal_macd_golden", "signal_n_day_high",
    "signal_boll_breakout_upper", "signal_ma20_breakout",
    "signal_ma_dead_5_20", "signal_macd_dead", "signal_n_day_low",
    "signal_boll_breakdown_lower", "signal_ma20_breakdown",
]


def _added_dates_bj(entries: list[dict]) -> dict[str, date]:
    """自选条目的 added_at (naive UTC ISO 串) → 北京日期; 空值/非法值跳过该条。

    added_at 由 watchlist.add_batch 以 datetime.utcnow() 写入 (无 Z 后缀), 属 UTC 语义;
    必须显式按 UTC 解析再转北京 —— 否则北京时间 00:00-08:00 加入的记录会被算到前一天。
    换算惯例同 backtest/minute_replay.py, 服务器本地时区不参与 (CONTRIBUTING §3.3)。
    """
    out: dict[str, date] = {}
    for entry in entries:
        raw = entry.get("added_at")
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        out[entry["symbol"]] = dt.astimezone(CN_TZ).date()
    return out


def _added_base_closes(
    repo,
    added_dates: dict[str, date],
    stock_symbols: set[str],
) -> pl.DataFrame:
    """取各标的「加入日或之前最近一个交易日」的前复权收盘价, 作为区间收益基准。

    返回 symbol/base_close 两列表; 无可用值返回空表, 由调用方按 null 降级。

    - **只覆盖股票**: 内存历史缓存 (get_enriched_range) 仅含股票, ETF/指数只有最新日
      缓存, 逐只回补会在行情 tick 热路径上形成 N+1 parquet 扫描, 故显式降级为「—」。
    - **基准日取 <= 加入日**: 周末/节假日/停牌自然回落到前一交易日。与 K 线竖线的
      「不 snap」有意不同 —— 价格口径可以回落, 日期事实不行。
    - **超出缓存窗口 (约 300 个自然日) 的标的直接跳过**, 不触发 scan 兜底。
    """
    stock_targets = {s: d for s, d in added_dates.items() if s in stock_symbols}
    if not stock_targets:
        return pl.DataFrame()  # 没有可算的股票标的 → 不碰 repo
    span = repo.get_enriched_history_span()
    if span is None:
        return pl.DataFrame()
    span_start, span_end = span
    in_range = {s: d for s, d in stock_targets.items() if d >= span_start}
    if len(in_range) < len(stock_targets):
        logger.debug(
            "自选加入后涨跌幅: %d/%d 只早于可查询历史 (%s 之前), 降级为 null",
            len(stock_targets) - len(in_range), len(stock_targets), span_start,
        )
    if not in_range:
        return pl.DataFrame()
    # 窗口必须钳制在缓存区间内: get_enriched_range 是全有或全无的 (cache_min > start
    # 或 cache_max < end 即整体返回 None), 一只越界的标的会让全部标的失去基准价。
    # start > end 只出现在「加入日晚于缓存末日」(本地数据未同步) 时, 退化为取缓存末日。
    end = min(max(in_range.values()), span_end)
    start = min(min(in_range.values()), end)
    hist = repo.get_enriched_range(
        start, end, symbols=list(in_range), columns=["symbol", "date", "close"]
    )
    if hist is None or hist.is_empty():
        return pl.DataFrame()
    added = pl.DataFrame(
        {"symbol": list(in_range), "added_date": list(in_range.values())},
        schema={"symbol": pl.Utf8, "added_date": pl.Date},
    )
    return (
        hist.join(added, on="symbol", how="inner")
        .filter(pl.col("date") <= pl.col("added_date"))
        .group_by("symbol")
        .agg(pl.col("close").sort_by(pl.col("date")).last().alias("base_close"))
    )


def _latest_watchlist_rows(frame: pl.DataFrame, symbols: list[str]) -> pl.DataFrame:
    """仅收敛请求的标的，按日期和行情时间选取完整行，避免关联放大。"""
    selected = frame.filter(pl.col("symbol").is_in(symbols))
    order = [column for column in ("date", "quote_ts") if column in selected.columns]
    if order:
        selected = selected.sort(order, descending=True, nulls_last=True, maintain_order=True)
    return selected.unique(subset=["symbol"], keep="first", maintain_order=True)


def _market_base_closes(
    repo, added_dates: dict[str, date], market: str,
) -> pl.DataFrame:
    """港美股「加入日基准价」: 只扫加入日这几个分区, 不做全量历史 scan。

    A 股那份基准价来自 enriched 内存历史缓存 (本身只含 A 股), 港美股不在里面,
    这里直接读 kline_daily 的对应日期分区并做市场切分。读不到的标的 (停牌/未同步)
    由调用方降级成 null。
    """
    if not added_dates:
        return pl.DataFrame()
    from app.markets import filter_symbols

    base = repo.store.data_dir

    def _resolve(d: date, back: int = 10) -> date | None:
        """加入日当天可能休市 (周末/假期), 回落到之前最近一个有分区的交易日。"""
        for i in range(back + 1):
            cand = d - timedelta(days=i)
            if (base / "kline_daily" / f"date={cand}" / "part.parquet").exists():
                return cand
        return None

    resolved: dict[str, date] = {}
    for sym, d in added_dates.items():
        rd = _resolve(d)
        if rd is not None:
            resolved[sym] = rd
    if not resolved:
        return pl.DataFrame()

    wanted = set(resolved)
    frames: list[pl.DataFrame] = []
    for d in sorted(set(resolved.values())):
        p = base / "kline_daily" / f"date={d}" / "part.parquet"
        try:
            sub = pl.read_parquet(p, columns=["symbol", "date", "close"])
        except Exception as e:  # noqa: BLE001
            logger.debug("base close read failed for %s: %s", d, e)
            continue
        sub = filter_symbols(sub, market).filter(pl.col("symbol").is_in(list(wanted)))
        if not sub.is_empty():
            frames.append(sub)
    if not frames:
        return pl.DataFrame()
    hist = pl.concat(frames, how="diagonal_relaxed").with_columns(
        pl.col("date").cast(pl.Date)
    )
    added = pl.DataFrame(
        {"symbol": list(resolved), "added_date": list(resolved.values())},
        schema={"symbol": pl.Utf8, "added_date": pl.Date},
    )
    return (
        hist.join(added, on="symbol", how="inner")
        .filter(pl.col("date") == pl.col("added_date"))
        .group_by("symbol")
        .agg(pl.col("close").last().alias("base_close"))
    )


def _overlay_market_realtime(repo, df: pl.DataFrame, as_of, symbols: list[str]) -> pl.DataFrame:
    """用当日 kline_daily 的实时快照覆盖 enriched 静态行的价格与 quote_ts。

    跨市场实时服务(MarketQuoteService)只写 kline_daily —— 不能写 enriched 分区,
    否则会 bump A 股 matrix generation、让 A 股 enriched 缓存预热反复失败。
    故港美股自选在这里读日K把实时价叠加到 enriched 行; 无实时行的标的保留原值。
    """
    if df.is_empty() or as_of is None:
        return df
    daily_path = repo.store.data_dir / "kline_daily" / f"date={as_of}" / "part.parquet"
    if not daily_path.exists():
        return df
    try:
        rt = pl.read_parquet(
            daily_path,
            columns=["symbol", "close", "open", "high", "low", "volume", "amount", "quote_ts"],
        )
    except Exception:  # noqa: BLE001 - 日K缺列/损坏时降级为静态 enriched
        return df
    if rt.is_empty() or "quote_ts" not in rt.columns:
        return df
    rt = (
        rt.filter(pl.col("symbol").is_in(symbols))
        .filter(pl.col("quote_ts").is_not_null() & (pl.col("quote_ts") > 0))
        .unique(subset=["symbol"], keep="last")
    )
    if rt.is_empty():
        return df
    overlay_cols = ["close", "open", "high", "low", "volume", "amount", "quote_ts"]
    rt = rt.select(["symbol"] + [pl.col(c).alias(f"_rt_{c}") for c in overlay_cols])
    out = df.join(rt, on="symbol", how="left")
    exprs = []
    for c in overlay_cols:
        rt_col = f"_rt_{c}"
        if c in out.columns:
            exprs.append(pl.coalesce(pl.col(rt_col), pl.col(c)).alias(c))
        else:
            exprs.append(pl.col(rt_col).alias(c))
    out = out.with_columns(exprs).drop([f"_rt_{c}" for c in overlay_cols])
    return out


def _watchlist_enriched_market(request: Request, market: str) -> dict:
    """港美股自选表。

    数据源与 cn 不同: cn 读 repo 的 A 股内存快照, 港美股走 ScreenerService 的
    按市场慢路径 (parquet + 市场维表 + 即时指标)。列契约与 cn 对齐, 缺的列
    (涨跌停价/连板/扩展字段) 一律 null —— 港美股本来就没有涨跌停。

    ext_columns 对港美股无意义 (扩展数据表都是 A 股口径), 调用方传了也忽略。
    """
    t0 = time.perf_counter()
    repo = request.app.state.repo
    from app.markets import market_of

    entries = [
        r for r in watchlist.list_symbols()
        if market_of(str(r.get("symbol", ""))) == market
    ]
    symbols = [r["symbol"] for r in entries]
    empty = {"rows": [], "as_of": None, "elapsed_ms": 0,
             "dates": {"stock": None, "etf": None, "index": None}, "market": market}
    if not symbols:
        return empty

    from app.services.screener import ScreenerService

    svc = ScreenerService(repo, market=market)
    as_of = svc.latest_date()
    if as_of is None:
        return empty
    frame = svc._load_enriched_for_date(as_of)
    if frame is None or frame.is_empty():
        return empty

    df = frame.filter(pl.col("symbol").is_in(symbols))
    if df.is_empty():
        return empty

    # 叠加当日实时快照(quote_ts/close) —— 实时价只写 kline_daily, 不写 enriched。
    df = _overlay_market_realtime(repo, df, as_of, symbols)

    # 名称 + 股本: 都取自该市场维表 (A 股 instruments 池里没有港股行)
    name_map = repo.get_market_name_map(market)
    df = df.with_columns(
        pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8).alias("name"),
        pl.lit("stock").alias("asset_type"),
        # 港美股无涨跌停: 显式给 null, 前端渲染 "—" 而不是沿用 A 股价格笼子
        pl.lit(None, dtype=pl.Float64).alias("limit_up_price"),
        pl.lit(None, dtype=pl.Float64).alias("limit_down_price"),
    )
    if "float_shares" not in df.columns:
        inst = repo.get_instruments_market(market)
        if not inst.is_empty() and {"symbol", "float_shares"}.issubset(inst.columns):
            df = df.join(
                inst.select(["symbol", "float_shares"]).unique(subset=["symbol"], keep="first"),
                on="symbol", how="left",
            )

    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares", "asset_type"] if c in df.columns]
    df = df.select(keep)

    # 加入时间 + 加入以来涨跌幅 (与 cn 同口径: 加入日收盘价为基准)
    added_at_df = pl.DataFrame(
        {"symbol": symbols, "added_at": [(r.get("added_at") or None) for r in entries]},
        schema={"symbol": pl.Utf8, "added_at": pl.Utf8},
    )
    df = df.join(added_at_df, on="symbol", how="left")
    added_dates = _added_dates_bj(entries)
    base_df = _market_base_closes(repo, added_dates, market)
    if base_df.is_empty() or "close" not in df.columns:
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias("pct_since_added"))
    else:
        df = (
            df.join(base_df, on="symbol", how="left")
            .with_columns(
                pl.when(
                    pl.col("base_close").is_not_null()
                    & pl.col("base_close").is_finite()
                    & (pl.col("base_close") > 0)
                    & pl.col("close").is_not_null()
                )
                .then(pl.col("close") / pl.col("base_close") - 1.0)
                .otherwise(None)
                .alias("pct_since_added")
            )
            .drop("base_close")
        )

    # NaN/Inf 统一置 null, 前端不必逐列判断
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite()).then(None).otherwise(pl.col(c)).alias(c)
            for c in float_cols
        ])

    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(
        pl.col("symbol").replace_strict(order_map, default=len(symbols), return_dtype=pl.Int32).alias("_sort_order")
    ).sort("_sort_order").drop("_sort_order")

    rows = df.to_dicts()
    from app.services.quote_freshness import QuoteFreshness

    freshness = QuoteFreshness.for_request(request)
    # 港美股按市场时区/时段判定: 有 quote_ts 的实时快照显示 live, 仍是静态日K
    # 的行降级为 historical(static_daily)。A 股内存快照路径不变。
    for row in rows:
        row["quote_freshness"] = freshness.describe(
            row, market=market, static_daily=True,
        )
    return {
        "rows": rows,
        "as_of": str(as_of),
        "elapsed_ms": (time.perf_counter() - t0) * 1000,
        "dates": {"stock": str(as_of), "etf": None, "index": None},
        "market": market,
    }


@router.get("/enriched")
def watchlist_enriched(
    request: Request,
    ext_columns: str | None = Query(None, description="逗号分隔的 ext 列: config_id.field_name"),
    market: str = Query("cn", description="逻辑市场: cn/hk/us"),
):
    """自选 enriched 数据 — 直接从 enriched 最新日读取, 无重计算。

    仅两列为按行向量化现算 (不落盘): 涨跌停价, 以及「加入后涨跌幅」(pct_since_added,
    以加入日收盘价为基准)。本端点在行情 tick 热路径上被反复调用, 故不做历史 scan 兜底。

    ext_columns 参数示例: "industry_rating.score,fund_flow.net_inflow"
    会动态 LEFT JOIN 对应的 ext_{config_id} DuckDB view。

    market: cn 走 A 股内存快照 (原路径, 零改动); hk/us 走按市场即时计算路径。
    前端按当前市场传参, 保证切换市场后自选表不会混入其它市场的标的。
    """
    m = _validate_market(market) or "cn"
    if m != "cn":
        return _watchlist_enriched_market(request, m)

    t0 = time.perf_counter()

    repo = request.app.state.repo
    # 自选可跨市场持有: cn 路径只取 A 股条目, 否则港股行会以「指标全 null」的形式
    # 混进 A 股自选表 (共享存储切分后 enriched 里已没有港美股行)。
    from app.markets import market_of

    entries = [
        r for r in watchlist.list_symbols()
        if market_of(str(r.get("symbol", ""))) == "cn"
    ]
    symbols = [r["symbol"] for r in entries]
    if not symbols:
        return {"rows": [], "as_of": None, "elapsed_ms": 0,
                "dates": {"stock": None, "etf": None, "index": None}}

    # 按资产拆分自选 symbol; ETF enriched 是独立缓存, 仅自选真的含 ETF 才去加载
    # (避免无 ETF 用户在缓存冷启动时触发 ETF 全量懒加载)
    etf_set = repo.get_etf_symbol_set()
    index_set = repo.get_index_symbol_set()
    etf_symbols = [s for s in symbols if s in etf_set]
    index_symbols = [s for s in symbols if s not in etf_set and s in index_set]
    stock_symbols = [s for s in symbols if s not in etf_set and s not in index_set]

    df_e, cache_date = repo.get_enriched_latest()

    # 以自选列表为主表 LEFT JOIN enriched, 保证自选的每一只都返回一行;
    # 不在 enriched 缓存里的标的 (新股/冷门股/新用户未同步) 指标为 null, 前端渲染为 "—".
    # 旧实现是 df_e.filter(is_in(stock_symbols)), 方向反了 (以 enriched 为主),
    # 会把不在缓存 universe 里的自选静默丢弃.
    if stock_symbols:
        watchlist_df = pl.DataFrame({"symbol": stock_symbols})
        if df_e.is_empty():
            df = watchlist_df
        else:
            df = watchlist_df.join(_latest_watchlist_rows(df_e, stock_symbols), on="symbol", how="left")
    else:
        df = pl.DataFrame()

    # ETF 行合并; 缺失列 (换手率/涨跌停信号等) 为 null
    etf_date = None
    if etf_symbols:
        df_etf_all, etf_date = repo.get_enriched_latest_asset("etf")
        etf_watchlist_df = pl.DataFrame({"symbol": etf_symbols})
        if not df_etf_all.is_empty():
            # ETF 同样以自选为主表 LEFT JOIN, 缺失标的指标为 null
            df_etf = etf_watchlist_df.join(_latest_watchlist_rows(df_etf_all, etf_symbols), on="symbol", how="left")
        else:
            df_etf = etf_watchlist_df
        df = df_etf if df.is_empty() else pl.concat([df, df_etf], how="diagonal_relaxed")

    # 指数行合并 (镜像 ETF 分支); 缺失列 (换手率/涨跌停信号等) 为 null
    index_date = None
    if index_symbols:
        df_idx_all, index_date = repo.get_enriched_latest_asset("index")
        idx_watchlist_df = pl.DataFrame({"symbol": index_symbols})
        if not df_idx_all.is_empty():
            df_idx = idx_watchlist_df.join(_latest_watchlist_rows(df_idx_all, index_symbols), on="symbol", how="left")
        else:
            df_idx = idx_watchlist_df
        df = df_idx if df.is_empty() else pl.concat([df, df_idx], how="diagonal_relaxed")

    # as_of 取三类缓存中较旧者
    dates = [d for d in (cache_date if stock_symbols else None, etf_date, index_date) if d is not None]
    as_of = min(dates) if dates else None
    # 按资产类型的行情日期 (ISO): as_of 是三类取 min 的全局值, 分不清哪类过期。
    # 前端分时缩略图续画据此判定「该类行情是否为当日」—— ETF 未开实时拉取时
    # etf 缓存停在旧日, 过期 close 拼到当日分钟K尾部会画出错价 (issue 场景)。
    # None = 未加载/无缓存, 前端一律按不新鲜处理 (fail-closed)。
    quote_dates = {
        "stock": str(cache_date) if cache_date else None,
        "etf": str(etf_date) if etf_date else None,
        "index": str(index_date) if index_date else None,
    }
    if df.is_empty():
        return {"rows": [], "as_of": str(as_of) if as_of else None,
                "elapsed_ms": 0, "dates": quote_dates}

    # JOIN float_shares (仅股票有) + 名称 (股票/ETF 统一走 get_name_map)
    df_i = repo.get_instruments()
    if not df_i.is_empty() and "float_shares" in df_i.columns:
        shares = df_i.select(["symbol", "float_shares"]).unique(subset=["symbol"], keep="first")
        df = df.join(shares, on="symbol", how="left")
    name_map = repo.get_name_map(df["symbol"].to_list())
    df = df.with_columns(
        pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8).alias("name")
    )

    # 标注资产类型: 前端据此渲染徽标/豁免板块筛选/分时列降级
    asset_map = {**{s: "etf" for s in etf_symbols}, **{s: "index" for s in index_symbols}}
    df = df.with_columns(
        pl.col("symbol").replace_strict(asset_map, default="stock", return_dtype=pl.Utf8).alias("asset_type")
    )

    # 定向补齐仅作缺失行回退，不把旧日K注入全市场实时快照。
    prepared_reader = getattr(repo, "get_prepared_daily", None)
    if callable(prepared_reader):
        parts = []
        for asset, group in (("stock", stock_symbols), ("etf", etf_symbols), ("index", index_symbols)):
            missing = df.filter(pl.col("symbol").is_in(group))
            if "close" in missing.columns:
                missing = missing.filter(pl.col("close").is_null())
            if missing.height:
                fallback = prepared_reader(asset, missing["symbol"].to_list())
                if isinstance(fallback, pl.DataFrame) and not fallback.is_empty():
                    parts.append(_latest_watchlist_rows(fallback, missing["symbol"].to_list()).with_columns(
                        pl.lit(asset).alias("asset_type"), pl.lit(True).alias("prepared_daily"),
                    ))
        if parts:
            prepared = pl.concat(parts, how="diagonal_relaxed")
            df = pl.concat([df.filter(~pl.col("symbol").is_in(prepared["symbol"].to_list())), prepared], how="diagonal_relaxed")
            # 回退行也由维表统一补名称/股本。
            df = df.with_columns(pl.col("symbol").replace_strict(name_map, default=None, return_dtype=pl.Utf8).alias("name"))

    # 选择内置需要的列
    keep = [c for c in _WATCHLIST_COLS + ["name", "float_shares", "asset_type", "prepared_daily"] if c in df.columns]
    df = df.select(keep)

    # 涨跌停价 (交易所整数分半进位口径, 仅股票): prev_close/名称已在行上, 对自选的
    # 几十~几百行向量化现算为亚毫秒级, 不写回 enriched 存储。ETF (跨境/债券 5% 等)
    # 与指数的涨跌幅规则不在 price_limits 覆盖内, 置 null 由前端渲染 "—"。
    as_of_date = as_of if isinstance(as_of, date) else None
    if as_of_date is None and as_of:
        try:
            as_of_date = date.fromisoformat(str(as_of)[:10])
        except ValueError:
            as_of_date = None
    if {"symbol", "prev_close", "name", "asset_type"}.issubset(df.columns) and as_of_date is not None:
        pct = polars_price_limit_pct(
            pl.col("symbol"),
            pl.lit(as_of_date),
            polars_is_risk_warning_name(pl.col("name")),
        )
        stock_with_prev = (pl.col("asset_type") == "stock") & pl.col("prev_close").is_not_null()
        df = df.with_columns(
            pl.when(stock_with_prev)
            .then(polars_limit_price(pl.col("prev_close"), pct, up=True))
            .otherwise(None)
            .alias("limit_up_price"),
            pl.when(stock_with_prev)
            .then(polars_limit_price(pl.col("prev_close"), pct, up=False))
            .otherwise(None)
            .alias("limit_down_price"),
        )

    # 「加入日期」与「加入以来」— 与涨跌停价同为读时现算, 不落盘。只在这一处挂一次即
    # 覆盖 stock/etf/index 三分支。pct_since_added 为小数口径 (与 momentum_* 一致)。
    try:
        added_at_df = pl.DataFrame(
            {
                "symbol": symbols,
                # 旧 schema 迁移补的空串归一为 null, 前端只需判断一种「缺失」
                "added_at": [(r.get("added_at") or None) for r in entries],
            },
            schema={"symbol": pl.Utf8, "added_at": pl.Utf8},
        )
        with_base = df.join(added_at_df, on="symbol", how="left")
        base_df = _added_base_closes(repo, _added_dates_bj(entries), set(stock_symbols))
        if base_df.is_empty():
            # 无基准价 (缓存冷 / 全为 ETF 或超窗): 整列降级为 null
            df = with_base.with_columns(pl.lit(None, dtype=pl.Float64).alias("pct_since_added"))
        else:
            # 整链算完再回绑 df, 异常时 df 保持原样
            df = (
                with_base.join(base_df, on="symbol", how="left")
                .with_columns(
                    pl.when(
                        pl.col("base_close").is_not_null()
                        & pl.col("base_close").is_finite()
                        & (pl.col("base_close") > 0)
                        & pl.col("close").is_not_null()
                    )
                    .then(pl.col("close") / pl.col("base_close") - 1.0)
                    .otherwise(None)
                    .alias("pct_since_added")
                )
                .drop("base_close")
            )
    except Exception as e:
        # 装饰列失败不得影响自选主表; 两列必须存在 (值可为 null), 否则前端排序/筛选静默失效
        logger.warning("自选加入日期/加入后涨跌幅计算跳过: %s", e, exc_info=True)
        df = df.with_columns(
            pl.lit(None, dtype=pl.Utf8).alias("added_at"),
            pl.lit(None, dtype=pl.Float64).alias("pct_since_added"),
        )

    # 动态 JOIN 扩展数据表
    ext_specs = _parse_ext_columns(ext_columns) if ext_columns else []
    if ext_specs:
        db = repo.store.db
        data_dir = repo.store.data_dir
        from app.api.ext_data import _read_ext_dataframe
        from app.services.ext_data import ExtConfigStore, collapse_ext_rows_at

        ext_store = ExtConfigStore(data_dir)
        configs = {c.id: c for c in ext_store.load_all()}

        for config_id, field_name, at in ext_specs:
            view_name = f"ext_{config_id}"
            ext_col_name = f"{config_id}__{field_name}"
            try:
                # 扩展时序数据必须只取最新分区；否则一个 symbol 会按历史分区数被 JOIN 放大。
                cfg = configs.get(config_id)
                if cfg:
                    ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                else:
                    ext_df = pl.from_arrow(db.query(
                        f"SELECT symbol, {quote_ident(field_name)} FROM {view_name}"
                    ).arrow())
                if not ext_df.is_empty() and "symbol" in ext_df.columns:
                    time_field = cfg.pull.time_field if cfg and cfg.pull else None
                    # 收敛在 select 之前: 时间列被裁掉后 collapse 会静默回退末行
                    ext_df = (
                        collapse_ext_rows_at(ext_df, at, time_field)
                        .select(["symbol", field_name])
                        .rename({field_name: ext_col_name})
                    )
                    df = df.join(ext_df.select(["symbol", ext_col_name]), on="symbol", how="left")
            except Exception:
                # view 不存在或字段不存在，尝试直接读 parquet
                cfg = configs.get(config_id)
                if cfg:
                    try:
                        ext_df, _ = _read_ext_dataframe(cfg, data_dir)
                        if not ext_df.is_empty() and "symbol" in ext_df.columns and field_name in ext_df.columns:
                            ext_df = (
                                ext_df
                                .select(["symbol", field_name])
                                .unique(subset=["symbol"], keep="last")
                                .rename({field_name: ext_col_name})
                            )
                            df = df.join(ext_df, on="symbol", how="left")
                    except Exception as e2:
                        logger.debug("ext join fallback failed for %s.%s: %s", config_id, field_name, e2)

    # 最终输出也保证每标的一行，兼容旧名单或附加数据中的重复记录。
    df = df.unique(subset=["symbol"], keep="first", maintain_order=True)

    # sanitize NaN / Inf
    float_cols = [c for c in df.columns if df[c].dtype.is_float()]
    if float_cols:
        df = df.with_columns([
            pl.when(pl.col(c).is_nan() | pl.col(c).is_infinite())
              .then(None)
              .otherwise(pl.col(c))
              .alias(c)
            for c in float_cols
        ])

    # 按自选添加顺序（新加的在前）重排行
    order_map = {s: i for i, s in enumerate(symbols)}
    df = df.with_columns(pl.col("symbol").map_elements(lambda s: order_map.get(s, len(symbols)), return_dtype=pl.Int32).alias("_sort_order"))
    df = df.sort("_sort_order").drop("_sort_order")

    rows = df.to_dicts()
    from app.services.quote_freshness import QuoteFreshness

    freshness = QuoteFreshness.for_request(request)
    for row in rows:
        row["quote_freshness"] = freshness.describe(row)
    elapsed = (time.perf_counter() - t0) * 1000
    return {"rows": rows, "as_of": str(as_of) if as_of else None,
            "elapsed_ms": elapsed, "dates": quote_dates}


def _parse_ext_columns(ext_columns: str) -> list[tuple[str, str, str | None]]:
    """解析并去重扩展列；同一输出列存在歧义时返回 400。"""
    from app.services.ext_data import parse_ext_columns

    try:
        return parse_ext_columns(ext_columns)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
