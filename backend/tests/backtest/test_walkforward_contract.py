"""Walk-forward 契约对齐测试 — quantjourney-bt 契约面（scheme/purge/DSR/top-K/统计）。

被测（app/backtest/walkforward.py 追加的 quantjourney 契约对齐段）:
- Fold / QJFold 数据契约 + compute_pre_oos_purge / compute_purge_embargo（pre-OOS purge）
- fold_scheme_factory + Rolling/Expanding/Anchored/CPCVFoldScheme（scheme 生成）
- 标准正态 CDF/PPF、DSR/PSR、_expected_max_sr、deflated_sharpe
- top-K 秩稳定性：selected_trial_rank_logit / walk_forward_top_k_rank_failure_rate
- overfit/efficiency/sharpe_decay / aggregate_overfit_ratio / aggregate_efficiency
- 绿黄红解读：MetricVerdict / interpret_metrics
- 聚合：aggregate_oos_returns / compute_composite_metrics / bootstrap_sharpe_ci
- 契约摘要：walkforward_contract_summary（best-effort，未提供 trial 级 OOS 时标 None）

全部自包含（无 scipy），仅依赖项目既有 numpy/pandas。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app.backtest.walkforward import (
    AnchoredFoldScheme,
    CPCVFoldScheme,
    ExpandingFoldScheme,
    compute_composite_metrics,
    compute_pre_oos_purge,
    compute_purge_embargo,
    fold_scheme_factory,
    interpret_metrics,
    QJFold,
    RollingFoldScheme,
    selected_trial_rank_logit,
    walk_forward_top_k_rank_failure_rate,
)


# ── pre-OOS purge ─────────────────────────────────────────────────────────

def test_purge_no_purge_returns_none():
    # 无 purge: 返回 (有效IS天数, None, None)
    eff, ps, pdays = compute_pre_oos_purge(
        is_start="2024-01-01",
        is_dates=pd.date_range("2024-01-01", periods=10, freq="D"),
        oos_start="2024-06-01",
        purge_days=0,
        extra_pre_oos_purge_pct=None,
        trading_dates=pd.date_range("2024-01-01", periods=60, freq="D"),
        max_holding_period_days=None,
    )
    assert ps is None and pdays is None
    assert eff == 10
    assert eff >= 9


def test_purge_with_pct_and_holding():
    import app.backtest.walkforward as wf

    eff, ps, pdays = wf.compute_pre_oos_purge(
        is_start="2024-01-01",
        is_dates=pd.date_range("2024-01-01", periods=10, freq="D"),
        oos_start="2024-06-01",
        purge_days=3,
        extra_pre_oos_purge_pct=0.1,
        trading_dates=pd.date_range("2024-01-01", periods=60, freq="D"),
        max_holding_period_days=5,
    )
    # effective_purge = max(3, 5) = 5, extension = int(0.1*10)=1 -> total 6
    assert eff == 10 - 6 - 1


def test_purge_embargo_deprecated_alias():
    import app.backtest.walkforward as wf

    with pytest.warns(DeprecationWarning):
        eff, ps, pdays = wf.compute_purge_embargo(
            is_start="2024-01-01",
            is_dates=pd.date_range("2024-01-01", periods=10, freq="D"),
            oos_start="2024-06-01",
            purge_days=2,
            embargo_pct=0.2,
            trading_dates=pd.date_range("2024-01-01", periods=60, freq="D"),
        )
    assert ps is not None


# ── fold schemes ──────────────────────────────────────────────────────────

def _mk_config(scheme="rolling", **kw):
    import app.backtest.walkforward as wf

    base = dict(
        strategy_id="s",
        symbols=None,
        start=pd.Timestamp("2024-01-01"),
        end=pd.Timestamp("2024-12-31"),
        param_grid={},
        scheme=scheme, train_months=1, test_months=1, min_train_months=0,
        step_months=1, purge_days=0, extra_pre_oos_purge_pct=0.0,
        max_holding_period_days=None,
    )
    base.update(kw)
    return wf.WalkForwardConfig(**base)


def _dates():
    return pd.date_range("2024-01-01", periods=120, freq="D")


def test_scheme_factory_rolling():
    cfg = _mk_config(scheme="rolling")
    scheme = wf_fold_scheme_factory(cfg) if False else _make_fold_scheme(cfg)
    folds = scheme.generate_folds(pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"), _dates())
    assert len(folds) > 0
    f0 = folds[0]
    assert f0.fold_id == 0
    assert f0.scheme == "rolling"
    assert f0.train_days > 0 and f0.oos_days > 0
    assert f0.effective_is_days >= 0


def test_scheme_factory_invalid_scheme_raises():
    import app.backtest.walkforward as wf

    with pytest.raises(ValueError):
        wf.fold_scheme_factory(_mk_config(scheme="bogus"))


def test_scheme_factory_cpcv_not_implemented():
    import app.backtest.walkforward as wf

    cfg = _mk_config(scheme="cpcv")
    scheme = wf.fold_scheme_factory(cfg)
    with pytest.raises(NotImplementedError):
        scheme.generate_folds(pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"), _dates())


def test_expanding_anchored_schemes_generate():
    for scheme_name in ("expanding", "anchored"):
        cfg = _mk_config(scheme=scheme_name)
        scheme = _make_fold_scheme(cfg)
        folds = scheme.generate_folds(pd.Timestamp("2024-01-01"), pd.Timestamp("2024-12-31"), _dates())
        assert len(folds) > 0
        assert folds[0].scheme == scheme_name


# ── DSR / PSR ─────────────────────────────────────────────────────────────

def test_norm_cdf_known_values():
    import app.backtest.walkforward as wf

    assert abs(wf._norm_cdf(0.0) - 0.5) < 1e-12
    assert abs(wf._norm_cdf(1.959963984540054) - 0.975) < 1e-9
    assert abs(wf._norm_cdf(-1.959963984540054) - 0.025) < 1e-9


def test_norm_ppf_known_values():
    import app.backtest.walkforward as wf

    assert abs(wf._norm_ppf(0.5) - 0.0) < 1e-12
    assert abs(wf._norm_ppf(0.975) - 1.959963984540054) < 1e-9
    assert abs(wf._norm_ppf(0.025) - (-1.959963984540054)) < 1e-9
    assert abs(wf._norm_ppf(0.99) - 2.326347874040849) < 1e-8
    assert abs(wf._norm_ppf(0.999) - 3.090232306167813) < 1e-8


def test_expected_max_sr_scales_with_std():
    import app.backtest.walkforward as wf

    sr0 = wf._expected_max_sr(1.0, n_trials=16)
    assert math.isfinite(sr0) and sr0 > 0
    assert abs(wf._expected_max_sr(2.0, 16) - 2.0 * sr0) < 1e-9


def test_deflated_sharpe_bounds():
    import app.backtest.walkforward as wf

    # 强显著收益: DSR 应接近 1
    d = wf.deflated_sharpe([0.5, 0.6, 0.7], n_trials=3, effective_n_trials=3,
                           observed_sr=0.7, n_obs=252, skewness=0.0, kurtosis=3.0,
                           benchmark_sr=0.0)
    assert 0.0 < d <= 1.0
    # 极低显著收益: DSR 应低于 0.8
    d2 = wf.deflated_sharpe([0.01, 0.02, 0.03], n_trials=3, effective_n_trials=3,
                            observed_sr=0.03, n_obs=252, benchmark_sr=0.0)
    assert d2 < 0.8


def test_deflated_sharpe_degenerate_falls_back_to_psr():
    import app.backtest.walkforward as wf

    # 单 trial 无选择偏差: 退化到 vs benchmark 的 PSR
    d = wf.deflated_sharpe([0.5], n_trials=1, effective_n_trials=1,
                           observed_sr=0.5, n_obs=252, benchmark_sr=0.2)
    psr = wf.probabilistic_sharpe(0.5, benchmark_sr=0.2, n_obs=252, skewness=0.0, kurtosis=3.0)
    assert abs(d - psr) < 1e-9


# ── top-K 秩稳定性 ────────────────────────────────────────────────────────

def test_selected_trial_rank_logit_best_wins():
    import app.backtest.walkforward as wf

    # 选中是唯一最好 -> rank 近 best -> omega 大 -> logit > 0 (top half)
    logit = wf.selected_trial_rank_logit(0.9, [0.1, 0.5, 0.9])
    assert logit is not None and logit > 0


def test_selected_trial_rank_logit_worst_loses():
    import app.backtest.walkforward as wf

    # 选中是最差 -> rank 1 -> omega 小 -> logit < 0 (bottom half)
    logit = wf.selected_trial_rank_logit(0.1, [0.5, 0.9, 0.95])
    assert logit is not None and logit < 0


def test_selected_trial_rank_logit_uncomputable():
    import app.backtest.walkforward as wf

    assert wf.selected_trial_rank_logit(0.5, []) is None


def test_top_k_rank_failure_rate():
    import app.backtest.walkforward as wf

    rate = wf.walk_forward_top_k_rank_failure_rate([-1.0, 0.0, 1.0, 2.0])
    assert 0.0 <= rate <= 1.0
    assert rate == 0.5  # 1/2 logit <= 0 ([-1.0, 0.0])


# ── overfit / efficiency / decay / 解读 ───────────────────────────────────

def test_overfit_ratio():
    import app.backtest.walkforward as wf

    assert wf.overfit_ratio(3.0, 1.5) == 2.0
    assert wf.overfit_ratio(3.0, 0.5) == 6.0  # OOS 正 -> IS/OOS 比
    assert wf.overfit_ratio(3.0, 0.0) == float("inf")  # OOS <= 0 且 IS>0 -> 无意义 inf
    assert wf.overfit_ratio(0.5, 0.0) == float("inf")  # IS>0 且 OOS<=0 -> inf (非 interpretable)


def test_sharpe_decay_negative_slope():
    import app.backtest.walkforward as wf

    assert abs(wf.sharpe_decay([-2.0, -1.5, -1.0, -0.5]) - 0.5) < 1e-9
    assert wf.sharpe_decay([]) == 0.0


def test_aggregate_overfit_and_efficiency():
    import app.backtest.walkforward as wf

    r = wf.aggregate_overfit_ratio([3.0, 3.0], [1.5, 0.5])
    assert abs(r - wf.overfit_ratio(3.0, 1.0)) < 1e-12
    eff = wf.aggregate_efficiency([1.0], [0.8])
    assert abs(eff - 0.8) < 1e-12


def test_interpret_metrics_green_yellow_red():
    import app.backtest.walkforward as wf

    m = {"overfit_ratio": 2.0, "efficiency": 0.9, "deflated_sharpe": 0.99,
         "breakeven_bps": 30, "n_folds": 6}
    verdicts = wf.interpret_metrics(m, oos_sharpe=1.0)
    by_name = {v.name: v for v in verdicts}
    assert by_name["overfit_ratio"].signal == "yellow"
    assert by_name["efficiency"].signal == "green"
    assert by_name["deflated_sharpe"].signal == "green"

    # OOS <= 0 时 overfit 被门控为红
    verdicts2 = wf.interpret_metrics({"overfit_ratio": 2.0, "deflated_sharpe": 0.9}, oos_sharpe=-0.5)
    assert verdicts2[0].signal == "red"


# ── 聚合 OOS / 组合指标 / bootstrap ───────────────────────────────────────

def test_aggregate_oos_returns_compounds():
    import app.backtest.walkforward as wf

    s1 = pd.Series({"2024-05-01": 0.01, "2024-05-02": 0.01})
    s2 = pd.Series({"2024-05-01": -0.01, "2024-05-02": 0.0})
    rets, nav = wf.aggregate_oos_returns([s1, s2])
    assert rets["2024-05-01"] == pytest.approx((0.01 + -0.01) / 2)
    assert len(nav) == 2


def test_composite_metrics_empty():
    import app.backtest.walkforward as wf

    assert wf.compute_composite_metrics(pd.Series(dtype=float)).keys() == {
        "sharpe", "cagr", "max_dd", "volatility"}


def test_bootstrap_ci_returns_pair_or_none():
    import app.backtest.walkforward as wf

    rng = pd.Series(np.random.default_rng(0).normal(0.0005, 0.01, 50))
    ci = wf.bootstrap_sharpe_ci(rng, n_resamples=50, seed=1, risk_free_rate=0.0, trading_days=252)
    assert ci is not None
    assert ci[0] <= ci[1]


def test_bootstrap_ci_short_returns_none():
    import app.backtest.walkforward as wf

    assert wf.bootstrap_sharpe_ci(pd.Series([0.01], dtype=float)) is None


# ── 契约摘要（best-effort）────────────────────────────────────────────────

def test_contract_summary_fingerprint_and_overfit():
    import app.backtest.walkforward as wf

    folds = [
        {"fold_id": 0, "is_score": 3.0, "oos_objective": 1.5, "test_end": None},
        {"fold_id": 1, "is_score": 3.0, "oos_objective": 0.5, "test_end": None},
    ]
    cfg = wf.WalkForwardConfig(strategy_id="s", symbols=None, start=None, end=None,
                               param_grid={}, objective="sortino", train_days=252,
                               test_days=63, step_days=63)
    s = wf.walkforward_contract_summary(folds, cfg)
    assert "fingerprint" in s and len(s["fingerprint"]) == 16
    assert s["overfit_ratio"] == pytest.approx(wf.aggregate_overfit_ratio([3.0, 3.0], [1.5, 0.5]))
    assert s["dsr"] is None  # 需 trial 级 OOS
    assert s["rank_stability_failure_rate"] is None
    assert "notes" in s


# ── helpers for scheme factory (avoid circular import noise) ──────────────

def _make_fold_scheme(cfg):
    import app.backtest.walkforward as wf
    return wf.fold_scheme_factory(cfg)


def wf_fold_scheme_factory(cfg):  # noqa: F811 (only used by placeholder guard above)
    import app.backtest.walkforward as wf
    return wf.fold_scheme_factory(cfg)
