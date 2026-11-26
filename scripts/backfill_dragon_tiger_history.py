#!/usr/bin/env python3
"""从 fuyao 历史龙虎榜接口回补 data/dragon_tiger/date=*.json。

C 类因子（dragon_tiger/institutional）真正可回测的关键一步：
  - fuyao provider.dragon_tiger(board_type, date) 支持一年内历史日期（实证 code=1003
    超出一年拒绝），字段含 org_net_value / net_value / hot_money_net_value / hot_rank /
    concept_list。当前日期正好覆盖本地 K 线全区间（2025-11 ~ 2026-11）。
  - 落盘格式对齐 services/dragon_tiger.py：{state, org:{stock_items}, all:{stock_items}}
    供 scripts/ingest_dragon_tiger_factors.py 转成 ext_data timeseries 因子列。

数据源插件化纪律（docs/khunter-integration-plan.md §7）：
  - 取数一律经 custom.loader.get_provider("fuyao") 走插件 provider，
    禁止脚本直接 import 插件 client 模块（provider 是唯一取数入口）；
  - provider 生命周期归 loader 所有，脚本不调 close()。

纪律：
  - 只拉真实接口返回，失败/非交易日/超限日期跳过，绝不插值伪造；
  - 断点续传：已存在的 date=*.json 默认跳过（--force 覆盖）；
  - 逐交易日拉取（走本地 kline_daily 分区的日期，避免无效周末请求）；
  - 默认仅最近一年，--limit-days 可限制本次拉取天数（先小批验证）。

运行：
  cd backend && uv run --frozen python ../scripts/backfill_dragon_tiger_history.py --limit-days 20
  cd backend && uv run --frozen python ../scripts/ingest_dragon_tiger_factors.py   # 转成因子列
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import settings  # noqa: E402
from app.services.fs_utils import atomic_write_text  # noqa: E402

ONE_YEAR_DAYS = 360  # 保守留余量（fuyao 精确一年，接口自身会拒绝超限）


def trading_days(data_dir: Path) -> list[date]:
    """从本地 kline_daily 分区取交易日列表（真实有数据的日期）。"""
    kdir = data_dir / "kline_daily"
    days = []
    for p in kdir.glob("date=*"):
        try:
            days.append(date.fromisoformat(p.name.removeprefix("date=")))
        except ValueError:
            continue
    return sorted(days)


def build_payload(org: dict, all_board: dict, trade_date: str) -> dict:
    """对齐 services/dragon_tiger.py 的落盘结构，供 ingest 脚本解析。"""
    return {
        "state": "ok",
        "trade_date": trade_date,
        "org": {"stock_items": org.get("stock_items") or []},
        "all": {"stock_items": all_board.get("stock_items") or []},
    }


def main() -> int:
    p = argparse.ArgumentParser(description="回补 fuyao 历史龙虎榜 JSON")
    p.add_argument("--limit-days", type=int, default=0, help="本次最多处理 N 个交易日（0=全部）")
    p.add_argument("--force", action="store_true", help="覆盖已存在的 JSON（默认跳过）")
    p.add_argument("--sleep", type=float, default=0.2, help="每次请求后休眠秒数（限速）")
    args = p.parse_args()

    data_dir = settings.data_dir
    dt_dir = data_dir / "dragon_tiger"
    dt_dir.mkdir(parents=True, exist_ok=True)

    # 数据源插件化：经 provider 注册表取 fuyao（loader import 即完成插件扫描注册；
    # key 未配置时 fuyao 不会注册，此处 fail-fast 给出明确指引）
    from app.data_providers.custom import loader

    try:
        provider = loader.get_provider("fuyao")
    except ValueError:
        print("[fatal] fuyao 插件未注册（检查 secrets.json/.env 的 fuyao_api_key），无法回补")
        return 1

    today = date.today()
    cutoff = today - timedelta(days=ONE_YEAR_DAYS)
    days = [d for d in trading_days(data_dir) if d >= cutoff and d <= today]
    if args.limit_days > 0:
        days = days[-args.limit_days:]
    print(f"[info] 目标交易日 {len(days)} 天: {days[0] if days else '-'} ~ {days[-1] if days else '-'}")

    ok = skipped = failed = 0
    t0 = time.perf_counter()
    for i, d in enumerate(days, 1):
        out = dt_dir / f"date={d.isoformat()}.json"
        if out.exists() and not args.force:
            skipped += 1
            continue
        try:
            org = provider.dragon_tiger(board_type="org", date=d.isoformat())
            all_board = provider.dragon_tiger(board_type="all", date=d.isoformat())
            payload = build_payload(org, all_board, d.isoformat())
            atomic_write_text(out, json.dumps(payload, ensure_ascii=False))
            ok += 1
            if i % 10 == 0 or i == len(days):
                print(f"[progress] {i}/{len(days)} {d} ok={ok} skip={skipped} fail={failed} "
                      f"elapsed={time.perf_counter()-t0:.1f}s")
        except Exception as exc:  # noqa: BLE001 - 非交易日/超限跳过，不中断整批
            failed += 1
            msg = str(exc)[:100]
            # 非交易日(code=1002)/超一年(code=1003)属预期，静默计数
            if "1002" not in msg and "1003" not in msg:
                print(f"[warn] {d}: {msg}")
        time.sleep(args.sleep)

    print(f"[done] ok={ok} skipped={skipped} failed={failed} elapsed={time.perf_counter()-t0:.1f}s")
    print(f"[next] cd backend && uv run --frozen python ../scripts/ingest_dragon_tiger_factors.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
