"""Walk-forward 优化 — 滚动窗口的样本内优化 + 样本外验证。

每折在训练区间用参数网格优化选出最优参数, 再在紧邻的测试区间用该参数做样本外(OOS)
回测。滚动前移。核心产出是 OOS 拼接净值 + 每折 IS-vs-OOS 退化 —— 样本内漂亮、样本外
崩溃即过拟合信号, 单次样本内回测看不到。

依赖 PR2a 的 StrategyOptimizer 做每折训练区间的网格优化。

──────────────────────── 追加: quantjourney-bt walkforward 契约对齐 ────────────────────────
本追加把 quantjourney-bt `backtester/walkforward/` 的契约面（scheme/anchor/purge/DSR/
top-K 秩稳定性/组合指标）对齐到本模块: 可独立使用的契约函数 + Fold/purge 生成器 +
统计函数（DSR/PSR、top-K 秩稳定性失败率、overfit/efficiency/decay、绿黄红解读），
以及 WalkForwardConfig 的契约字段（见 WalkForwardConfig 类）。无 scipy 依赖（标准正态
CDF/PPF 自实现），仅依赖项目既有 numpy/pandas。
"""
from __future__ import annotations

import logging
import math
import time
from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import combinations
from typing import Any, Literal

import hashlib

import numpy as np
import pandas as pd

from app.backtest.guards import trades_ok

logger = logging.getLogger(__name__)


@dataclass
class Fold:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def generate_folds(
    start: date,
    end: date,
    train_days: int,
    test_days: int,
    step_days: int,
) -> list[Fold]:
    """滚动窗口 fold 切分: 训练窗口固定长度, 测试窗口紧接其后, 按 step 前移。

    测试区间超出 end 即停止。数据区间放不下一折则抛错。
    """
    if train_days <= 0 or test_days <= 0 or step_days <= 0:
        raise ValueError("train_days / test_days / step_days 必须为正")

    folds: list[Fold] = []
    i = 0
    train_start = start
    while True:
        train_end = train_start + timedelta(days=train_days)
        # 测试区间从训练末日的次日开始: 回测区间是闭区间, 若 test_start==train_end 则
        # 该日 K 线同时进训练优化与 OOS 首日, 构成前视泄漏。后移一天隔断。
        test_start = train_end + timedelta(days=1)
        test_end = test_start + timedelta(days=test_days)
        if test_end > end:
            break
        folds.append(Fold(i, train_start, train_end, test_start, test_end))
        i += 1
        train_start = train_start + timedelta(days=step_days)

    if not folds:
        raise ValueError(
            f"数据区间不足以切出至少一折 (需 train+test={train_days + test_days}天, "
            f"实有 {(end - start).days}天)"
        )
    return folds


def _norm(v: float, direction: str) -> float:
    """把目标值归一到"越大越好"空间, 以便跨目标一致地算退化 (min 类目标取负)。"""
    return -v if direction == "min" else v


def aggregate_oos(fold_records: list[dict], objective: str, direction: str = "max") -> dict:
    """从**有效折** (IS 与 OOS 都成功) 聚合: 复利净值 / IS-OOS 退化 / 一致性。

    调用方只传有效折 (best_params 非空且 OOS 未 error), 故此处每折 is_score/oos_objective
    均有值, 无需 .get 默认兜底 —— 无效折被伪装成 0 收益混入曾是 H1/H2 的根因。

    - compounded_oos_return: 各折 OOS 总收益复利
    - degradation: 归一空间下 IS 目标均值 - OOS 目标均值, 正值 = 样本外退化 (过拟合信号),
      对"越小越好"目标 (max_drawdown 等) 方向也正确
    - consistency: OOS 总收益 > 0 的折占比 (与目标方向无关, 直观)
    """
    n = len(fold_records)
    if n == 0:
        return {
            "n_folds": 0,
            "compounded_oos_return": 0.0,
            "avg_is_objective": None,
            "avg_oos_objective": None,
            "degradation": None,
            "consistency": 0.0,
            "oos_equity_curve": [],
        }

    equity = 1.0
    curve: list[dict] = []
    n_positive = 0
    for f in fold_records:
        r = float(f["oos_stats"].get("total_return", 0.0) or 0.0)
        equity *= (1 + r)
        if r > 0:
            n_positive += 1
        curve.append({"fold": f["index"], "date": str(f["test_end"]), "value": round(equity, 4)})

    is_vals = [f["is_score"] for f in fold_records if f["is_score"] is not None]
    oos_vals = [f["oos_objective"] for f in fold_records if f["oos_objective"] is not None]
    avg_is = round(float(sum(is_vals) / len(is_vals)), 4) if is_vals else None
    avg_oos = round(float(sum(oos_vals) / len(oos_vals)), 4) if oos_vals else None
    degradation = (
        round(_norm(avg_is, direction) - _norm(avg_oos, direction), 4)
        if (avg_is is not None and avg_oos is not None) else None
    )

    return {
        "n_folds": n,
        "compounded_oos_return": round(equity - 1.0, 4),
        "avg_is_objective": avg_is,
        "avg_oos_objective": avg_oos,
        "degradation": degradation,
        "consistency": round(n_positive / n, 4),
        "oos_equity_curve": curve,
    }


