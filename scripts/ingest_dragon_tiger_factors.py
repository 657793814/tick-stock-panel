#!/usr/bin/env python3
"""把 data/dragon_tiger/date=*.json 的机构净买数据转成 ext_data timeseries 因子列。

这是 stock-picker 整合 C 类因子（institutional/dragon_tiger）的真实数据管道预演：
- 来源：services/dragon_tiger.py 每日落盘的 JSON（当日/机构榜）
- 目标：data/ext_data/ext_dragon_org/timeseries/date=*/part.parquet
        数值字段（org_net_value / dragon_net_value）自动注册为 base 因子
- 可重复运行：按 date 分区覆盖写；只有实际拉取过的日期才有数据（早期缺失 →
  回测时该因子为 null，走权重让渡，符合 stock-picker factor_daily.db 的覆盖率语义）

运行：
    cd backend && uv run --frozen python ../scripts/ingest_dragon_tiger_factors.py

注意：当前仓库仅有少量日期的龙虎榜 JSON（样例数据）。数据越积越多，因子回测
覆盖越完整；脚本不会伪造任何历史值。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import polars as pl

# 让脚本能从 backend/ 之外以 uv run python 直接运行
BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.ext_data import (  # noqa: E402
    ExtConfig,
    ExtConfigStore,
    ExtField,
    write_ext_parquet,
)
from app.services.fs_utils import atomic_write_text  # noqa: E402

CONFIG_ID = "ext_dragon_org"
FIELDS = [
    ExtField("symbol", "string", "标的代码"),
    ExtField("org_net_value", "float", "机构净买额(元)"),
    ExtField("dragon_net_value", "float", "龙虎榜净买额(元)"),
    ExtField("hot_money_net_value", "float", "游资净买额(元)"),
    ExtField("hot_rank", "float", "龙虎榜热度排名(低=热)"),
]


def build_config() -> ExtConfig:
    return ExtConfig(
        id=CONFIG_ID,
        label="龙虎榜机构净买(逐日)",
        mode="timeseries",
        fields=FIELDS,
        description=(
            "stock-picker 整合：龙虎榜机构净买额，逐日分区，可回测。"
            "数据来自 services/dragon_tiger.py 每日落盘 JSON（只有拉取过的日期有值）。"
        ),
        symbol_map={"type": "mapped", "col": "symbol"},
        code_map={"type": "computed", "from": "symbol", "method": "strip_exchange"},
        market_level=False,
    )


def parse_one(path: Path) -> pl.DataFrame | None:
    """解析单个 date=*.json，返回 (symbol, org_net_value, dragon_net_value) 帧。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[skip] {path.name}: 解析失败 {exc}")
        return None
    if raw.get("state") not in ("ok", "fallback_prev"):
        return None

    org_items = (raw.get("org") or {}).get("stock_items") or []
    all_items = (raw.get("all") or {}).get("stock_items") or []
    if not org_items and not all_items:
        return None

    # 机构净买/游资净买/热度排名取 org 榜；龙虎榜净买取 all 榜。
    # 字段可能部分缺失（机构席位无游资时不返回 hot_money_net_value）→ 容错留空。
    org_map: dict[str, float] = {}
    hot_money_map: dict[str, float] = {}
    hot_rank_map: dict[str, float] = {}
    for item in org_items:
        sym = item.get("thscode")
        if not sym:
            continue
        for src_key, target in (
            ("org_net_value", org_map),
            ("hot_money_net_value", hot_money_map),
            ("hot_rank", hot_rank_map),
        ):
            val = item.get(src_key)
            if val is not None:
                target[sym] = float(val)
    dragon_map: dict[str, float] = {}
    for item in all_items:
        sym = item.get("thscode")
        val = item.get("net_value")
        if sym and val is not None:
            dragon_map[sym] = float(val)

    symbols = sorted(set(org_map) | set(dragon_map) | set(hot_money_map) | set(hot_rank_map))
    rows = [
        {
            "symbol": sym,
            "org_net_value": org_map.get(sym),
            "dragon_net_value": dragon_map.get(sym),
            "hot_money_net_value": hot_money_map.get(sym),
            "hot_rank": hot_rank_map.get(sym),
        }
        for sym in symbols
    ]
    return pl.DataFrame(rows, schema={
        "symbol": pl.Utf8,
        "org_net_value": pl.Float64,
        "dragon_net_value": pl.Float64,
        "hot_money_net_value": pl.Float64,
        "hot_rank": pl.Float64,
    })


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    data_dir = root / "data"
    dt_dir = data_dir / "dragon_tiger"
    if not dt_dir.exists():
        print(f"[warn] 无龙虎榜数据目录: {dt_dir}")
        return 0

    paths = sorted(dt_dir.glob("date=*.json"))
    if not paths:
        print(f"[warn] 无龙虎榜 JSON: {dt_dir}")
        return 0

    config = build_config()
    # 写 config.json（幂等，覆盖为当前脚本定义的配方）
    store = ExtConfigStore(data_dir)
    cfg_path = store._config_path(CONFIG_ID)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cfg_path, json.dumps(config.to_dict(), ensure_ascii=False, indent=2))

    total = 0
    for path in paths:
        snap_raw = path.stem.removeprefix("date=")
        try:
            snap = date.fromisoformat(snap_raw)
        except ValueError:
            print(f"[skip] 非法日期目录名: {path.name}")
            continue
        df = parse_one(path)
        if df is None or df.is_empty():
            continue
        n = write_ext_parquet(df, config, data_dir, snapshot_date=snap)
        total += n
        print(f"[ok] {snap}: {n} rows -> {CONFIG_ID}/timeseries/date={snap}")

    print(f"[done] 共写入 {total} 行，来自 {len(paths)} 个日期文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
