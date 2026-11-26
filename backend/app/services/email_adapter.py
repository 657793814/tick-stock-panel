"""SMTP email notification adapter.

Transport failures are isolated from alert persistence and SSE delivery. SMTP credentials
are supplied by the caller from ``secrets_store`` and never logged here.
"""
from __future__ import annotations

import logging
import smtplib
import time
from contextlib import suppress
from email.message import EmailMessage
from email.utils import parseaddr

logger = logging.getLogger(__name__)

SECURITY_MODES = {"ssl", "starttls", "none"}
_MAX_ATTEMPTS = 2


def is_valid_email(address: str) -> bool:
    """Small dependency-free mailbox validation suitable for configuration checks."""
    parsed = parseaddr((address or "").strip())[1]
    if parsed != (address or "").strip() or parsed.count("@") != 1:
        return False
    local, domain = parsed.rsplit("@", 1)
    return bool(local and domain and "." in domain and " " not in parsed)


def is_configured(config: dict) -> bool:
    """Return whether the non-secret fields are sufficient to attempt delivery."""
    sender = str(config.get("from_address") or config.get("username") or "").strip()
    recipients = config.get("to_addresses") or []
    return bool(config.get("host") and sender and recipients)


# 常见 SMTP 失败 → 用户能照着改的提示。键为错误文本里的特征片段 (小写匹配)。
_FAILURE_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("535", "authenticationerror", "login fail"),
        "SMTP 认证被拒绝: 请确认已开启邮箱的 SMTP 服务, 并填写「授权码」而不是登录密码",
    ),
    (
        # smtplib 收到 535 后连接被服务端直接关闭, 异常退化为这条消息
        ("connection unexpectedly closed", "serverdisconnected"),
        "SMTP 服务器在认证阶段断开连接: 通常是邮箱未开启 SMTP 服务, 或密码填的不是授权码",
    ),
    (
        ("network is unreachable", "errno 101", "errno 51", "no route to host"),
        "无法连接到 SMTP 主机(网络不可达): 请确认主机填的是服务商 SMTP 地址"
        "(如 smtp.qq.com, 不是 mail.qq.com), 且当前网络允许该端口出网",
    ),
    (
        ("timed out", "timeout"),
        "连接 SMTP 超时: 主机或端口不通, 或该端口被网络/防火墙拦截",
    ),
    (
        ("name or service not known", "getaddrinfo", "nodename nor servname", "temporary failure in name resolution"),
        "SMTP 主机域名解析失败: 请检查主机名是否拼写正确",
    ),
    (
        ("connection refused", "errno 111"),
        "SMTP 端口被拒绝: 请核对端口与加密方式(465=SSL, 587=STARTTLS, 25=无加密)",
    ),
    (
        ("certificate", "ssl"),
        "TLS 握手失败: 加密方式与端口不匹配(465 用 SSL, 587 用 STARTTLS)",
    ),
)


def explain_failure(error: str) -> str:
    """把 SMTP 异常翻译成可操作的提示; 未命中时回退原文。"""
    text = (error or "").strip()
    low = text.lower()
    for keys, hint in _FAILURE_HINTS:
        if any(k in low for k in keys):
            return f"{hint}({text})" if text else hint
    return text or "未知错误"


def send_email(
    config: dict,
    password: str,
    subject: str,
    body: str,
    *,
    max_attempts: int = _MAX_ATTEMPTS,
) -> bool:
    """Send one UTF-8 plain-text email through SSL, STARTTLS, or plain SMTP."""
    ok, _ = _deliver(config, password, subject, body, max_attempts)
    return ok


def send_email_diagnose(
    config: dict,
    password: str,
    subject: str,
    body: str,
) -> tuple[bool, str]:
    """诊断用单次投递, 把失败原因原样带出给设置页。

    生产链路仍走 send_email 的静默重试; 这里只服务「测试」按钮 —— 用户需要看到
    是主机不可达还是认证被拒, 而不是一句「详情见后端日志」。
    返回的 error 不含密码 (SMTP 响应码/网络异常里没有凭据)。
    """
    return _deliver(config, password, subject, body, 1)


def _deliver(
    config: dict,
    password: str,
    subject: str,
    body: str,
    max_attempts: int,
) -> tuple[bool, str]:
    """实际投递逻辑。返回 (是否成功, 最后一次失败原因)。"""
    if not is_configured(config):
        return False, "SMTP 配置不完整(缺少主机/发件人/收件人)"

    host = str(config.get("host") or "").strip()
    try:
        port = int(config.get("port", 465))
    except (TypeError, ValueError):
        return False, "SMTP 端口不是有效数字"
    security = str(config.get("security") or "ssl")
    username = str(config.get("username") or "").strip()
    sender = str(config.get("from_address") or username).strip()
    recipients = [str(item).strip() for item in config.get("to_addresses", [])]
    if (
        not 1 <= port <= 65535
        or security not in SECURITY_MODES
        or not is_valid_email(sender)
        or not recipients
        or any(not is_valid_email(item) for item in recipients)
    ):
        return False, "SMTP 端口/加密方式/发件人/收件人 存在非法值"

    message = EmailMessage()
    message["Subject"] = str(subject or "TickFlow 通知")
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(str(body or ""))

    last_err = ""
    for attempt in range(1, max_attempts + 1):
        smtp = None
        try:
            if security == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=10)
            else:
                smtp = smtplib.SMTP(host, port, timeout=10)
                if security == "starttls":
                    smtp.ehlo()
                    smtp.starttls()
                    smtp.ehlo()
            if username:
                smtp.login(username, password)
            smtp.send_message(message)
            # Delivery already succeeded; a failed QUIT must not retry and duplicate the email.
            try:
                smtp.quit()
            except Exception:
                with suppress(Exception):
                    smtp.close()
            return True, ""
        except Exception as exc:  # SMTP/network errors must not escape
            last_err = f"{type(exc).__name__}: {exc}"
            if smtp is not None:
                with suppress(Exception):
                    smtp.close()
        if attempt < max_attempts:
            time.sleep(1)

    logger.warning("邮件推送最终失败(已尝试 %d 次): %s", max_attempts, last_err)
    return False, last_err