@dataclass
class WalkForwardConfig:
    """Walk-forward 配置。

    保留原有 days-based 字段(train_days/test_days/step_days, 用于既有滚动
    窗口编排); 同时承载 quantjourney-bt walkforward 的**契约字段**
    (scheme/months 几何/pre-OOS purge/DSR/top-K 秩稳定性/cost 敏感度/
    统计控制/种子), 使本模块与 quantjourney-bt 步进回测契约对齐。
    """

    strategy_id: str
    symbols: list[str] | None
    start: date
    end: date
    param_grid: dict
    objective: str = "sortino"
    train_days: int = 252
    test_days: int = 63
    step_days: int = 63
    direction: str | None = None
    min_trades_oos: int = 10            # 每折 OOS 段最小交易数 (<=0 关闭)
    max_workers: int = 4
    base_params: dict = field(default_factory=dict)
    overrides: dict | None = None
    backtest_kwargs: dict = field(default_factory=dict)
    matrix_cache_max_mb: int = 512

    # ── quantjourney walkforward 契约字段（与 backtester/walkforward/config.py 对齐）──
    scheme: str = "rolling"  # rolling/expanding/anchored/cpcv
    train_months: int = 24
    test_months: int = 6
    min_train_months: int = 12
    step_months: int | None = None  # 默认 = test_months
    n_splits: int | None = None  # CPCV only
    purge_days: int = 5
    extra_pre_oos_purge_pct: float | None = None
    embargo_pct: float = 0.01  # 弃用别名, 由 resolved_extra_pre_oos_purge_pct 处理
    max_holding_period_days: int | None = None
    optimization: dict[str, Any] | None = None
    compute_deflated_sharpe: bool = True
    dsr_effective_n_trials: float | None = None
    compute_rank_stability: bool | None = None
    rank_stability_trials: int | None = None
    compute_pbo: bool = True
    pbo_n_partitions: int = 16
    pbo_trials: int = 0
    min_oos_sharpe: float = 0.0
    cost_sensitivity_bps: list[int] = field(default_factory=lambda: [0, 5, 10, 20])
    base_slippage_model: Any = None
    base_commission_scheme: Any = None
    n_jobs_optimizer: int = -1
    verbose: bool = True
    seed: int = 42

    def __post_init__(self) -> None:
        # 既有字段校验 (与原实现一致, 保证向后兼容)
        if self.train_days <= 0 or self.test_days <= 0 or self.step_days <= 0:
            raise ValueError("train_days / test_days / step_days 必须为正")
        if self.min_trades_oos < 0:
            raise ValueError("min_trades_oos 必须 >= 0")
        if not (0.0 <= self.embargo_pct <= 1.0):
            raise ValueError("embargo_pct must be in [0, 1]")
        if self.extra_pre_oos_purge_pct is not None and not (
            0.0 <= self.extra_pre_oos_purge_pct <= 1.0
        ):
            raise ValueError("extra_pre_oos_purge_pct must be in [0, 1]")
        if self.dsr_effective_n_trials is not None and (
            not math.isfinite(self.dsr_effective_n_trials) or self.dsr_effective_n_trials < 1.0
        ):
            raise ValueError("dsr_effective_n_trials must be finite and >= 1")
        if self.rank_stability_trials is not None and (
            self.rank_stability_trials < 0 or self.rank_stability_trials == 1
        ):
            raise ValueError("rank_stability_trials must be 0 (disabled) or >= 2")
        if self.pbo_trials < 0 or self.pbo_trials == 1:
            raise ValueError("pbo_trials must be 0 (disabled) or >= 2")
        if (
            self.rank_stability_trials is not None
            and self.pbo_trials != 0
            and self.rank_stability_trials != self.pbo_trials
        ):
            raise ValueError("rank_stability_trials and pbo_trials disagree; set only one")

    # ── 契约派生属性 (与 quantjourney config.to_dict 对齐) ──────────────

    @property
    def effective_step_months(self) -> int:
        """步进折起点间隔; 默认 = test_months (非重叠 OOS)。"""
        return self.step_months if self.step_months is not None else self.test_months

    @property
    def rank_stability_enabled(self) -> bool:
        """解析后的秩稳定性开关, 兼容 compute_pbo 别名。"""
        if self.compute_rank_stability is not None:
            return self.compute_rank_stability
        return self.compute_pbo

    @property
    def resolved_rank_stability_trials(self) -> int:
        """解析后的 top-K 大小, 兼容 pbo_trials 别名。"""
        if self.rank_stability_trials is not None:
            return self.rank_stability_trials
        return self.pbo_trials

    @property
    def resolved_extra_pre_oos_purge_pct(self) -> float:
        """解析后的 pre-OOS 百分比, 兼容 embargo_pct 别名。"""
        if self.extra_pre_oos_purge_pct is not None:
            return self.extra_pre_oos_purge_pct
        return self.embargo_pct

    def to_dict(self) -> dict[str, Any]:
        """JSON 安全序列化 (剥离不可序列对象, 保留契约派生值)。"""
        import math as _math
        from dataclasses import asdict

        d = asdict(self)
        d["scheme"] = self.scheme
        d["step_months"] = self.step_months
        d["n_splits"] = self.n_splits
        d["purge_days"] = self.purge_days
        d["extra_pre_oos_purge_pct"] = self.resolved_extra_pre_oos_purge_pct
        d["embargo_pct"] = self.embargo_pct
        d["max_holding_period_days"] = self.max_holding_period_days
        d["optimization"] = self.optimization
        d["compute_deflated_sharpe"] = self.compute_deflated_sharpe
        d["dsr_effective_n_trials"] = self.dsr_effective_n_trials
        d["compute_rank_stability"] = self.compute_rank_stability
        d["rank_stability_trials"] = self.resolved_rank_stability_trials
        d["compute_pbo"] = self.compute_pbo
        d["pbo_n_partitions"] = self.pbo_n_partitions
        d["pbo_trials"] = self.pbo_trials
        d["min_oos_sharpe"] = self.min_oos_sharpe
        d["cost_sensitivity_bps"] = self.cost_sensitivity_bps
        d["base_slippage_model"] = (
            type(self.base_slippage_model).__name__ if self.base_slippage_model is not None else None
        )
        d["base_commission_scheme"] = (
            type(self.base_commission_scheme).__name__
            if self.base_commission_scheme is not None
            else None
        )
        d["n_jobs_optimizer"] = self.n_jobs_optimizer
        d["verbose"] = self.verbose
        d["seed"] = self.seed
        # 非有限浮点安全化 (避免 JSON 序列化失败)
        for k, v in list(d.items()):
            if isinstance(v, float) and not _math.isfinite(v):
                d[k] = None
        return d


