"""共享存储下的市场隔离路由测试（方案 §5.2 / §10）。

核心不变量：所有面向单市场的读路径必须按 symbol 后缀过滤，
A 股结果不混入 .HK/.US，HK/US 结果不混入 A 股。
"""
from __future__ import annotations

import types
from datetime import date
from pathlib import Path

import polars as pl

from app.indicators.pipeline import compute_all
from app.services.screener import ScreenerService

AS_OF = date(2026, 11, 20)


def _panel() -> pl.DataFrame:
    """混合市场的最新日快照。"""
    return pl.DataFrame({
        "symbol": ["600519.SH", "000001.SZ", "00700.HK", "AAPL.US"],
        "date": [AS_OF] * 4,
        "open": [10.0] * 4,
        "high": [12.0] * 4,
        "low": [9.0] * 4,
        "close": [11.0] * 4,
        "volume": [1000.0] * 4,
        "amount": [11000.0] * 4,
    })


def _service(market: str, panel: pl.DataFrame | None = None) -> ScreenerService:
    svc = ScreenerService(types.SimpleNamespace(store=types.SimpleNamespace(data_dir="/nonexistent")), market=market)
    svc._load_enriched_for_date_raw = lambda _d: panel if panel is not None else pl.DataFrame()  # type: ignore[method-assign]
    return svc


def test_screener_service_cn_excludes_hk_us():
    data = _service("cn", _panel())._load_enriched_for_date(AS_OF)
    assert set(data["symbol"]) == {"600519.SH", "000001.SZ"}


def test_screener_service_hk_excludes_cn_and_us():
    data = _service("hk", _panel())._load_enriched_for_date(AS_OF)
    assert set(data["symbol"]) == {"00700.HK"}


def test_screener_service_us_excludes_cn_and_hk():
    data = _service("us", _panel())._load_enriched_for_date(AS_OF)
    assert set(data["symbol"]) == {"AAPL.US"}


def test_screener_history_cache_key_isolated_by_market():
    """缓存键含 market 维度：同 (asset, date, lookback) 两市场生成不同的键。"""
    from app.markets import market_cache_key

    assert market_cache_key("cn", "stock", str(AS_OF), "5") != market_cache_key(
        "hk", "stock", str(AS_OF), "5"
    )
    # 源码中 _history_cache 的键为 (asset_type, market, target_date, lookback)
    import inspect
    from app.services import screener as screener_mod

    src = inspect.getsource(screener_mod.ScreenerService._load_enriched_history_raw)
    assert "self.market" in src and "cache_key" in src


def test_compute_all_hk_us_always_has_limit_columns_without_instruments():
    """HK/US 即使没有 instruments，也必须产出 signal_limit_* 列（恒为假/0）。"""
    base = _panel().filter(pl.col("symbol") == "00700.HK")
    out = compute_all(base, instruments=None, market="hk")
    for col in ["signal_limit_up", "signal_limit_down",
                "signal_limit_down_recovery", "signal_broken_limit_up",
                "consecutive_limit_ups", "consecutive_limit_downs"]:
        assert col in out.columns, f"缺列 {col}"
        assert out[col].drop_nulls().len() == 0 or set(out[col].to_list()) <= {False, 0}


def test_compute_all_cn_invariants_when_no_instruments():
    """A 股无 instruments 时不补齐涨跌停列（保持改造前行为）。"""
    base = _panel().filter(pl.col("symbol") == "600519.SH")
    out = compute_all(base, instruments=None, market="cn")
    assert "signal_limit_up" not in out.columns


def test_screener_uses_market_instruments_for_hk(monkeypatch):
    """港美股维表必须取自 instruments_index_{market}，否则 name/float_shares 全空。"""
    hk_inst = pl.DataFrame({"symbol": ["00700.HK"], "name": ["腾讯控股"], "float_shares": [9.09e9]})
    cn_inst = pl.DataFrame({"symbol": ["600519.SH"], "name": ["贵州茅台"], "float_shares": [1.2e9]})
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir="/nonexistent"),
        get_instruments_market=lambda m="cn": hk_inst,
        get_instruments_asset=lambda a="stock": cn_inst,
    )
    assert ScreenerService(repo, market="hk")._market_instruments()["symbol"].to_list() == ["00700.HK"]
    assert ScreenerService(repo, market="cn")._market_instruments()["symbol"].to_list() == ["600519.SH"]


def test_screener_non_cn_bypasses_a_share_repo_cache():
    """repo 的 latest/history 缓存是 A 股口径，港美股不能读它。"""
    # 前序用例可能已把 (stock, hk, AS_OF) 的帧写进进程级缓存
    ScreenerService.clear_history_cache()
    calls: list[str] = []
    repo = types.SimpleNamespace(
        store=types.SimpleNamespace(data_dir=Path("/nonexistent")),
        get_enriched_latest_asset=lambda *a, **k: calls.append("latest") or (pl.DataFrame(), None),
        get_enriched_history=lambda *a, **k: calls.append("history") or pl.DataFrame(),
        get_instruments_market=lambda m="cn": pl.DataFrame(),
        get_instruments_asset=lambda a="stock": pl.DataFrame(),
        get_historical_shares=lambda: pl.DataFrame(),
    )
    svc = ScreenerService(repo, market="hk")
    assert svc._load_enriched_for_date(AS_OF).is_empty()
    assert calls == []
