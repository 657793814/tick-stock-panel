"""图像模型配置与解析辅助测试。"""
from __future__ import annotations

from app import secrets_store
from app.api import settings as settings_api
from app.services import image_provider


def test_normalize_image_endpoint_keeps_version_segment():
    assert image_provider.normalize_image_endpoint("https://llm.runninghub.ai/v1") == (
        "https://llm.runninghub.ai/v1/images/generations"
    )
    assert image_provider.normalize_image_endpoint("https://open.bigmodel.cn/api/paas/v4") == (
        "https://open.bigmodel.cn/api/paas/v4/images/generations"
    )
    assert image_provider.normalize_image_endpoint("https://api.openai.com/v1/images/generations") == (
        "https://api.openai.com/v1/images/generations"
    )


def test_find_image_ref_supports_nested_b64_and_url():
    assert image_provider._find_image_ref({"data": [{"b64_json": "YWJj"}]}) == ("b64", "YWJj")
    assert image_provider._find_image_ref({"output": {"images": [{"url": "https://x/y.png"}]}}) == (
        "url", "https://x/y.png",
    )
    assert image_provider._find_image_ref({"created": 1}) is None


def test_image_settings_do_not_clear_text_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(secrets_store, "_path", lambda: tmp_path / "secrets.json")
    secrets_store.save({"ai_api_key": "text-key", "ai_model": "text-model"})
    secrets_store.save({"image_api_key": "image-key", "image_model": "image-model"})

    settings_api.save_image_settings(settings_api.ImageSettingsIn(
        base_url="https://llm.runninghub.ai/v1", model="gemini-image", size="1024x1024",
    ))

    data = secrets_store.load()
    assert data["image_model"] == "gemini-image"
    assert data["image_base_url"] == "https://llm.runninghub.ai/v1"
    assert data["image_api_key"] == "image-key"  # 未传 api_key 保持原值
    assert data["ai_api_key"] == "text-key"
    assert data["ai_model"] == "text-model"

    settings_api.clear_image_settings()
    data = secrets_store.load()
    assert "image_model" not in data
    assert "image_api_key" not in data
    assert data["ai_api_key"] == "text-key"