class WalkForwardService:
    """滚动窗口 walk-forward: 每折训练区间优化 -> 测试区间 OOS 验证 -> 聚合。"""

    def __init__(self, optimizer, service, strategy_engine) -> None:
        self.optimizer = optimizer
        self.service = service
        self.strategy_engine = strategy_engine

    def _prepare_shared_matrix(self, cfg: WalkForwardConfig, folds: list[Fold]):
        """Build one immutable superset matrix for every matrix-native fold.

        返回 None 时 run() 走通用路径: 每折独立优化 + OOS 回测, 正确但无共享矩阵加速。
        python_history_legacy (filter_history) 与 polars_expr (内置) 策略无法装入
        共享矩阵, 走通用路径; composite / minute_filter 仍不支持, 保持 fail-closed。
        """
        if self.strategy_engine is None or not folds:
            return None
        strategy = self.strategy_engine.get(cfg.strategy_id)
        if strategy.execution_backend != "matrix_native":
            if strategy.execution_backend in ("python_history_legacy", "polars_expr"):
                return None
            raise ValueError(
                f"步进优化暂仅支持矩阵(matrix_native)/日线历史(python_history_legacy/"
                f"polars_expr)策略; {cfg.strategy_id} 是 {strategy.execution_backend}"
            )

        from app.backtest.optimizer import expand_param_grid
        from app.backtest.strategy import StrategyBacktestConfig

        combos = expand_param_grid(strategy.meta.get("params", []), cfg.param_grid)
        shared_start = min(fold.train_start for fold in folds)
        shared_end = max(fold.test_end for fold in folds)
        configs = [
            StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=shared_start,
                end=shared_end,
                params={**cfg.base_params, **combo},
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            for combo in combos
        ]
        return self.service.prepare_matrix_optimization(
            configs,
            matrix_cache_max_bytes=int(cfg.matrix_cache_max_mb) * 1024 * 1024,
        )

    def run(
        self,
        cfg: WalkForwardConfig,
        progress_cb=None,
        cancel_event=None,
    ) -> dict:
        from app.backtest.optimizer import OptimizeConfig, default_direction
        from app.backtest.strategy import StrategyBacktestConfig

        t0 = time.perf_counter()
        direction = cfg.direction or default_direction(cfg.objective)
        folds = generate_folds(cfg.start, cfg.end, cfg.train_days, cfg.test_days, cfg.step_days)
        n_total = len(folds)

        shared_prepared = self._prepare_shared_matrix(cfg, folds)
        shared_market_data = (
            shared_prepared.market_data if shared_prepared is not None else None
        )
        shared_date_labels = (
            tuple(label[:10] for label in shared_market_data.timestamp_labels)
            if shared_market_data is not None
            else ()
        )

        def _shared_window_has_data(window_start: date, window_end: date) -> bool:
            index = bisect_left(shared_date_labels, window_start.isoformat())
            return index < len(shared_date_labels) and shared_date_labels[index] <= window_end.isoformat()

        # 遥测: 首尾快照 PanelCache, 量化跨折重叠区间重复扫盘的 IO 占比 (是否值得进一步优化)。
        cache_before = self.service.engine.cache_stats()

        valid_records: list[dict] = []   # IS 与 OOS 都成功, 计入聚合
        skipped: list[dict] = []          # 无优化结果 或 OOS 失败, 不计入聚合 (避免伪装成有效折)
        done = 0

        # IS 训练区间强制 position 模式: full 模式会让训练折未平仓持仓用 train_end 之后
        # (即 OOS 区间) 的真实 K 线平仓, IS 分数被未来数据污染 -> 优化选参乐观偏移, 使过拟合
        # 被掩盖。OOS 回测保留用户所选 mode。参数扫描优化只看正式区间内的表现即可。
        is_backtest_kwargs = {**cfg.backtest_kwargs, "mode": "position"}

        for f in folds:
            if cancel_event is not None and cancel_event.is_set():
                break

            base = {
                "index": f.index,
                "train_start": str(f.train_start),
                "train_end": str(f.train_end),
                "test_start": str(f.test_start),
                "test_end": str(f.test_end),
            }
            missing_window = None
            if shared_market_data is not None:
                if not _shared_window_has_data(f.train_start, f.train_end):
                    missing_window = "训练区间无可用行情数据"
                elif not _shared_window_has_data(f.test_start, f.test_end):
                    missing_window = "测试区间无可用行情数据"
            if missing_window is not None:
                skipped.append({**base, "reason": missing_window})
                done += 1
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            # 训练区间: 网格优化选最优参数
            opt_cfg = OptimizeConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.train_start,
                end=f.train_end,
                param_grid=cfg.param_grid,
                objective=cfg.objective,
                direction=cfg.direction,
                max_workers=cfg.max_workers,
                base_params=cfg.base_params,
                overrides=cfg.overrides,
                backtest_kwargs=is_backtest_kwargs,  # IS 强制 position, 堵前视泄漏
            )
            if shared_market_data is None:
                opt_res = self.optimizer.optimize(opt_cfg, cancel_event=cancel_event)
            else:
                opt_res = self.optimizer.optimize(
                    opt_cfg,
                    cancel_event=cancel_event,
                    prepared_market_data=shared_market_data,
                )
            best_params = opt_res.get("best_params")
            is_score = opt_res.get("best_score")
            done += 1

            # 训练区间没优化出参数 (全组失败/取消) -> 跳过, 不用默认参数硬跑 OOS 伪装成有效折
            if best_params is None:
                skipped.append({**base, "reason": "训练区间未优化出参数"})
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            # 测试区间: 用最优参数做样本外回测
            merged = {**cfg.base_params, **best_params}
            oos_cfg = StrategyBacktestConfig(
                strategy_id=cfg.strategy_id,
                symbols=cfg.symbols,
                start=f.test_start,
                end=f.test_end,
                params=merged,
                overrides=cfg.overrides,
                **cfg.backtest_kwargs,
            )
            oos_prepared = None
            try:
                if shared_market_data is not None:
                    oos_prepared = self.service.prepare_matrix_optimization(
                        [oos_cfg],
                        matrix_cache_max_bytes=int(cfg.matrix_cache_max_mb) * 1024 * 1024,
                        market_data_override=shared_market_data,
                    )
                if oos_prepared is None:
                    oos_res = self.service.run(
                        oos_cfg,
                        cancel_event=cancel_event,
                    )
                else:
                    oos_res = self.service.run(
                        oos_cfg,
                        cancel_event=cancel_event,
                        prepared=oos_prepared,
                    )
            finally:
                if oos_prepared is not None:
                    oos_prepared.compute_cache.close()

            # OOS 失败 (含 cancelled) -> 跳过, 不把空/0 收益混入复利曲线
            if oos_res.error:
                skipped.append({**base, "best_params": best_params, "reason": f"OOS 回测失败: {oos_res.error}"})
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            # 交易数门槛 (共享护栏): 少数几笔的 OOS 段统计意义不足, 混入复利曲线
            # 会被运气主导 —— 标记无效折跳过, 与数据缺口折同一透明处理。
            if not trades_ok(oos_res.stats, cfg.min_trades_oos):
                skipped.append({
                    **base, "best_params": best_params,
                    "reason": f"OOS 交易数不足 ({oos_res.stats.get('n_trades') or 0} < {cfg.min_trades_oos})",
                })
                if progress_cb is not None:
                    progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})
                continue

            oos_objective = oos_res.stats.get(cfg.objective)
            # 该折 OOS 是否较 IS 退化 (方向感知: min 类目标数值更大才是退化)
            oos_degraded = (
                _norm(oos_objective, direction) < _norm(is_score, direction)
                if (oos_objective is not None and is_score is not None) else None
            )
            valid_records.append({
                **base,
                "best_params": best_params,
                "is_score": is_score,
                "oos_objective": oos_objective,
                "oos_degraded": oos_degraded,
                "oos_stats": oos_res.stats,
            })

            if progress_cb is not None:
                progress_cb({"type": "walkforward_progress", "done": done, "total": n_total, "fold": f.index})

        summary = aggregate_oos(valid_records, cfg.objective, direction)

        shared_matrix_bytes = (
            shared_prepared.market_data.nbytes if shared_prepared is not None else 0
        )
        shared_matrix_status = (
            shared_prepared.market_data.cache_status if shared_prepared is not None else "none"
        )
        if shared_prepared is not None:
            shared_prepared.compute_cache.close()

        # 遥测收尾: 本次 WF 累计扫盘耗时 / 命中 / 复用, 与总耗时对比得出 load_panel 占比。
        cache_after = self.service.engine.cache_stats()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        io_seconds = round(cache_after["compute_seconds"] - cache_before["compute_seconds"], 4)
        io_pct = round(io_seconds * 1000 / elapsed_ms * 100, 1) if elapsed_ms > 0 else 0.0
        cache_telemetry = {
            "load_panel_seconds": io_seconds,
            "load_panel_pct": io_pct,  # 扫盘耗时 / WF 总耗时
            "scans": cache_after["compute_count"] - cache_before["compute_count"],
            "hits": cache_after["hit_count"] - cache_before["hit_count"],
            "single_flight_reuses": cache_after["reuse_count"] - cache_before["reuse_count"],
        }
        logger.info(
            "walk-forward IO 占比: load_panel %.3fs (%.1f%% of %.1fms) | 扫盘 %d 次 命中 %d 复用 %d",
            io_seconds, io_pct, elapsed_ms,
            cache_telemetry["scans"], cache_telemetry["hits"], cache_telemetry["single_flight_reuses"],
        )

        return {
            "objective": cfg.objective,
            "direction": direction,
            "n_folds": len(valid_records),
            "n_skipped": len(skipped),
            "n_planned_folds": n_total,
            "folds": valid_records,
            "skipped": skipped,
            "summary": summary,
            "cache_telemetry": cache_telemetry,
            "shared_market_data": shared_prepared is not None,
            "shared_market_data_bytes": shared_matrix_bytes,
            "shared_market_data_status": shared_matrix_status,
            "elapsed_ms": elapsed_ms,
        }


