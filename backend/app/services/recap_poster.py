"""每日复盘海报：把复盘文本 + 图像模型底图合成为一张可分享的竖版海报。

图像模型只负责生成金融氛围底图（不含文字），中文标题/摘要/要点用 Pillow 叠加，
这样即使模型不擅长中文排版，海报文字依然清晰可读。
"""
from __future__ import annotations

import io
import re
import zlib
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

POSTER_WIDTH = 1080
POSTER_HEIGHT = 1620

_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+(.+?)\s*$")
_MD_NOISE_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)|[*_`>#]")

# 项目内置中文字体优先（随源码一起打包/挂载，Docker  slim 镜像没有任何系统 CJK 字体）。
# 之前容器里找不到系统字体 → _safe_text 把所有中文替换成 '?'，海报全是问号。
_BUNDLED_CJK_FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "wqy-microhei.ttc"

# 其次尝试各平台常见系统字体（本地开发/桌面环境）。
_CJK_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)


@lru_cache(maxsize=1)
def _cjk_font_path() -> str | None:
    if _BUNDLED_CJK_FONT.is_file():
        return str(_BUNDLED_CJK_FONT)
    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _cjk_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size=size, index=0)
        except OSError:
            pass
    return ImageFont.load_default()


def _safe_text(text: str) -> str:
    if _cjk_font_path():
        return text
    # 无中文字体时 bitmap 默认字体无法绘制汉字，退化为 ASCII 占位。
    return "".join(ch if ord(ch) < 128 else "?" for ch in text)


def _draw_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font, fill) -> None:
    draw.text(xy, _safe_text(text), font=font, fill=fill)


def _clean_line(line: str) -> str:
    text = _MD_NOISE_RE.sub(lambda m: m.group(1) if m.group(1) else "", line)
    return re.sub(r"\s+", " ", text).strip()


def extract_summary(content: str, fallback: str = "") -> str:
    """从复盘 markdown 中抓一句话定调；找不到则取首个实质段落。"""
    lines = content.splitlines()
    for idx, line in enumerate(lines):
        if re.search(r"一句话|定调|核心结论|摘要", line) and not _HEADING_RE.match(line):
            candidate = _clean_line(line.lstrip("：: "))
            if 8 <= len(candidate) <= 200:
                return candidate
        if _HEADING_RE.match(line) and re.search(r"一句话|定调|摘要", line):
            for nxt in lines[idx + 1: idx + 5]:
                if nxt.strip() and not _HEADING_RE.match(nxt):
                    candidate = _clean_line(nxt)
                    if 8 <= len(candidate) <= 200:
                        return candidate
    for line in lines:
        if _HEADING_RE.match(line) or not line.strip():
            continue
        candidate = _clean_line(line)
        if len(candidate) >= 12:
            return candidate[:160]
    return _clean_line(fallback)[:160]


def extract_points(content: str, limit: int = 4) -> list[str]:
    """抓复盘要点：优先无序/有序列表，其次章节标题（排除摘要类标题）。"""
    points: list[str] = []
    for line in content.splitlines():
        match = _BULLET_RE.match(line)
        if not match:
            continue
        candidate = _clean_line(match.group(1))
        if 6 <= len(candidate) <= 90 and candidate not in points:
            points.append(candidate)
        if len(points) >= limit:
            return points
    # 列表已够用就直接返回；不足 2 条时才用章节标题补位。
    # 用标题硬凑会把「盘面总览」这种栏目名当成要点，读起来很怪。
    if len(points) >= 2:
        return points
    for line in content.splitlines():
        match = _HEADING_RE.match(line)
        if not match:
            continue
        candidate = _clean_line(match.group(1))
        if re.search(r"一句话|定调|摘要|风险提示|免责|总结", candidate):
            continue
        if 3 <= len(candidate) <= 30 and candidate not in points:
            points.append(candidate)
        if len(points) >= limit:
            break
    return points


def build_image_prompt(
    content: str,
    *,
    as_of: str = "",
    summary: str = "",
    emotion_label: str = "",
    emotion_score: int | None = None,
) -> str:
    """生成有明确画面主体的海报底图提示词。

    关键约束：必须有可辨识的具体画面主体（发光的 K 线主图、城市天际线、
    数据网格等），严禁纯色/纯渐变平铺——否则合成后就是一张没内容的纯背景。
    中文标题与正文由 Pillow 叠加，因此只要求模型不输出任何文字。
    """
    headline = extract_summary(content, summary)
    points = extract_points(content, 3)
    mood = f"{emotion_label}（情绪分 {emotion_score}）" if emotion_score is not None else emotion_label
    return (
        "竖版 2:3 高端财经杂志封面级主视觉，电影级布光，8K 写实渲染，画面饱满有细节。\n"
        f"主题：A 股每日复盘 · {as_of or '最新交易日'}；市场情绪：{mood or '中性'}。\n"
        f"核心叙事：{headline[:120]}。\n"
        f"盘面线索：{('、'.join(points))[:120]}。\n"
        "【必须出现的画面主体】画面中下部是一组发光的红绿 K 线蜡烛与成交量柱状，"
        "叠加一条贯穿画面的上升发光趋势曲线，曲线带粒子光点；"
        "背景是深蓝色摩天大楼城市天际线夜景与玻璃幕墙反光，天空有细腻的网格线、"
        "光带与数据节点，营造出专业金融交易大厅的氛围。主体清晰、层次分明、明暗对比强烈。\n"
        "【构图】画面上方约四分之一与下方约五分之一区域相对简洁、偏暗，"
        "用于后续叠加标题文字；中部为画面主体，细节最丰富。不要大面积空白或纯色块。\n"
        "【配色】深蓝黑底（#070B18）为主，红金高光（#E8433C / #F5B942）强调强势，"
        "青绿（#12B76A）点缀，霓虹光晕与玻璃质感，色彩层次丰富。\n"
        "【严禁】不要生成任何文字、字母、数字、汉字、Logo、水印、坐标轴或图表标签；"
        "不要出现真实股票名称与代码；不要纯色或纯渐变背景、不要空旷无内容的画面。"
    )


def _is_flat_image(image: Image.Image) -> bool:
    """判断底图是否近乎纯色/纯渐变（模型偶发返回空画面时兜底）。"""
    import statistics

    small = image.convert("RGB").resize((64, 64))
    pixels = list(small.getdata())
    # 亮度的标准差 + 边缘变化量：纯色/平滑渐变两者都极低。
    lum = [0.299 * r + 0.587 * g + 0.114 * b for r, g, b in pixels]
    if statistics.pstdev(lum) < 12:
        return True
    edges = 0
    w = 64
    for y in range(63):
        for xx in range(63):
            a = lum[y * w + xx]
            edges += abs(a - lum[y * w + xx + 1]) + abs(a - lum[(y + 1) * w + xx])
    return edges / (63 * 63 * 2) < 1.6


def _fallback_background(accent: tuple[int, int, int], seed: int) -> Image.Image:
    """程序化生成一张有 K 线/网格/光点的金融主视觉，作为纯色底图的兜底。"""
    import random

    rng = random.Random(seed)
    w, h = POSTER_WIDTH, POSTER_HEIGHT
    img = Image.new("RGB", (w, h), (7, 11, 24))
    d = ImageDraw.Draw(img)
    # 深蓝渐变天空
    for y in range(1100):
        t = y / 1100
        d.line((0, y, w, y), fill=(int(7 + 20 * t), int(11 + 26 * t), int(24 + 52 * t)))
    # 城市天际线剪影 + 窗光
    for _ in range(28):
        bx = rng.randint(-40, w)
        bw = rng.randint(50, 120)
        bh = rng.randint(180, 620)
        by = 1100 - bh
        d.rectangle((bx, by, bx + bw, 1100), fill=(12, 20, 42))
        for wy in range(by + 12, 1090, 22):
            for wx in range(bx + 8, bx + bw - 8, 16):
                if rng.random() < 0.26:
                    d.rectangle((wx, wy, wx + 5, wy + 9), fill=rng.choice([
                        (245, 190, 80), (90, 170, 220), (220, 90, 80),
                    ]))
    # 地面网格
    d.rectangle((0, 1100, w, h), fill=(6, 10, 22))
    for y in range(1100, h, 6):
        d.line((0, y, w, y), fill=(10, 16, 32))
    # 发光 K 线 + 成交量
    mid = 820
    candles = []
    for i in range(42):
        cx = 70 + i * 24
        base = mid + rng.randint(-170, 170)
        op = base - rng.randint(10, 60)
        cl = base + rng.randint(-60, 60)
        col = (232, 72, 60) if cl >= op else (24, 170, 110)
        d.line((cx, min(op, cl), cx, max(op, cl)), fill=col, width=2)
        d.rectangle((cx - 7, min(op, cl), cx + 7, max(op, cl)), outline=col, width=2)
        vh = rng.randint(40, 190)
        d.rectangle((cx - 6, 1180 - vh, cx + 6, 1180), fill=(col[0] // 2, col[1] // 2, col[2] // 2))
        candles.append((cx, mid + int(170 * (i / 41) ** 1.4) - 110 + rng.randint(-24, 24)))
    # 趋势曲线 + 光点
    d.line(candles, fill=accent, width=5)
    for px, py in candles[::3]:
        d.ellipse((px - 5, py - 5, px + 5, py + 5), fill=(255, 220, 120))
    return img.filter(ImageFilter.GaussianBlur(0.6))


def _cover_resize(image: Image.Image, width: int, height: int) -> Image.Image:
    src_ratio = image.width / image.height
    dst_ratio = width / height
    if src_ratio > dst_ratio:
        new_h = height
        new_w = round(height * src_ratio)
    else:
        new_w = width
        new_h = round(width / src_ratio)
    resized = image.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _wrap(text: str, font, max_width: int, draw: ImageDraw.ImageDraw, limit: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for ch in text:
        trial = current + ch
        if current and draw.textlength(_safe_text(trial), font=font) > max_width:
            lines.append(current)
            current = ch
            if len(lines) >= limit:
                break
        else:
            current = trial
    if current and len(lines) < limit:
        lines.append(current)
    if lines and len(lines) == limit and len("".join(lines)) < len(text):
        lines[-1] = lines[-1][:-1] + "…"
    return lines


def _emotion_color(score: int | None) -> tuple[int, int, int]:
    # A 股惯例：强=红，弱=绿。
    if score is None:
        return (245, 158, 11)
    if score >= 55:
        return (240, 68, 56)
    if score <= 45:
        return (18, 183, 106)
    return (245, 158, 11)


def prepare_background(
    image_bytes: bytes,
    *,
    as_of: str = "",
    emotion_score: int | None = None,
) -> Image.Image:
    """把模型底图处理成海报背景（纯色兜底 + 压暗 + 暗角），不含任何文字/卡片。

    单独抽出来，既方便测试断言「底图是否有真实细节」，也让排版层可以复用。
    """
    accent = _emotion_color(emotion_score)
    with Image.open(io.BytesIO(image_bytes)) as raw:
        source = raw.convert("RGB")
    # 模型偶发返回近乎纯色的底图时，用程序化金融主视觉兜底，避免"纯背景色"海报。
    # 用稳定哈希做种子：内置 hash() 受 PYTHONHASHSEED 影响，每次进程启动结果不同，
    # 会让同一份复盘在不同机器上生成不同底图（也让测试不可复现）。
    if _is_flat_image(source):
        seed = zlib.crc32((as_of or "tickflow").encode("utf-8"))
        source = _fallback_background(accent, seed=seed)
    base = _cover_resize(source, POSTER_WIDTH, POSTER_HEIGHT).convert("RGBA")

    # 全图轻度压暗，让底图细节仍然可见；上下文字区再叠加更深的渐变。
    veil = Image.new("RGBA", base.size, (0, 0, 0, 0))
    vd = ImageDraw.Draw(veil)
    for y in range(POSTER_HEIGHT):
        t = y / POSTER_HEIGHT
        # 中部(0.30~0.72)仅轻压暗保留画面主体；顶部标题区与底部信息区压暗保证可读。
        if t < 0.30:
            alpha = 150 - int(70 * t / 0.30)      # 顶部 150 → 80
        elif t <= 0.72:
            alpha = 70                              # 主体区轻压暗
        else:
            alpha = 80 + int(95 * (t - 0.72) / 0.28)  # 底部 80 → 175
        vd.line((0, y, POSTER_WIDTH, y), fill=(4, 8, 18, alpha))
    base = Image.alpha_composite(base, veil)

    # 四角柔和暗角，增强纵深感。
    vignette = Image.new("L", base.size, 0)
    vg = ImageDraw.Draw(vignette)
    vg.ellipse((-260, -320, POSTER_WIDTH + 260, POSTER_HEIGHT + 320), fill=255)
    vignette = vignette.filter(ImageFilter.GaussianBlur(150))
    dark = Image.new("RGBA", base.size, (2, 5, 14, 0))
    dark.putalpha(Image.eval(vignette, lambda a: 150 - a))
    base = Image.alpha_composite(base, dark)
    return base


def _frosted_panel(base: Image.Image, box: tuple[int, int, int, int], radius: int,
                   alpha: int = 150) -> Image.Image:
    """毛玻璃卡片：裁切区域先高斯模糊，再叠半透明深色，底图细节透出而不干扰阅读。

    之前用 alpha=205 的实心卡片，把 K 线/天际线几乎完全盖死，合成后就是
    「大片纯色 + 几行字」。这里改为真正磨砂：保留纹理，同时压住高光。
    """
    left, top, right, bottom = box
    region = base.crop(box).filter(ImageFilter.GaussianBlur(18))
    tint = Image.new("RGBA", region.size, (8, 15, 32, alpha))
    panel = Image.alpha_composite(region, tint)

    mask = Image.new("L", region.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, region.width - 1, region.height - 1), radius=radius, fill=255,
    )
    out = base.copy()
    out.paste(panel, (left, top), mask)
    return out


def render_poster(
    image_bytes: bytes,
    *,
    content: str,
    as_of: str = "",
    summary: str = "",
    emotion_label: str = "",
    emotion_score: int | None = None,
) -> bytes:
    """把底图与复盘文字合成为 PNG 海报。

    底图主体（中部）保持可见，只在文字区做局部压暗；用半透明磨砂卡片承载
    文字，避免整张图变成"纯背景色 + 几行字"。
    """
    accent = _emotion_color(emotion_score)
    base = prepare_background(
        image_bytes, as_of=as_of, emotion_score=emotion_score,
    )

    f_brand = _font(24)
    f_title = _font(64, bold=True)
    f_date = _font(28)
    f_badge = _font(26)
    f_section = _font(30, bold=True)
    f_body = _font(30)
    f_small = _font(22)
    f_footer = _font(20)
    x = 82

    # 量字宽需要 draw 对象，此时还没合成，先用 1x1 画布做纯测量。
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))

    # 先把摘要按真实字宽折行，卡片高度跟着行数走：固定 160px 高时，
    # 3 行摘要会溢出卡片、压到「关键要点」标题上。
    summary_text = extract_summary(content, summary) or "市场维持结构性分化，关注主线延续与量能变化。"
    summary_lines = _wrap(summary_text, f_body, 880, measure, 3)

    summary_top, summary_title_y, summary_text_y = 400, 428, 476
    summary_bottom = summary_text_y + len(summary_lines) * 44 + 26
    summary_bottom = max(summary_bottom, 560)

    # 要点卡片：高度固定到底部，条目间距按可用空间均分（条数少时间距自动加大），
    # 避免 3 条时下半张卡片塌成一片空白。
    points = extract_points(content, 4)
    if not points:
        points = ["指数结构分化", "板块主线轮动", "资金情绪变化", "关注量能延续"]
    panel_top, panel_bottom = summary_bottom + 40, 1370
    content_top, content_bottom = panel_top + 92, panel_bottom - 70
    points_gap = 166
    if len(points) > 1:
        points_gap = max(130, min(260, (content_bottom - content_top - 104) // (len(points) - 1)))
    points_top = content_top + max(
        0, (content_bottom - content_top - ((len(points) - 1) * points_gap + 104)) // 2
    )

    # 顶部品牌区横带
    top_band = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(top_band).rectangle((0, 0, POSTER_WIDTH, 130), fill=(6, 12, 26, 110))
    base = Image.alpha_composite(base, top_band)
    # 一句话定调 / 关键要点：磨砂玻璃卡片（透视底图纹理）
    base = _frosted_panel(base, (56, summary_top, 1024, summary_bottom), radius=28, alpha=150)
    base = _frosted_panel(base, (56, panel_top, 1024, panel_bottom), radius=32, alpha=140)

    draw = ImageDraw.Draw(base)

    # 顶部：品牌 + 装饰短线
    _draw_text(draw, (x, 50), "TICKFLOW · AI DAILY REVIEW", f_brand, (176, 194, 222))
    draw.rounded_rectangle((x, 92, x + 104, 98), radius=3, fill=accent + (255,))
    draw.rounded_rectangle((x + 116, 94, x + 152, 96), radius=1, fill=(120, 138, 168, 160))
    _draw_text(draw, (x, 150), "A股每日复盘", f_title, (255, 255, 255))
    _draw_text(draw, (x, 236), as_of or datetime.now().strftime("%Y-%m-%d"), f_date, (188, 204, 228))

    # 情绪徽章（半透明胶囊）
    badge_text = f"市场情绪  {emotion_label or '中性'}"
    if emotion_score is not None:
        badge_text += f"  {emotion_score}"
    badge_w = int(draw.textlength(_safe_text(badge_text), font=f_badge)) + 48
    draw.rounded_rectangle((x, 294, x + badge_w, 350), radius=28, fill=accent + (230,))
    draw.rounded_rectangle((x, 294, x + badge_w, 350), radius=28, outline=(255, 255, 255, 70), width=1)
    _draw_text(draw, (x + 24, 306), badge_text, f_badge, (255, 255, 255))

    # 一句话定调
    _draw_text(draw, (x, summary_title_y), "一句话定调", f_section, accent)
    for i, line in enumerate(summary_lines):
        _draw_text(draw, (x, summary_text_y + i * 44), line, f_body, (246, 249, 253))

    # 关键要点：编号圆点 + 分隔线
    _draw_text(draw, (x, panel_top + 28), "关键要点", f_section, accent)
    for i, point in enumerate(points):
        top = points_top + i * points_gap
        # 编号圆点
        draw.ellipse((x, top + 6, x + 34, top + 40), fill=accent + (235,))
        num = str(i + 1)
        nw = draw.textlength(num, font=f_small)
        draw.text((x + 17 - nw / 2, top + 9), num, font=f_small, fill=(255, 255, 255))
        lines = _wrap(point, f_body, 800, draw, 2)
        for j, line in enumerate(lines):
            _draw_text(draw, (x + 54, top + j * 44), line, f_body, (234, 239, 248))
        if i < len(points) - 1:
            # 分隔线放在两条要点的视觉中点，随自适应间距一起移动
            sep_y = top + points_gap - 62
            draw.line((x + 54, sep_y, 990, sep_y), fill=(90, 104, 132, 90), width=1)

    # 底部分隔线与免责声明：跟随卡片高度下移，避免要点少时中间留一大块空白。
    divider_y = panel_bottom + 40
    draw.line((x, divider_y, 998, divider_y), fill=(120, 138, 168, 90), width=1)
    draw.rounded_rectangle((x, divider_y + 34, 998, divider_y + 98), radius=16, fill=(10, 18, 36, 200))
    _draw_text(draw, (x + 22, divider_y + 52), "仅供研究学习 · 不构成任何投资建议", f_small, (182, 196, 218))
    _draw_text(draw, (x, divider_y + 130), f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}", f_footer, (146, 162, 190))

    out = io.BytesIO()
    base.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


async def generate_recap_poster(
    content: str,
    *,
    as_of: str = "",
    summary: str = "",
    emotion_label: str = "",
    emotion_score: int | None = None,
) -> bytes | None:
    """调用图像模型生成底图并合成海报；未配置模型或失败时返回 None。

    推送链路专用：图片只是增强项，任何失败都不能阻断报告外发。
    """
    import logging

    from app.services.image_provider import generate_image, image_configured

    if not image_configured():
        return None
    logger = logging.getLogger(__name__)
    try:
        prompt = build_image_prompt(
            content,
            as_of=as_of,
            summary=summary,
            emotion_label=emotion_label,
            emotion_score=emotion_score,
        )
        generated = await generate_image(prompt)
        return render_poster(
            generated.content,
            content=content,
            as_of=as_of,
            summary=summary,
            emotion_label=emotion_label,
            emotion_score=emotion_score,
        )
    except Exception as exc:  # noqa: BLE001 — 带图是增强项, 失败降级为纯文本推送
        logger.warning("生成复盘海报失败(推送将仅发文本): %s", exc)
        return None
