"""飞书自建应用机器人推送 (feishu_app) — 服务、设置端点与渠道白名单。

不触网: httpx.post / httpx.get 全部替换为假响应。
"""
from __future__ import annotations

import pytest

from app.api.settings import WebhookTestIn
from app.api.settings import test_webhook as run_webhook_test
from app.services import feishu_app
from app.strategy.monitor_rules import normalize as normalize_rule

APP_ID = "cli_test_app"
APP_SECRET = "test-secret"
CHAT_ID = "oc_test_chat"


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _clear_token_cache():
    feishu_app._token_cache.clear()
    yield
    feishu_app._token_cache.clear()


def _fake_post(monkeypatch, *, token_code=0, send_code=0):
    """记录请求, 按 token/send 两个端点返回不同 code。"""
    calls = {"token": 0, "send": []}

    def post(url, json=None, params=None, headers=None, timeout=None):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            calls["token"] += 1
            if token_code:
                return _Resp({"code": token_code, "msg": "invalid app"})
            return _Resp({"code": 0, "tenant_access_token": f"t-{calls['token']}", "expire": 7200})
        calls["send"].append({"json": json, "params": params, "headers": headers})
        if send_code:
            return _Resp({"code": send_code, "msg": "no permission"})
        return _Resp({"code": 0, "data": {"message_id": "om_1"}})

    monkeypatch.setattr(feishu_app.httpx, "post", post)
    return calls


def test_send_text_builds_im_v1_message_request(monkeypatch):
    calls = _fake_post(monkeypatch)

    ok, err = feishu_app.send_text_diagnose(APP_ID, APP_SECRET, CHAT_ID, "告警标题", "000001.SZ 涨停")

    assert (ok, err) == (True, "")
    sent = calls["send"][0]
    assert sent["params"] == {"receive_id_type": "chat_id"}
    assert sent["headers"]["Authorization"] == "Bearer t-1"
    assert sent["json"]["receive_id"] == CHAT_ID
    assert sent["json"]["msg_type"] == "text"
    # content 必须是 JSON 字符串, 否则飞书报 230001
    assert '"text": "告警标题\\n000001.SZ 涨停"' in sent["json"]["content"]


def test_token_is_cached_across_sends(monkeypatch):
    calls = _fake_post(monkeypatch)

    assert feishu_app.send_text(APP_ID, APP_SECRET, CHAT_ID, "A", "b") is True
    assert feishu_app.send_text(APP_ID, APP_SECRET, CHAT_ID, "A", "b") is True

    # 两次推送共用一次 token 请求
    assert calls["token"] == 1
    assert len(calls["send"]) == 2


def test_secret_change_invalidates_cached_token(monkeypatch):
    calls = _fake_post(monkeypatch)

    feishu_app.send_text(APP_ID, APP_SECRET, CHAT_ID, "A", "b")
    feishu_app.send_text(APP_ID, "another-secret", CHAT_ID, "A", "b")

    assert calls["token"] == 2


def test_send_reports_feishu_error_code(monkeypatch):
    _fake_post(monkeypatch, send_code=230002)

    ok, err = feishu_app.send_text_diagnose(APP_ID, APP_SECRET, CHAT_ID, "A", "b")

    assert ok is False
    assert "230002" in err


def test_invalid_token_code_retries_with_fresh_token(monkeypatch):
    calls = _fake_post(monkeypatch, send_code=99991663)

    ok, err = feishu_app.send_text_diagnose(APP_ID, APP_SECRET, CHAT_ID, "A", "b")

    # 重换 token 后仍失败(假响应恒定报错), 但必须发生第二次 token + 第二次投递
    assert ok is False
    assert calls["token"] == 2
    assert len(calls["send"]) == 2
    assert "99991663" in err


def test_token_request_failure_returns_reason(monkeypatch):
    _fake_post(monkeypatch, token_code=10014)

    ok, err = feishu_app.send_text_diagnose(APP_ID, APP_SECRET, CHAT_ID, "A", "b")

    assert ok is False
    assert "App ID/Secret" in err


