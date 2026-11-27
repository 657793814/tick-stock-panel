"""XTick 路由数据集定向测试: daily/minute/full_minute/moneyflow 解析与 fail-closed。

行结构 fixture 依据官方 Java SDK bean (Minute: time=北京毫秒, volume=手, amount=元)
与本项目前端列定义构造; 上游未经真实 Token 实测, 解析器对未知结构一律 fail-closed。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.data_providers.base import ProviderRequestError
from app.market_time import cn_today
from app.plugins.xtick.provider import (
    XTickProvider,
    _kline_daily_row,
    _kline_minute_row,
    _moneyflow_row,
    _naive_of,
)

_BJ = timezone(timedelta(hours=8))


def _ms_of(day: datetime) -> int:
    return int(day.timestamp() * 1000)


def test_naive_of_accepts_ms_and_strings():
    dt = datetime(2026, 11, 27, 10, 30, tzinfo=_BJ)
    assert _naive_of(_ms_of(dt)) == datetime(2026, 11, 27, 10, 30)
    assert _naive_of("2026-11-27 10:30:00") == datetime(2026, 11, 27, 10, 30)
    assert _naive_of("2026-11-27") == datetime(2026, 11, 27)
    assert _naive_of(None) is None
    assert _naive_of("garbage") is None


def test_daily_row_field_names_and_units():
    row = _kline_daily_row("000001.SZ", {
        "time": "2026-11-27", "open": 12.4, "high": 12.5, "low": 12.1,
        "close": 12.23, "volume": 2122408, "amount": 2605535117,
    })
    assert row is not None
    assert row["date"].isoformat() == "2026-11-27"
    assert row["close"] == 12.23
    assert row["volume"] == 2122408.0   # 官方 bean 口径: 手
    assert row["amount"] == 2605535117.0


def test_daily_row_rejects_missing_time_or_close():
    assert _kline_daily_row("000001.SZ", {"open": 1, "close": 1}) is None
    assert _kline_daily_row("000001.SZ", {"time": "2026-11-27"}) is None
    assert _kline_daily_row("000001.SZ", "not-a-dict") is None


def test_minute_row_ms_timestamp_and_window():
    day = datetime(2026, 11, 27)
    inside = _ms_of(datetime(2026, 11, 27, 10, 30, tzinfo=_BJ))
    outside = _ms_of(datetime(2026, 11, 26, 10, 30, tzinfo=_BJ))
    row = _kline_minute_row("000001.SZ", {
        "time": inside, "code": "000001", "open": 12.2, "high": 12.3,
        "low": 12.1, "close": 12.23, "volume": 446, "amount": 5462858,
    }, day, day + timedelta(days=1))
    assert row is not None
    assert row["datetime"] == datetime(2026, 11, 27, 10, 30)
    assert row["volume"] == 446.0
    assert _kline_minute_row(
        "000001.SZ", {"time": outside, "close": 1}, day, day + timedelta(days=1),
    ) is None


def _routing_provider() -> XTickProvider:
    return XTickProvider()


def test_get_daily_via_kline(monkeypatch):
    provider = _routing_provider()
    captured: list[dict] = []

    def fake_raw(kind, params=None):
        captured.append(dict(params or {}))
        assert kind == "kline"
        return [
            {"time": "2026-11-27", "open": 12.4, "high": 12.5, "low": 12.1,
             "close": 12.23, "volume": 100, "amount": 123000.0},
            {"time": "bad-row"},
        ]

    monkeypatch.setattr(provider, "raw", fake_raw)
    frame = provider.get_daily(["000001.SZ"], datetime(2026, 11, 20), datetime(2026, 11, 27))
    assert frame.height == 1
    assert captured[0]["fq"] == 1            # 路由口径: 不复权
    assert captured[0]["period"] == "1d"
    assert captured[0]["startDate"] == "2026-11-20"


def test_get_minute_per_day_requests(monkeypatch):
    provider = _routing_provider()
    calls: list[dict] = []

    def fake_raw(kind, params=None):
        calls.append(dict(params or {}))
        assert kind == "kline" and params["period"] == "1m"
        day = params["startDate"]
        return [{
            "time": _ms_of(datetime.fromisoformat(f"{day} 10:30").replace(tzinfo=_BJ)),
            "code": "000001", "close": 12.23, "volume": 10, "amount": 122300.0,
            "open": 12.2, "high": 12.3, "low": 12.1,
        }]

    monkeypatch.setattr(provider, "raw", fake_raw)
    frame = provider.get_minute(
        ["000001.SZ"], datetime(2026, 11, 26), datetime(2026, 11, 27, 23, 59),
    )
    assert frame.height == 2                  # 两天各 1 根
    assert {c["startDate"] for c in calls} == {"2026-11-26", "2026-11-27"}


def test_intraday_batch_prefers_batch_endpoint(monkeypatch):
    provider = _routing_provider()
    today = cn_today()
    inside = _ms_of(datetime(today.year, today.month, today.day, 10, 30, tzinfo=_BJ))
    yesterday = _ms_of(datetime(today.year, today.month, today.day, 10, 30, tzinfo=_BJ) - timedelta(days=1))
    kinds: list[str] = []

    def fake_raw(kind, params=None):
        kinds.append(kind)
        assert kind == "realtime_minute"
        assert params["type"] == 1
        return [
            {"time": inside, "code": "000001", "close": 12.23, "volume": 10,
             "amount": 122300.0, "open": 12.2, "high": 12.3, "low": 12.1},
            {"time": yesterday, "code": "000001", "close": 12.0, "volume": 10,
             "amount": 120000.0, "open": 12.0, "high": 12.0, "low": 12.0},
            {"code": "999999", "close": 1},   # 无法识别代码 → 丢弃
        ]

    monkeypatch.setattr(provider, "raw", fake_raw)
    frame = provider.get_intraday_batch(["000001.SZ"])
    assert frame.height == 1                  # 只保留当日行
    assert frame["symbol"][0] == "000001.SZ"
    assert kinds == ["realtime_minute"]       # 批量成功时不触发回退


def test_intraday_batch_falls_back_when_unparseable(monkeypatch):
    provider = _routing_provider()
    today = cn_today()

    def fake_raw(kind, params=None):
        if kind == "realtime_minute":
            return [{"unexpected": "shape"}]  # 结构异常
        assert kind == "kline" and params["period"] == "1m"
        day = params["startDate"]
        return [{
            "time": _ms_of(datetime.fromisoformat(f"{day} 14:00").replace(tzinfo=_BJ)),
            "code": "000001", "close": 12.23, "volume": 10, "amount": 1,
            "open": 12.2, "high": 12.3, "low": 12.1,
        }]

    monkeypatch.setattr(provider, "raw", fake_raw)
    frame = provider.get_intraday_batch(["000001.SZ"])
    assert frame.height == 1                  # 回退单标的 1m K线
    assert frame["datetime"][0].date() == today


def test_moneyflow_maps_xtick_tiers_to_eastmoney_schema():
    row = _moneyflow_row("000001.SZ", {
        "date": "2026-11-27",
        "buymostamount": 100.0, "sellmostamount": 30.0,     # 超大单
        "buybigamount": 50.0, "sellbigamount": 10.0,        # 大单
        "buymediumamount": 20.0, "sellmediumamount": 20.0,  # 中单 → 丢弃
        "buysmallamount": 5.0, "sellsmallamount": 25.0,     # 小单
    })
    assert row is not None
    assert row["super_large_net"] == pytest.approx(70.0)
    assert row["large_net"] == pytest.approx(40.0)
    assert row["main_net"] == pytest.approx(110.0)          # 主力 = 超大 + 大
    assert row["small_net"] == pytest.approx(-20.0)
    assert row["main_rate"] is None and row["small_rate"] is None


def test_moneyflow_fail_closed_on_unknown_shape():
    """金额字段缺失 (结构变化) → 返回 None; 全部行不可解析 → provider 抛错。"""
    assert _moneyflow_row("000001.SZ", {"date": "2026-11-27", "foo": 1}) is None
    assert _moneyflow_row("000001.SZ", {"buymostamount": 1}) is None  # 无日期


def test_get_moneyflow_raises_when_all_rows_unparseable(monkeypatch):
    provider = _routing_provider()
    monkeypatch.setattr(provider, "raw", lambda kind, params=None: [{"weird": 1}])
    with pytest.raises(ProviderRequestError):
        provider.get_moneyflow(["000001.SZ"])


def test_get_moneyflow_empty_response_is_empty_frame(monkeypatch):
    provider = _routing_provider()
    monkeypatch.setattr(provider, "raw", lambda kind, params=None: [])
    frame = provider.get_moneyflow(["000001.SZ"])
    assert frame.is_empty()


def test_manifest_datasets_match_provider():
    """plugin.yaml 声明与 provider._DATASETS 一致, 能力矩阵候选才能如实展示。"""
    from pathlib import Path

    import yaml

    from app.plugins.xtick.provider import _DATASETS

    doc = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "app/plugins/xtick/plugin.yaml").read_text()
    )
    assert sorted(doc["datasets"]) == sorted(_DATASETS)