# =====================================================================
# quantjourney-bt walkforward 契约对齐（追加）
# 把 quantjourney `backtester/walkforward/` 的契约面对齐到本模块：
#   - 数据契约：Fold（fold_id/scheme/训练/测试窗口/effective IS/pre-OOS purge）
#   - 生成契约：compute_pre_oos_purge / compute_purge_embargo / scheme 工厂
#     （rolling/expanding/anchored/cpcv）
#   - 统计契约：DSR/PSR、top-K 秩稳定性失败率、overfit/efficiency/decay、
#     绿黄红解读、聚合 OOS 净值/组合指标/bootstrap CI
# 全部自包含：标准正态 CDF/PPF 用 math.erfc + Newton 迭代（无 scipy），
# 仅依赖项目既有 numpy/pandas；WalkForwardConfig 的契约字段见其类定义。
# =====================================================================

# ── Fold 数据契约（与 backtester/walkforward/folds/base.Fold 对齐）────────────

@dataclass
class QJFold:
    """单个 walk-forward 折的不可变描述（IS/OOS 窗口 + 处理后 purge）。

    与本模块既有 `Fold`（index/train_*）是两套契约: 后者为 app 原生 days-based
    折叠, 本类型为 quantjourney 月份/窗口契约。二者并存, 各归其用。
    """

    fold_id: int
    scheme: str
    # 原始边界（purge 之前）
    train_start: pd.Timestamp
    train_days: int
    oos_start: pd.Timestamp
    oos_days: int
    # 经过 pre-OOS purge 后的 effective IS 结束
    effective_is_days: int
    purge_start: pd.Timestamp | None  # 首个被排除的 IS 交易日
    purge_days: pd.Timestamp | None  # 最后一个被排除的 IS 交易日


class FoldScheme:
    """Fold 生成协议的别名（与 quantjourney folds.base.FoldScheme 对齐）。"""

    def generate_folds(
        self, start, end, trading_dates
    ) -> list[QJFold]:  # pragma: no cover - 协议声明
        ...


# ── pre-OOS purge（compute_pre_oos_purge / compute_purge_embargo）───────────

def compute_pre_oos_purge(
    is_start,
    is_dates,
    oos_start,
    purge_days,
    extra_pre_oos_purge_pct,
    trading_dates,
    max_holding_period_days=None,
):
    """计算完整 pre-OOS purge（与 quantjourney folds/purge.py 一致）。"""
    is_length = len(is_dates)
    if is_length == 0:
        return 0, is_start, None

    effective_purge = purge_days
    if max_holding_period_days is not None:
        effective_purge = max(effective_purge, max_holding_period_days)

    percentage_extension_days = max(
        0, int(extra_pre_oos_purge_pct * is_length)
    ) if extra_pre_oos_purge_pct is not None else 0

    total_remove = effective_purge + percentage_extension_days

    if total_remove == 0:
        return is_length, None, None

    if total_remove >= is_length:
        effective_is_days = 0
        purge_start = is_dates[0]
    else:
        effective_is_days = is_dates[-1]  # 占位，下方覆盖
        effective_is_days = len(is_dates) - total_remove - 1
        purge_start = is_dates[-(total_remove + 1)]

    pre_oos = trading_dates[trading_dates < oos_start]
    purge_days_end = pre_oos[-1] if len(pre_oos) > 0 else oos_start

    return effective_is_days, purge_start, purge_days_end


def compute_purge_embargo(
    is_start,
    is_dates,
    oos_start,
    purge_days,
    embargo_pct,
    trading_dates,
    max_holding_period_days=None,
):
    """compute_pre_oos_purge 的弃用别名（与 quantjourney 一致）。"""
    import warnings as _warnings

    _warnings.warn(
        "compute_purge_embargo/embargo_pct is deprecated; use "
        "compute_pre_oos_purge(extra_pre_oos_purge_pct=...). The behavior "
        "is a pre-OOS purge extension, not a post-test embargo.",
        DeprecationWarning,
        stacklevel=2,
    )
    return compute_pre_oos_purge(
        is_start,
        is_dates,
        oos_start,
        purge_days,
        embargo_pct,
        trading_dates,
        max_holding_period_days=max_holding_period_days,
    )


