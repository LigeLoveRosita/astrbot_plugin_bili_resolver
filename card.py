"""把 B 站视频信息 + 热门评论渲染成一张本地图片卡片。

设计要点：
- 纯本地 Pillow 绘制，不依赖外网 t2i 渲染服务，断网/被墙也能出图；
- 图片尺寸自适应内容高度（评论条数变化时自动伸缩）；
- 任何一步失败都向上抛异常，调用方回退到纯文字输出。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from .analysis_bilibili import logger

try:  # Pillow 可能没装，交给调用方回退文字
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover - 运行时才触发
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    ImageFont = None  # type: ignore


# ---------------------------------------------------------------- 主题常量

WIDTH = 900
PAD = 36
INNER = WIDTH - PAD * 2

BG = (255, 255, 255)
INK = (26, 28, 36)
BODY = (52, 55, 66)
SUB = (124, 129, 145)
FAINT = (150, 155, 170)
LINE = (234, 236, 242)
PANEL = (247, 248, 251)
ACCENT = (251, 114, 153)  # B 站粉
ACCENT_SOFT = (255, 236, 243)

COVER_H = 330

# 字体候选（Windows 优先，其次 Linux/macOS 常见中文字体）
_FONT_FILES = {
    "regular": [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyh.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ],
    "bold": [
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\msyhbd.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ],
}

_font_cache: Dict[Tuple[int, bool], Any] = {}
_font_path_cache: Dict[bool, Optional[str]] = {}
_last_cleanup = 0.0


def _font_path(bold: bool) -> Optional[str]:
    if bold in _font_path_cache:
        return _font_path_cache[bold]
    for p in _FONT_FILES["bold" if bold else "regular"]:
        if os.path.isfile(p):
            _font_path_cache[bold] = p
            return p
    _font_path_cache[bold] = None
    return None


def _font(size: int, bold: bool = False) -> Any:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    path = _font_path(bold)
    if path is None:
        raise RuntimeError("找不到可用的中文字体")
    # .ttc 取第一个字面（微软雅黑 Regular/Bold）
    font = ImageFont.truetype(path, size, index=0) if path.lower().endswith(".ttc") else ImageFont.truetype(path, size)
    _font_cache[key] = font
    return font


# ---------------------------------------------------------------- 文本工具

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002190-\U000021FF\U00002600-\U000027BF"
    "\U00002B00-\U00002BFF\U0001F1E6-\U0001F1FF\uFE0F\u200D]"
)
_EMOTE_TAG_RE = re.compile(r"\[[^\[\]\s]{1,12}\]")

# 一个「词」：CJK 单字、连续的西文/数字、空格、其他标点
_TOKEN_RE = re.compile(
    r"[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af]"
    r"|[A-Za-z0-9][A-Za-z0-9_'\-\.:/?=&%#@~]*"
    r"|\s+"
    r"|."
)


def sanitize(text: str) -> str:
    """去掉渲染不出来的 emoji / 表情占位符，压掉多余空白。"""
    if not text:
        return ""
    text = _EMOJI_RE.sub("", str(text))
    text = "".join(ch for ch in text if ord(ch) <= 0xFFFF)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _EMOTE_TAG_RE.sub("", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(text)


def wrap_text(
    draw: Any,
    text: str,
    font: Any,
    max_width: int,
    max_lines: Optional[int] = None,
) -> List[str]:
    """按像素宽度断行，超出 max_lines 时最后一行加省略号。"""
    text = sanitize(text)
    if not text:
        return []

    lines: List[str] = []
    cur = ""
    for tok in _tokens(text):
        if tok == "\n":
            lines.append(cur.rstrip())
            cur = ""
            continue
        cand = cur + tok
        if not cur or draw.textlength(cand, font=font) <= max_width:
            cur = cand
        else:
            lines.append(cur.rstrip())
            cur = "" if tok.isspace() else tok
            if max_lines and len(lines) >= max_lines:
                break
    if cur.strip() or not lines:
        lines.append(cur.rstrip())

    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        ell = "…"
        while last and draw.textlength(last + ell, font=font) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + ell
    return lines


def _draw_multiline(
    draw: Any,
    xy: Tuple[int, int],
    lines: List[str],
    font: Any,
    fill: Tuple[int, int, int],
    line_height: int,
) -> int:
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _pill(
    draw: Any,
    xy: Tuple[int, int],
    text: str,
    font: Any,
    fg: Tuple[int, int, int],
    bg: Optional[Tuple[int, int, int]] = None,
    outline: Optional[Tuple[int, int, int]] = None,
    pad_x: int = 10,
    pad_y: int = 4,
) -> Tuple[int, int]:
    """画一个圆角小标签，返回 (宽, 高)。"""
    tw = int(draw.textlength(text, font=font))
    th = font.size
    w = tw + pad_x * 2
    h = th + pad_y * 2 + 2
    box = [xy[0], xy[1], xy[0] + w, xy[1] + h]
    draw.rounded_rectangle(
        box, radius=h // 2, fill=bg, outline=outline, width=1 if outline else 0
    )
    draw.text(
        (xy[0] + pad_x, xy[1] + h // 2),
        text,
        font=font,
        fill=fg,
        anchor="lm",
    )
    return w, h


# ---------------------------------------------------------------- 图片处理

def _fit_cover(img: Any, w: int, h: int) -> Any:
    img = img.convert("RGB")
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, int(img.width * scale + 0.5)), max(h, int(img.height * scale + 0.5))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _rounded_mask(size: Tuple[int, int], radius: int) -> Any:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size[0] - 1, size[1] - 1], radius=radius, fill=255
    )
    return mask


def _circle_avatar(img: Any, size: int) -> Any:
    img = img.convert("RGB")
    side = min(img.width, img.height)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.crop((left, top, left + side, top + side)).resize(
        (size, size), Image.LANCZOS
    )
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


_AVATAR_COLORS = [
    (251, 114, 153),
    (120, 170, 255),
    (255, 176, 102),
    (128, 200, 160),
    (168, 148, 255),
    (255, 140, 130),
]


def _fallback_avatar(name: str, size: int) -> Any:
    color = _AVATAR_COLORS[sum(ord(c) for c in (name or "?")) % len(_AVATAR_COLORS)]
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(out)
    d.ellipse([0, 0, size - 1, size - 1], fill=color)
    ch = (sanitize(name) or "?")[0]
    d.text((size / 2, size / 2), ch, font=_font(int(size * 0.44), True), fill=(255, 255, 255), anchor="mm")
    return out


# ---------------------------------------------------------------- 组件绘制

def _draw_cover(canvas: Any, draw: Any, y: int, cover_img: Any, info: Dict[str, Any]) -> int:
    radius = 20
    img = _fit_cover(cover_img, INNER, COVER_H)
    canvas.paste(img, (PAD, y), _rounded_mask((INNER, COVER_H), radius))

    # 左上角品牌角标
    badge_font = _font(21, True)
    _pill(draw, (PAD + 16, y + 16), "bilibili", badge_font, (255, 255, 255), ACCENT, pad_x=14, pad_y=5)

    # 右下角时长
    duration = info.get("duration") or ""
    if duration:
        f = _font(20, True)
        tw = int(draw.textlength(duration, font=f))
        bx2, by2 = PAD + INNER - 16, y + COVER_H - 16
        box = [bx2 - tw - 20, by2 - 34, bx2, by2]
        draw.rounded_rectangle(box, radius=10, fill=(18, 18, 22))
        draw.text(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2), duration, font=f, fill=(255, 255, 255), anchor="mm")
    return y + COVER_H


def _draw_stats(draw: Any, y: int, stat: Dict[str, Any], fmt) -> int:
    h = 104
    draw.rounded_rectangle([PAD, y, PAD + INNER, y + h], radius=18, fill=PANEL)
    items = [
        ("播放", stat.get("view", 0)),
        ("点赞", stat.get("like", 0)),
        ("投币", stat.get("coin", 0)),
        ("收藏", stat.get("favorite", 0)),
        ("转发", stat.get("share", 0)),
        ("评论", stat.get("reply", 0)),
    ]
    col_w = INNER / len(items)
    label_font = _font(20)
    for i, (label, value) in enumerate(items):
        cx = PAD + col_w * i + col_w / 2
        draw.text((cx, y + 30), label, font=label_font, fill=SUB, anchor="ma")
        text = fmt(value)
        size = 30
        value_font = _font(size, True)
        while size > 17 and draw.textlength(text, font=value_font) > col_w - 14:
            size -= 1
            value_font = _font(size, True)
        draw.text((cx, y + 58), text, font=value_font, fill=INK, anchor="ma")
    return y + h


def _ellipsize(draw: Any, text: str, font: Any, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _draw_comment(
    canvas: Any,
    draw: Any,
    y: int,
    comment: Dict[str, Any],
    avatar: Any,
    body_width: int,
) -> int:
    avatar_size = 46
    tx = PAD + avatar_size + 16
    if avatar is None:
        avatar = _fallback_avatar(comment.get("uname", "?"), avatar_size)
    canvas.paste(avatar, (PAD, y), avatar)

    name_font = _font(22, True)
    like_font = _font(20)
    tag_font = _font(17, True)

    like_txt = f"赞 {comment.get('like', 0)}"
    like_w = int(draw.textlength(like_txt, font=like_font))

    # 角标占位先算出来，剩下的宽度才是昵称能用的
    badges = []
    if comment.get("is_up"):
        badges.append(("UP主", ACCENT, ACCENT_SOFT))
    if comment.get("pinned"):
        badges.append(("置顶", (255, 255, 255), (250, 180, 90)))
    badge_w = 0
    for label, _fg, _bg in badges:
        badge_w += int(draw.textlength(label, font=tag_font)) + 14 + 6

    name_max = INNER - (avatar_size + 16) - like_w - 18 - badge_w
    name = _ellipsize(
        draw, sanitize(comment.get("uname", "")) or "匿名用户", name_font, name_max
    )
    draw.text((tx, y + 1), name, font=name_font, fill=INK)
    name_w = int(draw.textlength(name, font=name_font))

    tag_x = tx + name_w + 10
    for label, fg, bg in badges:
        w, _ = _pill(draw, (tag_x, y + 6), label, tag_font, fg, bg, pad_x=7, pad_y=2)
        tag_x += w + 6

    # 右侧点赞
    draw.text((PAD + INNER, y + 4), like_txt, font=like_font, fill=FAINT, anchor="ra")

    body_font = _font(23)
    lines = wrap_text(draw, comment.get("message", ""), body_font, body_width, max_lines=3)
    y = _draw_multiline(draw, (tx, y + 34), lines, body_font, BODY, 36)

    for sub in comment.get("replies") or []:
        sub_font = _font(20)
        sub_text = f"{sanitize(sub.get('uname', ''))}：{sub.get('message', '')}"
        sub_lines = wrap_text(draw, sub_text, sub_font, body_width - 8, max_lines=1)
        if sub_lines:
            draw.rounded_rectangle([tx, y + 4, tx + 4, y + 24], radius=2, fill=(226, 229, 238))
            _draw_multiline(draw, (tx + 12, y), sub_lines, sub_font, SUB, 30)
            y += 30

    return y + 20


def _compose(
    info: Dict[str, Any],
    comments: List[Dict[str, Any]],
    total_comments: int,
    cover_img: Any,
    avatars: List[Any],
    fmt,
    with_details: bool = True,
) -> Any:
    canvas = Image.new("RGB", (WIDTH, 6000), BG)
    draw = ImageDraw.Draw(canvas)
    y = PAD

    if with_details:
        if cover_img is not None:
            y = _draw_cover(canvas, draw, y, cover_img, info)
            y += 26

        title_font = _font(35, True)
        title_lines = wrap_text(draw, info.get("title", ""), title_font, INNER, max_lines=2)
        y = _draw_multiline(draw, (PAD, y), title_lines, title_font, INK, 48)
        y += 22

        # UP 主 · 分区 · 发布时间
        meta_font = _font(22)
        x = PAD
        dot_y = y + 15
        draw.ellipse([x, dot_y - 5, x + 10, dot_y + 5], fill=ACCENT)
        x += 20
        up_name = sanitize(info.get("owner", {}).get("name", "")) or "未知UP主"
        draw.text((x, y), up_name, font=_font(23, True), fill=INK)
        x += int(draw.textlength(up_name, font=_font(23, True))) + 10
        meta_pieces = [info.get("tname"), info.get("pubdate")]
        if cover_img is None and info.get("duration"):
            # 没有封面时右上角的时长角标也没了，补到这一行
            meta_pieces.append(f"时长 {info['duration']}")
        for piece in meta_pieces:
            if not piece:
                continue
            draw.text((x, y), "·", font=meta_font, fill=(200, 204, 214))
            x += 14
            piece = sanitize(str(piece))
            draw.text((x, y), piece, font=meta_font, fill=SUB)
            x += int(draw.textlength(piece, font=meta_font)) + 12
        y += 40

        y = _draw_stats(draw, y, info.get("stat", {}), fmt)
        y += 20

        desc_text = (info.get("desc") or "").strip()
        if desc_text == (info.get("title") or "").strip():
            desc_text = ""  # 简介和标题一模一样时不重复占位
        desc_lines = wrap_text(draw, desc_text, _font(22), INNER, max_lines=2)
        if desc_lines:
            y = _draw_multiline(draw, (PAD, y), desc_lines, _font(22), SUB, 34)
            y += 6
    else:
        # 精简版：只保留标题一行作为上下文
        title_font = _font(26, True)
        title_lines = wrap_text(draw, info.get("title", ""), title_font, INNER, max_lines=1)
        y = _draw_multiline(draw, (PAD, y), title_lines, title_font, INK, 38)
        y += 14

    # 分隔线
    y += 8
    draw.line([PAD, y, PAD + INNER, y], fill=LINE, width=1)
    y += 26

    # 评论区标题
    draw.rounded_rectangle([PAD, y + 4, PAD + 5, y + 30], radius=3, fill=ACCENT)
    draw.text((PAD + 18, y), "热门评论", font=_font(27, True), fill=INK)
    if total_comments:
        draw.text(
            (PAD + INNER, y + 8),
            f"共 {fmt(total_comments)} 条",
            font=_font(20),
            fill=FAINT,
            anchor="ra",
        )
    y += 48

    if not comments:
        draw.text((PAD + 2, y), "这个视频还没有可展示的评论", font=_font(23), fill=FAINT)
        y += 44
    else:
        body_width = INNER - 46 - 16
        for i, c in enumerate(comments):
            y = _draw_comment(canvas, draw, y, c, avatars[i] if i < len(avatars) else None, body_width)
            if i < len(comments) - 1:
                draw.line([PAD + 62, y - 10, PAD + INNER, y - 10], fill=(242, 243, 248), width=1)
                y += 6

    # 底部：链接
    y += 6
    draw.line([PAD, y, PAD + INNER, y], fill=LINE, width=1)
    y += 22
    link_font = _font(21)
    bvid = info.get("bvid") or ""
    if bvid:
        _pill(draw, (PAD, y), bvid, _font(19, True), ACCENT, ACCENT_SOFT, pad_x=10, pad_y=3)
    link = info.get("link", "")
    link_max = INNER - (int(draw.textlength(bvid, font=_font(19, True))) + 40 if bvid else 0)
    link_lines = wrap_text(draw, link, link_font, link_max, max_lines=1)
    if link_lines:
        draw.text((PAD + (int(draw.textlength(bvid, font=_font(19, True))) + 40 if bvid else 0), y + 4), link_lines[0], font=link_font, fill=FAINT)
    y += 40

    return canvas.crop((0, 0, WIDTH, min(canvas.height, y + PAD - 12)))


# ---------------------------------------------------------------- 对外接口

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _download_image(session: Any, url: str, referer: str = "https://www.bilibili.com/") -> Any:
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    try:
        async with session.get(
            url,
            headers={"User-Agent": _UA, "Referer": referer},
            timeout=10,
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
        if not data or len(data) > 8 * 1024 * 1024:
            return None
        from io import BytesIO

        img = Image.open(BytesIO(data))
        img.load()
        return img
    except Exception as exc:  # 图片下载/解码失败不应影响整张卡片
        logger.debug(f"bili card 图片处理失败: {url} {exc!r}")
        return None


def _temp_path(prefix: str = "bili") -> str:
    global _last_cleanup
    base = os.path.join(os.environ.get("TEMP") or "/tmp", "astrbot_bili_resolver")
    os.makedirs(base, exist_ok=True)
    now = time.time()
    if now - _last_cleanup > 1800:
        _last_cleanup = now
        try:
            for name in os.listdir(base):
                p = os.path.join(base, name)
                if os.path.isfile(p) and now - os.path.getmtime(p) > 3600:
                    os.remove(p)
        except Exception:
            pass
    return os.path.join(base, f"{prefix}_{int(now * 1000)}_{os.getpid()}.png")


async def build_card(
    session: Any,
    info: Dict[str, Any],
    comments: List[Dict[str, Any]],
    total_comments: int,
    fmt,
    with_details: bool = True,
) -> str:
    """渲染卡片并返回本地 PNG 路径。"""
    if Image is None:
        raise RuntimeError("Pillow 未安装，无法生成图片卡片")

    cover_img = None
    if with_details:
        cover_img = await _download_image(session, info.get("pic", ""))

    avatars: List[Any] = []
    if comments:
        tasks = [
            _download_image(
                session,
                c.get("avatar", ""),
                referer="https://www.bilibili.com/",
            )
            for c in comments
        ]
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        for item in raw:
            if isinstance(item, Exception):
                avatars.append(None)
            elif item is None:
                avatars.append(None)
            else:
                try:
                    avatars.append(_circle_avatar(item, 46))
                except Exception:
                    avatars.append(None)

    loop = asyncio.get_running_loop()
    img = await loop.run_in_executor(
        None, _compose, info, comments, total_comments, cover_img, avatars, fmt, with_details
    )
    path = _temp_path("bili_card")
    await loop.run_in_executor(None, lambda: img.convert("RGB").save(path, "PNG", optimize=True))
    return path
