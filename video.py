"""B 站视频流下载。

两条路：
- `durl`（progressive MP4）：音轨已封装在同一个文件里，不需要 ffmpeg，清晰度通常到 1080P；
- `dash`（分离音视频）：4K / 1080P+ 只有这种，需要 ffmpeg 合轨。

ffmpeg 不是硬依赖：只有要下 4K/DASH 时才用得到，找不到时会自动降级到 1080P durl。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from typing import Any, Dict, List, Optional

from aiohttp import ClientTimeout

from .analysis_bilibili import BILI_UA, logger

QUALITY_QN = {
    "360P": 16,
    "480P": 32,
    "720P": 64,
    "1080P": 80,
    "1080P+": 112,
    "4K": 120,
}
QUALITY_LABEL = {
    6: "240P",
    16: "360P",
    32: "480P",
    64: "720P",
    74: "720P60",
    80: "1080P",
    112: "1080P+",
    116: "1080P60",
    120: "4K",
}
# 这些清晰度只能走 DASH
DASH_ONLY = {"1080P+", "4K"}

_DOWNLOAD_TIMEOUT = ClientTimeout(total=None, connect=20, sock_read=120)
_PROBE_TIMEOUT = ClientTimeout(total=25)

_last_cleanup = 0.0


class FFmpegMissing(RuntimeError):
    """需要合轨但找不到 ffmpeg。"""


class SizeLimit(ValueError):
    """文件超过体积上限。size 为探测到的字节数（未知时为 0）。"""

    def __init__(self, size: int = 0, quality_label: str = ""):
        super().__init__("SIZE_LIMIT")
        self.size = int(size or 0)
        self.quality_label = quality_label


def temp_dir() -> str:
    base = os.path.join(
        os.environ.get("TEMP") or "/tmp", "astrbot_bili_resolver", "video"
    )
    os.makedirs(base, exist_ok=True)
    return base


def cleanup_old_files(max_age: int = 3600) -> None:
    global _last_cleanup
    now = time.time()
    if now - _last_cleanup < 600:
        return
    _last_cleanup = now
    base = temp_dir()
    try:
        for name in os.listdir(base):
            path = os.path.join(base, name)
            if os.path.isfile(path) and now - os.path.getmtime(path) > max_age:
                os.remove(path)
    except OSError:
        pass


def _headers(cookie: str, referer: str) -> Dict[str, str]:
    headers = {"User-Agent": BILI_UA, "Referer": referer}
    if cookie:
        headers["Cookie"] = cookie
    return headers


# ---------------------------------------------------------------- ffmpeg

_ffmpeg_cache: Optional[str] = None


def find_ffmpeg(configured: str = "") -> Optional[str]:
    """找 ffmpeg：配置路径 > PATH > winget 安装目录 > 常见目录。"""
    global _ffmpeg_cache
    if configured and os.path.isfile(configured):
        return configured
    if _ffmpeg_cache and os.path.isfile(_ffmpeg_cache):
        return _ffmpeg_cache

    found = shutil.which("ffmpeg")
    if found:
        _ffmpeg_cache = found
        return found

    candidates: List[str] = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        candidates.append(
            os.path.join(local, "Microsoft", "WinGet", "Links", "ffmpeg.exe")
        )
        packages = os.path.join(local, "Microsoft", "WinGet", "Packages")
        try:
            for name in os.listdir(packages):
                if "ffmpeg" in name.lower():
                    for dirpath, _dirnames, filenames in os.walk(
                        os.path.join(packages, name)
                    ):
                        if "ffmpeg.exe" in filenames:
                            candidates.append(os.path.join(dirpath, "ffmpeg.exe"))
        except OSError:
            pass
        candidates.append(
            os.path.join(local, "Programs", "ffmpeg", "bin", "ffmpeg.exe")
        )
    candidates.extend(
        [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
        ]
    )
    for path in candidates:
        if os.path.isfile(path):
            _ffmpeg_cache = path
            return path
    return None


async def merge_av(
    video_path: str, audio_path: str, out_path: str, ffmpeg: str
) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        video_path,
        "-i",
        audio_path,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("ffmpeg 合轨超时")
    if proc.returncode != 0 or not os.path.isfile(out_path):
        msg = (stderr or b"").decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"ffmpeg 合轨失败：{msg}")


# ---------------------------------------------------------------- 取流

async def fetch_play_url(
    session: Any,
    bvid: str,
    cid: int,
    quality: str = "480P",
    cookie: str = "",
) -> Dict[str, Any]:
    """取播放地址。需要 4K/1080P+ 时返回 dash 结构。"""
    qn = QUALITY_QN.get(quality, 32)
    want_dash = quality in DASH_ONLY
    params = {
        "bvid": bvid,
        "cid": cid,
        "qn": qn,
        "fnval": 4048 if want_dash else 0,
        "fourk": 1,
    }
    if not want_dash:
        # html5 端只为拿 progressive mp4；DASH 请求带上它反而会被限制在 1080P
        params.update(platform="html5", high_quality=1)
    referer = f"https://www.bilibili.com/video/{bvid}"
    async with session.get(
        "https://api.bilibili.com/x/player/playurl",
        params=params,
        headers=_headers(cookie, referer),
        timeout=_PROBE_TIMEOUT,
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"取流接口 HTTP {resp.status}")
        payload = await resp.json(content_type=None)

    code = payload.get("code")
    if code != 0:
        raise RuntimeError(
            f"取流失败（code={code}）：{payload.get('message') or '未知错误'}"
        )

    data = payload.get("data") or {}
    if want_dash:
        dash = data.get("dash") or {}
        videos = dash.get("video") or []
        audios = dash.get("audio") or []
        if not videos:
            raise RuntimeError(
                "这个视频没有 4K/DASH 流（可能不是大会员账号，或视频本身没有高码率）"
            )
        candidates = [v for v in videos if int(v.get("id") or 0) <= qn]
        if candidates:
            pick = max(candidates, key=lambda v: int(v.get("id") or 0))
        else:
            pick = max(videos, key=lambda v: int(v.get("id") or 0))
        audio = max(audios, key=lambda a: int(a.get("bandwidth") or 0)) if audios else None
        qid = int(pick.get("id") or 0)
        return {
            "mode": "dash",
            "quality": qid,
            "quality_label": QUALITY_LABEL.get(qid, str(qid)),
            "video_url": pick.get("baseUrl") or pick.get("base_url") or "",
            "video_backup": pick.get("backupUrl") or pick.get("backup_url") or [],
            "audio_url": (audio.get("baseUrl") or audio.get("base_url")) if audio else "",
            "audio_backup": (audio.get("backupUrl") or audio.get("backup_url") or []) if audio else [],
            "width": pick.get("width"),
            "height": pick.get("height"),
            "fps": pick.get("frameRate"),
            "duration": round(float(dash.get("duration") or 0), 1),
            "size": 0,
        }

    durl = data.get("durl") or []
    if len(durl) > 1:
        raise RuntimeError("视频过长，B 站把它切成了多段，本插件暂不合并多段流")
    if not durl:
        raise RuntimeError("这个视频没有可直接下载的 MP4 流（可能是付费内容）")
    seg = durl[0]
    length_ms = int(seg.get("length") or 0)
    qid = int(data.get("quality") or 0)
    return {
        "mode": "durl",
        "quality": qid,
        "quality_label": QUALITY_LABEL.get(qid, "未知"),
        "url": seg.get("url") or "",
        "backup": [u for u in (seg.get("backup_url") or []) if u],
        "size": int(seg.get("size") or 0),
        "duration": round(length_ms / 1000, 1) if length_ms else 0,
    }


async def _probe_size(session: Any, url: str, headers: Dict[str, str]) -> int:
    """用 Range 请求探文件总大小（B 站 CDN 对 HEAD 支持不稳）。"""
    if not url:
        return 0
    try:
        async with session.get(
            url,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=_PROBE_TIMEOUT,
        ) as resp:
            content_range = resp.headers.get("Content-Range") or ""
            if "/" in content_range:
                total = content_range.rsplit("/", 1)[-1].strip()
                if total.isdigit():
                    return int(total)
            length = resp.headers.get("Content-Length")
            if length and length.isdigit():
                return int(length)
    except Exception as exc:
        logger.debug(f"探测视频大小失败: {exc!r}")
    return 0


async def probe_total(
    session: Any, play: Dict[str, Any], cookie: str = "", bvid: str = ""
) -> int:
    referer = f"https://www.bilibili.com/video/{bvid}"
    headers = _headers(cookie, referer)
    if play["mode"] == "durl":
        if not play.get("size"):
            play["size"] = await _probe_size(session, play.get("url", ""), headers)
        return int(play.get("size") or 0)

    video_size = await _probe_size(session, play.get("video_url", ""), headers)
    audio_size = await _probe_size(session, play.get("audio_url", ""), headers)
    play["size"] = video_size + audio_size
    return int(play["size"])


# ---------------------------------------------------------------- 下载

async def _download_to(
    session: Any,
    urls: List[str],
    dest: str,
    headers: Dict[str, str],
    max_bytes: int,
) -> int:
    last_error: Optional[Exception] = None
    for url in urls:
        if not url:
            continue
        written = 0
        try:
            async with session.get(
                url, headers=headers, timeout=_DOWNLOAD_TIMEOUT
            ) as resp:
                if resp.status not in (200, 206):
                    raise RuntimeError(f"HTTP {resp.status}")
                with open(dest, "wb") as fp:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise SizeLimit(written)
                        fp.write(chunk)
            if written < 1024:
                raise RuntimeError("下载到的文件过小")
            return written
        except ValueError:
            _safe_remove(dest)
            raise
        except Exception as exc:
            last_error = exc
            _safe_remove(dest)
            continue
    raise RuntimeError(f"视频下载失败：{last_error!r}")


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


async def prepare(
    session: Any,
    bvid: str,
    cid: int,
    quality: str,
    max_bytes: int,
    cookie: str = "",
    ffmpeg: str = "",
) -> Dict[str, Any]:
    """取流 + 下载（+ 合轨）。

    4K 需要 ffmpeg；找不到时抛 FFmpegMissing，由调用方决定是否降级。
    """
    play = await fetch_play_url(session, bvid, cid, quality, cookie)
    await probe_total(session, play, cookie, bvid)
    # max_bytes <= 0 表示不限制体积
    if max_bytes and play.get("size") and int(play["size"]) > max_bytes:
        raise SizeLimit(int(play["size"]), play.get("quality_label", ""))

    referer = f"https://www.bilibili.com/video/{bvid}"
    headers = _headers(cookie, referer)
    stamp = f"{bvid}_{cid}_{int(time.time())}"
    base = temp_dir()

    if play["mode"] == "durl":
        dest = os.path.join(base, f"bili_{stamp}.mp4")
        written = await _download_to(
            session,
            [play["url"], *(play.get("backup") or [])],
            dest,
            headers,
            max_bytes,
        )
        play["path"] = dest
        play["size"] = written
        return play

    exe = find_ffmpeg(ffmpeg)
    if not exe:
        raise FFmpegMissing("4K/1080P+ 需要 ffmpeg 合轨，但没找到 ffmpeg")

    v_path = os.path.join(base, f"v_{stamp}.m4s")
    a_path = os.path.join(base, f"a_{stamp}.m4s")
    out_path = os.path.join(base, f"bili_{stamp}.mp4")
    try:
        written = await _download_to(
            session,
            [play["video_url"], *(play.get("video_backup") or [])],
            v_path,
            headers,
            max_bytes,
        )
        if play.get("audio_url"):
            written += await _download_to(
                session,
                [play["audio_url"], *(play.get("audio_backup") or [])],
                a_path,
                headers,
                max_bytes,
            )
            await merge_av(v_path, a_path, out_path, exe)
        else:
            os.replace(v_path, out_path)
        if not os.path.isfile(out_path):
            raise RuntimeError("合轨后没有生成文件")
        play["path"] = out_path
        play["size"] = os.path.getsize(out_path) or written
        return play
    finally:
        _safe_remove(v_path)
        _safe_remove(a_path)


def human_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}GB"


def human_duration(seconds: float) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}分{seconds % 60:02d}秒"