def test_missing_config_is_rejected_without_request(monkeypatch):
    calls = _fake_post(monkeypatch)

    ok, err = feishu_app.send_text_diagnose(APP_ID, "", CHAT_ID, "A", "b")

    assert ok is False
    assert "尚未完整配置" in err
    assert calls["token"] == 0


def test_send_silent_wrapper_returns_false_and_logs(monkeypatch, caplog):
    _fake_post(monkeypatch, send_code=230002)

    assert feishu_app.send_text(APP_ID, APP_SECRET, CHAT_ID, "A", "b") is False
    assert "飞书应用机器人推送失败" in caplog.text


def test_list_chats_maps_items(monkeypatch):
    _fake_post(monkeypatch)
    monkeypatch.setattr(
        feishu_app.httpx,
        "get",
        lambda url, params=None, headers=None, timeout=None: _Resp({
            "code": 0,
            "data": {"items": [{"chat_id": "oc_1", "name": "测试群"}, {"name": "缺 id"}]},
        }),
    )

    chats, err = feishu_app.list_chats(APP_ID, APP_SECRET)

    assert err == ""
    assert chats == [{"chat_id": "oc_1", "name": "测试群"}]


def test_list_chats_reports_permission_error(monkeypatch):
    _fake_post(monkeypatch)
    monkeypatch.setattr(
        feishu_app.httpx,
        "get",
        lambda url, params=None, headers=None, timeout=None: _Resp({"code": 99991672, "msg": "no scope"}),
    )

    chats, err = feishu_app.list_chats(APP_ID, APP_SECRET)

    assert chats == []
    assert "99991672" in err


def test_webhook_test_endpoint_routes_feishu_app(monkeypatch):
    calls = {}
    monkeypatch.setattr("app.services.preferences.get_feishu_app_id", lambda: APP_ID)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_chat_id", lambda: CHAT_ID)
    monkeypatch.setattr("app.secrets_store.get_feishu_app_secret", lambda: APP_SECRET)
    monkeypatch.setattr(
        "app.services.feishu_app.send_text_diagnose",
        lambda app_id, secret, chat_id, title, body: calls.update(
            app_id=app_id, secret=secret, chat_id=chat_id, title=title, body=body
        ) or (True, ""),
    )

    result = run_webhook_test(WebhookTestIn(channel="feishu_app"))

    assert result["ok"] is True
    assert calls["chat_id"] == CHAT_ID
    assert calls["title"] == "TickFlow Stock Panel 推送测试"


def test_webhook_test_endpoint_surfaces_feishu_error(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_feishu_app_id", lambda: APP_ID)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_chat_id", lambda: CHAT_ID)
    monkeypatch.setattr("app.secrets_store.get_feishu_app_secret", lambda: APP_SECRET)
    monkeypatch.setattr(
        "app.services.feishu_app.send_text_diagnose",
        lambda *a, **k: (False, "飞书返回错误(230002): bot is not in chat"),
    )

    result = run_webhook_test(WebhookTestIn(channel="feishu_app"))

    assert result["ok"] is False
    assert "230002" in result["detail"]


def test_webhook_test_endpoint_requires_full_config(monkeypatch):
    monkeypatch.setattr("app.services.preferences.get_feishu_app_id", lambda: APP_ID)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_chat_id", lambda: "")
    monkeypatch.setattr("app.secrets_store.get_feishu_app_secret", lambda: APP_SECRET)

    result = run_webhook_test(WebhookTestIn(channel="feishu_app"))

    assert result["ok"] is False
    assert "尚未完整配置" in result["detail"]


def test_rule_normalize_keeps_feishu_app_channel():
    rule = normalize_rule({
        "id": "r1",
        "type": "price",
        "webhook_channels": ["feishu_app", "sms"],
    })

    assert rule["webhook_channels"] == ["feishu_app"]


