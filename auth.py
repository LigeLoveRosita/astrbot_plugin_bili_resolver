"""B 站扫码登录 + Cookie 持久化。

登录态存在 AstrBot 的 plugin_data 目录里（bili_login.json），插件重载/重启后
自动复用；配置里手填的 `bili_cookie` 优先级更高（方便临时覆盖）。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .analysis_bilibili import BILI_UA, logger

QR_GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_API = "https://api.bilibili.com/x/web-interface/nav"

COOKIE_FILENAME = "bili_login.json"
# 登录成功后需要保留的 cookie 字段
_COOKIE_KEYS = (
    "SESSDATA",
    "bili_jct",
    "DedeUserID",
    "DedeUserID__ckMd5",
    "sid",
    "buvid3",
)

# 扫码状态码
POLL_SUCCESS = 0
POLL_NOT_SCANNED = 86101
POLL_SCANNED = 86090
POLL_EXPIRED = 86038


def data_dir() -> str:
    """插件数据目录（AstrBot 的 data/plugin_data/<插件名>）。"""
    try:
        from astrbot.core.utils.astrbot_path import (  # type: ignore
            get_astrbot_plugin_data_path,
        )

        base = os.path.join(
            get_astrbot_plugin_data_path(), "astrbot_plugin_bili_resolver"
        )
    except Exception:
        here = os.path.dirname(os.path.abspath(__file__))
        base = os.path.join(
            os.path.dirname(os.path.dirname(here)),
            "plugin_data",
            "astrbot_plugin_bili_resolver",
        )
    os.makedirs(base, exist_ok=True)
    return base


def cookie_path() -> str:
    return os.path.join(data_dir(), COOKIE_FILENAME)


def load_login() -> Dict[str, Any]:
    path = cookie_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, dict) and data.get("cookie"):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"读取 B 站登录状态失败: {exc!r}")
    return {}


def save_login(cookie: str, info: Optional[Dict[str, Any]] = None) -> None:
    info = info or {}
    payload = {
        "cookie": cookie,
        "saved_at": int(time.time()),
        "uname": info.get("uname", ""),
        "mid": info.get("mid", 0),
        "vip": bool(info.get("vip", False)),
        "vip_label": info.get("vip_label", ""),
    }
    try:
        with open(cookie_path(), "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.error(f"保存 B 站登录状态失败: {exc!r}")


def clear_login() -> None:
    try:
        os.remove(cookie_path())
    except OSError:
        pass


def effective_cookie(config_cookie: str = "") -> str:
    """配置里手填的优先，其次用扫码登录保存的。"""
    config_cookie = (config_cookie or "").strip()
    if config_cookie:
        return config_cookie
    return str(load_login().get("cookie") or "")


def _cookie_from_url(url: str) -> str:
    """扫码成功返回的 url 里带着登录 cookie 参数。"""
    try:
        query = parse_qs(urlparse(url).query)
    except Exception:
        return ""
    parts = []
    for key in _COOKIE_KEYS:
        values = query.get(key)
        if values:
            parts.append(f"{key}={values[0]}")
    return "; ".join(parts)


def _cookie_from_jar(session: Any) -> str:
    parts = []
    try:
        for cookie in session.cookie_jar:
            if "bilibili" in (cookie["domain"] or ""):
                parts.append(f"{cookie.key}={cookie.value}")
    except Exception:
        return ""
    return "; ".join(parts)


async def generate_qr(session: Any) -> Dict[str, str]:
    async with session.get(
        QR_GEN, headers={"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    ) as resp:
        payload = await resp.json(content_type=None)
    if payload.get("code") != 0:
        raise RuntimeError(f"获取二维码失败: {payload.get('message')}")
    data = payload.get("data") or {}
    return {"url": data.get("url", ""), "qrcode_key": data.get("qrcode_key", "")}


async def poll_qr(
    session: Any, qrcode_key: str
) -> Tuple[int, str, str]:
    """返回 (状态码, 提示, cookie)。"""
    async with session.get(
        QR_POLL,
        params={"qrcode_key": qrcode_key},
        headers={"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"},
    ) as resp:
        payload = await resp.json(content_type=None)
    data = payload.get("data") or {}
    code = int(data.get("code", -1))
    message = data.get("message", "") or payload.get("message", "")
    cookie = ""
    if code == POLL_SUCCESS:
        cookie = _cookie_from_url(data.get("url", "")) or _cookie_from_jar(session)
    return code, message, cookie


def make_qr_png(text: str) -> str:
    """把登录链接渲染成二维码图片，返回本地路径。"""
    import qrcode

    qr = qrcode.QRCode(border=2, box_size=10)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#111111", back_color="#ffffff").convert("RGB")
    # 加一圈白边，聊天里更好扫
    from PIL import Image, ImageOps

    img = ImageOps.expand(img, border=16, fill="#ffffff")
    path = os.path.join(
        os.environ.get("TEMP") or "/tmp",
        f"bili_login_qr_{int(time.time())}.png",
    )
    img.save(path, "PNG")
    return path


async def fetch_nav(session: Any, cookie: str) -> Dict[str, Any]:
    """用 cookie 查登录状态 / 大会员信息。"""
    headers = {"User-Agent": BILI_UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie
    try:
        async with session.get(NAV_API, headers=headers) as resp:
            payload = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning(f"查询 B 站登录状态失败: {exc!r}")
        return {"is_login": False, "error": str(exc)}

    data = payload.get("data") or {}
    is_login = bool(data.get("isLogin"))
    vip_status = int(data.get("vipStatus") or 0)
    vip_type = int(data.get("vipType") or 0)
    if vip_status and vip_type:
        vip_label = "年度大会员" if vip_type == 2 else "大会员"
    else:
        vip_label = ""
    return {
        "is_login": is_login,
        "uname": data.get("uname", ""),
        "mid": data.get("mid", 0),
        "level": (data.get("level_info") or {}).get("current_level", 0),
        "vip": bool(vip_status),
        "vip_type": vip_type,
        "vip_label": vip_label,
        "code": payload.get("code"),
    }


async def check_login(session: Any, cookie: str) -> Dict[str, Any]:
    if not cookie:
        return {"is_login": False}
    return await fetch_nav(session, cookie)


async def wait_login(
    session: Any,
    qrcode_key: str,
    timeout: float = 180.0,
    interval: float = 2.0,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """轮询扫码状态。

    Returns:
        (info, cookie, 状态说明) —— info 为空表示没登上。
    """
    deadline = time.time() + timeout
    scanned = False
    while time.time() < deadline:
        try:
            code, message, cookie = await poll_qr(session, qrcode_key)
        except Exception as exc:
            logger.warning(f"轮询扫码状态失败: {exc!r}")
            await asyncio.sleep(interval)
            continue

        if code == POLL_SUCCESS and cookie:
            info = await fetch_nav(session, cookie)
            return info, cookie, "ok"
        if code == POLL_EXPIRED:
            return None, "", "二维码已失效，请重新发送 /b站登录"
        if code == POLL_SCANNED and not scanned:
            scanned = True
        await asyncio.sleep(interval)
    return None, "", "等待扫码超时，请重新发送 /b站登录"


def describe_login(login: Dict[str, Any]) -> str:
    if not login or not login.get("is_login"):
        return "未登录（3 分钟内只能看 720P，4K 需要登录 + 大会员）"
    vip = login.get("vip_label") or "普通用户"
    return f"{login.get('uname', '')}（UID {login.get('mid', '')}，{vip}）"
