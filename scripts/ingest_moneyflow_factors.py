#!/usr/bin/env python3
"""把东方财富「个股资金流日线」转成 ext_data timeseries 因子列（KHunter 整合）。

取数已封装为正规插件 backend/app/plugins/eastmoney/（plugin.yaml + provider +
client），本脚本只是插件的消费壳：插件管「怎么拉」，本脚本管「怎么落因子」。

数据源：
  - KHunter 用 Tushare `moneyflow_ths`（需 token，当前项目无）→ 本插件改用东财
    免 token 公开接口 `push2his.eastmoney.com/.../fflow/daykline`，实测可达，
    约 120 个交易日历史；字段恒等式 f52 == f55+f56（主力=大单+超大单）已实测验证。
  - provider 经 custom.loader 注册：get_provider("eastmoney") 可取，
    provider_has_dataset("eastmoney", "moneyflow") 为 True。

**T-1 滞后防前视**：资金流是当日盘后数据，若作为当日因子会用到收盘信息。脚本把
  资金流 date 平移到下一交易日落盘 → 回测时 date=T 的因子 = T-1 的资金流，无未来函数。

限流纪律（重要）：东财对短时间高频请求会临时封 IP（实测 6+ 次/几秒 → HTTP 000）。
  - provider 内置逐股节流(1.2s)；脚本层再叠加 --sleep 与断点续传（已有分区跳过）
  - 优先拉股票池（`--symbols-file`），不拉全市场 5605 只

运行：
    cd backend
    # 单股验证（东财封禁解除后）
    uv run --frozen python ../scripts/ingest_moneyflow_factors.py --symbol 600000.SH
    # 股票池批量
    uv run --frozen python ../scripts/ingest_moneyflow_factors.py --symbols-file ../data/my_pool.txt
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import date
from pathlib import Path

import polars as pl

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.plugins.eastmoney.client import parse_klines, to_secid  # noqa: E402,F401 (re-export 供测试)
from app.plugins.eastmoney.provider import EastmoneyProvider  # noqa: E402
from app.services.ext_data import (  # noqa: E402
    ExtConfig,
    ExtConfigStore,
    ExtField,
    write_ext_parquet,
)
from app.services.fs_utils import atomic_write_text  # noqa: E402

CONFIG_ID = "ext_moneyflow"
FIELDS = [
    ExtField("symbol", "string", "标的代码"),
    ExtField("main_net", "float", "主力净额(元,T-1)=大单+超大单"),
    ExtField("large_net", "float", "大单净额(元,T-1)"),
    ExtField("super_large_net", "float", "超大单净额(元,T-1)"),
    ExtField("small_net", "float", "小单净额(元,T-1)"),
    ExtField("main_rate", "float", "主力净额占比(%,T-1)"),
    ExtField("large_rate", "float", "大单净额占比(%,T-1)"),
    ExtField("small_rate", "float", "小单净额占比(%,T-1)"),
]


def build_config() -> ExtConfig:
    return ExtConfig(
        id=CONFIG_ID,
        label="个股主力资金流(T-1)",
        mode="timeseries",
        fields=FIELDS,
        description=(
            "KHunter 整合 capital_flow：eastmoney 插件个股资金流日线，T-1 滞后防前视。"
            "主力净额=大单+超大单(f52=f55+f56 已验证)。元为单位，正=净流入。"
        ),
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        market_level=False,
    )


def apply_t1_lag(panel: pl.DataFrame, data_dir: Path) -> pl.DataFrame:
    """把资金流 date 平移到下一交易日（当日盘后数据 → 次交易日生效，防前视）。"""
    days = sorted(
        date.fromisoformat(p.name.removeprefix("date="))
        for p in (data_dir / "kline_daily").glob("date=*")
        if _is_iso_date(p.name.removeprefix("date="))
    )
    next_day = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    if not next_day:
        return panel.with_columns(pl.lit(None, dtype=pl.Date).alias("eff_date")).filter(False)
    out = panel.with_columns(
        pl.col("date").map_elements(lambda d: next_day.get(d), return_dtype=pl.Date).alias("eff_date")
    ).filter(pl.col("eff_date").is_not_null()).drop("date").rename({"eff_date": "date"})
    return out


def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _existing_dates(data_dir: Path, symbol: str) -> set[date]:
    """断点续传：返回该 symbol 已落的生效日期集合。"""
    out = set()
    part_root = data_dir / "ext_data" / CONFIG_ID / "timeseries"
    for p in part_root.glob("date=*/part.parquet"):
        try:
            d = date.fromisoformat(p.parent.name.removeprefix("date="))
        except ValueError:
            continue
        try:
            df = pl.read_parquet(p, columns=["symbol"])
            if symbol in df["symbol"].to_list():
                out.add(d)
        except Exception:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", help="单股验证，如 600000.SH")
    ap.add_argument("--symbols-file", help="股票池文件，每行一个 symbol")
    ap.add_argument("--sleep", type=float, default=1.5, help="每股请求间隔秒(限流,叠加在插件节流之上)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少只(0=全部)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"

    symbols: list[str] = []
    if args.symbol:
        symbols = [args.symbol]
    elif args.symbols_file:
        symbols = [s.strip() for s in Path(args.symbols_file).read_text().splitlines() if s.strip()]
    else:
        ap.error("需指定 --symbol 或 --symbols-file")
    if args.limit:
        symbols = symbols[: args.limit]

    config = build_config()
    store = ExtConfigStore(data_dir)
    cfg_path = store._config_path(CONFIG_ID)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cfg_path, json.dumps(config.to_dict(), ensure_ascii=False, indent=2))

    provider = EastmoneyProvider()
    total = 0
    for i, symbol in enumerate(symbols):
        if i:
            time.sleep(args.sleep + random.uniform(0, 0.3))
        try:
            raw = provider.get_moneyflow([symbol])
        except Exception as exc:  # noqa: BLE001 — 单股失败不中断整批
            print(f"[{i+1}/{len(symbols)}] {symbol} 拉取失败: {exc}", flush=True)
            time.sleep(5)
            continue
        if raw.is_empty():
            print(f"[{i+1}/{len(symbols)}] {symbol} 无资金流数据，跳过", flush=True)
            continue
        panel = apply_t1_lag(raw, data_dir)
        existing = _existing_dates(data_dir, symbol)
        n = 0
        for d in sorted(panel["date"].unique().to_list()):
            if d in existing:
                continue
            day_df = panel.filter(pl.col("date") == d).drop("date")
            n += write_ext_parquet(day_df, config, data_dir, snapshot_date=d)
        total += n
        print(f"[{i+1}/{len(symbols)}] {symbol}: 原始{raw.height}行 生效{panel.height}行 新写{n}行", flush=True)
    print(f"[done] 共写入 {total} 行 -> {CONFIG_ID}/timeseries/date=*")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
