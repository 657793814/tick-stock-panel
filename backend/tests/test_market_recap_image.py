"""复盘海报生成/读取 API 测试。"""
from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from app.api import market_recap
from app.services import image_provider


def _client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(market_recap.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    return TestClient(app)


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (640, 960), (20, 30, 50)).save(buf, format="PNG")
    return buf.getvalue()


def test_generate_image_and_read_back(tmp_path, monkeypatch):
    async def fake_generate(prompt: str, **kwargs):
        assert "2026-11-27" in prompt
        return image_provider.GeneratedImage(content=_png(), mime="image/png", ext="png")

    monkeypatch.setattr(image_provider, "image_configured", lambda: True)
    monkeypatch.setattr(image_provider, "generate_image", fake_generate)

    c = _client(tmp_path)
    r = c.post("/api/market-recap/generate-image", json={
        "as_of": "2026-11-27",
        "content": "## 一句话定调\n指数缩量分化。\n\n- 沪指收涨\n- 半导体领涨",
        "emotion_label": "偏强",
        "emotion_score": 62,
    })
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["ok"] is True
    assert payload["url"].startswith("/api/market-recap/images/")
    assert (tmp_path / "recap_images" / payload["name"]).is_file()

    image_res = c.get(payload["url"])
    assert image_res.status_code == 200
    assert image_res.headers["content-type"].startswith("image/png")
    assert image_res.content.startswith(b"\x89PNG")


def test_generate_image_requires_model_and_content(tmp_path, monkeypatch):
    c = _client(tmp_path)
    monkeypatch.setattr(image_provider, "image_configured", lambda: False)
    r = c.post("/api/market-recap/generate-image", json={"content": "复盘"})
    assert r.status_code == 400
    assert "生图模型" in r.json()["detail"]

    monkeypatch.setattr(image_provider, "image_configured", lambda: True)
    r = c.post("/api/market-recap/generate-image", json={"content": "  "})
    assert r.status_code == 400


def test_image_name_traversal_rejected(tmp_path):
    c = _client(tmp_path)
    assert c.get("/api/market-recap/images/..%2F..%2Fsecrets.json").status_code == 404
    assert c.get("/api/market-recap/images/not-a-png.txt").status_code == 404
