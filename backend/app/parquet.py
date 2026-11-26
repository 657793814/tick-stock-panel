"""Polars parquet helpers."""
from __future__ import annotations

from typing import Any

import polars as pl

DAILY_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "quote_ts": pl.Int64,
}

ENRICHED_STORAGE_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.Utf8,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
    "raw_close": pl.Float64,
    "raw_high": pl.Float64,
    "raw_low": pl.Float64,
    "turnover_rate": pl.Float64,
    # 连板/连跌数按「最宽整型」声明, 读取时 integer_cast="upcast" 把 UInt32/Int32
    # 一律提升过来。历史教训: 声明 UInt32 时, 只要有一个写入方写了 Int32 (港美股
    # 早期用裸 pl.lit(0)), 按 schema 扫描整张 kline_daily_enriched 就会报
    # "incoming Int32 != target UInt32" 并整体失败 —— 一个写入方的类型偏差
    # 会让所有市场的数据同时读不出来。宽类型 + upcast 让读取对宽度不敏感。
    "consecutive_limit_ups": pl.Int64,
    "consecutive_limit_downs": pl.Int64,
    "quote_ts": pl.Int64,
}


def align_storage_dtypes(df: pl.DataFrame, schema: dict[str, pl.DataType]) -> pl.DataFrame:
    """把已声明的存储列对齐到标准 dtype, 未声明列原样保留。

    数据源常用整型表达 volume/amount (实时快照股数、扶摇 floor 取整), 而存储
    契约声明 Float64。同一分区混入两种类型后, 之后按 schema 扫描整张表会抛
    "incoming Int64 != target Float64", 让整个同步阶段失败。落盘前统一归一,
    strict 保持默认: 转换不了的数据直接报错, 不静默写成 null。
    """
    casts = [
        pl.col(name).cast(dtype)
        for name, dtype in schema.items()
        if name in df.columns and df.schema[name] != dtype
    ]
    return df.with_columns(casts) if casts else df


def scan_parquet_compat(source: Any, **kwargs: Any) -> pl.LazyFrame:
    """Scan partitioned parquet while tolerating additive schema changes."""
    kwargs.setdefault("missing_columns", "insert")
    kwargs.setdefault("extra_columns", "ignore")
    return pl.scan_parquet(source, **kwargs)


def scan_daily_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    kwargs.setdefault("schema", DAILY_STORAGE_SCHEMA)
    # 价格/量额声明 Float64, 但历史上确有整型写入方 (实时快照 volume 为整型,
    # 扶摇 floor 取整), 已落盘的分区因此与 schema 不一致。整型/窄浮点 → Float64
    # 是保值转换, 允其通过才能读到存量数据; 写入侧由 align_storage_dtypes 归一。
    # 日K 唯一的整型存储列 quote_ts 各写入方均为毫秒 Int64, 故不需整型宽度容忍。
    kwargs.setdefault(
        "cast_options",
        pl.ScanCastOptions(integer_cast="allow-float", float_cast="upcast"),
    )
    return scan_parquet_compat(source, **kwargs)


def scan_enriched_parquet(source: Any, **kwargs: Any) -> pl.LazyFrame:
    # 两类历史 dtype 偏差要同时兼容:
    #   1) volume/amount 声明 Float64, 写入方可能写了 Int64 → 需 allow-float;
    #   2) 连板/连跌数声明 Int64, 写入方可能写 UInt32/Int32 → upcast 才放行。
    # Polars 的 integer_cast 一次只能选一种, 因此扫描时把整型计数列按 Float64
    # 声明(allow-float 对 UInt32/Int32/Int64 全部放行), 扫描后再转回 Int64。
    _INT_COUNT_COLS = ("consecutive_limit_ups", "consecutive_limit_downs")
    scan_schema = {
        name: (pl.Float64 if name in _INT_COUNT_COLS else dtype)
        for name, dtype in ENRICHED_STORAGE_SCHEMA.items()
    }
    kwargs.setdefault("schema", scan_schema)
    kwargs.setdefault(
        "cast_options",
        pl.ScanCastOptions(integer_cast="allow-float", float_cast="upcast"),
    )
    lf = scan_parquet_compat(source, **kwargs)
    # 调用方可能用自定义 schema 的 LazyFrame 桩 (单测) 或缺列的历史分区,
    # 只对实际存在的计数列回转, 避免凭空造列。
    present = set(lf.collect_schema().names())
    casts = [
        pl.col(name).cast(pl.Int64, strict=False)
        for name in _INT_COUNT_COLS
        if name in present
    ]
    return lf.with_columns(casts) if casts else lf
