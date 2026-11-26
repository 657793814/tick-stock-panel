"""飞书自建应用机器人推送 (im/v1/messages)。

与「自定义机器人 Webhook」(services/webhook_adapter.send_feishu) 的区别:
- Webhook 是群内机器人, 地址即凭证, 只能往建群时选定的那个群推;
- 应用机器人用 App ID + App Secret 换 tenant_access_token, 按 chat_id 推送到
  机器人已加入的任意群, 权限由应用后台配置决定。

凭证与目标群由用户在「设置 → IM 管理」维护。失败返回原因而不是抛异常:
生产链路(告警/复盘)依赖这里的 bool, 设置页的「测试」需要具体错误文案。
"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://open.feishu.cn/open-apis"
_TIMEOUT = 10.0
# 图片上传是二进制传输, 比文本接口慢, 单独放宽超时
_UPLOAD_TIMEOUT = 30.0
# token 失效类错误码: 清缓存重换一次 token 再试
_TOKEN_INVALID_CODES = {99991661, 99991663, 99991668}

_lock = threading.Lock()
# app_id -> (app_secret, token, 过期时间戳)。secret 参与比对: 用户改密钥后旧 token 立即失效。
_token_cache: dict[str, tuple[str, str, float]] = {}


def is_configured(app_id: str, app_secret: str, chat_id: str) -> bool:
    """三项齐全才认为可投递 (缺一项时调用方应跳过而不是发一条失败日志)。"""
    return bool((app_id or "").strip() and (app_secret or "").strip() and (chat_id or "").strip())


def _invalidate(app_id: str) -> None:
    with _lock:
        _token_cache.pop(app_id, None)


def _get_token(app_id: str, app_secret: str) -> tuple[str, str]:
    """换 tenant_access_token。返回 (token, 错误原因), token 为空表示失败。"""
    now = time.time()
    with _lock:
        hit = _token_cache.get(app_id)
        if hit and hit[0] == app_secret and hit[2] > now:
            return hit[1], ""

    try:
        resp = httpx.post(
            f"{_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 网络/解析失败一律降级为文案, 不能抛给告警主流程
        return "", f"请求飞书失败: {type(exc).__name__}: {exc}"

    if data.get("code") != 0:
        return "", f"App ID/Secret 换取 token 失败({data.get('code')}): {data.get('msg')}"
    token = str(data.get("tenant_access_token") or "")
    if not token:
        return "", "飞书未返回 tenant_access_token"
    expire = int(data.get("expire") or 0)
    with _lock:
        # 提前 2 分钟过期, 避免边界上用到刚失效的 token
        _token_cache[app_id] = (app_secret, token, now + max(expire - 120, 60))
    return token, ""


def _send_once(token: str, chat_id: str, text: str) -> tuple[bool, int, str]:
    """单次投递。返回 (成功, 飞书 code, 错误原因)。"""
    try:
        resp = httpx.post(
            f"{_BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 同上, 投递失败只降级不抛异常
        return False, -1, f"请求飞书失败: {type(exc).__name__}: {exc}"
    code = int(data.get("code") or 0)
    if code == 0:
        return True, 0, ""
    return False, code, f"飞书返回错误({code}): {data.get('msg')}"


def send_text(
    app_id: str,
    app_secret: str,
    chat_id: str,
    title: str,
    body: str,
) -> bool:
    """推一条文本消息到 chat_id。失败静默(记 WARNING), 与其它推送渠道一致。"""
    ok, err = send_text_diagnose(app_id, app_secret, chat_id, title, body)
    if not ok:
        logger.warning("飞书应用机器人推送失败: %s", err)
    return ok


def send_text_diagnose(
    app_id: str,
    app_secret: str,
    chat_id: str,
    title: str,
    body: str,
) -> tuple[bool, str]:
    """带原因的投递, 供设置页「测试」使用。"""
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    chat_id = (chat_id or "").strip()
    if not is_configured(app_id, app_secret, chat_id):
        return False, "尚未完整配置 App ID / App Secret / 目标群"

    text = str(title or "").strip()
    if body:
        text = f"{text}\n{body}" if text else str(body)

    token, err = _get_token(app_id, app_secret)
    if not token:
        return False, err

    ok, code, err = _send_once(token, chat_id, text)
    if ok:
        return True, ""
    if code in _TOKEN_INVALID_CODES:
        # token 被后台作废(改权限/重装应用)时缓存还在, 强制换一次
        _invalidate(app_id)
        token, err = _get_token(app_id, app_secret)
        if token:
            ok, _, err = _send_once(token, chat_id, text)
            if ok:
                return True, ""
    return False, err


def send_image(
    app_id: str,
    app_secret: str,
    chat_id: str,
    image_bytes: bytes,
    *,
    filename: str = "poster.png",
) -> tuple[bool, str]:
    """推送一张图片到 chat_id（复盘海报）。

    飞书发图需要两步：先上传素材换 image_key，再用 image 消息发出去。
    自定义机器人 webhook 没有这个上传能力，所以只有自建应用能做带图推送。
    """
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    chat_id = (chat_id or "").strip()
    if not is_configured(app_id, app_secret, chat_id):
        return False, "尚未完整配置 App ID / App Secret / 目标群"
    if not image_bytes:
        return False, "图片内容为空"

    token, err = _get_token(app_id, app_secret)
    if not token:
        return False, err

    image_key = ""
    for attempt in range(2):
        try:
            resp = httpx.post(
                f"{_BASE}/im/v1/images",
                headers={"Authorization": f"Bearer {token}"},
                data={"image_type": "message"},
                files={"image": (filename, image_bytes, "image/png")},
                timeout=_UPLOAD_TIMEOUT,
            )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 - 上传失败只返回原因
            return False, f"上传图片失败: {type(exc).__name__}: {exc}"

        code = int(data.get("code") or 0)
        if code == 0:
            image_key = str((data.get("data") or {}).get("image_key") or "")
            break
        if code in _TOKEN_INVALID_CODES and attempt == 0:
            _invalidate(app_id)
            token, err = _get_token(app_id, app_secret)
            if not token:
                return False, err
            continue
        return False, f"上传图片被拒({code}): {data.get('msg')}"

    if not image_key:
        return False, "飞书未返回 image_key"

    try:
        resp = httpx.post(
            f"{_BASE}/im/v1/messages",
            params={"receive_id_type": "chat_id"},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "image",
                "content": json.dumps({"image_key": image_key}),
            },
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return False, f"发送图片失败: {type(exc).__name__}: {exc}"

    code = int(data.get("code") or 0)
    if code == 0:
        return True, ""
    return False, f"飞书发送图片错误({code}): {data.get('msg')}"


def list_chats(app_id: str, app_secret: str) -> tuple[list[dict], str]:
    """列出机器人已加入的群, 供设置页选择目标群。返回 (列表, 错误原因)。"""
    app_id = (app_id or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_id or not app_secret:
        return [], "请先填写 App ID 和 App Secret"

    token, err = _get_token(app_id, app_secret)
    if not token:
        return [], err

    try:
        resp = httpx.get(
            f"{_BASE}/im/v1/chats",
            params={"page_size": 100},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT,
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - 读群列表失败返回原因, 不抛异常
        return [], f"请求飞书失败: {type(exc).__name__}: {exc}"

    if int(data.get("code") or 0) != 0:
        return [], f"读取群列表失败({data.get('code')}): {data.get('msg')}"
    items = ((data.get("data") or {}).get("items")) or []
    return [
        {"chat_id": str(it.get("chat_id") or ""), "name": str(it.get("name") or "")}
        for it in items
        if it.get("chat_id")
    ], ""
