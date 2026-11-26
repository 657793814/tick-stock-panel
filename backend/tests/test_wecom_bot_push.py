"""企微智能机器人长连接主动推送 (aibot_send_msg) 与全局渠道开关回归。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from app.api.settings import WebhookTestIn
from app.api.settings import test_webhook as run_webhook_test
from app.services import preferences, quote_service
from app.services.wecom_bot_service import WecomBotService


class _FakeLoop:
    """把线程安全的回调同步执行, 便于单测不启真实事件循环。"""

    def call_soon_threadsafe(self, fn, *args):
        result = fn(*args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)


class _FakeWs:
    def __init__(self, service: WecomBotService, errcode: int = 0, errmsg: str = "ok"):
        self.service = service
        self.errcode = errcode
        self.errmsg = errmsg
        self.sent: list[dict] = []

    async def send(self, raw: str):
        frame = json.loads(raw)
        self.sent.append(frame)
        # 模拟服务端按 req_id 回响应
        if frame.get("cmd") == "aibot_send_msg":
            self.service._resolve_pending({
                "headers": frame["headers"],
                "errcode": self.errcode,
                "errmsg": self.errmsg,
            })


def _connected_service(errcode: int = 0, errmsg: str = "ok"):
    svc = WecomBotService()
    ws = _FakeWs(svc, errcode, errmsg)
    svc._connected = True
    svc._ws = ws
    svc._ws_loop = _FakeLoop()
    return svc, ws


def test_send_text_uses_aibot_send_msg_and_resolves_by_req_id():
    svc, ws = _connected_service()
    ok, err = svc.send_text("oc_group_1", "告警内容", chat_type=2)

    assert (ok, err) == (True, "ok")
    frame = ws.sent[0]
    assert frame["cmd"] == "aibot_send_msg"
    assert frame["headers"]["req_id"]
    assert frame["body"] == {
        "chatid": "oc_group_1",
        "chat_type": 2,
        "msgtype": "markdown",
        "markdown": {"content": "告警内容"},
    }
    assert svc._pending == {}


def test_send_text_returns_wecom_error_detail():
    svc, _ = _connected_service(errcode=93000, errmsg="invalid chatid")

    ok, err = svc.send_text("bad_chat", "hello", chat_type=2)

    assert ok is False
    assert "93000" in err and "invalid chatid" in err


def test_send_text_without_connection_returns_actionable_detail():
    svc = WecomBotService()

    ok, err = svc.send_text("oc_group_1", "hello", chat_type=2)

    assert ok is False
    assert "长连接未连接" in err


def test_chat_id_discovered_from_callback(monkeypatch):
    saved = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(saved))
    monkeypatch.setattr(preferences, "save", lambda updates: saved.update(updates))
    svc = WecomBotService()

    svc._log_incoming(json.dumps({
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "r1"},
        "body": {
            "chatid": "oc_from_callback",
            "chattype": "group",
            "from": {"userid": "u1"},
            "msgtype": "text",
            "text": {"content": "@机器人 在吗"},
        },
    }))

    chats = preferences.get_wecom_bot_chats()
    assert [c["chat_id"] for c in chats] == ["oc_from_callback"]
    assert chats[0]["chat_type"] == "group"


def test_enabled_channels_default_all_and_write_both_keys(monkeypatch):
    saved = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(saved))
    monkeypatch.setattr(preferences, "save", lambda updates: saved.update(updates))

    assert set(preferences.get_webhook_enabled_channels()) == set(preferences.PUSH_CHANNELS)
    assert preferences.set_webhook_enabled_channels(["wecom_bot", "invalid", "wecom_bot"]) == [
        "wecom_bot"
    ]
    assert saved["webhook_enabled_channels"] == ["wecom_bot"]
    assert saved["webhook_default_channels"] == ["wecom_bot"]


class _FakeExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args))


def _prepare_dispatch(monkeypatch, enabled):

    for name in (
        "get_feishu_webhook_url",
        "get_feishu_webhook_secret",
        "get_wecom_webhook_url",
        "get_custom_webhook_url",
        "get_feishu_app_id",
        "get_feishu_app_chat_id",
    ):
        monkeypatch.setattr(f"app.services.preferences.{name}", lambda: "")
    monkeypatch.setattr("app.services.preferences.get_email_smtp_config", lambda: {})
    monkeypatch.setattr("app.secrets_store.get_email_smtp_password", lambda: "")
    monkeypatch.setattr("app.secrets_store.get_custom_webhook_secret", lambda: "")
    monkeypatch.setattr("app.secrets_store.get_feishu_app_secret", lambda: "")
    monkeypatch.setattr(
        "app.services.preferences.get_webhook_enabled_channels", lambda: enabled
    )
    fake = _FakeExecutor()
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", fake)
    return fake


def test_alert_dispatches_wecom_bot_when_enabled_and_connected(monkeypatch):
    svc, _ = _connected_service()
    monkeypatch.setattr(WecomBotService, "instance", classmethod(lambda cls: svc))
    monkeypatch.setattr("app.services.preferences.get_wecom_bot_chat_id", lambda: "oc_group_1")
    fake = _prepare_dispatch(monkeypatch, ["wecom_bot"])

    svc_obj = object.__new__(quote_service.QuoteService)
    engine = SimpleNamespace(rules={"r1": {"webhook_channels": ["wecom_bot"]}})
    svc_obj._maybe_send_webhook(
        [{"rule_id": "r1", "source": "price", "symbol": "000001.SZ", "message": "涨至10元"}],
        engine,
    )

    assert len(fake.calls) == 1
    fn, args = fake.calls[0]
    assert fn == svc.send_text
    assert args[0] == "oc_group_1"
    assert "000001.SZ" in args[1]


def test_alert_skips_channels_disabled_globally(monkeypatch):
    fake = _prepare_dispatch(monkeypatch, [])
    monkeypatch.setattr("app.services.preferences.get_feishu_webhook_url", lambda: "feishu-url")

    svc_obj = object.__new__(quote_service.QuoteService)
    engine = SimpleNamespace(rules={"r1": {"webhook_channels": ["feishu"]}})
    svc_obj._maybe_send_webhook(
        [{"rule_id": "r1", "source": "price", "symbol": "000001.SZ", "message": "涨至10元"}],
        engine,
    )

    assert fake.calls == []


def test_webhook_test_wecom_bot_reports_connection_state(monkeypatch):
    svc, _ = _connected_service()
    monkeypatch.setattr(WecomBotService, "instance", classmethod(lambda cls: svc))
    monkeypatch.setattr("app.services.preferences.get_wecom_bot_chat_id", lambda: "oc_group_1")
    monkeypatch.setattr("app.services.preferences.get_wecom_bot_chat_type", lambda: 2)

    result = run_webhook_test(WebhookTestIn(channel="wecom_bot"))

    assert result["ok"] is True
    assert svc._ws.sent[0]["body"]["chatid"] == "oc_group_1"
