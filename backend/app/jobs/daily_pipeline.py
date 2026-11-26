"""盘后管道 + 盘前维表同步。

调度:
  09:10 盘前 — 同步个股维表 instruments (全量覆盖)
  15:35 盘后 — 日K同步 + 增量除权因子 + enriched 计算 + 刷新视图
  (默认 15:35: 盘后固定价 15:30 终止 + 供应商日线定稿缓冲, 见 preferences)

盘后同步策略:
  日 K: QuoteService 交易时段已实时落盘 → 有数据时跳过 batch,首次拉 1 年区间
  除权因子: 从已有数据最新日期的下一天开始增量获取,避免重复拉取和计算
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date as _date
from datetime import timedelta as _td
from pathlib import Path

import polars as pl
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.indicators.pipeline import filter_halt_days, run_pipeline
from app.market_time import cn_today
from app.services import index_sync, instrument_sync, kline_sync
from app.services import preferences as _prefs
from app.tickflow.capabilities import Cap, CapabilitySet
from app.tickflow.pools import get_pool
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

ProgressCb = Callable[..., None]


def _prune_partial_enriched_partitions(daily_dir: Path, enriched_dir: Path) -> list[str]:
    """删除 symbol 覆盖不完整的 enriched 日期分区, 返回被删的日期 (#223)。

    自选实时路径会在全市场 enriched 生成前提前创建当日分区 (只有几只自选),
    仅按日期目录计数比较会把它误判为完整分区而跳过计算, 造成日K缺失与
    均线错误。按与加工相同的停牌过滤口径检查 symbol 覆盖, 不能直接比较
    行数: 正常剔除停牌记录会让 enriched 少行, 导致每次管道都删除重算。
    删除后 run_pipeline(new_dates_only=True) 会把它们当"新日期"全市场补齐。
    daily 同日分区不存在 (今日日K尚未同步) 时不处理, 留给当日正常流程。
    """
    import shutil

    pruned: list[str] = []
    for part in enriched_dir.glob("date=*"):
        daily_part = daily_dir / part.stem
        if not daily_part.exists():
            continue
        try:
            expected: set[str] = set()
            # 每次只读单文件的停牌判定列, 不加载全历史或指标宽表。
            for path in daily_part.glob("*.parquet"):
                schema = pl.read_parquet_schema(path)
                if not {"symbol", "open", "high"}.issubset(schema):
                    raise ValueError("daily 缺少 symbol/open/high, 无法判断有效标的覆盖")
                columns = [c for c in ("symbol", "open", "high", "volume", "amount") if c in schema]
                daily = pl.read_parquet(path, columns=columns)
                expected.update(filter_halt_days(daily)["symbol"].drop_nulls().to_list())
            actual: set[str] = set()
            for path in part.glob("*.parquet"):
                actual.update(pl.read_parquet(path, columns=["symbol"])["symbol"].drop_nulls().to_list())
        except Exception as e:
            logger.warning("enriched 覆盖检查跳过 %s, 保留分区: %s", part.name, e)
            continue
        if expected - actual:
            shutil.rmtree(part, ignore_errors=True)
            pruned.append(part.stem.split("=")[1])
    return pruned


def _prune_stale_price_partitions(
    daily_dir: Path, enriched_dir: Path, max_dates: int = 5
) -> list[str]:
    """删除收盘价与官方日线不一致的 enriched 日期分区。

    实时 flush 写入的当日分区行数与 daily 相同, 但收盘价可能停留在收盘集合
    竞价前的快照 (实测: TickFlow 实时端点收盘后仍长期返回旧价, 3392/5554 只
    股票当日收盘价与官方日线不符), #223 的行数校验识别不到。对最近若干交易日
    做值级比对: enriched.raw_close 与 daily.close 任一标的差超过半个最小报价
    单位即删分区, 由后续增量重算按官方日线全市场重建。
    """
    import shutil

    common = sorted(
        (
            p.stem.split("=", 1)[1]
            for p in enriched_dir.glob("date=*")
            if (daily_dir / p.stem).exists()
        ),
        reverse=True,
    )[:max_dates]
    pruned: list[str] = []
    for ds in common:
        try:
            daily = pl.read_parquet(
                daily_dir / f"date={ds}" / "*.parquet", columns=["symbol", "close"]
            )
            enr = pl.read_parquet(
                enriched_dir / f"date={ds}" / "*.parquet", columns=["symbol", "raw_close"]
            )
        except Exception:
            continue  # 列缺失/不可读 → 交给既有完整性检查兜底
        joined = enr.join(daily, on="symbol", how="inner").drop_nulls()
        if joined.is_empty():
            continue
        bad = joined.filter((pl.col("raw_close") - pl.col("close")).abs() > 0.005)
        if not bad.is_empty():
            shutil.rmtree(enriched_dir / f"date={ds}", ignore_errors=True)
            pruned.append(ds)
    return pruned


class PipelineStageError(RuntimeError):
    """管道有阶段软失败(数据可能陈旧)时抛出, 让上层 job_store 把任务标记为 failed。

    这些阶段单独 try/except 吞掉异常以不中断整条管道, 但一旦失败即代表对应数据陈旧。
    抛出前进度协议已走完(done/100), 故前端进度条正常收尾, 仅终态如实反映为 failed ——
    不再"部分失败却报成功"。
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("盘后管道部分阶段失败: " + "; ".join(errors))


def _noop(stage: str, pct: int, msg: str, **kwargs) -> None:  # noqa: ARG001
    pass


def _workday_gap(from_date: _date, to_date: _date) -> int:
    """统计 (from_date, to_date] 内的工作日数（周末不计）。

    用于判断同步后数据是否落后于请求区间终点。港美股本地节假日与 A 股不完全
    一致，极端情况下会误报；此处仅作提示，不改变任务成功状态。
    """
    if from_date >= to_date:
        return 0
    gap = 0
    day = from_date + _td(days=1)
    while day <= to_date:
        if day.weekday() < 5:
            gap += 1
        day += _td(days=1)
    return gap


def _invalidate(table: str | None = None) -> None:
    """stage 写完调用,让 /api/data/status 只重算被影响的那张表。"""
    from app.api.data import invalidate_data_cache
    invalidate_data_cache(table)


def _resolve_universe(capset: CapabilitySet, repo=None) -> list[str]:
    """解析标的池 — 本地 instruments 维表 + 自选 (单一来源, 无池兜底)。

    不再拉 CN_Equity_A 池: 其 universe 匹配曾错配到 Expired 变体、触发
    SW1 行业聚合兜底, 产出 4224 只的残缺池。维表由盘后管道 Step 0 先同步,
    这里只读 parquet; 维表缺失时仅剩自选, 宁缺毋假 (不塞演示标的),
    由上游按空池结果如实呈现。

    repo 传入时过滤自选里的指数 symbol (指数日K走独立 kline_index_* 存储,
    进股票池会污染 kline_daily/kline_minute)。ETF 刻意保留 (既有行为)。
    """
    base: set[str] = set(get_pool("watchlist"))
    inst_path = Path(settings.data_dir) / "instruments" / "instruments.parquet"
    if inst_path.exists():
        try:
            inst = pl.read_parquet(inst_path, columns=["symbol"])
            base.update(inst["symbol"].to_list())
        except Exception as e:  # noqa: BLE001
            logger.warning("instruments 维表读取失败, 标的池仅剩自选: %s", e)
    else:
        logger.warning("instruments 维表缺失, 标的池仅剩自选 %d 只", len(base))
    if repo is not None:
        base -= set(repo.get_index_symbol_set())
    return sorted(base)


def run_instruments_sync(repo: KlineRepository) -> dict:
    """盘前同步个股维表。

    维表含当日涨跌停价 (limit_up/down), 同步完成后刷新 enriched 内存缓存,
    确保跨天后连板梯队/策略等读到的是基于最新维表的数据 (而非前一交易日残留)。
    """
    rows = instrument_sync.sync_instruments(repo.store.data_dir)
    _refresh_instruments_view(repo)
    _invalidate("instruments")
    # 维表更新后重建 enriched 缓存 (clear + refresh, 与设置页「清理并刷新」同等效果)
    if rows > 0:
        repo.clear_cache()
        repo.refresh_cache()
    return {"instruments_rows": rows}


def _market_sync_state_path(data_dir, market: str) -> Path:
    return Path(data_dir) / f".market_sync_{market}.json"


def _load_synced_symbols(data_dir, market: str) -> set[str]:
    """已成功同步过日K的标的（港美股按市场记账）。

    免费档拉到全市场美股需要上小时, 同步支持 limit 分次推进。没有这份账本时
    每次 limit 都取同样的一批（按代码排序靠前的）, 反复点「更新」永远只覆盖
    前 N 只; 有了它就能把「还没同步过」的标的排在前面, 分几次把覆盖面铺满。
    """
    import json

    try:
        payload = json.loads(_market_sync_state_path(data_dir, market).read_text())
        symbols = payload.get("symbols") or []
        return {str(s) for s in symbols if s}
    except Exception:  # noqa: BLE001 — 账本损坏等价于「没同步过」, 不阻塞同步
        return set()


def _save_synced_symbols(data_dir, market: str, symbols: list[str]) -> None:
    import json

    path = _market_sync_state_path(data_dir, market)
    merged = _load_synced_symbols(data_dir, market) | set(symbols)
    tmp = path.with_suffix(".json.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"market": market, "symbols": sorted(merged)}))
        tmp.replace(path)
    except Exception as e:  # noqa: BLE001 — 仅影响下次排序, 不影响数据正确性
        logger.warning("%s 同步账本写入失败: %s", market, e)


def run_market_sync(
    market: str,
    repo: KlineRepository,
    on_progress: ProgressCb | None = None,
    count_days: int = 400,
    capset: CapabilitySet | None = None,
    max_symbols: int | None = None,
    refresh_synced: bool = False,
    sync_indices: bool = True,
) -> dict:
    """港美股数据同步 + enriched 计算（多市场扩展）。

    流程: 个股维表(market 过滤) → 日K(前复权批量拉取) → enriched 计算。
    A 股走 run_now；本函数服务港美股，供 POST /api/pipeline/run-market 按需触发。

    本地存储口径适配（现状适配，非照搬参考目录结构）: 本地 A 股只有单一 stock 日K/
    enriched 存储，无港美股独立目录；此处把 hk/us 日K 写入同一 kline_daily /
    kline_daily_enriched，由既有 run_pipeline 统一计算 enriched（merge-upsert，
    只对指定市场标的重算，不触碰 A 股）。参考实现的 kline_daily_{hk,us} 目录拆分
    与本地现状冲突，按 Team 工作准则「现状适配」改为共享存储。

    返回: {"universe": n, "daily_rows": n, "enriched_rows": n, "skipped": [...]}

    capset: 可选能力集，用于按当前档位取日K批量大小/速率；缺省沿用 100/60。
    max_symbols: 可选上限，只同步前 N 只标的（试跑/受限于带宽时用）。
    refresh_synced: 增量模式。只刷新「账本里已同步过」的标的，不再优先拉未同步标的。
        供定时任务使用: 每日只补最近几天, 覆盖面的扩张仍由手动同步推进
        （否则每次 cron 都要把 1.2 万只美股全量拉一遍）。
    sync_indices: 是否顺带同步该市场主要指数（港美股指数不走 TickFlow，见
        services.index_sync_market）。失败只记 skipped, 不阻断个股同步。
    """
    from datetime import datetime as _dt, timedelta as _td

    emit = on_progress or _noop
    skipped: list[str] = []

    # Step -1: 该市场主要指数（腾讯免费源）。指数与个股互不依赖, 失败不影响个股同步;
    # 之前只有 /api/indices/market 的 POST 会触发, 定时/手动同步个股后指数永远停在
    # 首次点击那天, 指数页显示的是陈旧收盘。
    if sync_indices:
        emit("sync_indices", 2, f"同步{market}主要指数…")
        try:
            from app.services import index_sync_market
            idx_rows = index_sync_market.sync_market_indices(market, repo.store.data_dir)
            emit("sync_indices", 4, f"{market} 指数同步完成,{idx_rows} 行")
        except Exception as e:  # noqa: BLE001 — 指数只是展示层, 不影响个股管道
            logger.warning("%s 指数同步失败: %s", market, e)
            skipped.append("indices")

    # Step 0: 该市场个股维表（单独存放，不污染 A 股 stock 池）
    # 维表缺失时自动同步一次：港美股维表由 instrument_sync.sync_market_instruments
    # 写入 instruments_index_{market}/，此前没有任何调用方，导致 run-market 恒返回
    # 「无标的」——这正是切到港美股后处处空白的原因。
    inst = repo.get_instruments_market(market)
    if inst.is_empty() or "symbol" not in inst.columns:
        emit("sync_instruments", 3, f"同步{market}个股维表…")
        try:
            inst_rows = instrument_sync.sync_market_instruments(repo.store.data_dir, market)
            emit("sync_instruments", 8, f"{market} 维表同步完成,{inst_rows} 只标的")
        except Exception as e:  # noqa: BLE001 — 上游不可用时如实回报, 不静默
            logger.warning("%s 个股维表同步失败: %s", market, e)
            skipped.append("instruments")
        inst = repo.get_instruments_market(market)

    universe: list[str] = []
    if not inst.is_empty() and "symbol" in inst.columns:
        from app.markets import filter_symbols
        inst = filter_symbols(inst, market)
        universe = sorted(inst["symbol"].to_list())
    if not universe:
        emit("resolve_universe", 10, f"{market} 无标的（个股维表为空，请稍后重试）")
        return {"universe": 0, "daily_rows": 0, "enriched_rows": 0,
                "skipped": [*skipped, "universe"]}
    total_instruments = len(universe)
    if refresh_synced:
        # 增量: 只碰已同步过的标的。一只都没同步过时退回全量, 否则定时任务永远空跑。
        synced = _load_synced_symbols(repo.store.data_dir, market)
        if synced:
            universe = [s for s in universe if s in synced]
            if not universe:
                return {"universe": 0, "daily_rows": 0, "enriched_rows": 0,
                        "skipped": [*skipped, "universe"],
                        "coverage": {"synced": len(synced), "instruments": total_instruments}}
    if max_symbols and len(universe) > max_symbols:
        # 未同步过的优先, 让「分次同步」自然推进覆盖面而不是每次都拉同一批。
        synced = _load_synced_symbols(repo.store.data_dir, market)
        fresh = [s for s in universe if s not in synced]
        stale = [s for s in universe if s in synced]
        universe = (fresh + stale)[:max_symbols]
    emit("resolve_universe", 10, f"{market} 标的池:{len(universe)} 只"
         + ("（增量刷新已同步标的）" if refresh_synced else "")
         + (f"（维表共 {total_instruments} 只）" if total_instruments > len(universe) else ""))

    def _coverage() -> dict:
        """本市场日K覆盖进度: 已同步过 / 维表总数。供前端提示「还没同步完」。"""
        return {
            "synced": len(_load_synced_symbols(repo.store.data_dir, market)),
            "instruments": total_instruments,
        }

    # Step 1: 日K（forward 前复权，港美股无除权因子表）
    end = _dt.now()
    start = end - _td(days=count_days)
    emit("sync_daily", 12, f"获取 {market} 日K [{start.date()} ~ {end.date()}]…")

    batch_size, rpm = 100, 60
    if capset is not None:
        try:
            from app.tickflow.capabilities import Cap
            from app.tickflow.rate_limits import resolve_limit
            if capset.has(Cap.KLINE_DAILY_BATCH):
                lim = resolve_limit(capset, Cap.KLINE_DAILY_BATCH, default_batch=100)
                batch_size, rpm = lim.batch, lim.rpm
        except Exception as e:  # noqa: BLE001 — 能力集异常只影响速率, 不影响正确性
            logger.warning("读取日K批量上限失败, 沿用默认 100/60: %s", e)

    def _chunk(cur: int, tot: int) -> None:
        emit("sync_daily", 12 + int(30 * cur / tot),
             f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    failed: list[str] = []
    df = kline_sync.sync_daily_batch(
        universe,
        batch_size=batch_size, rpm=rpm,
        start_time=start, end_time=end,
        on_chunk_done=_chunk,
        adjust="forward",
        failed_out=failed,
    )
    if failed:
        skipped.append("daily_partial")
    if df.is_empty():
        emit("sync_daily", 45, f"{market} 日K 未获取到数据")
        return {"universe": len(universe), "daily_rows": 0, "enriched_rows": 0,
                "skipped": [*skipped, "daily"], "coverage": _coverage()}
    repo.append_daily(df)
    daily_rows = df.height
    _save_synced_symbols(repo.store.data_dir, market, sorted(set(df["symbol"].to_list())))
    emit("sync_daily", 45, f"{market} 日K 写入 {daily_rows} 行")
    repo.refresh_cache()

    # 缺口告警: 对比「实际落地最新交易日」与「请求区间终点」。
    # 曾出现任务报成功(写入行数正常)但当天个股K线未发布,落地停在上一交易日,
    # 自选因而显示过期数据 —— 此处把差值显式记入日志与 result,避免误判。
    fetched_latest = df["date"].max()
    gap_weekdays = _workday_gap(fetched_latest, end.date())
    freshness = {
        "requested_end": end.date().isoformat(),
        "fetched_latest": fetched_latest.isoformat(),
        "gap_weekdays": gap_weekdays,
    }
    if gap_weekdays > 0:
        msg = (f"{market} 日K 落后请求区间终点 {gap_weekdays} 个工作日: "
               f"最新 {fetched_latest}, 请求至 {end.date()}"
               "（数据源可能尚未发布当日数据，自选将展示已落地的最新交易日）")
        logger.warning("market sync freshness gap: %s", msg)
        emit("sync_daily", 46, msg)

    # Step 2: enriched 计算（只重算该市场标的，merge-upsert，不触碰 A 股）
    emit("compute_enriched", 60, f"计算 {market} enriched 指标…")
    try:
        enriched_rows = run_pipeline(
            symbols=universe,
            market=market,
            on_batch_done=lambda cur, tot: emit(
                "compute_enriched", 60 + int(33 * cur / tot),
                f"{market} enriched 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True),
        )
    except Exception:  # noqa: BLE001
        logger.exception("%s enriched 计算失败", market)
        skipped.append("enriched")
        enriched_rows = 0
    emit("compute_enriched", 90, f"{market} enriched 完成,{enriched_rows} 行")
    repo.refresh_cache()
    # 选股侧有进程内 TTL 缓存 (历史窗口 + 港美股单日帧), 同步完成后立即失效,
    # 否则页面最多 1 分钟内仍显示同步前的空结果。
    try:
        from app.services.screener import ScreenerService
        ScreenerService.clear_history_cache()
    except Exception as e:  # noqa: BLE001 — 缓存清理失败不影响已落盘的数据
        logger.warning("清理选股缓存失败: %s", e)

    # 刷新 DuckDB 视图（新 parquet 已写入）
    try:
        repo.store._register_views()  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        logger.warning("refresh views failed: %s", e)
    emit("done", 100, f"{market} 管道完成")
    return {"market": market, "universe": len(universe), "daily_rows": daily_rows,
            "enriched_rows": enriched_rows, "skipped": skipped,
            "coverage": _coverage(), "freshness": freshness}


def run_now(
    repo: KlineRepository,
    capset: CapabilitySet,
    on_progress: ProgressCb | None = None,
    override_start_date: _date | None = None,
    override_symbols: list[str] | None = None,
) -> dict:
    """立即执行一次盘后管道,支持进度回调。

    跳过的 stage **不 emit**,避免前端把"无 capability"的卡片错误标记为 active/done。
    result 里带 skipped_stages 列表供前端展示。

    override_start_date: 传入时强制走 batch 拉取分支,用该日期作为日K/除权/指数的
        拉取起点(到今天),用于「数据修正/补数据」场景。None 时走原有自动判定逻辑。
    override_symbols: 数据修正时只重拉这些股票的日K (issue #514: 修复只补坏标的,
        不再全市场重拉)。None = 全标的池。仅作用于日K (kline_daily) 阶段;
        ETF/指数族沿用各自的全量口径。
    """
    emit = on_progress or _noop
    skipped: list[str] = []
    # 阶段软失败累积: 下列阶段 try/except 吞异常以不中断管道, 但失败即代表数据可能陈旧。
    # 管道末尾若非空则抛 PipelineStageError, 让任务终态如实标记为 failed(而非误报成功)。
    stage_errors: list[str] = []

    # Step 0: 先同步个股维表, 再解析标的池 — 确保标的池基于最新 instruments
    emit("sync_instruments", 2, "同步个股维表…")
    inst_rows = instrument_sync.sync_instruments(repo.store.data_dir)
    if inst_rows > 0:
        _refresh_instruments_view(repo)
    emit("sync_instruments", 8, f"个股维表同步完成,{inst_rows} 只标的")
    _invalidate("instruments")

    emit("resolve_universe", 9, "解析标的池…")
    universe = _resolve_universe(capset, repo)
    emit("resolve_universe", 10, f"标的池规模:{len(universe)} 只")

    # 已知异常不能先进入指标计算, 再在末尾才报失败。修补与自检页面共用链路。
    from app.services.integrity_check import repair_known_data
    repair_known_data(repo, capset, on_progress=emit)

    # Step 1: 日 K 同步
    #   override_start_date 传入 → 强制 batch 拉取 [override_start_date ~ today] (数据修正)
    #   付费档 + 今天有数据 → 实时行情接口拉一次覆写（1请求全市场）
    #   有历史数据 → batch K-line API 补齐缺口
    #   无任何数据 → batch K-line API 拉首次 1 年
    from datetime import date as _date
    from datetime import datetime as _dt
    from datetime import timedelta as _td
    latest_daily = repo.latest_daily_date()
    # 管道「今天」必须是北京日期: 美西主机 15:35 北京时间仍是本地昨天,
    # date.today() 会把昨日日K当成已齐, 当日官方收盘价永远拉不进来。
    today = cn_today()
    today_exists = latest_daily and latest_daily >= today
    new_daily_days = 0

    # 完整性自愈: 检测最近交易日的盘中快照/缺口 (盘中停机后次日开实时会留下
    # 中午快照, 而下方"今天已有数据→只刷今天"分支会让它永久留存)。
    # 命中 → 本次管道放弃实时覆写分支, 降级为从最早坏日起的范围拉取。
    integrity_issues: list = []
    stale_day: _date | None = None
    etf_stale_day: _date | None = None
    index_stale_day: _date | None = None
    if override_start_date is None:
        try:
            from app.services import data_integrity
            integrity_issues = data_integrity.scan_recent_integrity(
                repo.store.data_dir, today=today,
            )
            integrity_issues = [i for i in integrity_issues if i.table in data_integrity.active_daily_tables()]
            if integrity_issues:
                stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_daily",))
                etf_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_etf_daily",))
                index_stale_day = data_integrity.earliest_issue_day(integrity_issues, ("kline_index_daily",))
                logger.warning(
                    "integrity: 检测到 %d 个不完整分区(%s), 本次管道改走范围拉取修复",
                    len(integrity_issues), data_integrity.describe_issues(integrity_issues),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("integrity scan failed (soft, 按无坏数据处理): %s", e)
            integrity_issues = []
    # 日K范围拉取的起点(分支3补缺口/分支4首次/数据修正); 实时增量/跳过时为 None。
    # 供 Step 1.5 除权因子回溯范围对齐: 范围拉取→用日K范围, 非范围→最近N天兜底。
    daily_range_start: _date | None = None

    # A 股日K拉取开关(默认开);关闭时跳过日K同步,保留已有数据。
    # 数据修正(override_start_date)时即使关闭开关也强制拉取 — 修正就是来补数据的。
    pull_a_share = _prefs.get_pipeline_pull_a_share()
    if not pull_a_share and not override_start_date:
        emit("sync_daily", 45, "已跳过 A 股日K同步(拉取内容未勾选)")
        logger.info("sync_daily: skipped (pipeline_pull_a_share=False)")
    elif override_start_date:
        # 数据修正: 强制用传入日期作起点 batch 拉取, 忽略实时行情覆写分支。
        start_date = override_start_date
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] repair/override", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        # 完整性修复带了坏 symbol 清单时只拉这些标的 (issue #514);
        # 单票失败也要上浮 (failed_out), 不能只留 WARNING。
        symbols_for_repair = list(override_symbols) if override_symbols else universe
        repair_failed_symbols: list[str] = []
        written_daily = kline_sync.sync_and_persist_daily_batch(
            symbols_for_repair, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
            failed_out=repair_failed_symbols,
        )
        if override_symbols:
            emit("sync_daily", 45,
                 f"日K 完成(定向修复 {len(symbols_for_repair)} 只, 失败 {len(repair_failed_symbols)} 只)")
        else:
            gap_days = (today - start_date).days
            new_daily_days = gap_days
            emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info(
            "sync_daily: [%s ~ %s] done, symbols=%d(%s) failed=%d",
            start_date, today, len(symbols_for_repair),
            "targeted" if override_symbols else "universe",
            len(repair_failed_symbols),
        )
        if repair_failed_symbols:
            preview = ", ".join(repair_failed_symbols[:10])
            more = "" if len(repair_failed_symbols) <= 10 else f" 等 {len(repair_failed_symbols)} 只"
            stage_errors.append(f"日K修复有 {len(repair_failed_symbols)} 只标的拉取失败: {preview}{more}")
    elif (
        today_exists
        and stale_day is None
        and capset.has(Cap.QUOTE_POOL)
        and _prefs.get_daily_data_provider() == "tickflow"
    ):
        # 付费档:今天有数据(QuoteService 已落盘)→ 实时行情覆写,确保最新。
        # stale_day 非空时禁用本分支: "只刷今天"会让停机日的盘中快照永久留存,
        # 降级到下方 batch 路径从坏日起重拉。
        # free/none 档无 quote.pool 能力,即便今天已有数据(如从 expert 降级),
        # 也降级到下方 batch 路径刷新,避免调用无权限的实时行情接口。
        emit("sync_daily", 12, f"获取日K [{today} ~ {today}] 实时行情…")
        written_daily = kline_sync.sync_daily_by_quotes(repo)
        new_daily_days = 1
        emit("sync_daily", 45, f"日K 完成,{written_daily} 只标的")
        logger.info("sync_daily: [%s ~ %s] live quotes, %d symbols", today, today, written_daily)
    elif latest_daily or stale_day:
        # 有历史 → batch 补齐缺口。
        # 也覆盖"今天已有数据但无实时行情权限(free/none)"的降级场景:
        #   此时 start_date = latest_daily = today,batch 刷新当天日K。
        # 完整性修复场景: start_date = min(本地最新日, 最早坏日) —
        #   today_exists 时 latest_daily=今天, 不取 min 会漏掉坏日。
        start_date = min(d for d in (latest_daily, stale_day) if d is not None)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] %s", start_date, today,
                    "refresh today" if today_exists else "gap fill")

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        gap_days = (today - start_date).days
        new_daily_days = gap_days
        emit("sync_daily", 45, f"日K 完成,覆盖 {gap_days} 天")
        logger.info("sync_daily: [%s ~ %s] done, %d days", start_date, today, gap_days)
    else:
        # 首次：无任何数据 → batch 拉 1 年
        start_date = today - _td(days=365)
        daily_range_start = start_date
        emit("sync_daily", 12, f"获取日K [{start_date} ~ {today}]…")
        logger.info("sync_daily: [%s ~ %s] initial fetch", start_date, today)

        def _daily_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_daily", 12 + int(33 * cur / tot),
                 f"日K 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_daily = kline_sync.sync_and_persist_daily_batch(
            universe, repo, capset,
            start_date=_dt.combine(start_date, _dt.min.time()),
            end_date=_dt.combine(today, _dt.min.time()),
            on_chunk_done=_daily_chunk_progress,
        )
        new_daily_days = 365
        emit("sync_daily", 45, "日K 完成")
        logger.info("sync_daily: [%s ~ %s] done", start_date, today)
    _invalidate("daily")

    # 完整性修复时删除股票 enriched 的坏分区: 增量重算只算 enriched 里不存在
    # 的日期, 盘中快照日分区已存在(虽是错的), 不删永远不会被重算。删除后
    # Step 2 把这些日期当"新日期"重算 (剩余分区最近 60 天做历史前缀, 窗口 ≤5 天回看充足)。
    repair_start = override_start_date if override_start_date is not None else stale_day
    if repair_start is not None:
        try:
            from app.services.data_integrity import prune_enriched_partitions
            pruned = prune_enriched_partitions(
                repo.store.data_dir, repair_start, "kline_daily_enriched",
            )
            if pruned:
                logger.info("integrity: 已删除 %d 个待重算的 enriched 分区 (≥ %s)", pruned, repair_start)
        except Exception as e:  # noqa: BLE001
            logger.warning("enriched prune failed (soft): %s", e)


    # 单标的新鲜度: 全局 max(date) 会被任一有今日数据的标的"拉高", 掩盖停牌/复牌/
    # 一直拉失败而掉队的个股缺口(全局判据只刷"今天", 永不回补掉队标的的历史缺口)。
    # 这里检测并**可见化**(WARNING + 计入结果), 让掉队标的不再隐形。
    # (自动回补暂不做 —— 需带退市判定, 否则对已退市标的每轮空拉浪费 API 额度。)
    lagging_symbols: list[str] = []
    if pull_a_share and latest_daily:
        try:
            lagging_symbols = repo.symbols_lagging(today, min_gap_days=3)
            if lagging_symbols:
                logger.warning("日K新鲜度: %d 只标的落后 >3 日 (停牌/退市/拉取失败; 样例: %s)",
                               len(lagging_symbols), lagging_symbols[:10])
        except Exception as e:  # noqa: BLE001
            logger.warning("laggard detection failed: %s", e)
            stage_errors.append(f"laggard detection: {e}")

    # Step 1.5: 同步除权因子 — 范围与日K拉取方式对齐
    #   日K范围拉取(补缺口/首次) → 除权用日K范围 [daily_range_start, now]
    #     首次会覆盖整个日K区间内的历史除权事件; 补缺口天然只增量(起点=latest_daily≈昨天)
    #   日K实时增量/跳过(分支2/分支1) → 除权兜底拉最近 30 天, 补可能遗漏的新除权
    #     (这两类分支不拉历史日K, 除权不能用日K范围, 只能兜底最近几日)
    written_adj = 0
    affected_symbols: list[str] = []
    adj_provider = _prefs.get_adj_factor_provider()
    can_sync_adj = capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"
    if can_sync_adj:
        from datetime import datetime, timedelta
        adj_end = datetime.now()
        if daily_range_start is not None:
            adj_start = datetime.combine(daily_range_start, datetime.min.time())
        else:
            # 日K实时增量/跳过时, 除权兜底拉最近 N 天, 覆盖周末/长假/停机期间的新除权事件。
            # 15 天: 覆盖春节/国庆最长约10天长假 + 故障恢复缓冲; sync_adj_factor 内部 merge+unique 幂等, 多拉无副作用。
            adj_start = adj_end - timedelta(days=15)
        adj_start_str = adj_start.strftime("%Y-%m-%d")
        adj_end_str = adj_end.strftime("%Y-%m-%d")
        emit("sync_adj", 50, f"获取除权因子 [{adj_start_str} ~ {adj_end_str}]…")
        logger.info("sync_adj: [%s ~ %s] start", adj_start_str, adj_end_str)

        def _adj_chunk_progress(cur: int, tot: int) -> None:
            emit("sync_adj", 50 + int(10 * cur / tot),
                 f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)
        written_adj, affected_symbols = kline_sync.sync_adj_factor(
            universe, repo, capset,
            start_time=adj_start, end_time=adj_end,
            on_chunk_done=_adj_chunk_progress,
        )
        if affected_symbols:
            _refresh_single_view(repo, "adj_factor")
            emit("sync_adj", 60, f"除权因子完成,新增 {len(affected_symbols)} 只个股")
            logger.info("sync_adj: [%s ~ %s] done, %d symbols", adj_start_str, adj_end_str, len(affected_symbols))
        else:
            emit("sync_adj", 60, "除权因子完成,无新增")
            logger.info("sync_adj: [%s ~ %s] no new factors", adj_start_str, adj_end_str)
        _invalidate("adj_factor")
    else:
        skipped.append("sync_adj")
        logger.info("sync_adj skipped: no ADJ_FACTOR capability")

    # Step 2: 计算 enriched
    #   判断策略:
    #     - 首次 (enriched 目录不存在) → 全量
    #     - 往前扩展历史 (新日期 < enriched 已有最早日期) → 全量
    #       前面的除权因子会改变累积因子链,影响后面所有日期的复权价格
    #     - 往后新增日期或已有历史区间内的缺口
    #       → 增量补新区块(所有标的) + 受除权影响个股全日期重算
    #     - 无新日期 + 有新除权因子 → 增量: 只重算受影响个股的全部日期
    #     - 无新日期 + 无变化 → 跳过
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
    daily_dir = repo.store.data_dir / "kline_daily"
    daily_days = len(list(daily_dir.glob("date=*"))) if daily_dir.exists() else 0
    prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 部分分区修复 (#223) + 收盘价过期分区修复: 删除被实时合并提前创建、覆盖不全
    # 或收盘价停留在竞价前快照的 enriched 分区, 让下方计数比较与增量计算把它们
    # 重新当新日期处理 (值级比对以官方日线为准, 实时源不纠错也能自愈)
    if enriched_exists:
        partial_pruned = _prune_partial_enriched_partitions(daily_dir, enriched_dir)
        stale_pruned = _prune_stale_price_partitions(daily_dir, enriched_dir)
        pruned_dates = sorted(set(partial_pruned) | set(stale_pruned))
        if pruned_dates:
            logger.warning(
                "compute_enriched: 发现 %d 个异常 enriched 分区 (覆盖不全 %d / 收盘价过期 %d), "
                "已删除待重算: %s",
                len(pruned_dates), len(partial_pruned), len(stale_pruned),
                ", ".join(pruned_dates[:10]),
            )
            enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
            prev_enriched_days = len(list(enriched_dir.glob("date=*"))) if enriched_exists else 0

    # 判断新日期方向: 找 daily 和 enriched 的日期集合做比较
    forward_incremental = False
    backward_extension = False

    if daily_days > prev_enriched_days and enriched_exists:
        daily_dates = sorted(d.stem.split("=")[1] for d in daily_dir.glob("date=*"))
        enriched_dates = sorted(d.stem.split("=")[1] for d in enriched_dir.glob("date=*"))
        earliest_enriched = enriched_dates[0]
        new_dates = set(daily_dates) - set(enriched_dates)
        if new_dates:
            # 有新日期早于 enriched 最早日期 → 往前扩展
            if any(d < earliest_enriched for d in new_dates):
                backward_extension = True
            # 包含中间被删的异常分区; 没有新增末日也必须补算。
            # 往前扩展仍由下方优先走全量分支。
            forward_incremental = True

    def _enriched_batch_progress(cur: int, tot: int) -> None:
        emit("compute_enriched", 65 + int(23 * cur / tot),
             f"计算指标 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    if not enriched_exists or backward_extension:
        # 首次 或 往前扩展 → 全量
        emit("compute_enriched", 65, "全量计算 enriched…")
        logger.info("compute_enriched: full rebuild (first=%s, backward=%s, daily=%d, enriched=%d)",
                    not enriched_exists, backward_extension, daily_days, prev_enriched_days)
        written_enriched = run_pipeline(on_batch_done=_enriched_batch_progress)
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: full rebuild done, %d days", new_enriched_days)
    elif forward_incremental:
        # 往后新增日期: 增量补新区块 + 受影响个股全日期重算
        symbols_to_recompute = list(set(affected_symbols)) if affected_symbols else []
        emit("compute_enriched", 65,
             f"增量计算 enriched (新日期 + {len(symbols_to_recompute)} 只个股重算)…"
             if symbols_to_recompute else "增量计算 enriched (新日期)…")
        logger.info("compute_enriched: forward incremental, %d symbols to recompute",
                    len(symbols_to_recompute))
        written_enriched = run_pipeline(
            new_dates_only=True,
            symbols=symbols_to_recompute or None,
            on_batch_done=_enriched_batch_progress,
        )
        new_enriched_days = len(list(enriched_dir.glob("date=*")))
        emit("compute_enriched", 88, f"enriched 完成,覆盖 {new_enriched_days} 天")
        logger.info("compute_enriched: forward incremental done, %d days", new_enriched_days)
    elif affected_symbols:
        # 无新日期,仅除权因子变更 → 只重算受影响个股的全部日期
        emit("compute_enriched", 65, f"增量计算 enriched ({len(affected_symbols)} 只个股)…")
        logger.info("compute_enriched: adj_factor incremental, %d symbols", len(affected_symbols))
        written_enriched = run_pipeline(symbols=affected_symbols, on_batch_done=_enriched_batch_progress)
        emit("compute_enriched", 88, f"enriched 完成,{len(affected_symbols)} 只个股")
    else:
        written_enriched = 0
        logger.info("compute_enriched: skip (no new daily, no adj_factor changes)")
    _refresh_single_view(repo, "kline_enriched")
    _invalidate("enriched")

    # Step 2.1: 数据充足性可见化 (#303) — 空库首跑/仅当日实时覆写 1 天的库,
    # 均线/动量/量比等指标暖机不足, 管道各 stage 都"成功"但策略会静默全 0。
    enriched_total_days = warn_if_enriched_too_thin(repo.store.data_dir)

    # Step 2.3: 指数 / ETF 同步 — 物理分开存储；ETF 可复权，指数不复权。
    written_index_daily = 0
    written_etf_daily = 0
    index_count = 0
    etf_count = 0
    etf_adj_symbols = 0
    pull_index = _prefs.get_pipeline_pull_index()
    pull_etf = _prefs.get_pipeline_pull_etf()

    from app.data_providers import custom as custom_sources
    daily_provider = _prefs.get_daily_data_provider()
    daily_allowed = capset.has(Cap.KLINE_DAILY_BATCH) or (
        daily_provider != "tickflow" and custom_sources.provider_has_dataset(daily_provider, "daily")
    )
    if daily_allowed and (pull_index or pull_etf):
        _types = []
        if pull_index:
            _types.append("指数")
        if pull_etf:
            _types.append("ETF")
        emit("sync_index", 88, f"同步{'+'.join(_types)}日K…")
        # 子阶段进度分配: 88.0(开始) → 89.0(完成), 指数占前半, ETF 占后半
        try:
            if pull_index:
                emit("sync_index", 88, "同步指数维表…")
                index_count = index_sync.sync_index_instruments(repo, pull_index=True, pull_etf=False)
                emit("sync_index", 88, f"指数维表完成,{index_count} 只")
                index_dir = repo.store.data_dir / "kline_index_enriched"
                index_dates = sorted(
                    d.name[5:] for d in index_dir.glob("date=*")
                    if d.is_dir() and d.name.startswith("date=")
                ) if index_dir.exists() else []
                # 数据修正模式下用传入起点; 否则用本地指数最新日期补到今天;
                # 完整性修复时起点再提前到最早坏日 (实时写过今天的指数分区时,
                # "最新日期=今天"会让停机日的快照/缺口永久留存)
                if override_start_date:
                    index_start = override_start_date
                else:
                    index_start = _date.fromisoformat(index_dates[-1]) if index_dates else today - _td(days=365)
                if index_stale_day is not None and index_start > index_stale_day:
                    index_start = index_stale_day

                def _index_chunk(cur: int, tot: int) -> None:
                    emit("sync_index", 88, f"指数日K批次 {cur}/{tot}",
                         stage_pct=int(100 * cur / tot) if tot else 100, skip_log=cur < tot)

                written_index_daily = index_sync.sync_and_persist_index_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(index_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_index_chunk,
                )
                emit("sync_index", 88, f"指数日K完成,{written_index_daily} 行")
                _invalidate("index_instruments")
                _invalidate("index_daily")
                _invalidate("index_enriched")

            if pull_etf:
                emit("sync_index", 88, "同步 ETF 维表…")
                etf_count = index_sync.sync_etf_instruments(repo)
                emit("sync_index", 88, f"ETF 维表完成,{etf_count} 只")
                etf_symbols: list[str] = []
                etf_inst = repo.get_etf_instruments()
                if not etf_inst.is_empty() and "symbol" in etf_inst.columns:
                    etf_symbols = sorted(set(etf_inst["symbol"].to_list()))
                if etf_symbols and capset.has(Cap.ADJ_FACTOR):
                    try:
                        emit("sync_index", 88, "同步 ETF 除权因子…")
                        from datetime import datetime, timedelta
                        adj_end = datetime.now()
                        adj_path = repo.store.data_dir / "adj_factor_etf" / "all.parquet"
                        # 首次无文件时与 ETF 日K 默认一年窗口对齐, 不要只拉 30 天。
                        history_start = adj_end - timedelta(days=365)
                        adj_start = index_sync.etf_adj_factor_window_start(adj_path, history_start)
                        _, affected_etfs = index_sync.sync_etf_adj_factor(
                            etf_symbols,
                            repo,
                            capset,
                            start_time=adj_start,
                            end_time=adj_end,
                        )
                        etf_adj_symbols = len(affected_etfs)
                        emit("sync_index", 88, f"ETF 除权因子完成,{etf_adj_symbols} 只")
                    except Exception as e:  # noqa: BLE001
                        logger.warning("ETF adj_factor skipped: %s", e)
                        stage_errors.append(f"ETF adj_factor: {e}")
                etf_dir = repo.store.data_dir / "kline_etf_enriched"
                etf_dates = sorted(
                    d.name[5:] for d in etf_dir.glob("date=*")
                    if d.is_dir() and d.name.startswith("date=")
                ) if etf_dir.exists() else []
                if override_start_date is not None:
                    etf_start = override_start_date
                else:
                    etf_start = _date.fromisoformat(etf_dates[-1]) if etf_dates else today - _td(days=365)
                # 同指数: 完整性修复时把 ETF 起点提前到最早坏日
                if etf_stale_day is not None and etf_start > etf_stale_day:
                    etf_start = etf_stale_day

                def _etf_chunk(cur: int, tot: int) -> None:
                    emit("sync_index", 88, f"ETF 日K批次 {cur}/{tot}",
                         stage_pct=int(100 * cur / tot) if tot else 100, skip_log=cur < tot)

                written_etf_daily = index_sync.sync_and_persist_etf_daily(
                    repo,
                    capset,
                    start_date=_dt.combine(etf_start, _dt.min.time()),
                    end_date=_dt.combine(today, _dt.min.time()),
                    on_chunk_done=_etf_chunk,
                )
                emit("sync_index", 88, f"ETF 日K完成,{written_etf_daily} 行")
                _invalidate("etf_instruments")
                _invalidate("etf_daily")

            repo.refresh_index_views()
            emit(
                "sync_index",
                89,
                f"同步完成,指数 {index_count} 只/{written_index_daily} 行, ETF {etf_count} 只/{written_etf_daily} 行"
                + (f", ETF复权 {etf_adj_symbols} 只" if etf_adj_symbols else ""),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("sync_index/etf failed: %s", e)
            emit("sync_index", 89, f"指数/ETF同步失败:{e}")
            stage_errors.append(f"index/etf sync: {e}")
    else:
        skipped.append("sync_index")

    # Step 2.5: 分钟 K 同步(可选) — 未启用或无 capability 时静默跳过(不 emit)
    from app.services import preferences
    minute_on = preferences.get_minute_sync_enabled()
    minute_days = preferences.get_minute_sync_days()
    written_minute = 0
    # 门控口径与 api/kline._minute_allowed 一致: TickFlow 需 KLINE_MINUTE_BATCH
    # 能力; 自定义源声明 minute 数据集即放行 (resolver 异常按降级 TickFlow 处理)。
    _, minute_fallback, _minute_resolve_err = kline_sync._resolve_minute_provider(
        preferences.get_minute_data_provider()
    )
    minute_allowed = (not minute_fallback) or capset.has(Cap.KLINE_MINUTE_BATCH)
    if minute_on and minute_allowed:
        minute_start = today - _td(days=minute_days)
        emit("sync_minute", 90, f"获取分钟K [{minute_start} ~ {today}]…")
        logger.info("sync_minute: [%s ~ %s] start", minute_start, today)
        minute_symbols = _resolve_minute_symbols(capset, repo)
        def _minute_chunk_progress(cur: int, tot: int, seg_label: str = "") -> None:
            emit("sync_minute", 90 + int(3 * cur / tot),
                 f"分钟K 批次 {cur}/{tot}" + (f" [{seg_label}]" if seg_label else ""),
                 stage_pct=int(100 * cur / tot), skip_log=True)
        written_minute = kline_sync.sync_and_persist_minute(
            minute_symbols, repo, capset, days=minute_days,
            on_chunk_done=_minute_chunk_progress,
        )
        minute_dir = repo.store.data_dir / "kline_minute"
        minute_cover_days = len(list(minute_dir.glob("date=*"))) if minute_dir.exists() else 0
        emit("sync_minute", 93, f"分钟K完成,覆盖 {minute_cover_days} 天")
        logger.info("sync_minute: [%s ~ %s] done, %d days", minute_start, today, minute_cover_days)
        _invalidate("minute")
    else:
        skipped.append("sync_minute")
        if minute_on:
            logger.info("sync_minute skipped: minute provider has no capability/dataset")
        else:
            logger.info("sync_minute skipped: user disabled")

    # Step 2.6: 市场环境(regime) 增量计算 — enriched 已就绪后聚合环境指标。
    # 双检测(缺口+stale), 自动补算遗漏/被覆写的日。软失败: 不阻断主管道。
    # 默认关闭: regime 是本地聚合计算(非拉取), 首次/regime 表为空时需全量回填
    # 多日, 内存与耗时较高。用户可在数据页「市场环境」卡片设置里开启自动计算,
    # 或直接在该页面点「重算」手动触发(不受此开关影响)。
    regime_days = 0
    from app.services import preferences as _prefs_regime
    if not _prefs_regime.get_pipeline_regime_enabled():
        skipped.append("regime")
        logger.info("compute_regime skipped: user disabled (pipeline_regime_enabled=False)")
    else:
        try:
            emit("compute_regime", 90, "计算市场环境…")
            from app.api.regime import invalidate_regime_cache
            from app.services import regime_builder
            new_regime = regime_builder.compute_regime_incremental(repo, repo.store.data_dir)
            regime_days = new_regime.height if not new_regime.is_empty() else 0
            if regime_days:
                invalidate_regime_cache()
                logger.info("compute_regime: %d days", regime_days)
            emit("compute_regime", 92, f"市场环境 {regime_days} 天")
            # 阶段切换推送监控通知 (软失败, 不影响管道): 末两日阶段不同 = 今日发生切换。
            # 切入退潮/冰点为风险信号, 用 warn 级别; 其余 info。
            if regime_days:
                try:
                    _push_phase_change_alert(repo.store.data_dir)
                except Exception as e:
                    logger.warning("phase change alert failed (soft): %s", e)
        except Exception as e:  # noqa: BLE001
            logger.warning("compute_regime failed (soft): %s", e)
            stage_errors.append(f"compute_regime: {e}")
            skipped.append("regime")

    # Step 2.7: 市场主线(概念/行业涨停梯队聚合) 增量计算 — regime 同开关。
    # 只窄扫连板 >=1 的行, 增量通常 1 天, 开销可忽略。软失败: 不阻断主管道。
    mainline_rows = 0
    if not _prefs_regime.get_pipeline_regime_enabled():
        skipped.append("mainline")
    else:
        try:
            emit("compute_mainline", 93, "计算市场主线…")
            from app.services import market_mainline
            for _kind in ("concept", "industry"):
                rows = market_mainline.compute_mainline_incremental(
                    repo, repo.store.data_dir, kind=_kind
                )
                mainline_rows += rows.height if not rows.is_empty() else 0
            if mainline_rows:
                logger.info("compute_mainline: %d rows", mainline_rows)
            emit("compute_mainline", 94, f"市场主线 {mainline_rows} 行")
        except Exception as e:
            logger.warning("compute_mainline failed (soft): %s", e)
            stage_errors.append(f"compute_mainline: {e}")
            skipped.append("mainline")

    # Step 2.8: 模拟盘结算: 顺延单按当日开盘/收盘撮合 → 除权调整 → 定版净值。
    # 幂等: 重跑同日不会重复成交/二次除权 (订单状态与 corp_action 台账守卫)。
    # 必须在日K/除权同步之后, 才能读到当日 raw OHLC 与因子。核心账务不受
    # 「市场环境」等可选开关控制, 恒运行 (开销可忽略); 软失败不阻断主管道。
    paper_summary: dict = {}
    try:
        emit("paper_settle", 94, "模拟盘结算…")
        from app.strategy import paper as paper_trading
        # 逐账户结算 (账户间订单/台账隔离), 汇总合并供日志与结果展示
        totals = {"filled": 0, "expired": 0, "corp_actions": 0, "nav": None, "accounts": []}
        settled_days: set[str] = set()
        for acc_id in paper_trading.list_account_ids(repo.store.data_dir):
            # 补结算: 停机/管道在日K阶段失败会让那一整天没人撮合, 顺延单会一直
            # 挂 pending (页面表现为「全是待成交」)。这里回放到 today, 幂等。
            s = paper_trading.settle_pending_days(repo.store.data_dir, today.isoformat(), account_id=acc_id)
            totals["filled"] += s.get("filled", 0)
            totals["expired"] += s.get("expired", 0)
            totals["corp_actions"] += s.get("corp_actions", 0)
            if s.get("nav") is not None:
                totals["nav"] = s["nav"]
            settled_days.update(s.get("settled_days") or [])
            totals["accounts"].append({"account": acc_id, **{k: s.get(k) for k in ("filled", "expired", "corp_actions")}})
        paper_summary = {**totals, "settled_days": sorted(settled_days)}
        if len(settled_days) > 1:
            logger.warning("paper_settle 补结算 %d 个交易日: %s", len(settled_days), sorted(settled_days))
        if paper_summary.get("filled") or paper_summary.get("corp_actions"):
            logger.info("paper_settle: %s", paper_summary)
        # 结算成交留痕 (V3): next_open/close 单的成交发生在盘后管道内, 盘中钩子
        # 覆盖不到; 管道无 SSE 广播器, 这里补 alert_store 留痕进监控中心即可见。
        if totals["filled"]:
            try:
                from app.services import alert_store
                settle_events = []
                for acc_id in paper_trading.list_account_ids(repo.store.data_dir):
                    settle_events.extend(paper_trading.day_fill_events(
                        repo.store.data_dir, today.isoformat(), account_id=acc_id))
                if settle_events:
                    alert_store.append_many(repo.store.data_dir, settle_events)
            except Exception as e:
                logger.warning("模拟盘结算成交留痕失败: %s", e)
        emit("paper_settle", 94, "模拟盘结算完成")
    except Exception as e:
        logger.warning("paper_settle failed (soft): %s", e)
        stage_errors.append(f"paper_settle: {e}")

    # Step 3: 刷新视图
    emit("refresh_views", 95, "刷新 DuckDB 视图…")
    _refresh_views(repo)

    emit("done", 100, "完成")
    _invalidate(None)  # 兜底:全清

    result = {
        "universe_size": len(universe),
        "daily_days": new_daily_days,
        "adj_factor_symbols": len(affected_symbols),
        "enriched_days": written_enriched,
        "index_count": index_count,
        "index_daily_rows": written_index_daily,
        "etf_count": etf_count,
        "etf_daily_rows": written_etf_daily,
        "etf_adj_factor_symbols": etf_adj_symbols,
        "minute_rows": written_minute,
        "regime_days": regime_days,
        "mainline_rows": mainline_rows,
        "paper_settle": paper_summary,
        "lagging_symbols": len(lagging_symbols),
        "enriched_total_days": enriched_total_days,
        "integrity_repair_from": repair_start.isoformat() if repair_start else None,
        "integrity_issues": len(integrity_issues),
        "skipped_stages": skipped,
        "stage_errors": stage_errors,
    }

    # 修复路径后置校验 (issue #514): 修完必须重扫一遍, 仍有坏分区时如实报失败 —
    # 不再出现「修复自报成功 (integrity_issues 恒 0) 但坏行仍在」的假闭环。
    # 夜间常规管道 (无 override) 不走此逻辑 (它自己开头扫过)。
    if override_start_date is not None:
        try:
            from app.services import data_integrity as _di

            remaining_issues = _di.scan_recent_integrity(repo.store.data_dir, today=today)
            checked_tables = set(_di.active_daily_tables())
            remaining_issues = [i for i in remaining_issues if i.table in checked_tables]
            if remaining_issues:
                stage_errors.append(
                    f"数据修复后完整性仍不通过: {_di.describe_issues(remaining_issues)}"
                    f" (剩余 {len(remaining_issues)} 个不完整分区;"
                    " 请查看同步日志, 或到 数据→修正数据 手动执行)"
                )
                logger.warning(
                    "integrity: post-repair scan found %d remaining issues: %s",
                    len(remaining_issues), _di.describe_issues(remaining_issues),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("integrity: post-repair scan failed (soft): %s", e)

    # 有阶段软失败: 进度协议已走完(done/100, 前端进度条正常收尾), 但数据可能陈旧,
    # 抛出让上层 job_store 把终态标记为 failed —— 不再"部分失败却报成功"。
    if stage_errors:
        raise PipelineStageError(stage_errors)

    return result


def run_adj_factor_sync(
    repo: KlineRepository,
    capset: CapabilitySet,
    on_progress: ProgressCb | None = None,
) -> dict:
    """独立同步除权因子(数据页单独按钮)。

    与盘后管道 Step 1.5 的差异: 管道只拉日K范围或 15 天兜底窗口;
    这里 start_time=None 拉**全历史** —— 用于切换数据源后的因子修复、
    以及事件明细列扩表后的存量回填 (明细回填不改变 ex_factor,
    sync_adj_factor 的变化判定不会因此触发 enriched 重算)。

    下游联动与管道同语义: adj_factor 视图刷新 → 受影响个股 enriched
    局部重算(全部日期) → kline_enriched 视图刷新 → 两表缓存失效。
    enriched 尚未构建时跳过重算 (全量计算是盘后管道的职责)。
    """
    from datetime import datetime

    emit = on_progress or _noop
    adj_provider = _prefs.get_adj_factor_provider()
    if not (capset.has(Cap.ADJ_FACTOR) or adj_provider != "tickflow"):
        return {
            "adj_factor_symbols": 0,
            "adj_written": 0,
            "enriched_days": 0,
            "skipped_stages": ["sync_adj"],
            "reason": "无可用除权因子数据源",
        }

    emit("resolve_universe", 5, "解析标的池…")
    universe = _resolve_universe(capset, repo)
    emit("resolve_universe", 10, f"标的池规模:{len(universe)} 只")

    def _adj_chunk_progress(cur: int, tot: int) -> None:
        emit("sync_adj", 10 + int(55 * cur / tot),
             f"除权因子批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

    # 按需窗口: 钳到本地日K起点。前复权里早于任一根已存K线的事件在
    # cum/total 两侧同时出现、恰好抵消, 对所有已存价格零影响 (见
    # indicators.pipeline._apply_adj_factor) — 拉它们只会为远古事件
    # 逐只回补历史K线, 全市场一次曾多花 ~15 分钟。
    store_min = repo.earliest_daily_date()
    start = datetime(store_min.year, store_min.month, store_min.day) if store_min else None
    window_label = f"自 {store_min.isoformat()}" if store_min else "全历史"
    logger.info(
        "standalone adj sync: %d symbols, event window %s",
        len(universe), window_label,
    )
    emit("sync_adj", 10, f"获取除权因子 [{window_label}, {len(universe)} 只]…")
    written_adj, affected = kline_sync.sync_adj_factor(
        universe, repo, capset,
        start_time=start, end_time=datetime.now(),
        on_chunk_done=_adj_chunk_progress,
    )
    affected = sorted(set(affected))
    _refresh_single_view(repo, "adj_factor")
    emit("sync_adj", 65,
         f"除权因子完成,新增 {written_adj} 行,因子变更 {len(affected)} 只")
    _invalidate("adj_factor")

    # enriched 局部重算: 与管道「无新日期 + 有新除权因子」分支同语义 (只重算受影响个股)
    enriched_dir = repo.store.data_dir / "kline_daily_enriched"
    enriched_exists = enriched_dir.exists() and any(enriched_dir.glob("date=*"))
    written_enriched = 0
    if affected and enriched_exists:
        def _enriched_batch_progress(cur: int, tot: int) -> None:
            emit("compute_enriched", 65 + int(30 * cur / tot),
                 f"计算指标 批次 {cur}/{tot}", stage_pct=int(100 * cur / tot), skip_log=True)

        emit("compute_enriched", 65, f"增量计算 enriched ({len(affected)} 只个股)…")
        logger.info("compute_enriched: adj_factor incremental (standalone), %d symbols", len(affected))
        written_enriched = run_pipeline(symbols=affected, on_batch_done=_enriched_batch_progress)
        _refresh_single_view(repo, "kline_enriched")
        emit("compute_enriched", 95, f"enriched 完成,{len(affected)} 只个股")
        _invalidate("enriched")
    elif affected:
        logger.info("standalone adj sync: enriched 未构建, 跳过局部重算 (交由盘后管道全量计算)")

    emit("done", 100, "完成")
    return {
        "universe_size": len(universe),
        "adj_written": written_adj,
        "adj_factor_symbols": len(affected),
        "enriched_days": written_enriched,
        "skipped_stages": [],
    }


def warn_if_enriched_too_thin(data_dir: Path) -> int:
    """enriched 总覆盖天数; 低于常见指标暖机窗口时 WARN 引导全量回填 (#303)。

    返回天数供管道 result 上报。目录列举 O(天数), 不在热路径。
    """
    from app.services.screener import MIN_INDICATOR_WARMUP_DAYS, enriched_history_days

    days = enriched_history_days(data_dir)
    if days < MIN_INDICATOR_WARMUP_DAYS:
        logger.warning(
            "enriched 仅覆盖 %d 个交易日 (<%d): 指标暖机不足, 策略可能全部 0 命中且无提示 — "
            "建议全量回填 (同步标的维表 → 日K批量同步(带后缀符号) → 重算 enriched)",
            days, MIN_INDICATOR_WARMUP_DAYS,
        )
    return days


def _refresh_views(repo: KlineRepository) -> None:
    """刷新所有 DuckDB 视图 —— 委托给 repository 的唯一权威实现 rebuild_views()。"""
    repo.rebuild_views()


def _refresh_single_view(repo: KlineRepository, name: str) -> None:
    """刷新单个 DuckDB 视图。"""
    d = repo.store.data_dir.as_posix()
    paths = {
        "kline_daily": f"{d}/kline_daily/**/*.parquet",
        "kline_enriched": f"{d}/kline_daily_enriched/**/*.parquet",
        "kline_index_daily": f"{d}/kline_index_daily/**/*.parquet",
        "kline_index_enriched": f"{d}/kline_index_enriched/**/*.parquet",
        "kline_etf_daily": f"{d}/kline_etf_daily/**/*.parquet",
        "kline_etf_enriched": f"{d}/kline_etf_enriched/**/*.parquet",
        "kline_etf_minute": f"{d}/kline_etf_minute/**/*.parquet",
        "kline_minute": f"{d}/kline_minute/**/*.parquet",
        "adj_factor": f"{d}/adj_factor/**/*.parquet",
        "adj_factor_etf": f"{d}/adj_factor_etf/**/*.parquet",
        "instruments": f"{d}/instruments/**/*.parquet",
        "instruments_index": f"{d}/instruments_index/**/*.parquet",
        "instruments_etf": f"{d}/instruments_etf/**/*.parquet",
    }
    path = paths.get(name)
    if not path:
        return
    repo.refresh_view(name, path)


def _resolve_minute_symbols(capset: CapabilitySet, repo=None) -> list[str]:
    """分钟 K 同步标的 — 与日K共用同一标的池。"""
    return _resolve_universe(capset, repo)


def _refresh_instruments_view(repo: KlineRepository) -> None:
    """单独刷新 instruments 视图。"""
    d = repo.store.data_dir.as_posix()
    repo.refresh_view("instruments", f"{d}/instruments/**/*.parquet")


def _push_phase_change_alert(data_dir) -> None:
    """情绪周期阶段切换 → 推送监控通知(SSE toast + 监控中心)。

    阶段切换(如 退潮→冰点)是重要的市场信号, 原先只有打开市场环境页才能看到。
    复用 quote_service.push_alerts 广播通道; 未发生切换静默返回。
    """
    from app.services.market_phase import PHASE_LABELS
    from app.services.regime_builder import latest_phase_transition

    tr = latest_phase_transition(data_dir)
    if not tr:
        return
    prev, cur, d = tr
    msg = f"情绪周期阶段切换: {PHASE_LABELS.get(prev, prev)} → {PHASE_LABELS.get(cur, cur)} ({d})"
    severity = "warn" if cur in ("ebb", "ice") else "info"
    app_state = _get_app_state()
    qs = getattr(app_state, "quote_service", None) if app_state else None
    if qs:
        qs.push_alerts([{
            "source": "market",
            "type": "phase_change",
            "message": msg,
            "severity": severity,
        }])
    logger.info("phase change alert: %s (severity=%s)", msg, severity)


def _run_tracked(fn, job_label: str) -> bool:
    """调度触发时包装 JobStore 跟踪，确保同步历史有记录。

    单飞: 若已有活跃(pending∨running)任务(手动同步中), 本次调度直接跳过, 不并发。
    重任务执行槽: 再挡一层僵尸并发(reap 后线程仍活时不得并行写 parquet)。
    返回 True 仅表示任务已成功并且执行槽已释放。
    """
    from app.services.pipeline_jobs import (
        JobCancelledError,
        job_store,
        release_run_slot,
        run_with_capacity,
        try_acquire_run_slot,
    )

    job_id, is_new = job_store.create()
    if not is_new:
        logger.info("scheduled %s 跳过: 已有活跃任务在运行 (job_id=%s)", job_label, job_id)
        return False
    if not try_acquire_run_slot(job_id):
        logger.warning("scheduled %s 跳过: 重任务执行槽被占用(疑似上次任务卡死)", job_label)
        job_store.fail(job_id, f"scheduled {job_label} skipped: 已有数据任务在运行")
        return False

    def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                 skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    succeeded = False
    try:
        result = run_with_capacity(job_id, lambda: fn(on_progress=progress))
        job_store.succeed(job_id, result)
        succeeded = True
        logger.info("scheduled %s completed: job_id=%s", job_label, job_id)
    except JobCancelledError:
        # 已由 terminate() 标记失败(卡死/手动取消), 拉取线程在分块回调处自行退出
        logger.warning("scheduled %s cancelled: job_id=%s", job_label, job_id)
    except Exception:
        logger.exception("scheduled %s failed: job_id=%s", job_label, job_id)
        job_store.fail(job_id, f"scheduled {job_label} failed")
    finally:
        release_run_slot(job_id)
    return succeeded


def _scheduled_pipeline_task(pipeline_fn) -> None:
    """Run the scheduled lab cycle (weekly mining + daily guardian) after pipeline success."""
    if not _run_tracked(pipeline_fn, "daily_pipeline"):
        return
    try:
        from app.services.lab_schedule import run_scheduled_lab

        result = run_scheduled_lab(_get_app_state())
        logger.info("scheduled lab result: %s", result)
    except Exception:
        logger.exception(
            "scheduled lab enqueue failed; daily pipeline remains succeeded"
        )


# ================================================================
# 定时复盘 (AI 大盘复盘报告)
# ================================================================

REVIEW_JOB_ID = "scheduled_review"


async def _run_scheduled_review(repo) -> None:
    """定时复盘 job: 流式生成复盘 → 实时推 SSE(开着页面可见) → 落盘归档 → 推飞书。

    与手动「生成复盘」体验一致: 流式事件经 quote_service.push_review_event →
    /api/intraday/stream 的 review_progress 事件 → 前端 reviewStore, 用户开着复盘页
    即可看到报告边生成边显示, 切走再回来也能看到生成中/已生成。
    LLM 偶发断流(peer closed connection)时自动重试最多 2 次。
    任何异常都吞掉只记日志, 绝不影响调度器主循环。
    """
    import json

    try:
        from app import secrets_store as ss
        from app.services import market_recap_reports

        # AI Key 未配置时跳过(避免每日报错刷日志)
        if not ss.get_ai_key():
            logger.info("scheduled review skipped: AI key not configured")
            return

        app_state = _get_app_state()
        quote_service = getattr(app_state, "quote_service", None) if app_state else None
        depth_service = getattr(app_state, "depth_service", None) if app_state else None

        content, meta = await _stream_review_with_retry(repo, quote_service, depth_service)
        if not content:
            logger.warning("scheduled review produced no content (meta=%s)", meta)
            # 通知前端进入 error 态(若有页面在听)
            if quote_service:
                quote_service.push_review_event(json.dumps(
                    {"type": "error", "message": "复盘生成失败,请稍后手动重试"},
                    ensure_ascii=False))
            return

        # 落盘: 与手动生成完全相同的归档格式
        market_recap_reports.save_report({
            "as_of": meta.get("as_of"),
            "focus": "",
            "content": content,
            "summary": meta.get("summary", ""),
            "emotion_score": meta.get("emotion_score"),
            "emotion_label": meta.get("emotion_label", ""),
        })
        logger.info("scheduled review saved: as_of=%s", meta.get("as_of"))

        # 通知前端: 生成完成且已归档(archived=true 让前端只刷新列表, 不重复归档)
        if quote_service:
            quote_service.push_review_event(json.dumps(
                {"type": "done", "archived": True}, ensure_ascii=False))

        # 推送门控: review_push_mode=manual 时定时复盘只归档不推送,
        # 由用户对当日报告显式确认后才推; auto 时保持既有自动推送行为。
        # 失败静默降级, 不影响已归档的报告。
        if _prefs.get_review_push_mode() == "auto":
            dispatch_review_push(content, meta)
    except Exception as e:  # noqa: BLE001
        logger.exception("scheduled review failed: %s", e)
        # 兜底: 异常时通知前端停止「生成中」状态, 避免页面卡在 streaming
        try:
            app_state = _get_app_state()
            qs = getattr(app_state, "quote_service", None) if app_state else None
            if qs:
                import json as _json
                qs.push_review_event(_json.dumps(
                    {"type": "error", "message": "复盘生成异常,请稍后手动重试"},
                    ensure_ascii=False))
        except Exception:  # noqa: BLE001
            pass


async def _stream_review_with_retry(repo, quote_service, depth_service) -> tuple[str, dict]:
    """流式生成复盘, 每个事件推 SSE + 累积内容。LLM 断流时最多重试 2 次。

    返回 (content, meta)。重试时推一个 retry 事件让前端清空已累积内容重新开始。
    成功(收到 done/无 error)或耗尽重试后返回。
    """
    import asyncio
    import json

    from app.services.market_recap import recap_market_stream

    max_attempts = 3  # 初次 + 2 次重试
    last_meta: dict = {}
    content_parts: list[str] = []

    for attempt in range(1, max_attempts + 1):
        content_parts = []  # 每次重试重新累积
        failed = False
        try:
            async for evt_json in recap_market_stream(repo, quote_service, depth_service):
                evt = json.loads(evt_json)
                t = evt.get("type")

                # 推给前端(让开着页面的用户实时看到, 与手动一致)
                if quote_service:
                    quote_service.push_review_event(evt_json)

                if t == "meta":
                    last_meta = evt
                elif t == "delta" and evt.get("content"):
                    content_parts.append(evt["content"])
                elif t == "error":
                    failed = True
                    logger.warning("scheduled review stream error (attempt %d/%d): %s",
                                   attempt, max_attempts, evt.get("message"))
                    break  # 触发重试
                elif t == "done":
                    # 正常完成
                    return "".join(content_parts), last_meta
            # 流自然结束(无 done 事件)且有内容, 视为成功
            if content_parts and not failed:
                return "".join(content_parts), last_meta
        except Exception as e:  # noqa: BLE001
            # LLM 断流等异常(httpx.RemoteProtocolError)落到这里
            failed = True
            logger.warning("scheduled review stream exception (attempt %d/%d): %s",
                           attempt, max_attempts, e)

        # 失败: 决定是否重试
        if attempt < max_attempts:
            logger.info("scheduled review retrying in 3s (attempt %d → %d)", attempt, attempt + 1)
            # 通知前端: 即将重试, 清空已累积内容重新开始
            if quote_service:
                quote_service.push_review_event(json.dumps(
                    {"type": "retry", "attempt": attempt + 1}, ensure_ascii=False))
            await asyncio.sleep(3)

    # 耗尽重试, 返回已累积内容(可能为空)和最后 meta
    return "".join(content_parts), last_meta


def dispatch_review_push(content: str, meta: dict) -> None:
    """把复盘推送派发出去：带图模式下放后台线程，避免阻塞请求/事件循环。

    生图要 10~30 秒，海报又只是增强项；因此开启「附带海报」时用守护线程异步推，
    未开启时保持原有同步行为（便于调用方感知并复用既有测试断言）。
    """
    if not _prefs.get_review_push_image():
        _maybe_push_review(content, meta)
        return

    import threading

    threading.Thread(
        target=_maybe_push_review,
        args=(content, meta),
        name="review-push",
        daemon=True,
    ).start()


def _build_review_poster(content: str, meta: dict) -> bytes | None:
    """同步上下文里生成复盘海报（推送链路是 sync 的，内部自建事件循环）。

    已运行在事件循环中时不能直接 asyncio.run，放到独立线程里跑，避免
    "asyncio.run() cannot be called from a running event loop"。
    """
    import asyncio
    import threading

    from app.services.recap_poster import generate_recap_poster

    kwargs = {
        "as_of": meta.get("as_of") or "",
        "summary": meta.get("summary") or "",
        "emotion_label": meta.get("emotion_label") or "",
        "emotion_score": meta.get("emotion_score"),
    }

    def _run() -> bytes | None:
        return asyncio.run(generate_recap_poster(content, **kwargs))

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run()

    box: dict[str, bytes | None] = {}

    def _worker() -> None:
        try:
            box["poster"] = _run()
        except Exception:  # noqa: BLE001 — 已在 generate_recap_poster 内降级, 这里只兜底
            box["poster"] = None

    thread = threading.Thread(target=_worker, name="review-poster", daemon=True)
    thread.start()
    thread.join(timeout=240)  # 生图模型最慢 180s, 留余量; 超时按无图处理
    return box.get("poster")


def _maybe_push_review(content: str, meta: dict) -> None:
    """复盘报告归档后, 按 review_push_channels 选定的外部工具逐个推送完整报告。

    定时生成与手动生成共用本函数 (手动归档端点 POST /api/market-recap/reports 也会调用)。
    channels 为空则不推送; 复用监控中心的全局外部渠道配置。
    review_push_image=True 且已配置图像模型时, 额外生成一张海报随报告推送
    (企业微信群 / 邮件 / 飞书应用机器人支持带图; 飞书自定义机器人只有文本)。
    推送失败静默降级 (Webhook 是辅助通道), 不影响已归档的报告。
    """
    try:
        from app import secrets_store
        from app.services import email_adapter, feishu_app, preferences, webhook_adapter
        from app.services.wecom_bot_service import WecomBotService

        enabled_channels = set(preferences.get_webhook_enabled_channels())
        channels = [c for c in preferences.get_review_push_channels() if c in enabled_channels]
        if not channels:
            return

        emotion = f"{meta.get('emotion_label') or ''}".strip()
        as_of = meta.get("as_of") or ""
        subtitle = as_of + (f" · 情绪 {emotion}" if emotion else "")

        poster: bytes | None = None
        if preferences.get_review_push_image():
            poster = _build_review_poster(content, meta)

        for ch in channels:
            if ch == "feishu":
                url = preferences.get_feishu_webhook_url()
                if not url:
                    logger.info("review push(feishu) skipped: webhook not configured")
                    continue
                secret = preferences.get_feishu_webhook_secret()
                ok = webhook_adapter.send_feishu_card(
                    url, "每日复盘", subtitle, content, secret
                )
                logger.info("review push(feishu) %s", "sent" if ok else "failed")
            elif ch == "wecom":
                url = preferences.get_wecom_webhook_url()
                if not url:
                    logger.info("review push(wecom) skipped: webhook not configured")
                    continue
                # 企业微信 markdown 标题已含一级标题, subtitle 拼到正文首行
                full_body = (f"**{subtitle}**\n\n{content}" if subtitle else content)
                ok = webhook_adapter.send_wecom_markdown(
                    url, "每日复盘", full_body
                )
                logger.info("review push(wecom) %s", "sent" if ok else "failed")
                if poster:
                    img_ok = webhook_adapter.send_wecom_image(url, poster)
                    logger.info("review push(wecom image) %s", "sent" if img_ok else "failed")
            elif ch == "custom":
                url = preferences.get_custom_webhook_url()
                if not url:
                    logger.info("review push(custom) skipped: webhook not configured")
                    continue
                ok = webhook_adapter.send_custom(
                    url,
                    "每日复盘",
                    content,
                    "market_review",
                    meta,
                    secrets_store.get_custom_webhook_secret(),
                )
                logger.info("review push(custom) %s", "sent" if ok else "failed")
            elif ch == "feishu_app":
                app_id = preferences.get_feishu_app_id()
                app_secret = secrets_store.get_feishu_app_secret()
                chat_id = preferences.get_feishu_app_chat_id()
                if not feishu_app.is_configured(app_id, app_secret, chat_id):
                    logger.info("review push(feishu_app) skipped: app bot not configured")
                    continue
                body = (f"{subtitle}\n\n{content}" if subtitle else content)
                ok = feishu_app.send_text(app_id, app_secret, chat_id, "每日复盘", body)
                logger.info("review push(feishu_app) %s", "sent" if ok else "failed")
                if poster:
                    img_ok, img_err = feishu_app.send_image(app_id, app_secret, chat_id, poster)
                    logger.info(
                        "review push(feishu_app image) %s %s",
                        "sent" if img_ok else "failed", "" if img_ok else img_err,
                    )
            elif ch == "wecom_bot":
                bot_svc = WecomBotService.instance()
                chat_id = preferences.get_wecom_bot_chat_id()
                if bot_svc is None or not chat_id or not bot_svc.status().get("connected"):
                    logger.info("review push(wecom_bot) skipped: bot not connected or no chat")
                    continue
                body = (f"{subtitle}\n\n{content}" if subtitle else content)
                ok, err = bot_svc.send_text(chat_id, f"每日复盘\n{body}")
                logger.info("review push(wecom_bot) %s %s", "sent" if ok else "failed", err if not ok else "")
            elif ch == "email":
                config = preferences.get_email_smtp_config()
                if not email_adapter.is_configured(config):
                    logger.info("review push(email) skipped: SMTP not configured")
                    continue
                email_body = (f"{subtitle}\n\n{content}" if subtitle else content)
                email_kwargs = {}
                if poster:
                    email_kwargs["attachment"] = (f"复盘海报-{as_of or 'latest'}.png", poster, "image/png")
                ok = email_adapter.send_email(
                    config,
                    secrets_store.get_email_smtp_password(),
                    "每日复盘",
                    email_body,
                    **email_kwargs,
                )
                logger.info("review push(email) %s", "sent" if ok else "failed")
            # 未来更多渠道在此追加分支
    except Exception as e:  # noqa: BLE001
        logger.warning("review push error: %s", e)


def _register_review_job(scheduler, repo, hour: int, minute: int) -> None:
    """注册/更新定时复盘 job(工作日 mon-fri, Asia/Shanghai)。

    供 start_scheduler(启动时) 和 settings API(改时间时) 共用。
    用 replace_existing=True, 重复注册只更新 trigger。

    注意: _run_scheduled_review 是协程函数, 必须把函数对象本身(配合 args)传给
    add_job, 而非用 lambda 包裹 —— 否则 APScheduler 会把 lambda 当同步函数在线程池
    执行, 仅得到一个未 await 的协程对象, 复盘实际不会运行。
    """
    scheduler.add_job(
        _run_scheduled_review,
        args=[repo],
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=hour, minute=minute,
                            timezone="Asia/Shanghai"),
        id=REVIEW_JOB_ID,
        misfire_grace_time=7200,  # 复盘非关键, 允许 2 小时内补跑
        replace_existing=True,
    )


def start_scheduler(repo: KlineRepository, capset: CapabilitySet) -> AsyncIOScheduler:
    """启动调度器。

    工作日 09:10 — 同步个股维表
    工作日 HH:MM — 盘后管道（时间由用户偏好决定，默认 15:35）
    """
    from app.services import preferences
    sched = preferences.get_pipeline_schedule()
    inst_sched = preferences.get_instruments_schedule()

    scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")

    # 盘前: 同步 instruments（时间由偏好决定）
    def _instruments_task(on_progress=None):
        emit = on_progress or _noop
        emit("sync_instruments", 0, "同步个股维表…")
        result = run_instruments_sync(repo)
        emit("done", 100, f"个股维表同步完成,{result.get('instruments_rows', 0)} 只标的")
        return result

    scheduler.add_job(
        lambda: _run_tracked(_instruments_task, "instruments_sync"),
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=inst_sched["hour"], minute=inst_sched["minute"],
                            timezone="Asia/Shanghai"),
        id="pre_market_instruments",
        misfire_grace_time=1800,
        replace_existing=True,
    )

    # 盘后: 日 K + enriched（时间由偏好决定）
    def _pipeline_then_refresh(on_progress=None):
        # 与手动触发 (/api/pipeline/run) 对齐: 管道落盘后重建 Polars 内存缓存,
        # 否则 live_agg 的昨日连板数等基准列会停留在旧交易日, 次日开盘连板梯队
        # 整体少算一档 (仅手动触发或重启才会刷缓存, cron 调度路径此前漏了这步)。
        # 用 app.state 上的**实时** capset(周期重探会热更新它), 而非启动时捕获的
        # 旧 capset —— 否则 Key 中途过期/续费后, 调度管道仍按旧档位打端点。
        app_state = _get_app_state()
        capset_live = getattr(app_state, "capabilities", None) or capset
        # 管道运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = getattr(app_state, "quote_service", None)
        try:
            if qs:
                with qs.paused():
                    result = run_now(repo, capset_live, on_progress=on_progress)
            else:
                result = run_now(repo, capset_live, on_progress=on_progress)
        finally:
            # 即便有阶段软失败(run_now 末尾抛 PipelineStageError), 已落盘的日K/enriched
            # 仍需刷进内存缓存, 否则 live_agg 基准列停留在旧交易日。放 finally 保证部分
            # 成功也生效; 随后异常继续上抛, 由 _run_tracked 标记任务 failed。
            repo.refresh_cache()
        return result

    scheduler.add_job(
        lambda: _scheduled_pipeline_task(_pipeline_then_refresh),
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=sched["hour"], minute=sched["minute"],
                            timezone="Asia/Shanghai"),
        id="daily_pipeline",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 港美股盘后增量: 各自收盘 + 数据源落库后跑一次, 只刷新已同步过的标的。
    # 港股 16:50、美股次日 08:40（北京时间）。之前港美股只有「手动点同步」这一条
    # 入口, 不点就永远停在首次拉取那天, 于是切市场常常看到上周的收盘价。
    def _market_sync_task(market: str, count_days: int, on_progress=None):
        app_state = _get_app_state()
        capset_live = getattr(app_state, "capabilities", None) or capset
        result = run_market_sync(
            market, repo, on_progress=on_progress, capset=capset_live,
            count_days=count_days, refresh_synced=True,
        )
        repo.refresh_cache()
        return result

    # 美股时点说明(2026-11 实测): 16:00 ET 收盘 = 北京 05:00(冬令)/04:00(夏令),
    # 数据源聚合官方日K还要 1~2 小时, 原定 06:40 跑只落到前一交易日(11-25 06:42
    # 那轮止于 11-23, 11-24 全市场缺失), 于是美股日期稳定落后一天。08:40 北京 =
    # 收盘后 3.5~4.5 小时, 留足发布窗口; 8 分钟量级在 09:10 维表任务前跑完。
    # 周五场次收盘在北京周六清晨, 故周六补跑一轮, 否则周五K要等到周一才落地。
    for _mkt, _hour, _minute, _days, _dow in (
        ("hk", 16, 50, 30, "mon-fri"),
        ("us", 8, 40, 20, "mon-sat"),
    ):
        scheduler.add_job(
            lambda m=_mkt, d=_days: _run_tracked(
                lambda on_progress=None: _market_sync_task(m, d, on_progress),
                f"market_sync_{m}",
            ),
            trigger=CronTrigger(day_of_week=_dow, hour=_hour, minute=_minute,
                                timezone="Asia/Shanghai"),
            id=f"market_sync_{_mkt}",
            misfire_grace_time=7200,
            replace_existing=True,
        )

    # 盘后: 五档盘口 sealed 定版(时间由偏好决定, 默认15:02, 范围15:01~18:00)
    depth_sched = preferences.get_depth_finalize_time()

    def _depth_finalize():
        depth_svc = getattr(_get_app_state(), "depth_service", None) if _get_app_state() else None
        if depth_svc:
            depth_svc.finalize()

    scheduler.add_job(
        _depth_finalize,
        trigger=CronTrigger(day_of_week="mon-fri",
                            hour=depth_sched["hour"], minute=depth_sched["minute"],
                            timezone="Asia/Shanghai"),
        id="depth_finalize",
        misfire_grace_time=3600,
        replace_existing=True,
    )

    # 周期性能力重探: 付费 Key 中途过期/续费无需重启即可被发现。
    # 只热更新 app.state.capabilities(API 端点、盘后管道 _pipeline_then_refresh 均读它);
    # 档位变化记 WARNING, 让「Key 失效」在日志/前端可见, 不再静默按旧档位打 403 端点。
    def _reprobe_capabilities():
        from app.tickflow.policy import detect_capabilities, tier_label
        app_state = _get_app_state()
        if app_state is None:
            return
        try:
            old = getattr(app_state, "capabilities", None)
            old_n = len(old.all()) if old else -1
            new_capset = detect_capabilities(force=True)
            app_state.capabilities = new_capset
            new_n = len(new_capset.all())
            if old_n != new_n:
                logger.warning(
                    "能力集变化: %d → %d capabilities (档位=%s)。Key 过期/续费或端点波动, "
                    "已热更新 app.state.capabilities。", old_n, new_n, tier_label(),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("周期能力重探失败(保留现有能力集): %s", e)

    scheduler.add_job(
        _reprobe_capabilities,
        trigger=IntervalTrigger(minutes=60),
        id="reprobe_capabilities",
        misfire_grace_time=600,
        replace_existing=True,
    )

    # 定时复盘 (AI 大盘复盘报告): 工作日到点自动生成并归档。
    # 默认关闭 —— 仅当用户在复盘页开启时才注册 job。
    # 复用 recap_market_once(非流式) + market_recap_reports.save_report(落盘)。
    # quote_service / depth_service 通过 _get_app_state() 延迟取用。
    review_sched = preferences.get_review_schedule()
    if review_sched["enabled"]:
        _register_review_job(scheduler, repo, review_sched["hour"], review_sched["minute"])
        logger.info("scheduled_review enabled @%02d:%02d mon-fri",
                    review_sched["hour"], review_sched["minute"])

    scheduler.start()
    logger.info("scheduler started; instruments@%02d:%02d, pipeline@%02d:%02d, depth@%02d:%02d mon-fri",
                inst_sched["hour"], inst_sched["minute"], sched["hour"], sched["minute"],
                depth_sched["hour"], depth_sched["minute"])
    return scheduler


# app_state 延迟引用(start_scheduler 在 lifespan 早期调用, app.state 可能还没就绪)
_app_state_ref = None


def set_app_state(app_state) -> None:
    """lifespan 注册 app.state 引用, 供 scheduled job 访问 depth_service 等单例。"""
    global _app_state_ref
    _app_state_ref = app_state


def _get_app_state():
    return _app_state_ref