def test_default_channels_whitelist_accepts_feishu_app(monkeypatch):
    from app.services import preferences

    saved = {}
    monkeypatch.setattr(preferences, "load", lambda: dict(saved))
    monkeypatch.setattr(preferences, "save", lambda updates: saved.update(updates))

    assert preferences.set_webhook_default_channels(["feishu_app", "feishu", "sms"]) == [
        "feishu_app", "feishu",
    ]


class _FakeExecutor:
    """收集 _maybe_send_webhook 提交的投递任务 (不真跑线程)。"""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))


def _disable_other_channels(monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(
        "app.services.preferences.get_webhook_enabled_channels",
        lambda: sorted(preferences.PUSH_CHANNELS),
    )
    for name in (
        "get_feishu_webhook_url",
        "get_feishu_webhook_secret",
        "get_wecom_webhook_url",
        "get_custom_webhook_url",
    ):
        monkeypatch.setattr(f"app.services.preferences.{name}", lambda: "")
    monkeypatch.setattr("app.secrets_store.get_custom_webhook_secret", lambda: "")
    monkeypatch.setattr(
        "app.services.preferences.get_email_smtp_config",
        lambda: {"host": "", "from_address": "", "to_addresses": []},
    )
    monkeypatch.setattr("app.secrets_store.get_email_smtp_password", lambda: "")


def test_monitor_alert_dispatches_feishu_app_channel(monkeypatch):
    from types import SimpleNamespace

    from app.services import quote_service

    _disable_other_channels(monkeypatch)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_id", lambda: APP_ID)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_chat_id", lambda: CHAT_ID)
    monkeypatch.setattr("app.secrets_store.get_feishu_app_secret", lambda: APP_SECRET)
    fake = _FakeExecutor()
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", fake)

    svc = object.__new__(quote_service.QuoteService)
    engine = SimpleNamespace(rules={"r1": {"webhook_channels": ["feishu_app"]}})
    svc._maybe_send_webhook(
        [{"rule_id": "r1", "source": "price", "symbol": "000001.SZ", "name": "平安银行", "message": "涨至10元"}],
        engine,
    )

    assert len(fake.calls) == 1
    fn, args, _ = fake.calls[0]
    assert fn is feishu_app.send_text
    assert args[:3] == (APP_ID, APP_SECRET, CHAT_ID)
    assert "000001.SZ" in args[4]


def test_monitor_alert_skips_feishu_app_when_channel_not_selected(monkeypatch):
    from types import SimpleNamespace

    from app.services import quote_service

    _disable_other_channels(monkeypatch)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_id", lambda: APP_ID)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_chat_id", lambda: CHAT_ID)
    monkeypatch.setattr("app.secrets_store.get_feishu_app_secret", lambda: APP_SECRET)
    fake = _FakeExecutor()
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", fake)

    svc = object.__new__(quote_service.QuoteService)
    engine = SimpleNamespace(rules={"r1": {"webhook_channels": ["feishu"]}})
    svc._maybe_send_webhook(
        [{"rule_id": "r1", "source": "price", "symbol": "000001.SZ", "message": "涨至10元"}],
        engine,
    )

    assert fake.calls == []


def test_monitor_alert_skips_feishu_app_when_not_configured(monkeypatch):
    from types import SimpleNamespace

    from app.services import quote_service

    _disable_other_channels(monkeypatch)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_id", lambda: APP_ID)
    monkeypatch.setattr("app.services.preferences.get_feishu_app_chat_id", lambda: "")
    monkeypatch.setattr("app.secrets_store.get_feishu_app_secret", lambda: APP_SECRET)
    fake = _FakeExecutor()
    monkeypatch.setattr(quote_service, "_WEBHOOK_EXECUTOR", fake)

    svc = object.__new__(quote_service.QuoteService)
    engine = SimpleNamespace(rules={"r1": {"webhook_channels": ["feishu_app"]}})
    svc._maybe_send_webhook(
        [{"rule_id": "r1", "source": "price", "symbol": "000001.SZ", "message": "涨至10元"}],
        engine,
    )

    assert fake.calls == []
