"""财务/股东/事件因子: 基于本地快照的点时 (point-in-time) 无未来函数接入。

数据契约:
- 输入为 ``data/financials/{table}/part.parquet``; ``metrics`` 每行一份报告期
  财务指标, ``holder_metrics`` 每行一个报告期的股东结构聚合 (见
  ``plugins/xtick/holders.py``), ``lhb_metrics`` / ``margin_metrics`` 每行一个
  (标的, 交易日) 的龙虎榜/两融事件 (见 ``services/eastmoney_sync.py``);
- ``announce_date`` 是公告日 (日频表即交易日)。因子只在 **严格晚于公告日的交易日**
  才有值 (公告多在盘后发布, 保守取 T+1 生效), 此前保持 null;
- 报告期表 (metrics/holder_metrics) 按 (symbol, period_end) 累积, 生效后前向填充
  到下一期; 日频表 (lhb_metrics/margin_metrics) 只按交易日精确对齐, **不前向填充**
  —— 龙虎榜"没上榜"与"沿用上次上榜"是两件事;
- 无数据的标的/日期一律为 null, 绝不填 0 (填 0 会污染截面排名,
  例如资产负债率 0 会被当成最优杠杆)。唯一例外是因子显式声明 ``fill``
  (如"未上龙虎榜 ⇒ 净买占比 = 0"), 且只在数据覆盖区间内生效。

性能:
- 财务表约数千~数万行, join_asof 按 symbol 分组回填, 对百万行面板的代价是
  毫秒级; 矩阵路径每个因子只物化一张 float32 TxN 矩阵 (T~900, N~5500
  约 20MB), 且仅在策略/挖掘请求该因子时才构建。
- 日频表按 (symbol, date) 哈希连接, 单表约 1M 行也能在百毫秒级完成。
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import MappingProxyType
from typing import Any

import numpy as np
import polars as pl

logger = logging.getLogger(__name__)

# 因子名 -> {table: 来源表, column: 列名, price_ratio: 是否除以收盘价}
# pb_latest 单列声明为 bps 倒数口径: 因子值 = close / bps。
#
# 可选键:
#   freq="daily"  日频表 (东财龙虎榜/两融): 按交易日精确对齐, T 日数据在 T+1 生效,
#                 **不做前向填充** (没有行 = 当日无事件, 与"上一期报告期沿用"语义不同);
#   fill=0.0      无事件时填 0 (仅用于"未上榜 ⇒ 净买占比=0"这类语义上确为 0 的因子);
#                 未声明 fill 的因子保持 null, 由下游 IC/分层自动剔除。
FUNDAMENTAL_FACTORS: dict[str, dict[str, Any]] = {
    "pb_latest": {"table": "metrics", "column": "bps", "price_ratio": True},
    "roe_latest": {"table": "metrics", "column": "roe", "price_ratio": False},
    "gross_margin_latest": {"table": "metrics", "column": "gross_margin", "price_ratio": False},
    "net_margin_latest": {"table": "metrics", "column": "net_margin", "price_ratio": False},
    "revenue_yoy_latest": {"table": "metrics", "column": "revenue_yoy", "price_ratio": False},
    "net_income_yoy_latest": {"table": "metrics", "column": "net_income_yoy", "price_ratio": False},
    "debt_ratio_latest": {"table": "metrics", "column": "debt_to_asset_ratio", "price_ratio": False},
    # --- 股东结构 (XTick holder_metrics, 季报/临时公告口径) ---
    "holder_num": {"table": "holder_metrics", "column": "holder_num", "price_ratio": False},
    "holder_num_chg": {"table": "holder_metrics", "column": "holder_num_chg", "price_ratio": False},
    "top10_hold_ratio": {"table": "holder_metrics", "column": "top10_ratio", "price_ratio": False},
    "top10_flow_ratio": {"table": "holder_metrics", "column": "flow_ratio", "price_ratio": False},
    "top10_hhi": {"table": "holder_metrics", "column": "top10_hhi", "price_ratio": False},
    "top10_new_count": {"table": "holder_metrics", "column": "top10_new_count", "price_ratio": False},
    # --- 龙虎榜 (东方财富 lhb_metrics, 日频; 未上榜按 0 处理) ---
    "lhb_net_ratio": {
        "table": "lhb_metrics", "column": "lhb_net_ratio", "price_ratio": False,
        "freq": "daily", "fill": 0.0,
    },
    "lhb_on_board": {
        "table": "lhb_metrics", "column": "lhb_on_board", "price_ratio": False,
        "freq": "daily", "fill": 0.0,
    },
    "lhb_reason_count": {
        "table": "lhb_metrics", "column": "lhb_reason_count", "price_ratio": False,
        "freq": "daily", "fill": 0.0,
    },
    # --- 融资融券 (东方财富 margin_metrics, 日频; 比例口径均以流通市值归一) ---
    "margin_balance_ratio": {
        "table": "margin_metrics", "column": "margin_balance_ratio", "price_ratio": False,
        "freq": "daily",
    },
    "margin_buy_ratio": {
        "table": "margin_metrics", "column": "margin_buy_ratio", "price_ratio": False,
        "freq": "daily",
    },
    "margin_net_5d_ratio": {
        "table": "margin_metrics", "column": "margin_net_5d_ratio", "price_ratio": False,
        "freq": "daily",
    },
    "margin_short_ratio": {
        "table": "margin_metrics", "column": "margin_short_ratio", "price_ratio": False,
        "freq": "daily",
    },
}

FUNDAMENTAL_FACTOR_NAMES = frozenset(FUNDAMENTAL_FACTORS)


def _table_of(name: str) -> str:
    return str(FUNDAMENTAL_FACTORS[name].get("table") or "metrics")


def _is_daily(name: str) -> bool:
    """日频事件表 (龙虎榜/两融): 精确日对齐, 不前向填充。"""
    return str(FUNDAMENTAL_FACTORS[name].get("freq") or "") == "daily"


def _fill_value(name: str) -> float | None:
    value = FUNDAMENTAL_FACTORS[name].get("fill")
    return None if value is None else float(value)


_TABLE_SYNC_HINTS = {
    "metrics": "请先在 数据 → 财务同步 拉取财务数据",
    "holder_metrics": "请先在 /xtick 页「股东结构」同步股东数据",
    "lhb_metrics": "请先在 /eastmoney 页同步龙虎榜",
    "margin_metrics": "请先在 /eastmoney 页同步融资融券",
}


def missing_hint(names: Any) -> str:
    """按因子来源表给出"去哪同步"的具体指引 (而不是笼统的"先同步数据")。"""
    hints = [
        _TABLE_SYNC_HINTS.get(table, "请先同步该数据")
        for table in sorted(fundamental_tables(names))
    ]
    head = "本地没有该因子所需的数据"
    return f"{head}: {'；'.join(hints)}" if hints else head


def fundamental_tables(names: Any = None) -> set[str]:
    """请求的因子用到的来源表集合; names 为空时返回全部。"""
    if names is None:
        return {_table_of(name) for name in FUNDAMENTAL_FACTORS}
    return {
        _table_of(str(name))
        for name in names
        if str(name) in FUNDAMENTAL_FACTORS
    }


def load_fundamental_snapshot(
    data_dir: Path | None,
    table: str = "metrics",
) -> pl.DataFrame | None:
    """读取单张因子来源表快照; 文件缺失或无有效行时返回 None。

    返回列: symbol, announce_date, _announce (Date), 以及该表各因子的源列。
    """
    if data_dir is None:
        return None
    path = data_dir / "financials" / table / "part.parquet"
    if not path.exists():
        return None
    try:
        frame = pl.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 — 读失败按无数据处理, 下次同步重建
        logger.warning("读取 %s 因子快照失败: %s", table, exc)
        return None

    columns = {
        str(spec["column"])
        for name, spec in FUNDAMENTAL_FACTORS.items()
        if _table_of(name) == table
    }
    needed = {"symbol", "announce_date"} | columns
    if not needed.issubset(frame.columns):
        logger.warning("%s 因子快照缺少列: %s", table, sorted(needed - set(frame.columns)))
        return None
    snapshot = (
        frame.select(sorted(needed))
        .filter(
            pl.col("symbol").is_not_null()
            & pl.col("announce_date").is_not_null()
        )
        .with_columns(
            pl.col("announce_date").cast(pl.Utf8).str.slice(0, 10).str.to_date().alias("_announce")
        )
        .unique(subset=["symbol", "_announce"], keep="last", maintain_order=True)
        .sort(["symbol", "_announce"])
    )
    if snapshot.is_empty():
        return None
    return snapshot


def load_fundamental_snapshots(
    data_dir: Path | None,
    tables: Any = None,
) -> dict[str, pl.DataFrame]:
    """读取多张因子来源表; 缺失的表不出现在返回字典里。"""
    wanted = set(tables) if tables is not None else fundamental_tables()
    result: dict[str, pl.DataFrame] = {}
    for table in sorted(wanted):
        snapshot = load_fundamental_snapshot(data_dir, table)
        if snapshot is not None:
            result[table] = snapshot
    return result


def _snapshot_map(snapshot: Any) -> dict[str, pl.DataFrame]:
    """兼容单表入参 (DataFrame → metrics) 与多表入参 (dict)。"""
    if snapshot is None:
        return {}
    if not isinstance(snapshot, dict):
        return {"metrics": _with_announce(snapshot)}
    return {
        str(key): _with_announce(value)
        for key, value in snapshot.items()
        if value is not None
    }


def _with_announce(frame: pl.DataFrame) -> pl.DataFrame:
    """补齐点时门控所需的 _announce 日期列 (已加载的快照自带, 手工入参可能只有原列)。"""
    if "_announce" in frame.columns or "announce_date" not in frame.columns:
        return frame
    return frame.with_columns(
        pl.col("announce_date").cast(pl.Utf8).str.slice(0, 10).str.to_date().alias("_announce")
    )


def _requested(names: Any, missing: list[str] | None = None) -> list[str]:
    pool = missing if missing is not None else names
    return [str(name) for name in pool if str(name) in FUNDAMENTAL_FACTOR_NAMES]


def _attach_daily_table(
    panel: pl.DataFrame,
    snapshot: pl.DataFrame,
    table_names: list[str],
    columns: list[str],
    index: int,
) -> pl.DataFrame:
    """日频事件表: 交易日精确对齐, 面板 T 日使用 T-1 的数据 (不前向填充)。"""
    trade_col = f"_trade_{index}"
    right = (
        snapshot.select(["symbol", "_announce", *columns])
        .rename({"_announce": trade_col})
        .unique(subset=["symbol", trade_col], keep="last")
        .sort(["symbol", trade_col])
    )
    joined = panel.join(
        right,
        left_on=["symbol", "date"],
        right_on=["symbol", trade_col],
        how="left",
        maintain_order="left",
    )
    first_day = right[trade_col].min()
    expressions = []
    for name in table_names:
        spec = FUNDAMENTAL_FACTORS[name]
        value = pl.col(str(spec["column"]))
        if spec.get("price_ratio"):
            value = pl.when(value > 0).then(pl.col("close") / value).otherwise(None)
        # 面板已按 (symbol, date) 排序: 前一交易日的值即 T 日可用值
        value = value.shift(1).over("symbol")
        fill = _fill_value(name)
        if fill is not None:
            value = (
                pl.when(pl.col("date") >= pl.lit(first_day))
                .then(value.fill_null(fill))
                .otherwise(None)
            )
        expressions.append(value.alias(name))
    out = joined.with_columns(expressions)
    # polars 在 left_on/right_on 名称不同时会丢弃右侧键列; 兼容保留的情况
    return out.drop(trade_col) if trade_col in out.columns else out


def attach_fundamental_factors(
    panel: pl.DataFrame,
    snapshot: Any,
    names: Any,
) -> pl.DataFrame:
    """把财务/股东/事件因子列按公告日门控地并入日频面板。

    - snapshot 可以是单表 DataFrame (视为 metrics) 或多表 {table: DataFrame};
      某张表缺失时对应因子产出全 null 列, 保持面板形状;
    - 报告期表按公告日次日起前向填充; 日频表 (``freq="daily"``) 只做交易日
      精确对齐, 无事件的交易日保持 null 或按 ``fill`` 填 0;
    - 面板必须已按 (symbol, date) 排序 (存储与挖掘路径均满足)。
    """
    requested = _requested(names)
    missing_columns = [name for name in requested if name not in panel.columns]
    if not missing_columns:
        return panel
    snapshots = _snapshot_map(snapshot)

    joined = panel
    for index, table in enumerate(sorted({_table_of(name) for name in missing_columns})):
        table_names = [name for name in missing_columns if _table_of(name) == table]
        table_snapshot = snapshots.get(table)
        if table_snapshot is None:
            joined = joined.with_columns([
                pl.lit(None, dtype=pl.Float64).alias(name) for name in table_names
            ])
            continue

        columns = sorted({str(FUNDAMENTAL_FACTORS[name]["column"]) for name in table_names})
        if all(_is_daily(name) for name in table_names):
            joined = _attach_daily_table(joined, table_snapshot, table_names, columns, index)
            continue

        announce_col = f"_announce_{index}"
        effective_col = f"_effective_{index}"
        # asof 键取生效日 (公告日次日) 而非公告日: 直接用公告日回看会在换报告期的
        # 公告当日取到「尚未生效」的新一期并被门控置 null, 打断上一期的前向填充,
        # 与矩阵路径 (searchsorted side="right") 不一致。
        right = (
            table_snapshot.select(["symbol", "_announce", *columns])
            .rename({"_announce": announce_col})
            .with_columns(pl.col(announce_col).dt.offset_by("1d").alias(effective_col))
            .sort(["symbol", effective_col])
        )
        joined = joined.join_asof(
            right,
            left_on="date",
            right_on=effective_col,
            by="symbol",
            strategy="backward",
            check_sortedness=False,  # 双侧均已按 (symbol, key) 排序, 免除逐组检查开销
        )
        announced = pl.col(announce_col).is_not_null() & (pl.col("date") > pl.col(announce_col))
        expressions = []
        for name in table_names:
            spec = FUNDAMENTAL_FACTORS[name]
            source = pl.col(str(spec["column"]))
            if spec.get("price_ratio"):
                value = (
                    pl.when(source > 0)
                    .then(pl.col("close") / source)
                    .otherwise(None)
                )
            else:
                value = source
            expressions.append(
                pl.when(announced).then(value).otherwise(None).alias(name)
            )
        joined = joined.with_columns(expressions).drop([effective_col, announce_col])
    return joined


def _daily_matrix(
    market: Any,
    snapshot: pl.DataFrame,
    name: str,
    label_dates: np.ndarray,
    asset_index: dict[str, int],
    shape: tuple[int, int],
) -> np.ndarray:
    """日频事件表 → TxN 矩阵: 交易日 T 的值写到 T+1 行, 不前向填充。"""
    spec = FUNDAMENTAL_FACTORS[name]
    source = np.full(shape, np.nan, dtype=np.float32)
    column = str(spec["column"])
    if not snapshot.is_empty() and column in snapshot.columns:
        trade_dates = np.array(
            [str(value)[:10] for value in snapshot["announce_date"].to_list()],
            dtype="datetime64[D]",
        )
        values = snapshot[column].to_list()
        for row_index, symbol in enumerate(snapshot["symbol"].to_list()):
            column_index = asset_index.get(symbol)
            if column_index is None:
                continue
            day = trade_dates[row_index]
            start = int(np.searchsorted(label_dates, day, side="left"))
            if start >= shape[0]:
                continue
            # 数据日恰为面板交易日 → 次行生效; 否则取其后首个交易日 (更保守)
            target = start + 1 if label_dates[start] == day else start
            if target >= shape[0]:
                continue
            value = values[row_index]
            source[target, column_index] = np.nan if value is None else float(value)

    fill = _fill_value(name)
    if fill is not None and not snapshot.is_empty():
        first_day = np.datetime64(str(snapshot["announce_date"].min())[:10], "D")
        # 与 polars 路径一致: 覆盖起始日起即可填 0 (该日取的是前一交易日,
        # 而前一交易日尚无数据 ⇒ 语义上就是"无已知事件")
        first_row = int(np.searchsorted(label_dates, first_day, side="left"))
        if first_row < shape[0]:
            block = source[first_row:, :]
            source[first_row:, :] = np.where(np.isnan(block), fill, block)
    return source


def build_fundamental_matrices(
    market: Any,
    snapshot: Any,
    names: Any,
) -> dict[str, np.ndarray]:
    """为 MarketDataMatrix 构建财务/股东/事件因子 TxN float32 字段。

    与 attach_fundamental_factors 同一口径: 报告期表在公告日次一交易日起前向填充,
    日频事件表只在次一交易日生效; 无数据为 NaN。
    pb 类因子在矩阵侧用 close / bps 现算。
    """
    requested = _requested(names)
    if not requested:
        return {}

    snapshots = _snapshot_map(snapshot)
    shape = market.shape
    result: dict[str, np.ndarray] = {}
    asset_index = {symbol: index for index, symbol in enumerate(market.symbols)}
    label_dates = np.array(
        [label[:10] for label in market.timestamp_labels], dtype="datetime64[D]"
    )

    for name in requested:
        spec = FUNDAMENTAL_FACTORS[name]
        table_snapshot = snapshots.get(_table_of(name))
        if _is_daily(name):
            daily = (
                _daily_matrix(market, table_snapshot, name, label_dates, asset_index, shape)
                if table_snapshot is not None
                else np.full(shape, np.nan, dtype=np.float32)
            )
            if spec.get("price_ratio"):
                with np.errstate(divide="ignore", invalid="ignore"):
                    daily = (market.close / daily).astype(np.float32)
                daily[~(daily > 0) | np.isinf(daily)] = np.nan
            result[name] = daily
            continue
        source = np.full(shape, np.nan, dtype=np.float32)
        if table_snapshot is not None and not table_snapshot.is_empty():
            column = str(spec["column"])
            if column not in table_snapshot.columns:
                result[name] = source
                continue
            announce_text = table_snapshot["announce_date"].str.slice(0, 10)
            for row_index, symbol in enumerate(table_snapshot["symbol"].to_list()):
                column_index = asset_index.get(symbol)
                if column_index is None:
                    continue
                announce = announce_text[row_index]
                if announce is None:
                    continue
                # 公告日之后 (严格大于) 的首个时间行索引
                start = int(
                    np.searchsorted(label_dates, np.datetime64(announce, "D"), side="right")
                )
                if start >= shape[0]:
                    continue
                value = table_snapshot[column][row_index]
                numeric = float("nan") if value is None else float(value)
                # 新一期该指标为空时必须覆盖旧值为 NaN: 跳过写入会让同一行混用两期
                # 报告 (bps 取新期、roe 停在上一期), 与 polars 侧 join_asof
                # 只认最新一期整行的口径不一致。
                source[start:, column_index] = numeric if np.isfinite(numeric) else np.nan

        if spec.get("price_ratio"):
            with np.errstate(divide="ignore", invalid="ignore"):
                matrix = (market.close / source).astype(np.float32)
            matrix[~(source > 0)] = np.nan
            matrix[np.isinf(matrix)] = np.nan
            result[name] = matrix
        else:
            result[name] = source
    return result


def attach_matrix_fundamental_fields(
    market: Any, data_dir: Path | None, names: Any, *, evidence=None
) -> Any:
    """把财务/股东因子作为 matrix fields 附加到 (frozen) MarketDataMatrix 副本。"""
    import dataclasses

    requested = _requested(names)
    if not requested:
        return market
    snapshots = load_fundamental_snapshots(data_dir, fundamental_tables(requested))
    if evidence is not None:
        if "metrics" in snapshots:
            evidence.record_frame("financial_announcements", snapshots["metrics"])
        if "holder_metrics" in snapshots:
            evidence.record_frame("holder_announcements", snapshots["holder_metrics"])
        if "lhb_metrics" in snapshots:
            evidence.record_frame("lhb_events", snapshots["lhb_metrics"])
        if "margin_metrics" in snapshots:
            evidence.record_frame("margin_events", snapshots["margin_metrics"])
    extra = build_fundamental_matrices(market, snapshots, requested)
    if not extra:
        return market
    merged = {**dict(market.fields), **extra}
    for array in extra.values():
        array.flags.writeable = False
    return dataclasses.replace(market, fields=MappingProxyType(merged))
