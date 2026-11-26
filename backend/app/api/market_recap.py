"""AI 大盘复盘 API — 流式复盘 + 报告持久化 + 龙虎榜 + 盘前风向标。

路由前缀: /api/market-recap

端点:
  POST /analyze                AI 流式大盘复盘(NDJSON)
  POST /generate-image         复盘内容生成精美海报 PNG
  GET  /images/{name}          读取生成的海报图片
  GET  /reports                历史复盘列表
  POST /reports                保存一条复盘报告
  DELETE /reports/{report_id}  删除一条复盘报告
  GET  /dragon-tiger           龙虎榜三榜 (fuyao 专有, 历史按日缓存)
  GET  /auction-benchmark      盘前风向标 (fuyao 专有, 含当日/次日真实收益)
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date as date_cls
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.services import auction_benchmark, dragon_tiger, market_recap_reports, preferences
from app.services.market_recap import recap_market_stream
from app.services.ndjson_heartbeat import with_heartbeat

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/market-recap", tags=["market-recap"])


class AnalyzeRequest(BaseModel):
    """AI 大盘复盘请求。"""
    as_of: str | None = None  # 可选:复盘日期(YYYY-MM-DD),缺省取最新有数据日
    focus: str = ""           # 可选:用户追加的复盘关注点
    market: str = "cn"        # cn | hk | us（默认 cn）


@router.get("/dragon-tiger")
def get_dragon_tiger(
    request: Request,
    date: str | None = Query(default=None, description="复盘目标日 YYYY-MM-DD, 缺省取最近已发布交易日"),
):
    """龙虎榜三榜 (全部/机构/游资)。fuyao 专有, 未配置时 state=source_unavailable。

    非交易日/当日未发布由服务层自动回退到上一交易日 (state=fallback_prev)。
    """
    target = None
    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date 格式应为 YYYY-MM-DD, 收到: {date}")
    return dragon_tiger.get_dragon_tiger(
        request.app.state.repo.store.data_dir, target
    )


@router.get("/auction-benchmark")
def get_auction_benchmark(
    request: Request,
    date: str | None = Query(default=None, description="复盘目标日 YYYY-MM-DD, 缺省取最近交易日"),
):
    """盘前风向标 (同花顺竞价筛选名单 + 当日/次日真实收益)。

    fuyao 专有, 未配置时 state=source_unavailable; 非交易日由服务层自动回退。
    """
    target = None
    if date:
        try:
            target = date_cls.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date 格式应为 YYYY-MM-DD, 收到: {date}")
    return auction_benchmark.get_auction_benchmark(
        request.app.state.repo.store.data_dir, target
    )


@router.post("/analyze")
async def analyze_market(request: Request, req: AnalyzeRequest):
    """AI 大盘复盘 — NDJSON 流式返回。

    装配市场总览(指数/涨跌/连板/封板/板块/情绪雷达)→ 复盘提示词 →
    流式调用 LLM → 逐 chunk 以 NDJSON 推给前端(每行一个 JSON)。

    协议:
      {"type":"meta","as_of","emotion_score","emotion_label","summary"}
      {"type":"delta","content":"..."}
      {"type":"error","message":"..."}
      {"type":"done"}
    """
    import json
    from datetime import date as date_cls

    repo = request.app.state.repo
    quote_service = getattr(request.app.state, "quote_service", None)
    depth_service = getattr(request.app.state, "depth_service", None)

    # 港美股复盘门控（方案 §5.7）：不输出 A 股涨停/连板叙事。返回明确的
    # NDJSON gate，让前端提示用户该市场暂不支持 AI 复盘，而不是给错数据。
    if req.market in ("hk", "us"):
        async def market_gate():
            yield json.dumps({
                "type": "error",
                "code": "market_not_supported",
                "message": f"{req.market.upper()} 市场暂不支持 AI 大盘复盘（A 股涨停/连板口径不适用）",
                "market": req.market,
            }, ensure_ascii=False) + "\n"
            yield json.dumps({"type": "done", "market": req.market}, ensure_ascii=False) + "\n"

        return StreamingResponse(
            market_gate(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    as_of = None
    if req.as_of:
        try:
            as_of = date_cls.fromisoformat(req.as_of)
        except ValueError:
            raise HTTPException(400, f"as_of 格式应为 YYYY-MM-DD,收到: {req.as_of}")

    async def stream_gen():
        async for chunk in with_heartbeat(
            recap_market_stream(repo, quote_service, depth_service, as_of, req.focus),
        ):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ================================================================
# 复盘海报(图像模型生成氛围底图 + 服务端叠加清晰中文排版)
# ================================================================

_IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.png$")
_RECAP_IMAGE_KEEP = 30


def _recap_image_dir(request: Request) -> Path:
    path = Path(request.app.state.repo.store.data_dir) / "recap_images"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _prune_recap_images(directory: Path, keep: int = _RECAP_IMAGE_KEEP) -> None:
    files = sorted(directory.glob("*.png"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError as exc:  # noqa: BLE001 — 清理失败不影响生图主流程
            logger.warning("prune recap image failed: %s", exc)


class GenerateImageRequest(BaseModel):
    """根据复盘正文生成海报。"""
    as_of: str = ""
    content: str = Field(default="", max_length=40000)
    summary: str = Field(default="", max_length=1000)
    emotion_label: str = Field(default="", max_length=40)
    emotion_score: int | None = None


@router.post("/generate-image")
async def generate_recap_image(request: Request, req: GenerateImageRequest):
    """调用已配置的图像模型生成底图，再叠加复盘标题/摘要/要点输出 PNG 海报。"""
    from app.services.image_provider import ImageGenerationError, generate_image, image_configured
    from app.services.recap_poster import build_image_prompt, render_poster

    if not image_configured():
        raise HTTPException(
            status_code=400,
            detail="尚未配置生图模型：请到「设置 · AI 服务 · 图像模型」填写模型名称与接口地址",
        )
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="复盘内容为空，无法生成海报")

    prompt = build_image_prompt(
        req.content,
        as_of=req.as_of,
        summary=req.summary,
        emotion_label=req.emotion_label,
        emotion_score=req.emotion_score,
    )
    try:
        generated = await generate_image(prompt)
        poster = render_poster(
            generated.content,
            content=req.content,
            as_of=req.as_of,
            summary=req.summary,
            emotion_label=req.emotion_label,
            emotion_score=req.emotion_score,
        )
    except ImageGenerationError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 海报排版失败时给出可操作提示,不暴露堆栈
        logger.exception("render recap poster failed")
        raise HTTPException(status_code=500, detail=f"海报生成失败: {exc}") from exc

    directory = _recap_image_dir(request)
    name = f"recap-{uuid.uuid4().hex[:16]}.png"
    (directory / name).write_bytes(poster)
    _prune_recap_images(directory)
    return {
        "ok": True,
        "id": name.removesuffix(".png"),
        "name": name,
        "url": f"/api/market-recap/images/{name}",
        "width": 1080,
        "height": 1620,
    }


@router.get("/images/{name}")
def get_recap_image(request: Request, name: str):
    """读取生成的海报 PNG，严格校验文件名避免目录穿越。"""
    if not _IMAGE_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=404, detail="图片不存在")
    path = _recap_image_dir(request) / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="图片不存在")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ================================================================
# 报告 CRUD(历史复盘持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 大盘复盘报告。"""
    as_of: str
    focus: str = ""
    content: str
    summary: str = ""
    emotion_score: int | None = None
    emotion_label: str = ""
    push: bool = False  # 是否显式外发推送(manual 模式下需显式传 true)


@router.get("/reports")
def list_reports(request: Request):
    """获取全部历史复盘(按时间降序,后端已裁剪到上限)。"""
    return {"reports": market_recap_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条复盘报告。req.push=True 或 review_push_mode=auto 时才推送到外部渠道。"""
    report = market_recap_reports.save_report({
        "as_of": req.as_of,
        "focus": req.focus,
        "content": req.content,
        "summary": req.summary,
        "emotion_score": req.emotion_score,
        "emotion_label": req.emotion_label,
    })
    # 推送门控: manual 模式需显式 push=True; auto 模式保持归档即推。
    # 内部 try/except 静默降级, 不影响归档返回值。
    if req.push or preferences.get_review_push_mode() == "auto":
        from app.jobs.daily_pipeline import _maybe_push_review
        _maybe_push_review(req.content, {
            "as_of": req.as_of,
            "emotion_label": req.emotion_label,
        })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条复盘报告。"""
    ok = market_recap_reports.delete_report(report_id)
    return {"ok": ok}
