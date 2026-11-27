"""XTick 插件与服务层定向测试。

不访问真实上游: client/provider 均使用替身, 覆盖归一化、缓存、降级和安全边界。
"""
from __future__ import annotations

import gzip
import json
from types import SimpleNamespace

import pytest

from app import cache_store
from app.data_providers import custom as custom_sources
from app.data_providers.base import ProviderRequestError
from app.market_time import cn_today
from app.plugins.xtick import client as xtick_client
from app.plugins.xtick.client import XTickError
from app.plugins.xtick.provider import XTickProvider, _symbol
from app.services import xtick as xtick_service


@pytest.fixture
def provider(monkeypatch):
    instance = XTickProvider()
    monkeypatch.setattr(instance, "raw", lambda kind, params=None: [])
    return instance


class FakeRaw:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, kind, params=None):
        self.calls.append((kind, dict(params or {})))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def test_symbol_mapping_respects_asset_type():
    assert _symbol("000001") == "000001.SZ"
    assert _symbol("600519") == "600519.SH"
    assert _symbol("830001") == "830001.BJ"
    assert _symbol("000001", "index") == "000001.SH"
    assert _symbol("399001", "index") == "399001.SZ"
    assert _symbol("000001.SZ") == "000001.SZ"
    assert _symbol("abc123") is None
    assert _symbol("000001", "unknown") == "000001.SZ"


def test_realtime_quant_payload_normalized(provider, monkeypatch):
    payload = {
        "data": {
            "code": ["000001", "BAD"],
            "x001": [12.45, 1.0], "x002": [12.23, 1.0], "x003": [12.45, 1.0],
            "x004": [12.47, 1.0], "x005": [12.13, 1.0], "x006": [2122408, 1],
            "x007": [2605534976, 1], "x008": [-0.22, 0],
        },
    }
    fake = FakeRaw(payload)
    monkeypatch.setattr(provider, "raw", fake)
    rows = provider.get_realtime()
    assert fake.calls[0][0] == "quant_realtime"
    assert "x001" in fake.calls[0][1]["field"]
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "000001.SZ"
    assert row["volume"] == 2122408  # XTick 已是手, 不得再 /100
    assert row["amount"] == 2605534976
    assert row["change_pct"] == pytest.approx(-0.22 / 12.45)
    assert row["turnover_rate"] is None


def test_realtime_structure_change_warns(provider, monkeypatch):
    monkeypatch.setattr(provider, "raw", FakeRaw({"unexpected": 1}))
    assert provider.get_realtime() == []


def test_depth_batch_maps_five_levels(provider, monkeypatch):
    row = {
        "code": "000001", "time": 1795764600001,
        "bp1": 12.23, "bp2": 12.22, "bp3": 12.21, "bp4": 12.2, "bp5": 12.19,
        "bv1": 1446, "bv2": 877, "bv3": 507, "bv4": 2485, "bv5": 1512,
        "sp1": 12.24, "sp2": 12.25, "sp3": 12.26, "sp4": 12.27, "sp5": 12.28,
        "sv1": 1451, "sv2": 5328, "sv3": 2522, "sv4": 1249, "sv5": 859,
    }
    fake = FakeRaw([row])
    monkeypatch.setattr(provider, "raw", fake)
    depth = provider.get_depth_batch(["000001.SZ"])
    assert depth["000001.SZ"]["bid_prices"] == [12.23, 12.22, 12.21, 12.2, 12.19]
    assert depth["000001.SZ"]["ask_volumes"][0] == 1451
    assert depth["000001.SZ"]["timestamp"] == 1795764600001
    assert "all" not in fake.calls[0][1]["code"] or fake.calls[0][1]["code"] != "all"


def test_depth_batch_fails_closed(provider, monkeypatch):
    monkeypatch.setattr(provider, "raw", FakeRaw(XTickError(500, "HTTP 请求失败")))
    with pytest.raises(ProviderRequestError):
        provider.get_depth_batch(["000001"])


def test_vendor_error_envelope_and_token_redaction():
    from xtick.scripts.util.XTickUtil import process_data

    dummy_token = "0123456789abcdef0123456789abcdef"
    payload = {"code": -1, "message": f"bad token {dummy_token}", "data": None}
    with pytest.raises(XTickError) as exc:
        process_data(json.dumps(payload).encode(), dummy_token)
    assert dummy_token not in str(exc.value)
    assert "[已隐藏]" in str(exc.value)


def test_vendor_gzip_payload_decoded():
    from xtick.scripts.util.XTickUtil import process_data

    payload = [{"code": "000001"}]
    assert process_data(gzip.compress(json.dumps(payload).encode())) == payload


def test_unknown_kind_fails_closed():
    result = xtick_service.get_data("not_supported", {})
    assert result["state"] == "invalid_request"
    assert result["data"] is None


def test_missing_required_param_fails_closed(monkeypatch):
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: XTickProvider())
    result = xtick_service.get_data("kline", {"code": "000001"})
    assert result["state"] == "invalid_request"
    assert "startDate" in result["message"]


def test_service_cache_hit_and_ttl_tiers(monkeypatch):
    payload = [{"code": "000001"}]
    fake = FakeRaw(payload)
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: SimpleNamespace(raw=fake))
    store: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(cache_store, "get_json", lambda ns, key: store.get((ns, key)))
    monkeypatch.setattr(cache_store, "set_json", lambda ns, key, value, ttl: store.__setitem__((ns, key), value))

    today = cn_today().isoformat()
    live = xtick_service.get_data("board", {"tradeDate": today, "type": "1", "flag": "1"})
    assert live["state"] == "ok" and live["cached"] is False
    assert live["cache_ttl_seconds"] == xtick_service.TODAY_TTL_SECONDS
    hit = xtick_service.get_data("board", {"tradeDate": today, "type": "1", "flag": "1"})
    assert hit["cached"] is True and len(fake.calls) == 1

    historical = xtick_service.get_data(
        "lhb", {"tradeDate": "2026-01-02"},
    )
    assert historical["cache_ttl_seconds"] == xtick_service.CACHE_TTL_SECONDS


def test_empty_result_not_cached(monkeypatch):
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: SimpleNamespace(raw=FakeRaw([])))
    store: dict[tuple[str, str], dict] = {}
    monkeypatch.setattr(cache_store, "get_json", lambda ns, key: store.get((ns, key)))
    monkeypatch.setattr(cache_store, "set_json", lambda ns, key, value, ttl: store.__setitem__((ns, key), value))
    result = xtick_service.get_data("emotion", {"type": "1", "tradeDate": "2026-01-02"})
    assert result["state"] == "no_data"
    assert store == {}


def test_upstream_error_is_isolated(monkeypatch):
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(
        custom_sources, "get_provider",
        lambda name: SimpleNamespace(raw=FakeRaw(XTickError(429, "HTTP 请求失败"))),
    )
    result = xtick_service.get_data("board", {"tradeDate": "2026-11-27"})
    assert result["state"] == "upstream_error"
    assert result["data"] is None


def test_routes_registered_via_backend_extension():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/api/xtick/status" in paths
    assert "/api/xtick/data/{kind}" in paths


def test_client_rejects_unknown_kind():
    client = xtick_client.XTickClient(api_key="dummy")
    with pytest.raises(XTickError) as exc:
        client.raw("not_a_real_endpoint")
    assert exc.value.code == -3
    client.close()
