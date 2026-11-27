"""东方财富后端扩展: 日频事件数据 (龙虎榜 / 融资融券) 的同步与查询端点。

使用后端扩展注册器, 不改 app/main.py。取数与归一化在
``plugins/eastmoney`` + ``services/eastmoney_sync``, 这里只做参数校验与任务编排。

这些数据不是行情数据集 (不参与能力路由), 落盘后由 ``backtest/fundamentals``
的日频表机制注册为「龙虎榜」「融资融券」因子组。
"""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Request

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.services import eastmoney_sync as sync_service

EXTENSION_ID = "official.eastmoney"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

logger = logging.getLogger(__name__)

_SYNC_LOCK = threading.Lock()
_SYNC_STATE: dict = {"running": False, "table": None, "rows": 0, "error": None}


def setup(registrar: BackendExtensionRegistrar) -> None:
    router = APIRouter(prefix="/api/eastmoney", tags=["eastmoney"])

    def _data_dir(request: Request):
        return request.app.state.repo.store.data_dir

    def _require_table(table: str) -> str:
        if table not in sync_service.TABLES:
            raise HTTPException(status_code=404, detail=f"未知数据表: {table}")
        return table

    @router.get("/status")
    def status(request: Request) -> dict:
        tables = sync_service.table_status(_data_dir(request))
        return {
            "source": "eastmoney",
            "display_name": "东方财富",
            "available": True,  # 公开免 token 源, 恒可用 (不做网络探测)
            "tables": tables,
            "syncing": bool(_SYNC_STATE["running"]),
            "sync": dict(_SYNC_STATE),
        }

    @router.get("/data/{table}")
    def get_data(table: str, request: Request, symbol: str | None = None,
                 limit: int = 200) -> dict:
        _require_table(table)
        frame = sync_service.load_table(_data_dir(request), table)
        if frame.is_empty():
            return {"table": table, "columns": [], "rows": []}
        if symbol:
            frame = frame.filter(frame["symbol"] == symbol)
        limit = max(1, min(int(limit), 5000))
        frame = frame.sort("announce_date", descending=True).head(limit)
        return {
            "table": table,
            "columns": list(frame.columns),
            "rows": frame.to_dicts(),
        }

    @router.post("/sync/{table}")
    def sync(table: str, request: Request, days: int | None = None) -> dict:
        """增量同步 (后台线程); days 缺省补最近 20 个交易日。"""
        _require_table(table)
        if not _SYNC_LOCK.acquire(blocking=False):
            return {"status": "running", "message": "已有同步任务在执行"}
        data_dir = _data_dir(request)
        window = max(1, min(int(days or 20), 400))

        def _run() -> None:
            _SYNC_STATE.update(running=True, table=table, rows=0, error=None)
            try:
                rows = sync_service.sync_table(data_dir, table, days=window)
                _SYNC_STATE["rows"] = rows
            except Exception as exc:  # noqa: BLE001 — 后台线程必须自吞异常
                logger.warning("东方财富 %s 同步失败: %s", table, exc)
                _SYNC_STATE["error"] = type(exc).__name__
            finally:
                _SYNC_STATE["running"] = False
                _SYNC_LOCK.release()

        threading.Thread(target=_run, name=f"eastmoney-{table}-sync", daemon=True).start()
        return {"status": "started", "table": table, "days": window}

    registrar.include_router(router)
