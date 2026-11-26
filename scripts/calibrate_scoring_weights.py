"""Scoring 权重 OOS 校准工具（双闸门纪律）。

用途：为策略库策略的 META.scoring 权重寻找稳健配置。复用项目已验证的构件：
  - app.backtest.robust_optimizer: split_validation_windows / robust_score（K 段稳健分）
  - app.backtest.guards: trades_ok / validate_primary_objective（护栏）
  - app.strategy.scoring: effective_scoring（权重 override 语义）
  - app.backtest.strategy: StrategyBacktestService（回测，overrides.scoring_replace 覆盖权重）

纪律（见 docs/strategy-iteration.md §4、docs/robust-optimizer-plan.md）：
  1. 区间切 IS / OOS：OOS 封存，只在定型权重上跑一次，绝不参与权重搜索；
  2. IS 内 K 段切窗，按 R = 0.6×median + 0.4×worst 选权重（扛单段运气、惩罚偏科）；
  3. 主目标必须在白名单，最小交易数门槛 fail-closed；
  4. 样本充分性判定：数据不足时明确输出"统计意义不足"，不给出可信权重结论。

本脚本不修改任何策略文件、核心模块或数据；仅做只读回测 + 产出 JSON 报告。

用法：
  cd backend && uv run --frozen python ../scripts/calibrate_scoring_weights.py \
      --strategy csp_short_multifactor \
      --start 2025-11-01 --end 2026-11-20 \
      --step 0.25 --k 3 --objective sortino --min-trades 5 \
      --out ../data/backtest_results/weight_calibration_short.json
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from datetime import date, timedelta
from pathlib import Path

# 允许从 scripts/ 直接运行时导入 backend/app
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.backtest.engine import BacktestEngine  # noqa: E402
from app.backtest.guards import (  # noqa: E402
    trades_ok,
    validate_primary_objective,
)
from app.backtest.robust_optimizer import (  # noqa: E402
    robust_score,
    split_validation_windows,
)
from app.backtest.strategy import (  # noqa: E402
    StrategyBacktestConfig,
    StrategyBacktestService,
)
from app.factors.ext_factors import ensure_synced  # noqa: E402
from app.factors.store import load_runtime_registry  # noqa: E402
from app.strategy.engine import StrategyEngine  # noqa: E402
from app.tickflow.repository import DataStore, KlineRepository  # noqa: E402

# 权重网格精度上限：超过此组合数直接拒绝，避免误用炫技式搜索（双闸门反多重检验）。
MAX_COMBOS = 400
MIN_TRADES_DEFAULT = 5
OOS_FRACTION = 0.2  # 封存最后 20% 为留出集（strategy-iteration.md §4）


def simplex_weights(factors: list[str], step: float) -> list[dict[str, float]]:
    """生成 sum=1、每因子为 step 整数倍的权重组合（单纯形网格）。

    step=0.25、5 因子 → C(9,4)=126 组。step 必须整除 1。
    """
    units = round(1.0 / step)
    if abs(units * step - 1.0) > 1e-9:
        raise ValueError(f"step={step} 不能整除 1")
    if len(factors) < 2:
        raise ValueError("权重校准至少需要 2 个因子")
    combos: list[dict[str, float]] = []
    # stars and bars: 把 units 个不可分割单位分配给 len(factors) 个因子（允许 0）。
    # 用 units 个星 + (n-1) 个隔板的所有排列，去重后即为全部非负整数分配。
    n = len(factors)
    for cuts in itertools.combinations(range(units + n - 1), n - 1):
        allocations = []
        prev = -1
        for c in cuts:
            allocations.append(c - prev - 1)
            prev = c
        allocations.append((units + n - 1) - prev - 1)
        assert sum(allocations) == units
        combos.append({f: round(a * step, 6) for f, a in zip(factors, allocations)})
    return combos


def _override_for(weights: dict[str, float], directions: dict[str, str]) -> dict:
    """构造完整替换 scoring 的 override（不改策略文件）。"""
    return {
        "scoring": weights,
        "scoring_replace": True,
        "scoring_directions": directions,
    }


def _run_once(
    service: StrategyBacktestService,
    strategy_id: str,
    symbols: list[str],
    start: date,
    end: date,
    override: dict,
    objective: str,
    min_trades: int,
    **bt_kwargs,
) -> dict:
    """单段回测 → {objective, n_trades, ok}。异常按无效处理（fail-closed）。"""
    try:
        cfg = StrategyBacktestConfig(
            strategy_id=strategy_id,
            symbols=symbols,
            start=start,
            end=end,
            overrides=override,
            **bt_kwargs,
        )
        res = service.run(cfg)
        stats = res.stats or {}
        return {
            "objective": stats.get(objective),
            "n_trades": stats.get("n_trades"),
            "ok": res.error is None and trades_ok(stats, min_trades)
            and isinstance(stats.get(objective), (int, float)),
            "error": res.error,
        }
    except Exception as exc:  # noqa: BLE001 - 网格搜索需容错，坏组合记为无效而非中断
        return {"objective": None, "n_trades": None, "ok": False, "error": str(exc)[:200]}


def adequacy(span_days: int, k: int, warmup_days: int) -> dict:
    """样本充分性判定。数据不足时明确警告，不产出可信权重结论。"""
    usable = span_days - warmup_days
    per_seg = usable / k if k else 0
    # 经验阈值：每段至少 ~90 自然日（约 60 交易日）才有统计意义；留出集至少 60 天。
    oos_days = span_days * OOS_FRACTION
    issues = []
    if per_seg < 90:
        issues.append(f"每验证段仅 ~{per_seg:.0f} 天可用（建议 ≥90 天）")
    if oos_days < 60:
        issues.append(f"留出集仅 ~{oos_days:.0f} 天（建议 ≥60 天）")
    return {
        "span_days": span_days,
        "usable_days_after_warmup": usable,
        "days_per_is_segment": round(per_seg, 1),
        "oos_holdout_days": round(oos_days, 1),
        "statistically_sufficient": not issues,
        "issues": issues,
    }


def calibrate(
    strategy_id: str,
    symbols: list[str],
    start: date,
    end: date,
    factors: list[str],
    directions: dict[str, str],
    step: float,
    k: int,
    objective: str,
    min_trades: int,
    warmup_days: int,
    data_dir: Path,
    **bt_kwargs,
) -> dict:
    validate_primary_objective(objective)
    combos = simplex_weights(factors, step)
    if len(combos) > MAX_COMBOS:
        raise ValueError(f"权重组合 {len(combos)} > 上限 {MAX_COMBOS}，请调大 step 或精简因子")

    span = (end - start).days + 1
    is_end = start + timedelta(days=int(span * (1 - OOS_FRACTION))) - timedelta(days=1)
    oos_start = is_end + timedelta(days=1)
    # 留出集的数据密度检查（防止切出空 OOS）
    if oos_start > end:
        raise ValueError("区间太短，无法切出留出集")

    # 初始化运行时依赖
    load_runtime_registry(data_dir)
    ensure_synced(data_dir)
    store = DataStore(data_dir)
    service = StrategyBacktestService(
        BacktestEngine(KlineRepository(store)),
        StrategyEngine(strategy_dirs=[data_dir / "strategies" / "custom"]),
    )

    windows = split_validation_windows(start, is_end, k)
    report = {
        "strategy_id": strategy_id,
        "objective": objective,
        "direction": "max",
        "factors": factors,
        "directions": directions,
        "grid": {"step": step, "n_combos": len(combos)},
        "is_range": [str(start), str(is_end)],
        "oos_holdout": [str(oos_start), str(end)],
        "is_windows": [[str(a), str(b)] for a, b in windows],
        "guards": {"min_trades": min_trades, "primary_objective": objective},
        "adequacy": adequacy(span, k, warmup_days),
        "per_combo": [],
    }

    # IS: 每组权重 × 每段的稳健分（OOS 完全不参与）
    for wi, weights in enumerate(combos):
        override = _override_for(weights, directions)
        seg_vals: list[float | None] = []
        seg_detail = []
        for a, b in windows:
            r = _run_once(service, strategy_id, symbols, a, b, override,
                          objective, min_trades, **bt_kwargs)
            seg_vals.append(r["objective"] if r["ok"] else None)
            seg_detail.append({"range": [str(a), str(b)], **r})
        rs = robust_score(seg_vals)
        report["per_combo"].append({
            "weights": weights,
            "segment_objectives": seg_vals,
            "robust": rs,
            "segments": seg_detail,
        })

    valid = [c for c in report["per_combo"] if c["robust"]["score"] != float("-inf")]
    valid.sort(key=lambda c: c["robust"]["score"], reverse=True)
    report["n_valid_combos"] = len(valid)
    if not valid:
        report["conclusion"] = "所有权重组合在 IS 段均无有效回测，无法校准"
        return report

    best = valid[0]
    report["best_is_weights"] = best["weights"]
    report["best_is_robust"] = best["robust"]
    report["is_top5"] = [
        {"weights": c["weights"], "score": round(c["robust"]["score"], 4),
         "worst": c["robust"]["worst"], "spread": c["robust"]["spread"]}
        for c in valid[:5]
    ]
    # 权重漂移：各段冠军收敛度（IS 内，诊断用）
    champions = []
    for si in range(k):
        scored = [(c["segment_objectives"][si], c["weights"])
                  for c in report["per_combo"]
                  if c["segment_objectives"][si] is not None]
        if scored:
            _, w = max(scored, key=lambda x: x[0])
            champions.append(w)
    report["is_segment_champions"] = champions

    # OOS 封存判定：只在定型权重上跑一次，不搜索、不调参
    oos_override = _override_for(best["weights"], directions)
    oos_res = _run_once(service, strategy_id, symbols, oos_start, end,
                        oos_override, objective, min_trades, **bt_kwargs)
    is_avg = best["robust"]["median"]
    oos_val = oos_res["objective"]
    degradation = (
        round(is_avg - oos_val, 4)
        if is_avg is not None and oos_val is not None else None
    )
    report["oos_evaluation"] = {
        "weights": best["weights"],
        **oos_res,
        "is_median_objective": is_avg,
        "degradation": degradation,  # 正=样本外退化
    }

    # 判定（纪律：样本外只用于判定）
    suff = report["adequacy"]["statistically_sufficient"]
    if not suff:
        verdict = "不可采纳：样本统计意义不足，权重结果仅作工具演示，禁止上线"
    elif oos_res["ok"] and degradation is not None and degradation <= 0.1 * abs(is_avg or 1):
        verdict = "候选采纳：OOS 退化 <10%，可进入监控观察"
    else:
        verdict = "拒绝：OOS 退化过大或无效，维持原权重"
    report["conclusion"] = verdict
    report["golden_rule"] = "样本外只用于判定，不用于调参；正式采纳需人工复核 + 完整数据重跑"
    return report


def main() -> int:
    p = argparse.ArgumentParser(description="Scoring 权重 OOS 校准（双闸门）")
    p.add_argument("--strategy", required=True, help="策略 id（在 data/strategies/custom/ 下）")
    p.add_argument("--start", required=True, type=date.fromisoformat)
    p.add_argument("--end", required=True, type=date.fromisoformat)
    p.add_argument("--factors", nargs="+", required=True, help="待校准的 scoring 因子 id")
    p.add_argument("--directions", default="", help="方向，如 'momentum_20d:low,annual_vol_20d:low'")
    p.add_argument("--step", type=float, default=0.25)
    p.add_argument("--k", type=int, default=3, help="IS 段数（3-6）")
    p.add_argument("--objective", default="sortino")
    p.add_argument("--min-trades", type=int, default=MIN_TRADES_DEFAULT)
    p.add_argument("--warmup-days", type=int, default=120, help="因子预热自然日（用于充分性判定）")
    p.add_argument("--holding-days", type=int, default=5)
    p.add_argument("--max-positions", type=int, default=5)
    p.add_argument("--symbols", nargs="*", default=None, help="标的池；省略则取样本 A 股")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    directions = {}
    for item in filter(None, args.directions.split(",")):
        k, _, v = item.partition(":")
        directions[k.strip()] = v.strip() or "high"

    data_dir = _BACKEND.parent / "data"
    symbols = args.symbols
    if not symbols:
        import polars as pl
        syms = []
        for f in sorted((data_dir / "kline_daily_enriched").rglob("part.parquet")):
            syms += [s for s in pl.read_parquet(f, columns=["symbol"])["symbol"].unique().to_list()
                     if s.endswith((".SH", ".SZ", ".BJ"))]
            if len(syms) >= 60:
                break
        symbols = list(dict.fromkeys(syms))[:40]

    report = calibrate(
        strategy_id=args.strategy, symbols=symbols, start=args.start, end=args.end,
        factors=args.factors, directions=directions, step=args.step, k=args.k,
        objective=args.objective, min_trades=args.min_trades,
        warmup_days=args.warmup_days, data_dir=data_dir,
        holding_days=args.holding_days, max_positions=args.max_positions,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"标的数: {len(symbols)}  权重组合: {report['grid']['n_combos']}  有效组合: {report.get('n_valid_combos', 0)}")
    print(f"样本充分: {report['adequacy']['statistically_sufficient']}  问题: {report['adequacy']['issues']}")
    if "best_is_weights" in report:
        print(f"IS 最优权重: {report['best_is_weights']}")
        print(f"OOS: {report['oos_evaluation'].get('objective')}  退化: {report['oos_evaluation'].get('degradation')}")
    print(f"结论: {report['conclusion']}")
    print(f"报告: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
