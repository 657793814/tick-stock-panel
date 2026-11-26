"""复盘海报文案提取与排版测试。"""
from __future__ import annotations

import io

from PIL import Image

from app.services import recap_poster

_SAMPLE = """## 一句话定调
指数缩量分化，科技主线仍是资金共识。

## 盘面总览
- 沪指收涨 0.42%，创业板指涨 0.86%
- 两市成交额 1.68 万亿
- 半导体与算力硬件领涨

## 风险提示
高位股注意回撤。
"""


def _background() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (600, 900), (12, 20, 38)).save(buf, format="PNG")
    return buf.getvalue()


def test_extract_summary_and_points():
    assert recap_poster.extract_summary(_SAMPLE) == "指数缩量分化，科技主线仍是资金共识。"
    points = recap_poster.extract_points(_SAMPLE, 3)
    assert len(points) == 3
    assert points[0].startswith("沪指收涨")


def test_build_prompt_has_context_and_no_text_instruction():
    prompt = recap_poster.build_image_prompt(
        _SAMPLE, as_of="2026-11-27", emotion_label="偏强", emotion_score=62,
    )
    assert "2026-11-27" in prompt
    assert "偏强" in prompt
    assert "不要生成任何文字" in prompt
    # 明确限定画面主体与构图，避免模型返回纯色/空旷背景。
    assert "K 线" in prompt
    assert "必须出现" in prompt
    assert "纯色" in prompt


def test_render_poster_returns_1080x1620_png():
    out = recap_poster.render_poster(
        _background(),
        content=_SAMPLE,
        as_of="2026-11-27",
        summary="指数缩量分化",
        emotion_label="偏强",
        emotion_score=62,
    )
    assert out.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1080, 1620)


def test_flat_background_gets_fallback_artwork():
    """近乎纯色的底图应被程序化金融主视觉替换，海报中部不再是纯背景色。"""
    out = recap_poster.render_poster(
        _background(),  # 纯色 (12,20,38)
        content=_SAMPLE,
        as_of="2026-11-27",
        emotion_score=62,
    )
    with Image.open(io.BytesIO(out)) as img:
        # 中部主体区裁切后应有足够的亮度方差（K线/天际线细节）。
        mid = img.convert("L").crop((120, 700, 960, 1250)).resize((64, 32))
        pixels = list(mid.getdata())
        mean = sum(pixels) / len(pixels)
        var = sum((p - mean) ** 2 for p in pixels) / len(pixels)
        assert var > 20, f"兜底主视觉细节不足, 方差={var:.1f}"
