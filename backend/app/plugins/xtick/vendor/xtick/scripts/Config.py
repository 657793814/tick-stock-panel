# ruff: noqa
"""Vendored XTick 配置 shim。

来源: github.com/xticktop/skills @ 05087c5 (MIT)。
仅 SERVER_URL 保留上游默认值; 真实 base URL 由 app.plugins.xtick.client 在运行时注入。
上游仅提供 HTTP 端点 (HTTPS 返回 404), token 走 query string, 禁止写入日志。
"""


class Config:
    SERVER_URL = "http://api.xtick.top"  # Api调用服务地址
    TOKEN = ""  # 登录XTick官网，获取token
