"""回测入口的市场语义 — 标的池隔离、涨停类策略门控、市场参与任务 key。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import backtest as backtest_api
from app.backtest.strategy import StrategyBacktestConfig


def test_market_pool_keeps_same_market_symbols():
    assert backtest_api._market_pool(["600000.SH", "000001.SZ"], "cn") == [
        "600000.SH", "000001.SZ",
    ]
    assert backtest_api._market_pool(["00700.HK"], "hk") == ["00700.HK"]
    assert backtest_api._market_pool(["AAPL.US"], "us") == ["AAPL.US"]
    assert backtest_api._market_pool(None, "hk") is None


def test_market_pool_rejects_mixed_market_symbols():
    with pytest.raises(HTTPException) as excinfo:
        backtest_api._market_pool(["600000.SH", "00700.HK"], "cn")
    assert excinfo.value.status_code == 400
    assert "00700.HK" in str(excinfo.value.detail)


def test_market_pool_rejects_empty_pool():
    with pytest.raises(HTTPException) as excinfo:
        backtest_api._market_pool([], "us")
    assert excinfo.value.status_code == 400


def test_hk_us_requires_explicit_symbols():
    """港/美暂不支持全市场回测: 不指定标的时明确拒绝, 而不是混市场计算。"""
    for market in ("hk", "us"):
        with pytest.raises(HTTPException) as excinfo:
            backtest_api._guard_market_universe(None, market)
        assert excinfo.value.status_code == 400
        assert "标的池" in str(excinfo.value.detail)
    # A 股与显式标的照常放行
    backtest_api._guard_market_universe(None, "cn")
    backtest_api._guard_market_universe(["00700.HK"], "hk")


def _request_with_strategy(tags: list[str]):
    strategy = SimpleNamespace(meta={"tags": tags})
    engine = SimpleNamespace(get=lambda sid: strategy)
    state = SimpleNamespace(repo=None, strategy_engine=engine)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_limit_strategy_gated_on_hk_us_only():
    req = _request_with_strategy(["涨停"])
    # A 股放行
    backtest_api._guard_market_strategy(req, "s1", "cn")
    for market in ("hk", "us"):
        with pytest.raises(HTTPException) as excinfo:
            backtest_api._guard_market_strategy(req, "s1", market)
        assert excinfo.value.status_code == 400


def test_non_limit_strategy_allowed_on_hk_us():
    backtest_api._guard_market_strategy(_request_with_strategy(["动量"]), "s1", "hk")


def test_job_key_separates_markets():
    kwargs = dict(
        strategy_id="s1", symbols="00700.HK", start="2026-01-01", end="2026-06-01",
        matching="open_t+1", entry_fill=None, exit_fill=None, fees_pct=0.0002,
        slippage_bps=5.0, max_positions=10, max_exposure_pct=1.0,
        initial_capital=1_000_000.0, position_sizing="equal", params=None, overrides=None,
    )
    cn_key = backtest_api._make_job_key(**kwargs, market="cn")
    hk_key = backtest_api._make_job_key(**kwargs, market="hk")
    assert cn_key != hk_key, "同参数不同市场不能共用回测任务与结果缓存"


def test_strategy_config_market_defaults_to_cn():
    cfg = StrategyBacktestConfig(strategy_id="s1", symbols=None, start=None, end=None)
    assert cfg.market == "cn"
