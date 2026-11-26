"""启动期内存护栏: A 股 warmup 只扫 A 股; quote 不在 warmup 期间重复全量算。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl

import app.tickflow.repository as repository_mod
import app.services.screener as screener_mod
from app.services import quote_service
from app.services.screener import ScreenerService
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 11, 25)
COLS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount",
        "quote_ts", "raw_close", "raw_high", "raw_low"]


def _frame(symbols: list[str]) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": symbols,
        "date": [DAY] * len(symbols),
        "open": [10.0] * len(symbols),
        "high": [11.0] * len(symbols),
        "low": [9.0] * len(symbols),
        "close": [10.5] * len(symbols),
        "volume": [1000.0] * len(symbols),
        "amount": [10500.0] * len(symbols),
        "quote_ts": [None] * len(symbols),
        "raw_close": [10.5] * len(symbols),
        "raw_high": [11.0] * len(symbols),
        "raw_low": [9.0] * len(symbols),
    })


def test_warmup_scan_filters_cn_before_collect(tmp_path, monkeypatch):
    """warmup 只把 A 股历史收进内存, 港美标的在 scan 阶段就被裁掉。"""
    root = tmp_path / "kline_daily_enriched" / f"date={DAY}"
    root.mkdir(parents=True)
    _frame(["600000.SH", "000001.SZ", "AJG.US", "0700.HK"]).write_parquet(root / "part.parquet")

    repo = KlineRepository(DataStore(tmp_path))
    repo._instruments_cache = pl.DataFrame()
    repo._latest_enriched_date_duckdb = lambda: DAY
    repo.get_enriched_generation_snapshot = lambda _: SimpleNamespace(generation="g1")
    repo.get_matrix_data_generation = lambda _: "g1"
    repo._live_agg_baseline_date = lambda d: d
    repo._build_live_agg = lambda *_a, **_k: None
    repo._restore_missing_latest_rows = lambda _day, today, _full: today

    monkeypatch.setattr(
        "app.indicators.pipeline.compute_enriched_history_window",
        lambda df, *_a, **_k: df,
    )
    captured: list[pl.LazyFrame] = []
    original = repository_mod.guarded_collect

    def collect(lf, *args, **kwargs):
        captured.append(lf)
        return original(lf, *args, **kwargs)

    monkeypatch.setattr(repository_mod, "guarded_collect", collect)

    repo._refresh_enriched_impl()

    assert captured
    assert set(captured[0].collect()["symbol"]) == {"600000.SH", "000001.SZ"}
    assert repo._enriched_history_cache is not None
    assert repo._enriched_history_cache.height == 2


def test_quote_full_calc_skipped_while_warmup_runs(monkeypatch):
    """warmup 正在重建 live_agg 时, 实时行情不再并发跑一遍全量计算。"""
    monkeypatch.setattr(quote_service, "cn_today", lambda: DAY)
    called: list[int] = []
    monkeypatch.setattr(
        "app.indicators.pipeline.compute_enriched",
        lambda *_a, **_k: called.append(1) or pl.DataFrame(),
    )
    service = quote_service.QuoteService()
    service.set_repo(SimpleNamespace(
        _enriched_warming=True,
        get_live_agg=lambda: pl.DataFrame(),
        get_enriched_latest=lambda: (pl.DataFrame(), None),
        get_enriched_latest_asset=lambda _: (pl.DataFrame(), None),
    ))

    service._flush_live_enriched(_frame(["600000.SH"]), asset_type="stock")

    assert called == []


def _screener_with_mixed_parquet(tmp_path, market="hk"):
    day_dir = tmp_path / "kline_daily_enriched" / f"date={DAY}"
    day_dir.mkdir(parents=True)
    _frame(["600000.SH", "0700.HK", "AJG.US", "9988.HK"]).write_parquet(day_dir / "part.parquet")
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_instruments_market=lambda _m: pl.DataFrame(),
        get_instruments_asset=lambda: pl.DataFrame(),
        get_historical_shares=lambda: None,
    )
    svc = ScreenerService(repo, market=market)
    svc._assert_integrity = lambda *_a, **_k: None
    return svc


def _capture_guarded_collect(monkeypatch):
    captured: list[pl.LazyFrame] = []
    original = screener_mod.guarded_collect

    def collect(lf, *args, **kwargs):
        captured.append(lf)
        return original(lf, *args, **kwargs)

    monkeypatch.setattr(screener_mod, "guarded_collect", collect)
    monkeypatch.setattr("app.indicators.pipeline.compute_indicators", lambda df: df)
    monkeypatch.setattr("app.indicators.pipeline.compute_signals", lambda df: df)
    return captured


def test_screener_single_day_market_filter_pushed_down(tmp_path, monkeypatch):
    """港美股单日总览不再把 150 天全市场宽帧收进内存。"""
    svc = _screener_with_mixed_parquet(tmp_path)
    captured = _capture_guarded_collect(monkeypatch)

    svc._compute_enriched_full(_frame(["0700.HK"]), DAY)

    assert captured
    assert set(captured[0].collect()["symbol"]) == {"0700.HK", "9988.HK"}


def test_screener_history_window_market_filter_pushed_down(tmp_path, monkeypatch):
    """历史窗口慢路径同样在 scan 阶段裁掉其它市场。"""
    screener_mod._history_cache.clear()
    svc = _screener_with_mixed_parquet(tmp_path)
    captured = _capture_guarded_collect(monkeypatch)

    svc._load_enriched_history_raw(DAY, 20)

    assert captured
    assert set(captured[0].collect()["symbol"]) == {"0700.HK", "9988.HK"}
