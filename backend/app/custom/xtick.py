"""XTick 后端扩展: 专用能力 HTTP 端点。

使用后端扩展注册器, 不改 app/main.py。API 层只做参数透传,
归一化、缓存与降级全部在 services.xtick。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services import xtick as xtick_service

EXTENSION_ID = "official.xtick"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION


def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/xtick", tags=["xtick"])

    @router.get("/status")
    def status() -> dict:
        return xtick_service.availability()

    @router.get("/data/{kind}")
    def get_data(kind: str, request: Request) -> dict:
        return xtick_service.get_data(kind, dict(request.query_params))

    registrar.include_router(router)
