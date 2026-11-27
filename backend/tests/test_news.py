"""新闻资讯: 归一化、Redis 缓存口径、降级与 API 参数校验。"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import news as news_api
from app.data_providers import custom as custom_sources
from app.plugins.fuyao.client import FuyaoError
from app.services import news as news_service


@pytest.fixture(autouse=True)
def _fake_cache(monkeypatch):
    store: dict[str, tuple[float, object]] = {}

    def get(namespace, key, *, now=None):
        item = store.get(f"{namespace}:{key}")
        return item[1] if item else None

    def set_(namespace, key, value, ttl, *, now=None):
        store[f"{namespace}:{key}"] = (ttl, value)

    monkeypatch.setattr(news_service.cache_store, "get_json", get)
    monkeypatch.setattr(news_service.cache_store, "set_json", set_)
    return store


@pytest.fixture
def provider(monkeypatch):
    class FakeProvider:
        def __init__(self):
            self.calls: list[tuple[str, dict]] = []
            self.result: dict = {"items": [{"id": "1", "title": "示例"}]}
            self.error: Exception | None = None

        def news(self, kind, params):
            self.calls.append((kind, params))
            if self.error:
                raise self.error
            return self.result

    fake = FakeProvider()
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: True)
    monkeypatch.setattr(custom_sources, "get_provider", lambda name: fake)
    return fake


# ---------------------------------------------------------------- 缓存口径
def test_cache_ttl_seven_days_for_immutable_and_short_for_first_pages():
    svc = news_service
    assert svc.cache_ttl("article_detail", {"encoded_seq": "x"}) == svc.CACHE_TTL_SECONDS
    assert svc.cache_ttl("notice_detail", {"seq": "x"}) == svc.CACHE_TTL_SECONDS
    assert svc.cache_ttl("flash_list", {"tag_id": 21103, "seq": "9"}) == svc.CACHE_TTL_SECONDS
    assert svc.cache_ttl("topic_hot_list", {"tab_id": 1, "cursor": "c"}) == svc.CACHE_TTL_SECONDS
    assert svc.cache_ttl("stock_timeline", {"thscode": "300033.SZ", "offset": "o"}) == svc.CACHE_TTL_SECONDS

    assert svc.cache_ttl("flash_list", {"tag_id": 21103, "seq": "0"}) == svc.FLASH_FIRST_TTL_SECONDS
    assert svc.cache_ttl("topic_hot_list", {"tab_id": 1}) == svc.FRESH_TTL_SECONDS
    assert svc.cache_ttl("stock_timeline", {"thscode": "300033.SZ"}) == svc.FRESH_TTL_SECONDS
    assert svc.cache_ttl("flash_refresh", {"tag_id": 1}) == 0

    today = date(2026, 11, 27)
    past = (today - timedelta(days=1)).isoformat()
    assert svc.cache_ttl("anomaly_analysis", {"end_date": past}, today) == svc.CACHE_TTL_SECONDS
    assert svc.cache_ttl("anomaly_analysis", {"end_date": today.isoformat()}, today) == svc.FRESH_TTL_SECONDS


def test_cache_key_covers_all_params_and_is_order_insensitive():
    a = news_service.cache_key("flash_list", {"tag_id": 1, "seq": "0"})
    b = news_service.cache_key("flash_list", {"seq": "0", "tag_id": 1})
    c = news_service.cache_key("flash_list", {"tag_id": 1, "seq": "1"})
    assert a == b and a != c
    assert "flash_list" in a


# ---------------------------------------------------------------- 归一化
def test_flash_normalization_preserves_string_ids_and_nulls():
    result = news_service.normalize("flash_list", {
        "items": [{
            "id": "9007199254740993", "seq": "9007199254740995", "title": "快讯",
            "encoded_url": "https://example.org/a", "summary": None,
            "tags": [{"id": 21103, "name": "全球"}],
            "type": 1000, "create_time_ms": 1_700_000_000_000,
            "update_time_ms": 1_700_000_000_000, "pic_url": None,
        }],
    })
    row = result["items"][0]
    assert row["id"] == "9007199254740993"  # 长整数不丢精度
    assert row["tags"] == [{"id": 21103, "name": "全球"}]
    assert row["summary"] is None and row["pic_url"] is None
    assert row["create_time_ms"] == 1_700_000_000_000


def test_anomaly_time_keeps_upstream_value_without_unit_guess():
    result = news_service.normalize("anomaly_analysis", {
        "items": [{
            "analysis_id": "a1", "analysis_content": "<p>解读</p>",
            "key_word_list": ["涨停"], "create_time": 1000,
            "last_anomaly_time": None, "first_publish_time": 1000,
        }],
        "was_truncated": True,
    })
    assert result["items"][0]["create_time"] == 1000
    assert result["items"][0]["last_anomaly_time"] is None
    assert result["was_truncated"] is True


def test_notice_and_detail_normalization_tolerates_missing_fields():
    article = news_service.normalize("article_detail", {"title": "标题", "author": None})
    assert article["content"] == "" and article["author"] is None
    assert article["is_kuaixun"] is None
    notice = news_service.normalize("notice_detail", {"seq": "n1", "summary": None})
    assert notice["summary"] is None
    assert notice["monthly_report_interpretation"] is None


# ---------------------------------------------------------------- 服务编排
def test_get_news_source_unavailable_when_fuyao_not_registered(monkeypatch, provider):
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: False)
    result = news_service.get_news("flash_list", {"tag_id": 21103, "seq": "0"})
    assert result["state"] == "source_unavailable"
    assert "数据源" in result["message"]
    assert provider.calls == []


def test_get_news_caches_seven_days_and_hits_second_call(provider, _fake_cache):
    params = {"tag_id": 21103, "seq": "9007199254740995"}
    first = news_service.get_news("flash_list", params)
    second = news_service.get_news("flash_list", params)

    assert first["state"] == "ok" and first["data"]["items"][0]["title"] == "示例"
    assert first["cache_ttl_seconds"] == news_service.CACHE_TTL_SECONDS
    assert second["cached"] is True
    assert len(provider.calls) == 1
    key = news_service.cache_key("flash_list", params)
    assert _fake_cache[f"news:{key}"][0] == news_service.CACHE_TTL_SECONDS


def test_upstream_rate_limit_is_not_cached_and_retries(provider):
    provider.error = FuyaoError("限流", code=4001)
    first = news_service.get_news("flash_list", {"tag_id": 21103, "seq": "0"})
    assert first["state"] == "rate_limited"
    provider.error = None
    second = news_service.get_news("flash_list", {"tag_id": 21103, "seq": "0"})
    assert second["state"] == "ok"
    assert len(provider.calls) == 2


def test_empty_anomaly_is_no_data_with_degradation_warning(provider):
    provider.result = {"items": [], "was_truncated": False}
    result = news_service.get_news("anomaly_analysis", {
        "begin_date": "2026-11-01", "end_date": "2026-11-26",
    })
    assert result["state"] == "no_data"
    assert "降级" in result["message"]
    assert result["data"]["items"] == []


def test_anomaly_multi_day_is_chunked_deduped_and_sorted(provider):
    original = provider.news

    def per_day(kind, params):
        original(kind, params)
        day = params["begin_date"]
        return {
            "items": [{"analysis_id": "shared", "create_time": 100},
                      {"analysis_id": day, "create_time": 2000 + int(day[-2:])}],
            "was_truncated": day == "2026-11-26",
        }

    provider.news = per_day
    result = news_service.get_news("anomaly_analysis", {
        "begin_date": "2026-11-25", "end_date": "2026-11-26",
    })
    assert result["state"] == "ok"
    assert [call[1]["begin_date"] for call in provider.calls] == ["2026-11-25", "2026-11-26"]
    assert all(call[1]["begin_date"] == call[1]["end_date"] for call in provider.calls)
    ids = [row["analysis_id"] for row in result["data"]["items"]]
    assert ids.count("shared") == 1  # 跨日重复幂等键只保留一条
    assert ids[0] == "2026-11-26"  # 按 create_time 倒序
    assert result["data"]["was_truncated"] is True


def test_anomaly_partial_day_failure_is_surfaced(provider):
    original = provider.news

    def per_day(kind, params):
        original(kind, params)
        if params["begin_date"] == "2026-11-25":
            raise FuyaoError("数据源不可用", code=5003)
        return {"items": [{"analysis_id": "26", "create_time": 2}], "was_truncated": False}

    provider.news = per_day
    result = news_service.get_news("anomaly_analysis", {
        "begin_date": "2026-11-25", "end_date": "2026-11-26",
    })
    assert result["state"] == "ok" and result["data"]["partial"] is True
    assert result["data"]["skipped_days"] == 1
    assert "部分日期" in result["message"]


def test_events_client_only_error_has_actionable_message(provider):
    provider.error = FuyaoError("客户端专用", code=2004)
    result = news_service.get_news("events_search", {"query": "浙江", "size": 1})
    assert result["state"] == "upstream_error"
    assert "客户端专用" in result["message"]


def test_error_result_keeps_envelope_shape(provider):
    provider.error = FuyaoError("客户端专用", code=2004)
    result = news_service.get_news("events_search", {"query": "浙江", "size": 1})
    assert result["data"] is None
    assert result["cache_ttl_seconds"] == news_service.FRESH_TTL_SECONDS


def test_unknown_kind_fails_closed(provider):
    result = news_service.get_news("unknown", {})
    assert result["state"] == "invalid_request"
    assert provider.calls == []


# ---------------------------------------------------------------- API
@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(news_api.router)
    calls: list[tuple[str, dict]] = []

    def fake_get(kind, params):
        calls.append((kind, params))
        return {"state": "ok", "kind": kind, "data": {}, "cached": False, "message": None}

    monkeypatch.setattr(news_api.news_service, "get_news", fake_get)
    return TestClient(app), calls


def test_api_forwards_params_with_exact_upstream_names(client):
    http, calls = client
    assert http.get("/api/news/status").status_code == 200

    responses = [
        http.get("/api/news/articles/detail", params={"client_type": "web", "encoded_seq": "abc"}),
        http.get("/api/news/events", params={"query": "浙江", "size": 3}),
        http.get("/api/news/flash/list", params={"tag_id": 21103, "seq": "0"}),
        http.get("/api/news/flash/refresh", params={"tag_id": 21103, "seq": "7", "create_time_ms": 1_700_000_000_000}),
        http.get("/api/news/notices/detail", params={"seq": "n1"}),
        http.get("/api/news/topics/hot", params={"tab_id": 1, "important_only": True}),
        http.get("/api/news/anomaly/analysis", params={"begin_date": "2026-11-01", "end_date": "2026-11-26", "thscode": "300033.SZ"}),
        http.get("/api/news/stock/timeline", params={"thscode": "300033.SZ", "offset": "first", "size": 20}),
    ]
    assert all(r.status_code == 200 for r in responses)

    kinds = [c[0] for c in calls]
    assert kinds == [
        "article_detail", "events_search", "flash_list", "flash_refresh",
        "notice_detail", "topic_hot_list", "anomaly_analysis", "stock_timeline",
    ]
    assert calls[0][1] == {"client_type": "WEB", "encoded_seq": "abc"}
    assert calls[3][1]["create_time_ms"] == 1_700_000_000_000
    assert calls[5][1] == {"tab_id": 1, "limit": 10, "important_only": True}
    assert calls[6][1]["thscode"] == "300033.SZ"
    assert calls[7][1] == {"thscode": "300033.SZ", "offset": "first", "size": 20}


@pytest.mark.parametrize("path,params", [
    ("/api/news/articles/detail", {"client_type": "ANDROID", "encoded_seq": "x"}),
    ("/api/news/events", {"search_param": "all"}),
    ("/api/news/flash/refresh", {"tag_id": 1, "seq": "7", "create_time_ms": 1001}),
    ("/api/news/topics/hot", {"tab_id": 1, "limit": 11}),
    ("/api/news/anomaly/analysis", {"begin_date": "2026-11-26", "end_date": "2026-11-01"}),
    ("/api/news/anomaly/analysis", {"begin_date": "2026/11/01", "end_date": "2026-11-26"}),
    ("/api/news/anomaly/analysis", {"begin_date": "2026-10-01", "end_date": "2026-11-26"}),
    ("/api/news/stock/timeline", {"thscode": "300033.SZ", "size": 101}),
])
def test_api_rejects_invalid_params(client, path, params):
    http, calls = client
    # 手写校验返回 400; FastAPI Query 约束返回 422, 两者都不应到达服务层。
    assert http.get(path, params=params).status_code in (400, 422)
    assert calls == []
