#!/usr/bin/env python3
"""把 ADX(14) + KHunter 三态状态机结果转成 ext_data timeseries 因子列。

为当前项目补上缺失的 ADX regime 能力（参照 KHunter 的 ADX 择时状态机）：
  - 来源：data/kline_daily/ 本地 OHLC（284 交易日，无需外部 API）。
  - 计算：app.services.adx_regime（Wilder 平滑 ADX + khunter_algorithms 状态机）。
  - **T-1 滞后**：ADX 用当日收盘算，date 平移到下一交易日落盘 → 回测 date=T 用 T-1 的
    ADX 状态，无未来函数。
  - 目标：data/ext_data/ext_adx/timeseries/date=*/part.parquet，数值列自动注册为因子。

因子列：
  adx            ADX(14) 原始值
  adx_band_code  档位 1=trend / 2=sprout / 3=range / 0=未预热
  adx_dir_code   方向 1=up / -1=down / 0=未定
  adx_entry      入场放行 1/0（dir=up 且 band=trend）

运行：
    cd backend && uv run --frozen python ../scripts/ingest_adx_factors.py
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.adx_regime import build_adx_panel  # noqa: E402
from app.services.ext_data import (  # noqa: E402
    ExtConfig, ExtConfigStore, ExtField, write_ext_parquet,
)
from app.services.fs_utils import atomic_write_text  # noqa: E402

CONFIG_ID = "ext_adx"
FIELDS = [
    ExtField("symbol", "string", "标的代码"),
    ExtField("adx", "float", "ADX(14)原始值(T-1)"),
    ExtField("adx_band_code", "float", "档位1=trend/2=sprout/3=range(T-1)"),
    ExtField("adx_dir_code", "float", "方向1=up/-1=down(T-1)"),
    ExtField("adx_entry", "float", "入场放行1/0(T-1)"),
]


def build_config() -> ExtConfig:
    return ExtConfig(
        id=CONFIG_ID,
        label="ADX择时状态(T-1)",
        mode="timeseries",
        fields=FIELDS,
        description=(
            "KHunter 整合 ADX regime：Wilder ADX(14) + 三态状态机(迟滞/两日同向/反转日)。"
            "T-1 滞后防前视。adx_entry=dir上升且band=trend。"
        ),
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        market_level=False,
    )


def trading_day_index(data_dir: Path) -> dict[date, date]:
    days = sorted(
        date.fromisoformat(p.name.removeprefix("date="))
        for p in (data_dir / "kline_daily").glob("date=*")
        if _is_iso_date(p.name.removeprefix("date="))
    )
    return {days[i]: days[i + 1] for i in range(len(days) - 1)}


def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def build_adx_timeseries(data_dir: Path) -> pl.DataFrame:
    """读 kline_daily 全面板 → 算 ADX 状态 → T-1 平移，返回落盘帧。"""
    parts = sorted((data_dir / "kline_daily").glob("date=*/part.parquet"))
    if not parts:
        raise FileNotFoundError("无 kline_daily 数据")
    # 分区可能含额外列（如 quote_ts）→ 逐文件只读 OHLCV 列再合并，避免 schema 冲突
    _cols = ["symbol", "date", "high", "low", "close"]
    kline = pl.concat([
        pl.read_parquet(p, columns=_cols) for p in parts
    ], how="diagonal_relaxed")
    panel = build_adx_panel(kline)
    next_day = trading_day_index(data_dir)
    out = panel.with_columns(
        pl.col("date").map_elements(lambda d: next_day.get(d), return_dtype=pl.Date).alias("eff_date")
    ).filter(pl.col("eff_date").is_not_null()).drop("date").rename({"eff_date": "date"})
    return out


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"
    panel = build_adx_timeseries(data_dir)
    print(f"[info] ADX 面板 {panel.height} 行，覆盖 {panel['symbol'].n_unique()} 只股票，"
          f"{panel['date'].n_unique()} 个生效交易日（T-1 滞后）")

    config = build_config()
    store = ExtConfigStore(data_dir)
    cfg_path = store._config_path(CONFIG_ID)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cfg_path, json.dumps(config.to_dict(), ensure_ascii=False, indent=2))

    total = 0
    for d in sorted(panel["date"].unique().to_list()):
        day_df = panel.filter(pl.col("date") == d).drop("date")
        total += write_ext_parquet(day_df, config, data_dir, snapshot_date=d)
    print(f"[done] 共写入 {total} 行 -> {CONFIG_ID}/timeseries/date=*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
