"""Redis 优先缓存 + 内存降级。"""
from __future__ import annotations

import pytest

from app import cache_store


@pytest.fixture(autouse=True)
def reset_cache():
    cache_store.reset_for_tests()
    yield
    cache_store.reset_for_tests()


def test_memory_fallback_when_redis_disabled(monkeypatch):
    monkeypatch.setattr(cache_store, "_REDIS_ENABLED", False)

    cache_store.set_json("overview", "k", {"value": 1}, ttl=60)

    assert cache_store.get_json("overview", "k") == {"value": 1}
    cache_store.invalidate_namespace("overview")
    assert cache_store.get_json("overview", "k") is None


def test_redis_hit_and_memory_fallback_on_error(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.store = {}
            self.fail_get = False

        def get(self, key):
            if self.fail_get:
                raise ConnectionError("redis down")
            return self.store.get(key)

        def set(self, key, value, ex=None):
            self.store[key] = value

        def scan_iter(self, match=None, count=100):
            return [k for k in self.store if k.startswith("tf:overview:")]

        def delete(self, *keys):
            for key in keys:
                self.store.pop(key, None)

    client = FakeRedis()
    monkeypatch.setattr(cache_store, "_client_or_none", lambda: client)

    cache_store.set_json("overview", "a", {"v": 1}, ttl=120)
    assert client.store["tf:overview:a"] == '{"v":1}'
    assert cache_store.get_json("overview", "a") == {"v": 1}

    client.fail_get = True
    # Redis 报错后回退到刚才同时写入的内存副本
    assert cache_store.get_json("overview", "a") == {"v": 1}


def test_invalidate_clears_redis_and_memory(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.store = {"tf:overview:old": "{}"}

        def scan_iter(self, match=None, count=100):
            return [k for k in self.store if k.startswith("tf:overview:")]

        def delete(self, *keys):
            for key in keys:
                self.store.pop(key, None)

    client = FakeRedis()
    monkeypatch.setattr(cache_store, "_client_or_none", lambda: client)
    cache_store.set_json("overview", "old", {"v": 1}, ttl=60)

    cache_store.invalidate_namespace("overview")

    assert client.store == {}
    assert cache_store.get_json("overview", "old") is None
