#!/usr/bin/env python3
"""把 mainline_history 的题材强度转成 ext_data timeseries 因子列（T-1 滞后）。

C 类 hot_theme 因子的真实数据落地：
  - 来源：data/mainline_history/part.parquet（242 个交易日，market_mainline.py 从本地
    涨停连板 + 概念/行业成分算出，含 date/leader_symbol/score/rank）。这是真实历史，
    不依赖外部 API 凭证。
  - 聚合：同一 (leader_symbol, date) 出现在多个题材 → 取当日最高分 theme_score + 均值
    + 题材个数 + 概念个数，反映"该股作为题材龙头的强度与广度"。
  - **T-1 滞后**：mainline score 是当日盘后计算，若直接作为当日因子会前视。脚本把
    date 平移到"下一个交易日"落盘 → 回测时 date=T 的因子 = T-1 的题材分，无未来函数。
  - 目标：data/ext_data/ext_mainline_theme/timeseries/date=*/part.parquet，
    数值列自动注册为 base 因子，可进 META.scoring / DSL。

覆盖率警告：只有当日题材龙头（1350 只，多数股票仅 1-2 天）有值，非全市场面板；
缺失日在回测中走权重让渡。这是已知限制，不是数据伪造。

运行：
    cd backend && uv run --frozen python ../scripts/ingest_theme_factors.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.ext_data import (  # noqa: E402
    ExtConfig,
    ExtConfigStore,
    ExtField,
    write_ext_parquet,
)
from app.services.fs_utils import atomic_write_text  # noqa: E402

CONFIG_ID = "ext_mainline_theme"
FIELDS = [
    ExtField("symbol", "string", "标的代码"),
    ExtField("theme_score", "float", "当日最高题材强度分(0-100, T-1)"),
    ExtField("theme_score_avg", "float", "所处题材平均分(T-1)"),
    ExtField("theme_count", "float", "当日入选题材个数(T-1)"),
    ExtField("concept_count", "float", "其中概念题材个数(T-1)"),
]


def build_config() -> ExtConfig:
    return ExtConfig(
        id=CONFIG_ID,
        label="题材龙头强度(T-1)",
        mode="timeseries",
        fields=FIELDS,
        description=(
            "stock-picker 整合 hot_theme：mainline_history 题材龙头分，T-1 滞后防前视。"
            "仅题材龙头股有值（覆盖稀疏），缺失走权重让渡。"
        ),
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        market_level=False,
    )


def trading_day_index(data_dir: Path) -> tuple[list[date], dict[date, date]]:
    """返回交易日列表 + {trade_day: next_trade_day} 映射（用于 T-1 平移）。"""
    days = []
    for p in (data_dir / "kline_daily").glob("date=*"):
        try:
            days.append(date.fromisoformat(p.name.removeprefix("date=")))
        except ValueError:
            continue
    days = sorted(days)
    next_day = {}
    for i in range(len(days) - 1):
        next_day[days[i]] = days[i + 1]
    return days, next_day


def build_theme_panel(data_dir: Path) -> pl.DataFrame:
    """聚合 mainline → (symbol, date, 题材强度列)，date 已 T-1 平移到下一交易日。"""
    src = data_dir / "mainline_history" / "part.parquet"
    if not src.exists():
        raise FileNotFoundError(f"无 mainline_history: {src}")
    df = pl.read_parquet(src)
    agg = (
        df.filter(pl.col("leader_symbol").is_not_null() & pl.col("leader_symbol").str.contains("\\."))
        .group_by(["leader_symbol", "date"])
        .agg(
            pl.col("score").max().alias("theme_score"),
            pl.col("score").mean().alias("theme_score_avg"),
            pl.col("member").n_unique().alias("theme_count"),
            (pl.col("kind") == "concept").sum().alias("concept_count"),
        )
        .rename({"leader_symbol": "symbol"})
    )
    _, next_day = trading_day_index(data_dir)
    # T-1 滞后：原始 date（盘后算）→ 下一交易日生效
    agg = agg.with_columns(
        pl.col("date").map_elements(lambda d: next_day.get(d), return_dtype=pl.Date).alias("eff_date")
    ).filter(pl.col("eff_date").is_not_null()).drop("date").rename({"eff_date": "date"})
    return agg.select([
        "symbol", "date",
        pl.col("theme_score").cast(pl.Float64),
        pl.col("theme_score_avg").cast(pl.Float64),
        pl.col("theme_count").cast(pl.Float64),
        pl.col("concept_count").cast(pl.Float64),
    ])


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"
    panel = build_theme_panel(data_dir)
    print(f"[info] 题材面板 {panel.height} 行，覆盖 {panel['symbol'].n_unique()} 只股票，"
          f"{panel['date'].n_unique()} 个生效交易日（T-1 滞后）")

    config = build_config()
    store = ExtConfigStore(data_dir)
    cfg_path = store._config_path(CONFIG_ID)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cfg_path, json.dumps(config.to_dict(), ensure_ascii=False, indent=2))

    total = 0
    for d in sorted(panel["date"].unique().to_list()):
        day_df = panel.filter(pl.col("date") == d).drop("date")
        n = write_ext_parquet(day_df, config, data_dir, snapshot_date=d)
        total += n
    print(f"[done] 共写入 {total} 行 -> {CONFIG_ID}/timeseries/date=*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