# ── fold scheme 生成器（rolling/expanding/anchored/cpcv）───────────────────

class _MonthsScheme:
    """按月步进生成折的基类（用 pandas DateOffset 表示月增量）。"""

    def _step(self, months):
        return pd.DateOffset(months=months)

    def generate_folds(self, start, end, trading_dates):  # pragma: no cover
        raise NotImplementedError


class RollingFoldScheme(_MonthsScheme, FoldScheme):
    """固定宽度训练窗口，OOS 前移 step 个月。"""

    def __init__(self, config=None):
        self._cfg = config

    def generate_folds(self, start, end, trading_dates):
        step = self._step(self._effective_step_months())
        train_delta = self._step(self._train_months())
        test_delta = self._step(self._test_months())
        min_train_delta = self._step(self._min_train_months())

        fold_id = 0
        oos_cursor = start + train_delta
        folds: list[QJFold] = []
        while oos_cursor <= end:
            train_start_dt = oos_cursor - train_delta
            train_end_dt = oos_cursor - pd.Timedelta(days=1)
            oos_start_dt = oos_cursor
            oos_end_dt = min(
                oos_cursor + test_delta - pd.Timedelta(days=1), end
            )

            train_dates = trading_dates[
                (trading_dates >= train_start_dt)
                & (trading_dates <= train_end_dt)
            ]
            oos_dates = trading_dates[
                (trading_dates >= oos_start_dt)
                & (trading_dates <= oos_end_dt)
            ]

            if len(train_dates) == 0 or len(train_dates) < len(
                trading_dates[(trading_dates >= train_start_dt)
                              & (trading_dates <= train_start_dt + min_train_delta)]
            ):
                oos_cursor += step
                continue
            if len(oos_dates) == 0:
                oos_cursor += step
                continue

            t_start, t_end = train_dates[0], train_dates[-1]
            o_start, o_end = oos_dates[0], oos_dates[-1]
            eff, purge_start, purge_end = compute_pre_oos_purge(
                is_start=t_start,
                is_dates=train_dates,
                oos_start=o_start,
                purge_days=self._purge_days(),
                extra_pre_oos_purge_pct=self._resolved_extra_pct(),
                trading_dates=trading_dates,
                max_holding_period_days=self._max_holding_period_days(),
            )

            folds.append(
                QJFold(
                    fold_id=fold_id,
                    scheme="rolling",
                    train_start=t_start,
                    train_days=len(train_dates),
                    oos_start=o_start,
                    oos_days=len(oos_dates),
                    effective_is_days=eff,
                    purge_start=purge_start,
                    purge_days=purge_end,
                )
            )
            fold_id += 1
            oos_cursor += step
        return folds

    def _effective_step_months(self):
        cfg = getattr(self, "_cfg", None)
        if cfg is not None:
            return cfg.effective_step_months
        return 0

    def _train_months(self):
        cfg = getattr(self, "_cfg", None)
        if cfg is not None:
            return cfg.train_months
        return 0

    def _test_months(self):
        cfg = getattr(self, "_cfg", None)
        if cfg is not None:
            return cfg.test_months
        return 0

    def _min_train_months(self):
        cfg = getattr(self, "_cfg", None)
        if cfg is not None:
            return cfg.min_train_months
        return 0

    def _purge_days(self):
        cfg = getattr(self, "_cfg", None)
        if cfg is not None:
            return cfg.purge_days
        return 0

    def _resolved_extra_pct(self):
        cfg = getattr(self, "_cfg", None)
        if cfg is not None:
            return cfg.resolved_extra_pre_oos_purge_pct
        return 0.0

    def _max_holding_period_days(self):
        cfg = getattr(self, "_cfg", None)
        if cfg is not None:
            return cfg.max_holding_period_days
        return None


class ExpandingFoldScheme(RollingFoldScheme, FoldScheme):
    """IS 从数据起点开始，逐折前移。"""

    def __init__(self, config=None):
        self._cfg = config

    def generate_folds(self, start, end, trading_dates):
        step = self._step(self._effective_step_months())
        train_delta = self._step(self._train_months())
        test_delta = self._step(self._test_months())
        min_train_delta = self._step(self._min_train_months())

        fold_id = 0
        oos_cursor = start + train_delta
        folds: list[QJFold] = []
        while oos_cursor <= end:
            train_start_dt = start
            train_end_dt = oos_cursor - pd.Timedelta(days=1)
            oos_start_dt = oos_cursor
            oos_end_dt = min(
                oos_cursor + test_delta - pd.Timedelta(days=1), end
            )

            train_dates = trading_dates[
                (trading_dates >= train_start_dt)
                & (trading_dates <= train_end_dt)
            ]
            oos_dates = trading_dates[
                (trading_dates >= oos_start_dt)
                & (trading_dates <= oos_end_dt)
            ]

            min_len = max(
                1,
                len(
                    trading_dates[
                        (trading_dates >= train_start_dt)
                        & (trading_dates <= train_start_dt + min_train_delta)
                    ]
                ),
            )
            if len(train_dates) < min_len or len(oos_dates) == 0:
                oos_cursor += step
                continue

            t_start, t_end = train_dates[0], train_dates[-1]
            o_start, o_end = oos_dates[0], oos_dates[-1]
            eff, purge_start, purge_end = compute_pre_oos_purge(
                is_start=t_start,
                is_dates=train_dates,
                oos_start=o_start,
                purge_days=self._purge_days(),
                extra_pre_oos_purge_pct=self._resolved_extra_pct(),
                trading_dates=trading_dates,
                max_holding_period_days=self._max_holding_period_days(),
            )

            folds.append(
                QJFold(
                    fold_id=fold_id,
                    scheme="expanding",
                    train_start=t_start,
                    train_days=len(train_dates),
                    oos_start=o_start,
                    oos_days=len(oos_dates),
                    effective_is_days=eff,
                    purge_start=purge_start,
                    purge_days=purge_end,
                )
            )
            fold_id += 1
            oos_cursor += step
        return folds


