"""图像模型适配器 — OpenAI 兼容 images API。

与文本模型链路 (``ai_provider.py``) 完全隔离：
- 配置项前缀为 ``image_``，清空文本 AI 不会清掉生图配置；
- ``image_base_url`` / ``image_api_key`` 留空时回退到文本 AI 的同名配置，
  但 ``image_model`` 必须单独设置，避免把文本模型误当成生图模型。

支持的返回形态：JSON ``b64_json`` / ``url``（含常见嵌套结构）或直接返回图片字节。
"""
from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from app import secrets_store
from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_IMAGE_SIZE = "1024x1536"
DEFAULT_TIMEOUT_SECONDS = 180.0
MAX_IMAGE_BYTES = 20 * 1024 * 1024


class ImageGenerationError(RuntimeError):
    """生图失败，消息可直接展示给用户。"""


@dataclass(frozen=True)
class GeneratedImage:
    content: bytes
    mime: str
    ext: str


def _raw_image_config(key: str) -> str:
    """读取 image_* 原始字符串；空字符串代表「回退到文本 AI 配置」。"""
    return str(secrets_store.load().get(key) or "").strip()


def current_image_base_url() -> str:
    custom = _raw_image_config("image_base_url")
    if custom:
        return custom
    return secrets_store.get_ai_config("ai_base_url", settings.ai_base_url).strip()


def current_image_api_key() -> str:
    custom = _raw_image_config("image_api_key")
    if custom:
        return custom
    return secrets_store.get_ai_key().strip()


def current_image_model() -> str:
    return secrets_store.get_ai_config("image_model", settings.image_model).strip()


def current_image_size() -> str:
    return secrets_store.get_ai_config("image_size", settings.image_size).strip() or DEFAULT_IMAGE_SIZE


def image_configured() -> bool:
    """生图可用的最低条件：有模型名且能定位到接口。Key 允许本地无鉴权服务留空。"""
    return bool(current_image_model() and current_image_base_url())


def normalize_image_endpoint(base_url: str) -> str:
    """把用户填的 base URL 归一化到 images/generations 端点。

    复用文本模型的版本段识别：``/v1``、``/api/paas/v4`` 等不会被重复补 ``/v1``。
    """
    from app.services.ai_provider import normalize_openai_base_url

    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ImageGenerationError("尚未配置生图接口地址")
    if base.endswith("/images/generations"):
        return base
    if base.endswith("/images"):
        return f"{base}/generations"
    return f"{normalize_openai_base_url(base)}/images/generations"


def _detect_image(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if len(data) > 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    raise ImageGenerationError("图像模型返回的不是可识别的 PNG / JPEG / WebP 图片")


def _decode_b64(value: str) -> bytes:
    text = value.strip()
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    try:
        data = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ImageGenerationError(f"图像模型返回的 base64 数据无效: {exc}") from exc
    if not data:
        raise ImageGenerationError("图像模型返回了空图片")
    return data


def _find_image_ref(node: object) -> tuple[str, str] | None:
    """递归查找响应中的 b64_json / url，兼容多家兼容服务的嵌套结构。"""
    if isinstance(node, dict):
        for key in ("b64_json", "b64", "base64", "image_base64"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return ("b64", value)
        for key in ("url", "image_url"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return ("url", value)
        for value in node.values():
            found = _find_image_ref(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_image_ref(value)
            if found:
                return found
    return None


def _friendly_http_error(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    if status == 401:
        return "生图接口鉴权失败，请检查图像模型的 API Key"
    if status == 403:
        return "生图接口拒绝访问，请检查账户权限或模型名称"
    if status == 404:
        return "生图接口不存在，请检查接口地址（一般应填到 /v1）"
    if status == 429:
        return "生图接口限流，请稍后重试"
    if status >= 500:
        return f"生图服务暂时不可用（HTTP {status}）"
    detail = exc.response.text[:300].strip()
    return f"生图请求失败（HTTP {status}）: {detail}" if detail else f"生图请求失败（HTTP {status}）"


async def generate_image(
    prompt: str,
    *,
    model: str | None = None,
    size: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> GeneratedImage:
    """调用已配置的图像模型生成一张图片并返回字节内容。"""
    use_model = (model or current_image_model()).strip()
    if not use_model:
        raise ImageGenerationError("尚未配置生图模型，请先在「设置 · AI 服务」中填写图像模型")

    endpoint = normalize_image_endpoint(current_image_base_url())
    api_key = current_image_api_key()
    use_size = (size or current_image_size()).strip() or DEFAULT_IMAGE_SIZE
    user_agent = secrets_store.get_ai_config("ai_user_agent", settings.ai_user_agent) or settings.ai_user_agent
    headers = {"User-Agent": user_agent}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    payload = {"model": use_model, "prompt": prompt, "n": 1, "size": use_size}
    # 优先请求 base64，省去二次下载；部分兼容服务不接受该字段，400 后去掉重试。
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
        try:
            response = await client.post(endpoint, json={**payload, "response_format": "b64_json"})
            if response.status_code == 400:
                response = await client.post(endpoint, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ImageGenerationError(_friendly_http_error(exc)) from exc
        except httpx.RequestError as exc:
            raise ImageGenerationError(f"无法连接生图接口: {exc}") from exc

        content_type = response.headers.get("content-type", "").lower()
        if "image/" in content_type:
            data = response.content
        else:
            try:
                body = response.json()
            except ValueError as exc:
                raise ImageGenerationError("生图接口返回的不是 JSON 或图片") from exc
            ref = _find_image_ref(body)
            if not ref:
                raise ImageGenerationError("生图接口响应中未找到图片数据")
            kind, value = ref
            if kind == "b64":
                data = _decode_b64(value)
            else:
                image_url = urljoin(endpoint, value)
                # 图片 CDN 可能与 API 不同域：不带 Authorization，避免泄露 Key。
                async with httpx.AsyncClient(
                    timeout=timeout, headers={"User-Agent": user_agent}, follow_redirects=True,
                ) as downloader:
                    try:
                        image_res = await downloader.get(image_url)
                        image_res.raise_for_status()
                        data = image_res.content
                    except httpx.HTTPStatusError as exc:
                        raise ImageGenerationError(f"下载生图结果失败（HTTP {exc.response.status_code}）") from exc
                    except httpx.RequestError as exc:
                        raise ImageGenerationError(f"下载生图结果失败: {exc}") from exc

    if not data:
        raise ImageGenerationError("图像模型返回了空图片")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageGenerationError("图像模型返回的图片超过 20MB，请降低尺寸后重试")
    mime, ext = _detect_image(data)
    return GeneratedImage(content=data, mime=mime, ext=ext)
