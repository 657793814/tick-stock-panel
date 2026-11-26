"""日K取数路由: 只覆盖 A 股的插件源不得吞掉港美股标的。

扶摇/通达信等内置插件只提供 A 股日K。修补 2026-11-24 的 AJG.US 等美股时,
源返回 0 行曾被当成「源无数据」直接放弃, 导致这类标的永远无法修补。
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import kline_sync

CN, US = "600000.SH", "AJG.US"
CAPS = SimpleNamespace(has=lambda _cap: True, limits=lambda _cap: None)


def _daily(symbol: str) -> pl.DataFrame:
    return pl.DataFrame({
        "symbol": [symbol], "date": [datetime(2026, 11, 24).date()],
        "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.5],
        "volume": [100.0], "amount": [1000.0],
    })


def _route(monkeypatch, provider, *, has_daily: bool = True):
    from app.data_providers import custom
    monkeypatch.setattr(kline_sync.preferences, "get_daily_data_provider", lambda: "fuyao")
    monkeypatch.setattr(custom, "provider_has_dataset",
                        lambda name, dataset: has_daily and dataset == "daily")
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)


def _collect(symbols, asset_type="stock"):
    window = datetime(2026, 11, 24, tzinfo=CN_TZ)
    return list(kline_sync.iter_daily_frames(
        symbols, CAPS, start_time=window, end_time=window, asset_type=asset_type,
    ))


def test_us_symbols_fall_back_to_tickflow(monkeypatch):
    calls = []
    monkeypatch.setattr(kline_sync, "sync_daily_batch",
                        lambda symbols, **kw: calls.append(list(symbols)) or _daily(US))
    _route(monkeypatch, SimpleNamespace(iter_daily=lambda *a, **kw: iter(())))

    frames = _collect([US])

    assert calls == [[US]]
    assert pl.concat(frames)["symbol"].to_list() == [US]


def test_a_share_symbols_keep_the_configured_source(monkeypatch):
    calls = []
    monkeypatch.setattr(kline_sync, "sync_daily_batch",
                        lambda symbols, **kw: calls.append(list(symbols)) or _daily(CN))
    _route(monkeypatch, SimpleNamespace(iter_daily=lambda *a, **kw: iter(())))

    assert _collect([CN]) == []
    assert calls == []


def test_mixed_universe_only_sends_uncovered_symbols_to_tickflow(monkeypatch):
    calls = []
    monkeypatch.setattr(kline_sync, "sync_daily_batch",
                        lambda symbols, **kw: calls.append(list(symbols)) or _daily(US))
    _route(monkeypatch, SimpleNamespace(iter_daily=lambda *a, **kw: iter([_daily(CN)])))

    frames = _collect([CN, US])

    assert calls == [[US]]
    assert sorted(pl.concat(frames)["symbol"].to_list()) == [CN, US]


def test_non_stock_assets_keep_existing_fallback(monkeypatch):
    calls = []
    monkeypatch.setattr(kline_sync, "sync_daily_batch",
                        lambda symbols, **kw: calls.append(list(symbols)) or _daily("510300.SH"))
    _route(monkeypatch, SimpleNamespace(iter_daily=lambda *a, **kw: iter(())))

    frames = _collect(["510300.SH"], asset_type="etf")

    assert calls == [["510300.SH"]]
    assert len(frames) == 1


def test_provider_without_daily_dataset_still_requests_all_symbols(monkeypatch):
    """源完全不提供 daily 时交给 TickFlow 全量取, 不能只留下港美股。"""
    calls = []
    monkeypatch.setattr(kline_sync, "sync_daily_batch",
                        lambda symbols, **kw: calls.append(list(symbols)) or _daily(CN))
    _route(monkeypatch, SimpleNamespace(), has_daily=False)

    _collect([CN, US])

    assert calls == [[CN, US]]