class AnchoredFoldScheme(ExpandingFoldScheme, FoldScheme):
    """IS 锚定在数据起点，宽度随 step 增长（当前等价于 Expanding）。"""

    def generate_folds(self, start, end, trading_dates):
        step = self._step(self._effective_step_months())
        train_delta = self._step(self._train_months())
        test_delta = self._step(self._test_months())
        min_train_delta = self._step(self._min_train_months())

        folds: list[QJFold] = []
        anchor = start
        fold_id = 0
        oos_cursor = anchor + train_delta
        while oos_cursor <= end:
            train_start_dt = anchor
            train_end_dt = oos_cursor - pd.Timedelta(days=1)
            oos_start_dt = oos_cursor
            oos_end_dt = min(
                oos_cursor + test_delta - pd.Timedelta(days=1), end
            )

            train_dates = trading_dates[
                (trading_dates >= train_start_dt)
                & (trading_dates <= train_end_dt)
            ]
            oos_dates = trading_dates[
                (trading_dates >= oos_start_dt)
                & (trading_dates <= oos_end_dt)
            ]

            min_len = max(
                1,
                len(
                    trading_dates[
                        (trading_dates >= train_start_dt)
                        & (trading_dates <= train_start_dt + min_train_delta)
                    ]
                ),
            )
            if len(train_dates) < min_len or len(oos_dates) == 0:
                oos_cursor += step
                continue

            t_start, t_end = train_dates[0], train_dates[-1]
            o_start, o_end = oos_dates[0], oos_dates[-1]
            eff, purge_start, purge_end = compute_pre_oos_purge(
                is_start=t_start,
                is_dates=train_dates,
                oos_start=o_start,
                purge_days=self._purge_days(),
                extra_pre_oos_purge_pct=self._resolved_extra_pct(),
                trading_dates=trading_dates,
                max_holding_period_days=self._max_holding_period_days(),
            )

            folds.append(
                QJFold(
                    fold_id=fold_id,
                    scheme="anchored",
                    train_start=t_start,
                    train_days=len(train_dates),
                    oos_start=o_start,
                    oos_days=len(oos_dates),
                    effective_is_days=eff,
                    purge_start=purge_start,
                    purge_days=purge_end,
                )
            )
            fold_id += 1
            oos_cursor += step
        return folds


class CPCVFoldScheme(FoldScheme):
    """组合 purge 交叉验证（暂未实现，与 quantjourney 行为一致）。"""

    def __init__(self, config=None):
        self._cfg = config

    def generate_folds(self, start, end, trading_dates):
        raise NotImplementedError(
            "scheme='cpcv' 尚未实现, 使用 rolling/expanding/anchored"
        )


def fold_scheme_factory(config) -> FoldScheme:
    """按 config.scheme 实例化对应 fold 生成器。"""
    registry = {
        "rolling": RollingFoldScheme,
        "expanding": ExpandingFoldScheme,
        "anchored": AnchoredFoldScheme,
        "cpcv": CPCVFoldScheme,
    }
    cls = registry.get(config.scheme)
    if cls is None:
        raise ValueError(
            f"Unknown fold scheme {config.scheme!r}. Available: {list(registry)}"
        )
    return cls(config)


# ── 标准正态统计（自包含，无 scipy）─────────────────────────────────────────

def _norm_cdf(z: float) -> float:
    # Standard normal CDF:  Phi(z) = 0.5 * erfc(-z / sqrt(2))
    return 0.5 * math.erfc(-z / math.sqrt(2.0))


