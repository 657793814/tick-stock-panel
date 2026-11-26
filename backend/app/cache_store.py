"""Redis 优先、进程内内存兜底的 JSON 缓存。

设计目标:
  - 港美股总览这类昂贵结果在 Docker 内由 Redis 共享缓存承接, 重启后端也不会丢;
  - 没有 Redis / Redis 连接失败时自动降级到进程内字典, 启动和接口都不受影响;
  - Redis 重试有冷却窗口, 避免每次请求都为一个不可达实例付出连接超时。

只缓存 JSON 可序列化结果。Polars DataFrame 这类大对象仍走各服务原有的内存缓存。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_PREFIX = "tf"
_REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
_REDIS_ENABLED = os.getenv("REDIS_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
_REDIS_COOLDOWN_S = 30.0

_client: Any | None = None
_client_checked = False
_cooldown_until = 0.0
_memory: dict[str, tuple[float, Any]] = {}
_lock = threading.RLock()


def _memory_key(namespace: str, key: str) -> str:
    return f"{_PREFIX}:{namespace}:{key}"


def _client_or_none() -> Any | None:
    """返回可用 Redis 客户端; 不可达时进入冷却期并返回 None。"""
    global _client, _client_checked, _cooldown_until
    if not _REDIS_ENABLED:
        return None
    now = time.monotonic()
    if now < _cooldown_until:
        return None
    if not _client_checked:
        _client_checked = True
        try:
            import redis  # 延迟导入: 缺依赖时直接走内存兜底
        except Exception as exc:  # noqa: BLE001
            # 保留重试机会: 运行期补齐 redis 依赖后, 冷却结束即可自动接上,
            # 否则 _client_checked=True 会让进程永久停在内存兜底。
            _client_checked = False
            logger.warning("Redis client unavailable, cache falls back to memory: %s", exc)
            _cooldown_until = now + _REDIS_COOLDOWN_S
            return None
        try:
            _client = redis.from_url(
                _REDIS_URL,
                socket_connect_timeout=0.2,
                socket_timeout=0.2,
                decode_responses=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis client init failed, cache falls back to memory: %s", exc)
            _cooldown_until = now + _REDIS_COOLDOWN_S
            return None
    return _client


def _disable_redis(exc: Exception) -> None:
    global _cooldown_until
    _cooldown_until = time.monotonic() + _REDIS_COOLDOWN_S
    logger.warning("Redis cache unavailable (%s), falling back to memory for %.0fs",
                   exc, _REDIS_COOLDOWN_S)


def get_json(namespace: str, key: str, *, now: float | None = None) -> Any | None:
    """读取缓存; Redis 缺失或失败时回退进程内内存。"""
    now = time.monotonic() if now is None else now
    full_key = _memory_key(namespace, key)
    client = _client_or_none()
    if client is not None:
        try:
            raw = client.get(full_key)
            if raw is not None:
                return json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            _disable_redis(exc)
    with _lock:
        item = _memory.get(full_key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= now:
            _memory.pop(full_key, None)
            return None
        return value


def set_json(namespace: str, key: str, value: Any, ttl: float, *, now: float | None = None) -> None:
    """写入缓存; JSON 不可序列化时仅写内存。"""
    now = time.monotonic() if now is None else now
    full_key = _memory_key(namespace, key)
    expires_at = now + ttl
    with _lock:
        _memory[full_key] = (expires_at, value)
        if len(_memory) > 256:
            now = time.monotonic()
            for old_key in [k for k, (exp, _) in _memory.items() if exp <= now]:
                _memory.pop(old_key, None)

    client = _client_or_none()
    if client is None:
        return
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        client.set(full_key, payload, ex=max(1, int(ttl)))
    except TypeError:
        # Redis 只承接 JSON 结果; 复杂对象继续由调用方的内存缓存负责。
        pass
    except Exception as exc:  # noqa: BLE001
        _disable_redis(exc)


def invalidate_namespace(namespace: str, *, key_prefix: str | None = None) -> None:
    """清空一个命名空间(或其子前缀)的 Redis 与内存缓存。"""
    prefix = f"{_PREFIX}:{namespace}:{key_prefix or ''}"
    with _lock:
        for key in [k for k in _memory if k.startswith(prefix)]:
            _memory.pop(key, None)

    client = _client_or_none()
    if client is None:
        return
    try:
        keys = list(client.scan_iter(match=f"{prefix}*", count=100))
        if keys:
            client.delete(*keys)
    except Exception as exc:  # noqa: BLE001
        _disable_redis(exc)


def reset_for_tests() -> None:
    """仅供测试: 清空内存与客户端/冷却状态。"""
    global _client, _client_checked, _cooldown_until
    with _lock:
        _memory.clear()
    _client = None
    _client_checked = False
    _cooldown_until = 0.0
