# ruff: noqa
"""Vendored XTick HTTP 工具, 已按项目标准硬化。

来源: github.com/xticktop/skills @ 05087c5 (MIT)。
相对上游的改动:
  - requests -> httpx, 增加 10s 超时与一次 5xx/网络重试;
  - 检查 HTTP 状态码, 解析业务错误信封, 统一返回 list/dict;
  - 错误文案中替换 token, 防止凭据进入日志。
"""
from __future__ import annotations

import gzip
import json
import logging
import time
import zipfile
from io import BytesIO

import httpx

# httpx 的 INFO 日志会把完整 URL(query string 含 token)写入日志, 必须关闭。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class XTickError(RuntimeError):
    """上游请求失败。code 为 HTTP 状态码或业务错误码。"""

    def __init__(self, code: int | str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"XTick 上游错误 {code}: {message}")


def detect_compression_format(data: bytes) -> str:
    if len(data) >= 4 and data[:2] == b"PK" and data[2:4] in (b"\x03\x04", b"\x05\x06", b"\x07\x08"):
        return "zip"
    if len(data) >= 2 and data[:2] == b"\x1f\x8b":
        return "gzip"
    return "unknown"


def _safe(message: str, token: str) -> str:
    return message.replace(token, "[已隐藏]") if token else message


def process_data(data: bytes, token: str = "") -> object:
    try:
        if detect_compression_format(data) == "gzip":
            data = gzip.decompress(data)
        elif detect_compression_format(data) == "zip":
            with zipfile.ZipFile(BytesIO(data)) as archive:
                for name in archive.namelist():
                    if name.endswith(".json"):
                        data = archive.read(name)
                        break
                else:
                    raise XTickError(-2, "压缩包内未找到 JSON 文件")
        text = data.decode("utf-8")
        payload = json.loads(text)
    except XTickError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, zipfile.BadZipFile) as exc:
        raise XTickError(-2, f"上游响应不是有效 JSON: {type(exc).__name__}") from None

    if isinstance(payload, dict) and "code" in payload and "message" in payload:
        code = payload["code"]
        if code not in (0, None) and not payload.get("data"):
            raise XTickError(code, _safe(str(payload.get("message") or ""), token))
    return payload


def request(url: str, method: str = "get", params: dict | None = None) -> object:
    params = params or {}
    token = str(params.get("token") or "")
    for attempt in range(2):
        try:
            response = httpx.request(
                method.upper(), url, params=params, timeout=10.0, follow_redirects=True,
            )
            if response.status_code >= 500 and attempt == 0:
                time.sleep(0.2)
                continue
            response.raise_for_status()
            return process_data(response.content, token)
        except httpx.HTTPStatusError as exc:
            raise XTickError(exc.response.status_code, "HTTP 请求失败") from None
        except httpx.RequestError:
            if attempt == 0:
                time.sleep(0.2)
                continue
            raise XTickError(-1, "网络请求失败") from None
    raise XTickError(-1, "网络请求失败")
