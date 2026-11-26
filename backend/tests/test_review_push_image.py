"""复盘推送附带海报 — 偏好开关与各渠道带图能力。

不触网: 所有外发函数都被替换为记录调用。
"""
from __future__ import annotations

import base64
import hashlib

import pytest

from app.services import preferences

_POSTER = b"\x89PNG\r\n\x1a\n" + b"fake-png-body" * 10


@pytest.fixture
def _enabled_all_channels(monkeypatch):
    monkeypatch.setattr(
        "app.services.preferences.get_webhook_enabled_channels",
        lambda: sorted(preferences.PUSH_CHANNELS),
    )


def test_review_push_image_preference_defaults_off():
    # 同一会话里前面的用例可能已写入 True, 先归零再验证默认值语义
    preferences.set_review_push_image(False)
    assert preferences.get_review_push_image() is False
    assert preferences.set_review_push_image(True) is True
    assert preferences.get_review_push_image() is True
    assert preferences.set_review_push_image(False) is False
    assert preferences.get_review_push_image() is False


def test_review_push_image_persisted_via_api(monkeypatch):
    from app.api import settings as settings_api

    res = settings_api.update_review_push(
        settings_api.ReviewPushIn(channels=["wecom"], image=True)
    )
    assert res["review_push_image"] is True
    assert preferences.get_review_push_image() is True

    # 不传 image 时保持原值, 不会被重置
    res2 = settings_api.update_review_push(
        settings_api.ReviewPushIn(channels=["wecom"])
    )
    assert res2["review_push_image"] is True


def test_wecom_receives_image_message_after_markdown(monkeypatch, _enabled_all_channels):
    from app.jobs import daily_pipeline
    from app.services import webhook_adapter

    calls: list[tuple] = []
    monkeypatch.setattr(preferences, "get_review_push_channels", lambda: ["wecom"])
    monkeypatch.setattr(preferences, "get_wecom_webhook_url", lambda: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k")
    monkeypatch.setattr(preferences, "get_review_push_image", lambda: True)

    monkeypatch.setattr(
        webhook_adapter, "send_wecom_markdown",
        lambda *a: calls.append(("md", a)) or True,
    )
    monkeypatch.setattr(
        webhook_adapter, "send_wecom_image",
        lambda url, data: calls.append(("img", url, data)) or True,
    )
    monkeypatch.setattr(daily_pipeline, "_build_review_poster", lambda c, m: _POSTER)

    daily_pipeline._maybe_push_review("复盘正文", {"as_of": "2026-11-26"})

    assert [c[0] for c in calls] == ["md", "img"]
    assert calls[1][2] == _POSTER


def test_email_gets_poster_attachment(monkeypatch, _enabled_all_channels):
    from app.jobs import daily_pipeline
    from app.services import email_adapter

    captured: dict = {}
    config = {
        "host": "smtp.example.com",
        "username": "bot@example.com",
        "from_address": "bot@example.com",
        "to_addresses": ["alerts@example.com"],
    }
    monkeypatch.setattr(preferences, "get_review_push_channels", lambda: ["email"])
    monkeypatch.setattr(preferences, "get_email_smtp_config", lambda: config)
    monkeypatch.setattr(preferences, "get_review_push_image", lambda: True)
    monkeypatch.setattr("app.secrets_store.get_email_smtp_password", lambda: "pw")
    monkeypatch.setattr(
        email_adapter, "send_email",
        lambda *a, **kw: captured.update(args=a, kwargs=kw) or True,
    )
    monkeypatch.setattr(daily_pipeline, "_build_review_poster", lambda c, m: _POSTER)

    daily_pipeline._maybe_push_review("复盘正文", {"as_of": "2026-11-26"})

    attachment = captured["kwargs"]["attachment"]
    assert attachment[2] == "image/png"
    assert attachment[1] == _POSTER
    assert "2026-11-26" in attachment[0]


def test_feishu_webhook_channel_sends_text_only(monkeypatch, _enabled_all_channels):
    """飞书自定义机器人拿不到 image_key, 只发文本, 不应尝试发图。"""
    from app.jobs import daily_pipeline

    calls: list[str] = []
    monkeypatch.setattr(preferences, "get_review_push_channels", lambda: ["feishu"])
    monkeypatch.setattr(preferences, "get_feishu_webhook_url", lambda: "feishu-url")
    monkeypatch.setattr(preferences, "get_feishu_webhook_secret", lambda: "")
    monkeypatch.setattr(preferences, "get_review_push_image", lambda: True)
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_feishu_card",
        lambda *a: calls.append("feishu") or True,
    )
    monkeypatch.setattr(daily_pipeline, "_build_review_poster", lambda c, m: _POSTER)

    daily_pipeline._maybe_push_review("复盘正文", {"as_of": "2026-11-26"})

    assert calls == ["feishu"]


def test_poster_failure_degrades_to_text(monkeypatch, _enabled_all_channels):
    """生图失败时仍应发出文本报告。"""
    from app.jobs import daily_pipeline

    calls: list[str] = []
    monkeypatch.setattr(preferences, "get_review_push_channels", lambda: ["wecom"])
    monkeypatch.setattr(preferences, "get_wecom_webhook_url", lambda: "wecom-url")
    monkeypatch.setattr(preferences, "get_review_push_image", lambda: True)
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_wecom_markdown",
        lambda *a: calls.append("md") or True,
    )
    monkeypatch.setattr(
        "app.services.webhook_adapter.send_wecom_image",
        lambda *a: calls.append("img") or True,
    )
    monkeypatch.setattr(daily_pipeline, "_build_review_poster", lambda c, m: None)

    daily_pipeline._maybe_push_review("复盘正文", {"as_of": "2026-11-26"})

    assert calls == ["md"]


def test_wecom_image_payload_is_base64_with_md5(monkeypatch):
    from app.services import webhook_adapter

    sent: dict = {}
    monkeypatch.setattr(
        webhook_adapter, "_post_wecom",
        lambda url, payload: sent.update(url=url, payload=payload) or True,
    )

    assert webhook_adapter.send_wecom_image("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k", _POSTER) is True
    payload = sent["payload"]
    assert payload["msgtype"] == "image"
    assert base64.b64decode(payload["image"]["base64"]) == _POSTER
    assert payload["image"]["md5"] == hashlib.md5(_POSTER).hexdigest()


def test_wecom_image_rejects_oversized(monkeypatch):
    from app.services import webhook_adapter

    called: list = []
    monkeypatch.setattr(webhook_adapter, "_post_wecom", lambda *a: called.append(a) or True)

    big = b"\x89PNG\r\n\x1a\n" + b"x" * (2 * 1024 * 1024)
    assert webhook_adapter.send_wecom_image("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k", big) is False
    assert called == []


def test_email_adapter_builds_attachment():
    from email.message import EmailMessage

    msg = EmailMessage()
    msg.set_content("正文")
    msg.add_attachment(_POSTER, maintype="image", subtype="png", filename="poster.png")
    attachments = [part for part in msg.iter_attachments()]
    assert len(attachments) == 1
    assert attachments[0].get_filename() == "poster.png"
