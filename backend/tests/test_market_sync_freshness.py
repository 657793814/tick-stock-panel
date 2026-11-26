"""港美股同步的「落地日期缺口」告警。

曾出现同步任务报成功(写入行数正常),但数据源当日个股K线尚未发布,落地停在
上一交易日,自选据实展示旧日期 —— 用户误以为同步失效。本测试钉住缺口检测:
把「实际落地最新交易日 vs 请求区间终点」的工作日差写进 result/日志。
"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl

from app.jobs import daily_pipeline
from app.jobs.daily_pipeline import _workday_gap, run_market_sync
from app.services import kline_sync


def test_workday_gap_ignores_weekends() -> None:
    """周五→周一算 1 个工作日(缺周一),周五→周日算 0(仅周末)。"""
    assert _workday_gap(date(2026, 11, 20), date(2026, 11, 23)) == 1
    assert _workday_gap(date(2026, 11, 20), date(2026, 11, 22)) == 0
    assert _workday_gap(date(2026, 11, 18), date(2026, 11, 23)) == 3
    assert _workday_gap(date(2026, 11, 23), date(2026, 11, 23)) == 0
    assert _workday_gap(date(2026, 11, 24), date(2026, 11, 23)) == 0


def test_run_market_sync_reports_freshness_gap(monkeypatch, tmp_path) -> None:
    """落地日期落后终点时:result 带 freshness,进度里出现告警文案。"""
    stale = date(2026, 11, 20)
    monkeypatch.setattr(daily_pipeline, "_load_synced_symbols", lambda *a: ["00001.HK"])
    monkeypatch.setattr(daily_pipeline, "_save_synced_symbols", lambda *a: None)
    monkeypatch.setattr(daily_pipeline, "run_pipeline", lambda **kw: 1)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path, _register_views=lambda: None),
        get_instruments_market=lambda market: pl.DataFrame({"symbol": ["00001.HK"]}),
        append_daily=lambda df: None,
        refresh_cache=lambda: None,
    )

    def fake_batch(universe, **kwargs):
        return pl.DataFrame({"symbol": ["00001.HK"], "date": [stale], "close": [10.0]})

    monkeypatch.setattr(kline_sync, "sync_daily_batch", fake_batch)

    messages: list[str] = []

    def emit(stage, pct, msg, **kwargs):
        messages.append(msg)

    result = run_market_sync(
        "hk", repo, on_progress=emit, max_symbols=None,
        refresh_synced=True, sync_indices=False,
    )

    freshness = result["freshness"]
    assert freshness["fetched_latest"] == stale.isoformat()
    assert freshness["requested_end"] == date.today().isoformat()
    assert freshness["gap_weekdays"] == _workday_gap(stale, date.today())
    if freshness["gap_weekdays"] > 0:
        assert any("落后请求区间终点" in m for m in messages)