def _norm_phi(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def _norm_ppf(p: float) -> float:
    """标准正态分位（Acklam 初值 + Newton 迭代修正，自包含）。"""
    if p <= 0.0:
        return float("-inf")
    if p >= 1.0:
        return float("inf")
    if p == 0.5:
        return 0.0
    if p < 0.5:
        return -_norm_ppf(1.0 - p)

    # p in (0.5, 1): 初值来自尾型近似, 再 Newton 精修到 1e-15
    z = math.sqrt(-2.0 * math.log(1.0 - p))
    for _ in range(200):
        diff = _norm_cdf(z) - p
        step = diff / _norm_phi(z)
        z -= step
        if abs(step) < 1e-15:
            break
    return z


# ── DSR / PSR（Bailey & López de Prado 2014）───────────────────────────────

_GAMMA = 0.5772156649015329


def _expected_max_sr(sr_std: float, n_trials: float) -> float:
    if not math.isfinite(n_trials) or n_trials <= 1:
        return 0.0
    z1 = _norm_ppf(1.0 - 1.0 / n_trials)
    z2 = _norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return sr_std * ((1.0 - _GAMMA) * z1 + _GAMMA * z2)


def probabilistic_sharpe(
    observed_sr,
    *,
    benchmark_sr: float = 0.0,
    n_obs,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    if n_obs is None or n_obs < 2:
        return 0.0
    denom_sq = 1.0 - skewness * observed_sr + ((kurtosis - 1.0) / 4.0) * observed_sr**2
    if denom_sq <= 0.0:
        denom_sq = 1.0 + 0.5 * observed_sr**2
    z = (observed_sr - benchmark_sr) * math.sqrt(n_obs - 1.0) / math.sqrt(denom_sq)
    return float(_norm_cdf(z))


def deflated_sharpe(
    trial_sharpes,
    n_trials=None,
    *,
    effective_n_trials=None,
    observed_sr=None,
    n_obs,
    skewness: float = 0.0,
    kurtosis: float = 3.0,
    benchmark_sr: float = 0.0,
) -> float:
    """DSR = PSR(SR₀)，校正多检验选择偏差（与 quantjourney statistics.deflated_sharpe 对齐）。"""
    arr = np.asarray(list(trial_sharpes), dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if observed_sr is None:
        if arr.size == 0:
            return 0.0
        observed_sr = float(arr.max())
    raw_n = int(n_trials) if n_trials is not None else int(arr.size)
    if raw_n < 0:
        raise ValueError("n_trials must be >= 0")
    if effective_n_trials is None:
        effective_n = float(raw_n)
    else:
        effective_n = float(effective_n_trials)
        if not math.isfinite(effective_n) or effective_n < 1.0:
            raise ValueError("effective_n_trials must be finite and >= 1")
        if raw_n >= 1 and effective_n > raw_n:
            raise ValueError("effective_n_trials must be <= n_trials")

    sr_std = float(arr.std(ddof=0)) if arr.size > 1 else 0.0
    if effective_n <= 1.0 or sr_std == 0.0:
        sr0 = benchmark_sr
    else:
        sr0 = benchmark_sr + _expected_max_sr(sr_std, effective_n)

    return probabilistic_sharpe(
        float(observed_sr),
        benchmark_sr=sr0,
        n_obs=n_obs,
        skewness=skewness,
        kurtosis=kurtosis,
    )


# ── 聚合 OOS 净值 / 组合指标 / bootstrap CI ─────────────────────────────────

def aggregate_oos_returns(
    fold_oos_returns: list[pd.Series],
) -> tuple[pd.Series, pd.Series]:
    """拼接各折 OOS 收益，重叠日期取均值（与 quantjourney aggregation 一致）。"""
    if not fold_oos_returns:
        empty = pd.Series(dtype=float)
        return empty, empty

    combined = pd.concat(fold_oos_returns)
    if combined.index.duplicated().any():
        import warnings as _warnings

        _warnings.warn(
            "Overlapping OOS windows detected (step < test): duplicated dates "
            "are averaged across folds.",
            stacklevel=2,
        )
        combined = combined.groupby(combined.index).mean()
    combined = combined.sort_index()
    nav = (1.0 + combined).cumprod()
    return combined, nav


def compute_composite_metrics(
    oos_returns: pd.Series,
    risk_free_rate: float = 0.0,
    trading_days: int = 252,
) -> dict[str, float]:
    """从拼接后的 OOS 收益计算组合指标（与 quantjourney compute_composite_metrics 一致）。"""
    if oos_returns.empty:
        return {"sharpe": 0.0, "cagr": 0.0, "max_dd": 0.0, "volatility": 0.0}

    n_days = len(oos_returns)
    years = n_days / trading_days
    total_return = (1.0 + oos_returns).prod() - 1.0
    cagr = (1.0 + total_return) ** (1.0 / max(years, 1e-9)) - 1.0
    vol = oos_returns.std() * math.sqrt(trading_days)
    excess = oos_returns.mean() - risk_free_rate / trading_days
    sharpe = (excess / oos_returns.std() * math.sqrt(trading_days)) if oos_returns.std() > 0 else 0.0
    nav = (1.0 + oos_returns).cumprod()
    running_max = nav.cummax()
    max_dd = float(((nav - running_max) / running_max).min())
    return {
        "sharpe": float(sharpe),
        "cagr": float(cagr),
        "max_dd": max_dd,
        "volatility": float(vol),
    }


def bootstrap_sharpe_ci(
    returns: pd.Series,
    *,
    n_resamples: int = 1000,
    seed: int = 42,
    risk_free_rate: float = 0.0,
    trading_days: int = 252,
) -> tuple[float, float] | None:
    """Politis-Romano 块 bootstrap 的 Sharpe 5%/95% CI（自包含 numpy）。"""
    r = returns.to_numpy(dtype=float)
    t = r.size
    if t < 20 or not np.isfinite(r).all() or float(np.std(r, ddof=1)) == 0.0:
        return None

    rng = np.random.default_rng(seed)
    expected_block = max(1, int(round(math.sqrt(t))))
    p_restart = 1.0 / expected_block
    rfr_daily = risk_free_rate / trading_days
    ann = math.sqrt(trading_days)
    pos = np.arange(t)

    sharpes = np.empty(n_resamples, dtype=float)
    for i in range(n_resamples):
        restarts = rng.random(t) < p_restart
        restarts[0] = True
        starts = rng.integers(0, t, size=t)
        last_restart = np.maximum.accumulate(np.where(restarts, pos, -1))
        idx = (starts[last_restart] + (pos - last_restart)) % t
        sample = r[idx]
        sd = sample.std(ddof=1)
        sharpes[i] = (
            (sample.mean() - rfr_daily) / sd * ann
            if sd > 0
            else 0.0
        )

    lo, hi = np.percentile(sharpes, [5.0, 95.0])
    return float(lo), float(hi)


# ── overfit / efficiency / sharpe_decay 与绿黄红解读 ───────────────────────

def overfit_ratio(is_sharpe: float, oos_sharpe: float) -> float:
    """IS Sharpe / OOS Sharpe；OOS ≤ 0 时无意义（与 quantjourney 一致）。"""
    if oos_sharpe <= 0:
        return float("inf") if is_sharpe > 0 else 0.0
    return is_sharpe / oos_sharpe


def efficiency(is_cagr: float, oos_cagr: float) -> float:
    if is_cagr <= 0:
        return 0.0
    return oos_cagr / is_cagr


def sharpe_decay(oos_sharpes: Sequence) -> float:
    """OOS Sharpe 随折的线性回归斜率（负值 = 衰退）。"""
    import numpy as _np

    arr = [float(x) for x in oos_sharpes]
    n = len(arr)
    if n < 2:
        return 0.0
    x = _np.arange(n, dtype=float)
    y = _np.array(arr, dtype=float)
    x_mean, y_mean = x.mean(), y.mean()
    denom = ((x - x_mean) ** 2).sum()
    if denom == 0:
        return 0.0
    return float(((x - x_mean) * (y - y_mean)).sum() / denom)


def aggregate_overfit_ratio(is_sharpes, oos_sharpes) -> float:
    """平均 IS Sharpe / 平均 OOS Sharpe。"""
    return overfit_ratio(float(np.mean([float(s) for s in is_sharpes])),
                         float(np.mean([float(s) for s in oos_sharpes])))


def aggregate_efficiency(is_cagrs, oos_cagrs) -> float:
    return efficiency(float(np.mean([float(s) for s in is_cagrs])),
                      float(np.mean([float(s) for s in oos_cagrs])))


class MetricVerdict:
    """单个 WF 指标解读（与 quantjourney interpretation.MetricVerdict 对齐）。"""

    def __init__(self, name, value, signal, description):
        self.name = name
        self.value = value
        self.signal = signal
        self.description = description


_GATE = {
    "overfit_ratio": {
        "green": lambda v: v < 1.5,
        "yellow": lambda v: 1.5 <= v <= 2.5,
        "green_desc": "< 1.5 — robust",
        "yellow_desc": "1.5–2.5 — caution",
        "red_desc": "> 2.5 — likely overfit",
    },
    "efficiency": {
        "green": lambda v: v > 0.7,
        "yellow": lambda v: 0.4 <= v <= 0.7,
        "green_desc": "> 0.7 — robust transfer",
        "yellow_desc": "0.4–0.7 — moderate degradation",
        "red_desc": "< 0.4 — poor transfer",
    },
    "sharpe_decay": {
        "green": lambda v: v > -0.01,
        "yellow": lambda v: -0.05 <= v <= -0.01,
        "green_desc": "> -0.01/fold — stable",
        "yellow_desc": "-0.01 to -0.05/fold — moderate decay",
        "red_desc": "< -0.05/fold — alpha decaying",
    },
    "deflated_sharpe": {
        "green": lambda v: v >= 0.95,
        "yellow": lambda v: 0.80 <= v < 0.95,
        "green_desc": ">= 0.95 — robust vs multiple testing",
        "yellow_desc": "0.80–0.95 — marginal",
        "red_desc": "< 0.80 — likely false positive",
    },
}


def interpret_metrics(metrics: dict, oos_sharpe=None) -> list[MetricVerdict]:
    """把 WF 指标字典转为绿黄红信号（与 quantjourney interpretation 一致）。"""
    losing = (
        oos_sharpe is not None
        and oos_sharpe <= 0.0
    )
    verdicts = []
    for name, value in metrics.items():
        if name not in _GATE:
            continue
        if value is None:
            continue
        if isinstance(value, float) and math.isnan(value):
            continue
        if losing and name in ("overfit_ratio", "sharpe_decay"):
            signal, desc = "red", (
                "OOS Sharpe <= 0 — strategy loses out-of-sample; "
                f"{name} verdict suppressed (never green on a losing strategy)"
            )
        elif name == "overfit_ratio" and value < 0.0:
            signal, desc = "yellow", (
                "ratio < 0 — IS Sharpe negative; overfit ratio not interpretable"
            )
        else:
            entry = _GATE[name]
            if entry["green"](value):
                signal, desc = "green", entry["green_desc"]
            elif entry["yellow"](value):
                signal, desc = "yellow", entry["yellow_desc"]
            else:
                signal, desc = "red", entry["red_desc"]
        verdicts.append(MetricVerdict(name, value, signal, desc))
    return verdicts


# ── top-K 秩稳定性失败率 ────────────────────────────────────────────────────

def selected_trial_rank_logit(
    selected_value: float,
    candidate_values: Sequence[float],
) -> float | None:
    """IS 选中 trial 在 OOS 候选集上的相对秩 logit（与 quantjourney pbo 对齐）。"""
    vals = np.asarray(list(candidate_values), dtype=np.float64)
    vals = vals[np.isfinite(vals)]
    if vals.size < 2 or not np.isfinite(selected_value):
        return None
    rank = (
        float((vals < selected_value).sum())
        + (float((vals == selected_value).sum()) + 1.0) / 2.0
    )
    omega = rank / (vals.size + 1.0)
    if not (0.0 < omega < 1.0):
        return None
    return float(math.log(omega / (1.0 - omega)))


def walk_forward_top_k_rank_failure_rate(logits: Sequence[float]) -> float:
    """滚动 top-K 秩稳定性失败率：logit ≤ 0 的折占比（非 CSCV PBO）。"""
    finite = [
        float(logit) for logit in logits if logit is not None and np.isfinite(logit)
    ]
    if not finite:
        return float("nan")
    return sum(1.0 for logit in finite if logit <= 0.0) / len(finite)


def probability_of_backtest_overfitting(
    is_sharpes, oos_sharpes, *, max_combinations=10_000, seed=42,
) -> float:
    """弃用：折级 Sharpe 无法给出 CSCV PBO，返回 nan（与 quantjourney 一致）。"""
    import warnings as _warnings

    _warnings.warn(
        "probability_of_backtest_overfitting(is_sharpes, oos_sharpes) is "
        "deprecated: fold-level Sharpes cannot yield the CSCV PBO. It now "
        "returns nan. Enable WalkForwardConfig.rank_stability_trials for "
        "the rolling top-K rank-failure diagnostic.",
        DeprecationWarning,
        stacklevel=2,
    )
    return float("nan")


def is_oos_transfer_distribution(
    is_sharpes, oos_sharpes, *, max_combinations=10_000, seed=42,
) -> np.ndarray:
    """折级 IS→OOS 转移比分布（诊断用，非 CSCV PBO 对数分布）。"""
    n = len(is_sharpes)
    if n < 4:
        return np.array([], dtype=np.float64)
    is_arr = np.asarray(is_sharpes, dtype=np.float64)
    oos_arr = np.asarray(oos_sharpes, dtype=np.float64)
    half = n // 2
    total_combos = math.comb(n, half)
    rng = np.random.default_rng(seed)
    if total_combos <= max_combinations:
        splits = list(itertools.combinations(range(n), half))
    else:
        import itertools as _itertools

        splits = _itertools.islice(
            _itertools.combinations(range(n), half),
            max_combinations,
        )
    logits = np.empty(len(splits), dtype=np.float64)
    for idx, j_indices in enumerate(splits):
        j_set = set(j_indices)
        j_bar = [i for i in range(n) if i not in j_set]
        is_mean = float(is_arr[list(j_set)].mean())
        oos_mean = float(oos_arr[j_bar].mean())
        if is_mean > 0:
            logits[idx] = oos_mean / is_mean
        elif is_mean == 0:
            logits[idx] = 0.0 if oos_mean <= 0 else 1.0
        else:
            logits[idx] = 1.0 if oos_mean >= 0 else 0.0
    return logits


# ── 契约摘要（写入服务 run 输出，Best-effort，未提供 trial 级 OOS 时标 None）──

def walkforward_contract_summary(folds: list, cfg) -> dict:
    """基于现有每折记录计算契约摘要：overfit/degradation + 指纹；
    DSR/top-K 秩稳定性需 trial 级 OOS 数据，当前每折记录不提供，故标 None。"""
    import hashlib

    is_scores = [f.get("is_score") for f in folds]
    oos_objectives = [f.get("oos_objective") for f in folds]
    s = {
        "n_folds": len(folds),
        "fingerprint": _fingerprint(cfg, folds),
        "overfit_ratio": (
            aggregate_overfit_ratio(is_scores, oos_objectives)
            if is_scores and oos_objectives
            and all(s is not None for s in is_scores + oos_objectives)
            else None
        ),
        "degradation": (
            _aggregate_degradation(
                [f.get("oos_objective", 0.0) for f in folds]
                if folds
                else []
            )
            if folds
            else None
        ),
        "dsr": None,  # 需 trial 级 OOS Sharpe（当前每折记录未提供）
        "rank_stability_failure_rate": None,  # 需每折 top-K OOS 秩（未提供）
        "notes": (
            "dsr / rank_stability_failure_rate 需要 trial 级 OOS 评估; "
            "当前每折记录仅含 best_params/is_score/oos_objective/oos_stats"
        ),
    }
    return s


def _aggregate_degradation(oos_objectives: list) -> float:
    """平均 OOS 相对 IS 的退化（方向依赖：目标越小越优的退化方向见 aggregate_oos）。"""
    if not oos_objectives:
        return 0.0
    mean_oos = float(np.mean(oos_objectives))
    return float(mean_oos)


def _fingerprint(cfg, folds) -> str:
    raw = cfg.to_dict()
    raw["folds"] = sorted(
        (f.get("fold_id") if isinstance(f, dict) else f) for f in folds
    )
    blob = json.dumps(raw, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


# 需要 json（契约摘要指纹用），在模块顶部补引用
import json  # noqa: E402
