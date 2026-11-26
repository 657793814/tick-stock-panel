"""Screener 服务(§6.3)。

性能优化:
  - enriched parquet 仅存 14 列基础数据, 指标和信号即时计算
  - preset 策略: 从内存缓存或即时计算获取完整指标, ~10-50ms
  - custom SQL: DuckDB (用户传 SQL WHERE 字符串), ~10-50ms
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.parquet import scan_enriched_parquet
from app.polars_guard import guarded_collect
from app.tickflow.repository import KlineRepository

logger = logging.getLogger(__name__)

# 常用指标 (MA5/10/20、BOLL(20)、量比等) 的近似最小暖机窗口。
# 用于没有精确回看需求的场景 (自定义 SQL 筛选、盘后管道) 的数据充足性提示 (#303);
# 策略运行用 engine.required_history_bars 的精确值。
MIN_INDICATOR_WARMUP_DAYS = 30


def enriched_history_days(data_dir, asset_type: str = "stock", as_of: date | None = None) -> int:
    """本地 enriched 在 as_of 及之前覆盖的交易日数 (#303)。

    按 date=* 分区目录名计数 (目录列举 O(天数)), 不读 parquet 内容 —
    只做数据充足性提示, 不进入指标计算路径。
    """
    from app.tickflow.repository import enriched_dirname

    root = Path(data_dir) / enriched_dirname(asset_type)
    if not root.exists():
        return 0
    days = [d.name[5:] for d in root.glob("date=*") if d.is_dir() and len(d.name) > 5]
    if as_of is not None:
        as_of_s = as_of.isoformat()
        days = [d for d in days if d <= as_of_s]
    return len(days)


# ── 进程级历史数据缓存 (避免 run_all 每次重新扫描 parquet + 计算指标) ──
_history_cache: dict[tuple[str, str, date, int], tuple[float, pl.DataFrame]] = {}
_HISTORY_CACHE_TTL = 120.0  # 秒
# 港美股单日全指标帧的进程内缓存 (cn 由 repo 内存快照承担, 不进这里)
_market_day_cache: dict[tuple[str, str, date], tuple[float, pl.DataFrame]] = {}
_MARKET_DAY_CACHE_TTL = 60.0
# 港美股「最新有覆盖日期」的短缓存: 每次看板/筛选都会问, 扫 10 个分区约百毫秒。
_market_latest_cache: dict[tuple[str, str], tuple[float, date | None]] = {}
_MARKET_LATEST_TTL = 60.0

# load_prior_consecutive 最多回看多少个已存在的日分区 (缺列时继续往前找的上限)
_PRIOR_PARTITION_SCAN = 10


def _join_instruments(df: pl.DataFrame, instruments: pl.DataFrame) -> pl.DataFrame:
    if instruments.is_empty():
        return df
    if "name" not in df.columns:
        columns = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
        return df.join(instruments.select(columns), on="symbol", how="left")
    if "name" not in instruments.columns:
        return df
    missing = pl.col("name").is_null() | (pl.col("name").cast(pl.Utf8).str.strip_chars() == "")
    if not df.select(missing.any()).item():
        return df
    # 实时/历史缓存可能已有 name 列却只有部分名称，按行补齐且保持缓存本身不变。
    return (
        df.join(
            instruments.select("symbol", pl.col("name").alias("_instrument_name")),
            on="symbol", how="left", maintain_order="left",
        )
        .with_columns(
            pl.when(missing).then(
                pl.col("_instrument_name").cast(pl.Utf8).str.strip_chars().replace("", None)
            ).otherwise(pl.col("name")).alias("name")
        )
        .drop("_instrument_name")
    )


@dataclass
class ScreenerResult:
    as_of: date
    strategy: str | None
    rows: list[dict] = field(default_factory=list)
    total: int = 0
    elapsed_ms: float = 0.0


class ScreenerService:
    def __init__(self, repo: KlineRepository, asset_type: str = "stock", market: str = "cn") -> None:
        from app.markets import get_market

        get_market(market)  # 非法市场 fail-closed
        self.repo = repo
        self.asset_type = asset_type
        self.market = market
        from app.tickflow.repository import enriched_dirname
        self._enriched_dirname = enriched_dirname(asset_type)

    def _market_instruments(self) -> pl.DataFrame:
        """该市场的标的维表。

        cn 沿用共享 instruments 池; 港美股读 instruments_index_{market}。
        用 A 股维表 join 港美股会把这些行的 name/float_shares 全变成 null
        （进而换手率、市值、快照名称全空）。
        """
        if self.asset_type == "stock" and self.market != "cn":
            return self.repo.get_instruments_market(self.market)
        return self.repo.get_instruments_asset(self.asset_type)

    def _filter_market(self, df: pl.DataFrame) -> pl.DataFrame:
        """共享存储下按逻辑市场隔离：cn 保留 .SH/.SZ/.BJ 及无后缀数字，
        hk 仅 .HK，us 仅 .US。空表/无 symbol 列安全返回。"""
        from app.markets import filter_symbols

        return filter_symbols(df, self.market)

    @staticmethod
    def clear_history_cache(asset_type: str | None = None) -> None:
        """清空进程级 _history_cache (TTL 缓存)。

        清除数据后调用, 避免内存里的旧历史窗口残留导致策略/看板仍命中旧数据。
        """
        if asset_type is None:
            _history_cache.clear()
            _market_day_cache.clear()
            _market_latest_cache.clear()
        else:
            for key in list(_history_cache):
                if key[0] == asset_type:
                    _history_cache.pop(key, None)
            for key in list(_market_day_cache):
                if key[0] == asset_type:
                    _market_day_cache.pop(key, None)
            for key in list(_market_latest_cache):
                if key[1] == asset_type:
                    _market_latest_cache.pop(key, None)

    def _assert_integrity(self, end: date, *, enriched_only=False) -> None:
        from app.services.daily_health import assert_usable

        root = getattr(getattr(self.repo, "store", None), "data_dir", None)
        if root is not None:
            assert_usable(root, self.asset_type, end=end, enriched_only=enriched_only,
                          market=self.market)

    def _load_enriched_for_date_raw(self, target_date: date) -> pl.DataFrame:
        """从 enriched parquet 读取指定日期的基础数据并即时计算完整指标+信号（未做市场过滤）。"""
        self._assert_integrity(target_date, enriched_only=True)
        # repo 的最新日/历史缓存是按 A 股口径（compute_all(market="cn") + A 股维表）
        # 构建的单一快照, 港美股读它会拿到被 A 股维表 join 掉 name/float_shares 的行
        # (换手率/市值全空), 因此非 cn 一律走下面的 parquet + 按市场即时计算路径。
        if self.market == "cn":
            cache, cache_date = self.repo.get_enriched_latest_asset(self.asset_type)
            if cache is not None and not cache.is_empty() and cache_date == target_date:
                return _join_instruments(cache, self._market_instruments())

        # 尝试从 repo 级预计算历史缓存中提取目标日期 (仅 stock: 该缓存为股票专用)
        if self.asset_type == "stock" and self.market == "cn":
            cached_hist = self.repo.get_enriched_history(target_date, 1)
            if cached_hist is not None and not cached_hist.is_empty() and "date" in cached_hist.columns:
                df = cached_hist.filter(pl.col("date") == target_date)
                if not df.is_empty():
                    logger.debug("_load_enriched_for_date: repo history cache for %s", target_date)
                    return _join_instruments(df, self.repo.get_instruments_asset(self.asset_type))

        # 历史日期: 从 parquet 读取 14 列, 即时计算指标 (慢路径)
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        ds = target_date.isoformat()
        target_parquet = enriched_dir / f"date={ds}" / "part.parquet"

        if not target_parquet.exists():
            return pl.DataFrame()

        try:
            df = pl.read_parquet(target_parquet)
        except Exception as e:  # noqa: BLE001
            logger.warning("load_enriched_for_date failed: %s", e)
            return pl.DataFrame()

        if df.is_empty():
            return df

        # 即时计算指标: 需要加载历史窗口作 warmup
        df_full = self._compute_enriched_full(df, target_date)
        return df_full

    def _load_enriched_for_date(self, target_date: date) -> pl.DataFrame:
        """读取指定日期 enriched 并按实例 market 过滤（共享存储逻辑隔离）。"""
        if self.market == "cn":
            return self._filter_market(self._load_enriched_for_date_raw(target_date))
        # 港美股不走 A 股内存缓存, 每次都要扫 warmup 窗口并即时算指标 (秒级)。
        # 概览/快照/梯队会在同一屏内连续请求, 用 60s 进程内缓存挡住重复计算。
        key = (self.asset_type, self.market, target_date)
        now = time.monotonic()
        cached = _market_day_cache.get(key)
        if cached is not None and now - cached[0] < _MARKET_DAY_CACHE_TTL:
            return cached[1]
        df = self._filter_market(self._load_enriched_for_date_raw(target_date))
        _market_day_cache[key] = (now, df)
        return df

    def load_prior_consecutive(self, as_of: date, consec_col: str) -> pl.DataFrame:
        """窄读: 仅取前一交易日的 [symbol, consec_col] 两列 (谓词下推到单日 parquet)。

        consecutive_limit_ups / consecutive_limit_downs 是 enriched 的存储列,
        可直接从 parquet 读取, 无需 _load_enriched_for_date 的全量指标重算
        (历史日期该慢路径最坏会触发 9 次全市场 compute_enriched_full)。

        由近到远取 as_of 之前已存在的日分区 (即前一交易日), 读取其
        symbol + consec_col。存储列的值与重算值逐位一致 (连板计数为 run-length,
        150 天 warmup 完全覆盖 A 股最长连板, 二者相等)。

        返回列: symbol, prev_consec。找不到前一交易日时返回空 DataFrame。
        """
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        for candidate in self._prior_partition_dates(as_of, _PRIOR_PARTITION_SCAN):
            target_parquet = enriched_dir / f"date={candidate.isoformat()}" / "part.parquet"
            if not target_parquet.exists():
                continue
            try:
                lf = pl.scan_parquet(target_parquet)
                cols = lf.collect_schema().names()
            except Exception as e:  # noqa: BLE001
                logger.warning("load_prior_consecutive scan failed for %s: %s", candidate, e)
                return pl.DataFrame()
            # 存储列理论上必含 consec_col; 若该分区缺列则继续向前找 (与旧循环一致)
            if "symbol" not in cols or consec_col not in cols:
                continue
            try:
                return lf.select(
                    "symbol",
                    pl.col(consec_col).alias("prev_consec"),
                ).collect()
            except Exception as e:  # noqa: BLE001
                logger.warning("load_prior_consecutive read failed for %s: %s", candidate, e)
                return pl.DataFrame()
        return pl.DataFrame()

    def _prior_partition_dates(self, as_of: date, limit: int) -> list[date]:
        """enriched 目录里早于 as_of 的分区日期, 由近到远最多 limit 个。

        枚举分区目录而不是按自然日回看固定天数: 春节长假连着调休周末,
        相邻两个交易日能隔 10~11 个自然日 (如 2024-02-08 → 2024-02-19),
        固定窗口会整段落空。与 auction_benchmark._prev_trading_day
        「本地日K分区日期 = 已知交易日集合」同口径。
        """
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        days: list[date] = []
        try:
            entries = list(enriched_dir.iterdir())
        except OSError:
            return []
        for part in entries:
            if not part.name.startswith("date="):
                continue
            try:
                day = date.fromisoformat(part.name[5:])
            except ValueError:
                continue
            if day < as_of:
                days.append(day)
        days.sort(reverse=True)
        return days[:limit]

    def _compute_enriched_full(self, df_target: pl.DataFrame, target_date: date) -> pl.DataFrame:
        """从 14 列基础数据即时计算完整 enriched (含全部指标和信号)。

        读取历史数据作为指标计算的 warmup, 计算完成后只返回目标日期的行。
        """
        from app.indicators.pipeline import (
            compute_indicators,
            compute_limit_signals,
            compute_signals,
        )

        # 加载 warmup 历史 (目标日期前 ~120 天)
        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        start = target_date - timedelta(days=150)
        # turnover_rate 是 enriched 存储列, 必须随行透传: 否则即时计算后该列
        # 丢失, 自定义 SQL 用它做条件会 Binder Error 被吞成空结果 (#187)
        read_cols = ["symbol", "date", "open", "high", "low", "close", "volume",
                     "amount", "raw_close", "raw_high", "raw_low", "turnover_rate",
                     "quote_ts"]  # 实时快照时间戳: 港美股慢路径透传给新鲜度判定

        try:
            from app.markets import symbol_filter_expr

            lf = (
                scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
                .filter(
                    (pl.col("date") >= start)
                    & (pl.col("date") <= target_date)
                )
                # 共享 parquet 里同时存着 A 股/港股/美股; 市场过滤必须下推到
                # collect 之前, 否则港美股单次总览会把 150 天全市场宽帧收进
                # 内存再丢弃, 是容器 OOM 的主要来源。
                .filter(symbol_filter_expr(self.market))
                .sort(["symbol", "date"])
            )
            available = [c for c in read_cols if c in lf.collect_schema().names()]
            df_hist = guarded_collect(lf.select(available), priority="interactive")
        except Exception as e:  # noqa: BLE001
            logger.warning("warmup history load failed: %s", e)
            df_hist = df_target

        if df_hist.is_empty():
            df_hist = df_target

        # 共享存储: 先切出本市场, 否则 A 股维表的涨跌停/换手率会作用到港美股行
        from app.markets import filter_symbols
        df_hist = filter_symbols(df_hist, self.market)

        # 计算指标
        df_full = compute_indicators(df_hist)
        df_full = compute_signals(df_full)

        # 计算涨跌停信号 (需要 instruments; 涨停为股票专有, ETF 跳过)
        instruments = self._market_instruments()
        if self.asset_type == "stock" and self.market == "cn" and instruments is not None and not instruments.is_empty():
            df_full = compute_limit_signals(
                df_full,
                instruments,
                historical_shares=self.repo.get_historical_shares(),
            )
        elif self.asset_type == "stock" and self.market != "cn":
            # 港美股无涨跌停/连板: 补齐列契约 (恒 false/0), 且不覆盖存储里的
            # turnover_rate —— compute_limit_signals 会用 A 股维表重算换手率,
            # 对港美股只会算出 null。
            df_full = df_full.with_columns(
                pl.lit(False).alias("signal_limit_up"),
                pl.lit(False).alias("signal_limit_down"),
                pl.lit(False).alias("signal_limit_down_recovery"),
                pl.lit(False).alias("signal_broken_limit_up"),
                pl.lit(0).alias("consecutive_limit_ups"),
                pl.lit(0).alias("consecutive_limit_downs"),
            )

        # 只保留目标日期
        df_result = df_full.filter(pl.col("date") == target_date)

        return _join_instruments(df_result, instruments)

    def _load_enriched_history_raw(self, target_date: date, lookback_days: int) -> pl.DataFrame:
        """读取目标日期之前的基础行情数据（未做市场过滤）。

        缓存 miss 时走 scan_parquet + compute_indicators 慢路径。
        """
        self._assert_integrity(target_date, enriched_only=True)
        # 优先级 1: repo 级预计算缓存 (启动时 _refresh_enriched 已计算完整历史; 仅 stock)
        t0 = time.perf_counter()
        if self.asset_type == "stock" and self.market == "cn":
            cached = self.repo.get_enriched_history(target_date, lookback_days)
            if cached is not None and not cached.is_empty():
                # JOIN instruments (repo 缓存不含 name 等列)
                instruments = self._market_instruments()
                if instruments is not None and not instruments.is_empty() and "name" not in cached.columns:
                    inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"]
                                 if c in instruments.columns]
                    cached = cached.join(instruments.select(inst_cols), on="symbol", how="left")
                elapsed = (time.perf_counter() - t0) * 1000
                logger.info("_load_enriched_history(%s, %d): repo cache hit, %.1fms, %d rows",
                            target_date, lookback_days, elapsed, len(cached))
                return cached

        # 优先级 2: 进程级 history_cache (之前的 TTL 缓存)
        # market 进入缓存键: 共享存储下不同市场必须隔离, 否则切换市场命中旧结果。
        cache_key = (self.asset_type, self.market, target_date, lookback_days)
        now = time.monotonic()
        ttl_cached = _history_cache.get(cache_key)
        if ttl_cached is not None:
            ts, cached_df = ttl_cached
            if now - ts < _HISTORY_CACHE_TTL:
                logger.debug("history TTL cache hit: %s lookback=%d", target_date, lookback_days)
                return cached_df
            del _history_cache[cache_key]

        # 优先级 3: scan_parquet + compute_indicators (慢路径, ~5s)
        logger.warning("_load_enriched_history cache miss, computing indicators (%s, %d)...",
                       target_date, lookback_days)
        from app.indicators.pipeline import (
            compute_indicators,
            compute_limit_signals,
            compute_signals,
        )

        warmup = 60
        start = target_date - timedelta(days=min((lookback_days + warmup) * 2, 180))

        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        # 同 _compute_enriched_full: turnover_rate 存储列随行透传 (#187)
        read_cols = ["symbol", "date", "open", "high", "low", "close", "volume",
                     "amount", "raw_close", "raw_high", "raw_low", "turnover_rate",
                     "quote_ts"]  # 实时快照时间戳: 港美股慢路径透传给新鲜度判定

        try:
            from app.markets import symbol_filter_expr

            lf = (
                scan_enriched_parquet(str(enriched_dir / "**" / "*.parquet"))
                .filter((pl.col("date") >= start) & (pl.col("date") <= target_date))
                # 与 _compute_enriched_full 同理: 先在 scan 阶段裁掉其它市场,
                # 避免 180 天全市场宽帧进内存后再 filter_symbols 丢弃。
                .filter(symbol_filter_expr(self.market))
                .sort(["symbol", "date"])
            )
            available = [c for c in read_cols if c in lf.collect_schema().names()]
            df_hist = guarded_collect(lf.select(available), priority="interactive")
        except Exception as e:  # noqa: BLE001
            logger.warning("load_enriched_history failed: %s", e)
            return pl.DataFrame()

        if df_hist.is_empty():
            return pl.DataFrame()

        from app.markets import filter_symbols
        df_hist = filter_symbols(df_hist, self.market)

        df_full = compute_indicators(df_hist)
        df_full = compute_signals(df_full)

        instruments = self._market_instruments()
        if self.asset_type == "stock" and self.market == "cn" and instruments is not None and not instruments.is_empty():
            df_full = compute_limit_signals(
                df_full,
                instruments,
                historical_shares=self.repo.get_historical_shares(),
            )
        elif self.asset_type == "stock" and self.market != "cn":
            df_full = df_full.with_columns(
                pl.lit(False).alias("signal_limit_up"),
                pl.lit(False).alias("signal_limit_down"),
                pl.lit(False).alias("signal_limit_down_recovery"),
                pl.lit(False).alias("signal_broken_limit_up"),
                pl.lit(0).alias("consecutive_limit_ups"),
                pl.lit(0).alias("consecutive_limit_downs"),
            )

        if instruments is not None and not instruments.is_empty():
            inst_cols = [c for c in ["symbol", "name", "total_shares", "float_shares"] if c in instruments.columns]
            if "name" not in df_full.columns:
                df_full = df_full.join(instruments.select(inst_cols), on="symbol", how="left")

        # 裁剪掉 warmup 部分, 只保留 lookback 范围 (减少 group_by 开销)。
        # 按交易日计数: 从数据里实际存在的交易日序列取最后 lookback_days 个交易日,
        # 不能用 timedelta(days=N) (自然日), 否则周末/节假日会让窗口偏少, 与回测不一致。
        if "date" in df_full.columns:
            trading_dates = df_full["date"].unique().sort()
            if len(trading_dates) > lookback_days:
                lookback_start = trading_dates[-(lookback_days + 1)]
            else:
                lookback_start = trading_dates[0]
            df_full = df_full.filter(pl.col("date") >= lookback_start)

        df_full = df_full.sort(["symbol", "date"])

        elapsed = (time.perf_counter() - t0) * 1000
        logger.info("_load_enriched_history(%s, %d): computed in %.1fms, %d rows",
                    target_date, lookback_days, elapsed, len(df_full))

        _history_cache[cache_key] = (now, df_full)
        # TTL 先清一轮; 仍超上限时按最旧无条件淘汰 — 否则两分钟内出现 >10 个
        # 不同 (asset_type, date, lookback) 键时全部新鲜、零逐出, 数 GB 宽帧驻留
        if len(_history_cache) > 10:
            expired = [k for k, (ts, _) in _history_cache.items() if now - ts > _HISTORY_CACHE_TTL]
            for k in expired:
                del _history_cache[k]
            while len(_history_cache) > 10:
                oldest = min(_history_cache, key=lambda k: _history_cache[k][0])
                del _history_cache[oldest]

        return df_full

    def _load_enriched_history(self, target_date: date, lookback_days: int) -> pl.DataFrame:
        """读取历史窗口并按实例 market 过滤（共享存储逻辑隔离）。"""
        return self._filter_market(self._load_enriched_history_raw(target_date, lookback_days))

    def run(
        self,
        as_of: date,
        conditions: list[str],
        order_by: str | None = None,
        limit: int = 30,
        pool: list[str] | None = None,
    ) -> ScreenerResult:
        """自定义 SQL 条件筛选。

        先通过 Polars 即时计算完整指标, 再用 DuckDB 做 SQL WHERE 过滤。
        kline_enriched DuckDB 视图只有 14 列, 不能直接用于指标过滤。
        """
        self._assert_integrity(as_of)
        t0 = time.perf_counter()

        if not conditions:
            return ScreenerResult(as_of=as_of, strategy=None)

        # 从即时计算获取完整 enriched 数据
        df = self._load_enriched_for_date(as_of)
        if df.is_empty():
            return ScreenerResult(as_of=as_of, strategy=None)

        # Pool 过滤
        if pool:
            df = df.filter(pl.col("symbol").is_in(pool))

        # 用 DuckDB 做 SQL 过滤 (注册临时视图)
        # 用独立的 :memory: 连接 (而非复用 repo 共享连接的 cursor): conditions 是用户
        # 传入的 SQL 片段, 隔离连接下注入至多能碰 read_csv/read_parquet 文件; 若复用共享
        # 连接则会把 app 已注册的真实业务表也暴露给注入, 扩大攻击面。隔离连接创建开销极低。
        # 再关闭 external_access, 让注入的文件读写函数 (read_parquet/COPY 等) 直接报错,
        # 视图数据仍通过 con.register 注入, 不受该开关影响 (#224)。
        con = None
        try:
            import duckdb
            con = duckdb.connect(
                database=":memory:", config={"enable_external_access": False}
            )
            con.register("enriched", df.to_arrow())
            where = " AND ".join(f"({c})" for c in conditions)
            sql = f"SELECT * FROM enriched WHERE {where}"
            if order_by:
                sql += f" ORDER BY {order_by}"
            if limit:
                sql += f" LIMIT {limit}"
            df_result = con.execute(sql).pl()
        except Exception as e:  # noqa: BLE001
            logger.warning("screener SQL query failed: %s", e)
            df_result = pl.DataFrame()
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:  # noqa: BLE001
                    pass

        rows = df_result.to_dicts() if not df_result.is_empty() else []
        elapsed = (time.perf_counter() - t0) * 1000

        return ScreenerResult(
            as_of=as_of,
            strategy=None,
            rows=rows,
            total=len(rows),
            elapsed_ms=elapsed,
        )

    def build_strategy_context(
        self,
        engine,
        as_of: date,
        strategy_ids: list[str],
        *,
        timeframe: str = "1d",
        params_map: dict[str, dict] | None = None,
        overrides_map: dict[str, dict] | None = None,
        current: pl.DataFrame | None = None,
        market=None,
        cache_key: str | None = None,
    ):
        """按调用方要求装配标准策略数据上下文，不解释策略公式。"""
        from app.strategy.engine import StrategyDataContext

        self._assert_integrity(as_of)
        if current is None:
            current = self._load_enriched_for_date(as_of)
        if timeframe == "1m":
            # 分钟策略数据源是本地当日分钟K分区 (单分区文件直读), 与日线
            # enriched 历史窗口无关, 不走 required_history_bars 日线路径。
            history = self._load_minute_history(as_of, current)
            # 策略声明 META["daily_history_bars"] 时额外装配日线 enriched 窗口,
            # 供分钟策略叠加日线维度条件 (如 N 日内涨停过)。
            daily_history = None
            if engine is not None:
                daily_bars = engine.minute_daily_history_bars(strategy_ids)
                if daily_bars > 0:
                    daily_history = self._load_enriched_history(as_of, daily_bars)
            return StrategyDataContext(
                asset_type=self.asset_type,
                timeframe=timeframe,
                as_of=as_of,
                current=current,
                history=history,
                daily_history=daily_history,
                market=None,
                cache_key=cache_key,
            )
        history_bars = engine.required_history_bars(
            strategy_ids,
            params_map=params_map,
            overrides_map=overrides_map,
        )
        history = None
        if history_bars > 1:
            history = self._load_enriched_history(as_of, history_bars)
        # enriched 换代号: 供 engine.build_shared_matrix 的矩阵缓存做失效指纹
        # (取不到时 None → 缓存 fail-closed 不启用)
        try:
            data_generation = self.repo.get_matrix_data_generation(self.asset_type)
        except Exception:  # noqa: BLE001
            data_generation = None
        return StrategyDataContext(
            asset_type=self.asset_type,
            timeframe=timeframe,
            as_of=as_of,
            current=current,
            history=history,
            market=market,
            cache_key=cache_key,
            data_generation=data_generation,
        )

    def _load_minute_history(self, as_of: date, current: pl.DataFrame | None) -> pl.DataFrame:
        """分钟策略数据源: 优先 as_of 当日分钟分区, 缺失时回退全市场最近分区。

        只按日期直读单个分区文件 (get_minute_by_dates), 与全量 glob 扫描解耦,
        内存只随当日分区大小 (~67万行) 走。标的池限定为 enriched 快照 universe;
        分区与快照的日期差是允许的 (分钟分区可能比 enriched 更新, 行自带时间戳)。
        """
        if self.asset_type != "stock":
            raise ValueError("分钟策略当前仅支持 A 股")
        symbols: list[str] = []
        if current is not None and not current.is_empty():
            symbols = current["symbol"].cast(pl.Utf8).unique().to_list()
        if not symbols:
            return pl.DataFrame()
        df = self.repo.get_minute_by_dates(symbols, [as_of])
        if df.is_empty():
            fallback = self.repo.latest_minute_date_global()
            if fallback is None:
                raise ValueError(
                    "无分钟K数据 — 请先在 数据→分钟K 完成同步, 或开启盘中增量刷新"
                )
            if fallback != as_of:
                df = self.repo.get_minute_by_dates(symbols, [fallback])
        return df

    def latest_date(self) -> date | None:
        if self.market != "cn":
            # 共享存储：全局最新日期可能只有 A 股数据。hk/us 需要找到「本市场
            # 确实有数据」的最新分区，否则未同步时会错误显示 A 股日期。
            return self._latest_date_for_market()

        if self.asset_type != "stock":
            _, d = self.repo.get_enriched_latest_asset(self.asset_type)
            return d
        d = self.repo.enriched_latest_date()
        if d:
            return d
        # 回退 DuckDB
        try:
            res = self.repo.execute_one(
                "SELECT max(date) FROM kline_enriched",
            )
            if res and res[0]:
                d = res[0]
                return d if isinstance(d, date) else date.fromisoformat(str(d))
        except Exception:  # noqa: BLE001
            return None
        return None

    def _latest_date_for_market(self, scan_limit: int = 10) -> date | None:
        """返回本市场「最近且有足够覆盖」的 enriched 日期。

        共享存储下, 港美股盘后同步/盘中落库可能只写入少量标的, 若按「有任意
        一行就返回」会选中这种半成品分区 (实测 11-26 只有 45 只港股/0 只美股),
        把前一完整交易日 2000+ 只的数据遮蔽掉, 看板于是只剩几十个标的。
        改为: 统计最近 scan_limit 个分区的本市场标的数, 返回最近一个覆盖达到
        近窗最大值一半的日期; 全都偏少时退回最近日期。
        """
        from app.markets import symbol_filter_expr

        cache_key = (self.market, self.asset_type)
        now = time.monotonic()
        cached = _market_latest_cache.get(cache_key)
        if cached is not None and now - cached[0] < _MARKET_LATEST_TTL:
            return cached[1]

        enriched_dir = self.repo.store.data_dir / self._enriched_dirname
        try:
            parts = [p for p in enriched_dir.iterdir() if p.name.startswith("date=")]
        except OSError:
            return None
        days: list[date] = []
        for p in parts:
            try:
                days.append(date.fromisoformat(p.name[5:]))
            except ValueError:
                continue
        days.sort(reverse=True)

        counts: list[tuple[date, int]] = []
        for day in days[:scan_limit]:
            part = enriched_dir / f"date={day.isoformat()}" / "part.parquet"
            if not part.exists():
                continue
            try:
                lf = pl.scan_parquet(str(part))
                if "symbol" not in lf.collect_schema().names():
                    continue
                n = (
                    lf.select(symbol_filter_expr(self.market).alias("_hit"))
                    .filter(pl.col("_hit"))
                    .select(pl.len())
                    .collect()
                    .item()
                )
                if n:
                    counts.append((day, int(n)))
            except Exception as e:  # noqa: BLE001
                logger.warning("latest_date_for_market scan failed for %s: %s", day, e)
                continue

        result: date | None
        if not counts:
            result = None
        else:
            best = max(n for _, n in counts)
            threshold = max(1, best // 2)
            result = next((d for d, n in counts if n >= threshold), counts[0][0])
        _market_latest_cache[cache_key] = (now, result)
        return result

    def coverage_warnings(self, as_of: date, *, required_bars: int | None = None) -> list[str]:
        """数据充足性提示 (#303): enriched 覆盖不足时返回用户可读警告, 充足返回 []。

        空库首跑只拉到 1 个交易日时, 均线/动量/量比等指标暖机不足, 策略会静默
        全 0 — 这里把"数据不够"显式说出来。required_bars 缺省用通用暖机窗口
        (自定义 SQL 筛选); 策略运行传 engine.required_history_bars 的精确值。
        available 为 0 时 enriched 为空, 上层 latest_date 已 400, 不重复提示。
        """
        available = enriched_history_days(self.repo.store.data_dir, self.asset_type, as_of)
        if available == 0:
            return []
        need = required_bars if required_bars and required_bars > 0 else MIN_INDICATOR_WARMUP_DAYS
        if available >= need:
            return []
        return [
            f"本地数据仅覆盖 {available} 个交易日, 低于本次计算所需约 {need} 天暖机窗口 — "
            "指标可能失真或全部落空 (策略命中 0)。建议先全量回填日K并重算指标 "
            "(数据页「日K批量同步」, 符号需带交易所后缀)"
        ]
