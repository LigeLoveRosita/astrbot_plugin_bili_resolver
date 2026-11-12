import re
import os
import json
import time
import asyncio
import urllib.parse
from pathlib import PurePosixPath
from typing import List, Optional, Set, Union

import aiohttp
from aiohttp import ClientSession, ClientTimeout
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import astrbot.api.message_components as Comp
from astrbot.core.message.message_event_result import MessageChain

from . import analysis_bilibili
from . import auth as bili_auth
from . import card as bili_card
from . import video as bili_video
from .analysis_bilibili import (
    b23_extract,
    bili_keyword,
    check_duplicate,
    extract,
    fetch_comments,
    format_comments_text,
    format_video_text,
    handle_num,
    search_bili_by_title,
    video_info,
)

TEMPLATE_PRESET_EMOJI = (
    "🎬 标题：${标题}\n"
    "👤 UP主：${UP主}\n"
    "📝 简介：${简介}\n"
    "${封面}\n"
    "👍 点赞：${点赞} 🪙 投币：${投币}\n"
    "❤️ 收藏：${收藏} 🔄 转发：${转发}\n"
    "👀 观看：${观看} 💬 弹幕：${弹幕数量}"
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36 Edg/116.0.1938.69"
}

DEFAULT_TIMEOUT = ClientTimeout(total=15)

BILI_PATTERN = re.compile(
    r"(b23\.tv)|(bili(22|23|33|2233)\.cn)|(\.bilibili\.com)"
    r"|(\b(av|cv)(\d+))|\b(BV([a-zA-Z0-9]{10})+)"
    r"|(\[\[QQ小程序\]哔哩哔哩\])|(QQ小程序&amp;#93;哔哩哔哩)"
    r"|(QQ小程序&#93;哔哩哔哩)",
    re.I,
)

# 本插件的指令名，用于避免「指令里带链接」时被自动解析重复推送
COMMAND_PATTERN = re.compile(
    r"^[/!！]?\s*(评论|看评论|bili评论|搜视频|下视频|下载视频|b视频|取视频)(\s|$)",
    re.I,
)

IMAGE_SUFFIXES: Set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".jfif", ".webp",
}

# 允许的 bilibili 相关域名后缀
_ALLOWED_DOMAINS = (
    "bilibili.com",
    "b23.tv",
    "bilivideo.com",
    "bilivideo.cn",
    "bilivideo.net",
    "hdslb.com",
    "bili2233.cn",
    "bili22.cn",
    "bili23.cn",
    "bili33.cn",
)


def _is_allowed_domain(url: str) -> bool:
    """检查 URL 的域名是否在 bilibili 白名单内"""
    try:
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or ""
        host = host.lower().rstrip(".")
        return any(
            host == domain or host.endswith("." + domain)
            for domain in _ALLOWED_DOMAINS
        )
    except Exception:
        return False


def _find_qqdocurl(data: dict) -> str:
    """从已解析的 JSON dict 中查找 bilibili 相关的 qqdocurl"""
    meta = data.get("meta")
    if not isinstance(meta, dict):
        return ""
    for _key, val in meta.items():
        if isinstance(val, dict):
            url = val.get("qqdocurl", "") or val.get("url", "")
            if url and _is_allowed_domain(url):
                return url
    return ""


def _try_parse_json(text: str) -> str:
    """尝试从 JSON 字符串中提取 bilibili URL"""
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return _find_qqdocurl(data)
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


def _extract_from_raw_message(raw) -> str:
    """从 raw_message 的各种可能格式中提取 QQ小程序 bilibili URL。

    raw_message 可能是:
    - dict: 已解析的 JSON 卡片
    - list: OneBot 消息段列表 [{"type":"json","data":{"data":"{...}"}}]
    - str: CQ码字符串 或 纯 JSON 字符串
    """
    if raw is None:
        return ""

    # 1) raw 本身是 dict（已解析的 JSON 卡片）
    if isinstance(raw, dict):
        url = _find_qqdocurl(raw)
        if url:
            return url
        # 可能是单个 OneBot 消息段: {"type":"json","data":{"data":"{...}"}}
        if raw.get("type") == "json":
            inner = raw.get("data", {})
            if isinstance(inner, dict):
                json_str = inner.get("data", "")
                if isinstance(json_str, str):
                    url = _try_parse_json(json_str)
                    if url:
                        return url

    # 2) raw 是 list（OneBot 消息段列表）
    if isinstance(raw, list):
        for seg in raw:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "json":
                inner = seg.get("data", {})
                if isinstance(inner, dict):
                    json_str = inner.get("data", "")
                    if isinstance(json_str, str):
                        url = _try_parse_json(json_str)
                        if url:
                            return url
                elif isinstance(inner, str):
                    url = _try_parse_json(inner)
                    if url:
                        return url

    # 3) raw 是 str
    if isinstance(raw, str):
        raw_str = raw.strip()
        # 3a) 纯 JSON 字符串
        if raw_str.startswith("{"):
            url = _try_parse_json(raw_str)
            if url:
                return url
        # 3b) CQ码: [CQ:json,data=...] — data 内容可能经过转义
        cq_match = re.search(r'\[CQ:json,data=(.*?)\]', raw_str, re.S)
        if cq_match:
            cq_data = cq_match.group(1)
            # CQ码中逗号等字符会被转义，&amp; 必须最先解码
            cq_data = (
                cq_data
                .replace("&amp;", "&")
                .replace("&#44;", ",")
                .replace("&#91;", "[")
                .replace("&#93;", "]")
            )
            url = _try_parse_json(cq_data)
            if url:
                return url

    return ""


def _is_image(msg: str) -> bool:
    """判断字符串是否为图片 URL"""
    if not isinstance(msg, str) or not msg:
        return False
    try:
        parsed = urllib.parse.urlparse(msg)
        suffix = PurePosixPath(parsed.path).suffix.lower()
        return suffix in IMAGE_SUFFIXES
    except Exception:
        return False


def _flatten(container):
    """递归展开嵌套列表"""
    for i in container:
        if isinstance(i, (list, tuple)):
            yield from _flatten(i)
        else:
            yield i


def _format_msg(msg_list: List[Union[List[str], str]]) -> list:
    """将消息列表转换为 AstrBot 消息链"""
    flatten_msg_list = list(_flatten(msg_list))
    chain = []
    text_buffer = ""
    for i in flatten_msg_list:
        if not i:
            continue
        if _is_image(i):
            if text_buffer:
                chain.append(Comp.Plain(text_buffer))
                text_buffer = ""
            if i.startswith("http"):
                url = i
            elif i.startswith("//"):
                url = f"https:{i}"
            else:
                url = f"https://{i}"
            chain.append(Comp.Image.fromURL(url))
        else:
            text_buffer += str(i)
    if text_buffer:
        chain.append(Comp.Plain(text_buffer))
    return chain


@register(
    "astrbot_plugin_bili_resolver",
    "chufeng",
    "bilibili小组件等转链的工具,方便PC查看链接,"
    "因为之前用其他的转链总是被踢下线,所以自己写了个简单版的,"
    "从发布以来还没被踢下线",
    "1.4.0",
    "https://github.com/chufeng/astrbot_plugin_bili_resolver",
)
class BilibiliAnalysis(Star):

    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.trust_env = False
        self._session: Optional[ClientSession] = None

        # 功能开关
        self.enable_auto_parse = config.get("enable_auto_parse", True)
        self.enable_search = config.get("enable_search", True)

        # 图片卡片 / 评论
        self.enable_card_image = config.get("enable_card_image", True)
        self.enable_comments = config.get("enable_comments", True)
        try:
            self.comment_count = int(config.get("comment_count", 3))
        except (TypeError, ValueError):
            self.comment_count = 3
        try:
            self.comment_cmd_count = int(config.get("comment_cmd_count", 5))
        except (TypeError, ValueError):
            self.comment_cmd_count = 5
        self.comment_sort = config.get("comment_sort", "热门")

        # 视频文件下载
        self.enable_video_download = config.get("enable_video_download", True)
        self.video_auto_send = config.get("video_auto_send", False)
        self.video_quality = config.get("video_quality", "480P")
        try:
            self.video_max_mb = int(config.get("video_max_mb", 20))
        except (TypeError, ValueError):
            self.video_max_mb = 20
        try:
            self.video_max_duration = int(config.get("video_max_duration", 300))
        except (TypeError, ValueError):
            self.video_max_duration = 300
        self.bili_cookie = str(config.get("bili_cookie", "") or "").strip()
        # 没在配置里手填就用扫码登录保存的登录态
        self.login = bili_auth.load_login() if not self.bili_cookie else {}
        if not self.bili_cookie:
            self.bili_cookie = str(self.login.get("cookie") or "")
        analysis_bilibili.bili_cookie = self.bili_cookie
        self.ffmpeg_path = str(config.get("ffmpeg_path", "") or "").strip()
        self._video_sem = asyncio.Semaphore(2)

        # 图片开关，同步到 analysis_bilibili 模块
        analysis_bilibili.analysis_display_image = config.get(
            "enable_image", True
        )

        # 视频排版模板（根据预设选择）
        preset = config.get("template_preset", "原始格式")
        if preset == "原始格式":
            analysis_bilibili.analysis_video_template = ""
        elif preset == "简洁风格":
            analysis_bilibili.analysis_video_template = TEMPLATE_PRESET_EMOJI
        else:  # 自定义
            analysis_bilibili.analysis_video_template = config.get(
                "video_template", ""
            )

        # 群组白名单/黑名单
        self.group_whitelist_mode = config.get("group_whitelist_mode", False)
        self.group_list = [str(g) for g in config.get("group_list", [])]

    async def _get_session(self) -> ClientSession:
        """懒初始化并复用 ClientSession"""
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                trust_env=self.trust_env,
                headers=HEADERS,
                timeout=DEFAULT_TIMEOUT,
            )
        return self._session

    def _check_group(self, group_id: str) -> bool:
        """检查群组是否允许使用。返回 True 表示允许。"""
        if not group_id or not self.group_list:
            return True
        if self.group_whitelist_mode:
            return group_id in self.group_list
        else:
            return group_id not in self.group_list

    async def _video_reply(
        self,
        event: AstrMessageEvent,
        info: dict,
        session: ClientSession,
        comment_limit: int,
        with_details: bool = True,
        dedupe: bool = True,
        group_id: Optional[str] = None,
    ) -> List:
        """按配置组装视频回复：图片卡片优先，失败回退纯文字。"""
        vurl = info["text_vurl"]
        if dedupe and check_duplicate(group_id, vurl):
            return []

        comments, total = [], 0
        if self.enable_comments and comment_limit > 0:
            comments, total = await fetch_comments(
                info["aid"],
                session,
                limit=comment_limit,
                bvid=info.get("bvid", ""),
                owner_mid=info["owner"]["mid"],
                sort=self.comment_sort,
            )

        if self.enable_card_image:
            try:
                path = await bili_card.build_card(
                    session,
                    info,
                    comments,
                    total,
                    handle_num,
                    with_details=with_details,
                )
                return [event.image_result(path)]
            except Exception as e:
                logger.error(
                    f"B站图片卡片生成失败，回退纯文字: {e!r}", exc_info=True
                )

        if with_details:
            chain = _format_msg(format_video_text(info))
        else:
            chain = [Comp.Plain(f"🎬 {info['title']}\n{vurl}\n")]
        if comments:
            chain.append(Comp.Plain(format_comments_text(comments, total)))
        return [event.chain_result(chain)]

    async def _video_file_reply(
        self,
        event: AstrMessageEvent,
        info: dict,
        session: ClientSession,
        with_caption: bool = True,
    ) -> List:
        """提取视频文件并发送。返回消息结果列表（失败时是说明文字）。

        with_caption=False 时不发文字说明（自动模式已经有解析卡片了）。
        """
        if not self.enable_video_download:
            return [event.plain_result("视频下载功能已在插件配置里关闭")]

        link = info.get("link") or info.get("text_vurl", "")
        duration = int(info.get("duration_sec") or 0)
        if duration and self.video_max_duration > 0 and duration > self.video_max_duration:
            return [
                event.plain_result(
                    f"视频时长 {bili_video.human_duration(duration)}，"
                    f"超过 {bili_video.human_duration(self.video_max_duration)} 的上限，就不搬了～\n"
                    f"可以调「视频时长上限」或直接看：{link}"
                )
            ]

        cid = info.get("cid") or 0
        if not cid or not info.get("bvid"):
            return [event.plain_result("拿不到该视频的 cid，无法取流")]

        # 0 表示不限制体积
        max_bytes = (
            self.video_max_mb * 1024 * 1024 if self.video_max_mb > 0 else 0
        )
        bili_video.cleanup_old_files()
        note = ""
        try:
            async with self._video_sem:
                try:
                    result = await bili_video.prepare(
                        session,
                        info["bvid"],
                        cid,
                        self.video_quality,
                        max_bytes,
                        self.bili_cookie,
                        self.ffmpeg_path,
                    )
                except bili_video.FFmpegMissing:
                    # 没装 ffmpeg：4K/1080P+ 这种 DASH 流合不了，降级到 1080P durl
                    result = await bili_video.prepare(
                        session,
                        info["bvid"],
                        cid,
                        "1080P",
                        max_bytes,
                        self.bili_cookie,
                        "",
                    )
                    note = "（没找到 ffmpeg，已降级为 1080P）"
        except bili_video.SizeLimit as e:
            actual = f"约 {bili_video.human_size(e.size)}" if e.size else "体积过大"
            return [
                event.plain_result(
                    f"视频{actual}"
                    f"（{e.quality_label or self.video_quality}），"
                    f"超过 {self.video_max_mb}MB 上限，就不搬了～\n"
                    f"可以在插件配置里调「视频体积上限」，或直接看：{link}"
                )
            ]
        except Exception as e:
            logger.error(f"B站视频提取失败: {e!r}", exc_info=True)
            return [event.plain_result(f"视频提取失败：{e}")]

        path = result["path"]
        caption = f"🎬 {info.get('title', '')}"
        if info.get("part"):
            caption += f"（{info['part']}）"
        resolution = ""
        if result.get("width") and result.get("height"):
            resolution = f" {result['width']}x{result['height']}"
        caption += (
            f"\n{result.get('quality_label') or self.video_quality}{resolution} · "
            f"{bili_video.human_size(result.get('size') or os.path.getsize(path))} · "
            f"{link}{note}"
        )
        # QQ/OneBot 要求 video 段必须是消息里唯一的一段，所以文字和视频分开两条发
        results: List = [
            event.chain_result([Comp.Video.fromFileSystem(path)]),
        ]
        if with_caption:
            results.insert(0, event.plain_result(caption))
        return results

    async def _deliver(self, event: AstrMessageEvent, chain: list) -> bool:
        """直接发送一条消息（绕过 pipeline 的 yield 流程）。

        其它插件（如 meme_manager）可能在 on_decorating_result 里 stop 事件，
        导致 pipeline 不再消费后续 yield 的结果；自动发视频必须用 event.send 保证送达。
        """
        try:
            await event.send(MessageChain(chain=list(chain)))
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e!r}", exc_info=True)
            return False

    async def _handle_video_url(
        self,
        event: AstrMessageEvent,
        url: str,
        session: ClientSession,
        group_id: Optional[str],
        comment_limit: int,
        with_details: bool = True,
        dedupe: bool = True,
    ) -> List:
        info, _vurl, err = await video_info(url, session)
        if not info:
            return [event.plain_result(err or "视频解析失败")]
        return await self._video_reply(
            event,
            info,
            session,
            comment_limit=comment_limit,
            with_details=with_details,
            dedupe=dedupe,
            group_id=group_id,
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        """自动解析消息中的 Bilibili 链接"""
        if not self.enable_auto_parse:
            return

        # 群组白名单/黑名单检查
        group_id = (
            event.message_obj.group_id if event.message_obj else None
        )
        if not self._check_group(group_id):
            return

        text = event.message_str.strip()

        # 本插件指令（/评论、/搜视频…）自己会处理，避免重复推送
        if COMMAND_PATTERN.match(text):
            return

        # 尝试从 QQ小程序 JSON 卡片中提取 URL
        json_url = ""
        if event.message_obj:
            json_url = _extract_from_raw_message(
                event.message_obj.raw_message
            )
            if not json_url and event.message_obj.message:
                for comp in event.message_obj.message:
                    raw = getattr(comp, "raw", None) or getattr(
                        comp, "data", None
                    )
                    if raw:
                        json_url = _extract_from_raw_message(raw)
                        if json_url:
                            break

        # message_str 本身可能就是 JSON
        if not json_url and text.startswith("{"):
            json_url = _try_parse_json(text)

        if json_url:
            logger.info(f"从 JSON 卡片提取到 URL: {json_url}")
            text = json_url
        elif not text or not BILI_PATTERN.search(text):
            return

        try:
            session = await self._get_session()
            if re.search(
                r"(b23\.tv)|(bili(22|23|33|2233)\.cn)", text, re.I
            ):
                text = await b23_extract(text, session=session)

            url, _page, _time = extract(text)
            if "view?" in url:
                info, _vurl, _err = await video_info(url, session)
                if not info:
                    return
                results = await self._video_reply(
                    event,
                    info,
                    session,
                    comment_limit=self.comment_count,
                    group_id=group_id,
                )
                if not results:
                    return
                # 自动附带视频文件：卡片和视频都用 event.send 直发，避免被别的插件
                # stop 掉事件后 pipeline 丢掉后续 yield
                if self.video_auto_send and self.enable_video_download:
                    card_sent = False
                    if results and getattr(results[0], "chain", None):
                        card_sent = await self._deliver(event, results[0].chain)
                    if card_sent:
                        results = []
                    for vr in await self._video_file_reply(
                        event, info, session, with_caption=False
                    ):
                        chain = getattr(vr, "chain", None)
                        if chain:
                            await self._deliver(event, chain)
                    if card_sent:
                        event.stop_event()
                        return
                event.stop_event()
                for result in results:
                    yield result
                return

            msg = await bili_keyword(group_id, text, session=session)
        except Exception as e:
            logger.error(f"Bilibili 解析出错: {e!r}", exc_info=True)
            return

        if not msg:
            return

        # 只在有结果后才阻断
        event.stop_event()

        if isinstance(msg, str):
            if msg:
                yield event.plain_result(msg)
            return

        chain = _format_msg(msg)
        if chain:
            yield event.chain_result(chain)

    @filter.command("搜视频")
    async def search_video(self, event: AstrMessageEvent):
        """通过关键词搜索 Bilibili 视频"""
        if not self.enable_search:
            return

        group_id = (
            event.message_obj.group_id if event.message_obj else None
        )
        if not self._check_group(group_id):
            return

        text = event.message_str.strip()
        # 去除指令前缀
        for prefix in ["/搜视频", "搜视频"]:
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break

        if not text:
            yield event.plain_result("请输入搜索关键词，例如：/搜视频 猫咪")
            return

        event.stop_event()

        try:
            session = await self._get_session()
            search_url = await search_bili_by_title(text, session=session)
            if not search_url:
                yield event.plain_result("未找到相关视频")
                return

            url, _page, _time = extract(search_url)
            if "view?" in url:
                results = await self._handle_video_url(
                    event,
                    url,
                    session,
                    group_id,
                    comment_limit=self.comment_count,
                    dedupe=False,
                )
            else:
                msg = await bili_keyword(group_id, search_url, session=session)
                results = (
                    [event.plain_result(msg)]
                    if isinstance(msg, str) and msg
                    else []
                )
                if not results and msg:
                    chain = _format_msg(msg)
                    results = [event.chain_result(chain)] if chain else []
        except Exception as e:
            logger.error(f"Bilibili 搜索出错: {e!r}", exc_info=True)
            yield event.plain_result("搜索出错，请稍后再试")
            return

        for result in results:
            yield result

    @filter.command("评论", alias={"看评论", "bili评论"})
    async def view_comments(self, event: AstrMessageEvent):
        """看某个 B 站视频的热门评论：/评论 BV号 或 视频链接"""
        if not self.enable_comments:
            yield event.plain_result("评论功能已被关闭（插件配置 → 显示评论）")
            return

        group_id = (
            event.message_obj.group_id if event.message_obj else None
        )
        if not self._check_group(group_id):
            return

        # 指令参数里可能夹着链接/短链/BV号，直接把整条消息当作目标
        text = event.message_str.strip()
        if not BILI_PATTERN.search(text):
            yield event.plain_result(
                "用法：/评论 <视频链接 或 BV号>\n例如：/评论 BV1GJ411x7h7"
            )
            return

        event.stop_event()

        try:
            session = await self._get_session()
            if re.search(
                r"(b23\.tv)|(bili(22|23|33|2233)\.cn)", text, re.I
            ):
                text = await b23_extract(text, session=session)

            url, _page, _time = extract(text)
            if "view?" not in url:
                yield event.plain_result(
                    "目前只支持视频链接/番剧单集之外的视频评论～\n"
                    "请发送视频链接或 BV 号。"
                )
                return

            info, _vurl, err = await video_info(url, session)
            if not info:
                yield event.plain_result(err or "视频解析失败")
                return

            comments, total = await fetch_comments(
                info["aid"],
                session,
                limit=self.comment_cmd_count,
                bvid=info.get("bvid", ""),
                owner_mid=info["owner"]["mid"],
                sort=self.comment_sort,
            )
            if not comments:
                yield event.plain_result(
                    f"没拉到评论（视频关闭了评论区、或 B 站风控了）\n{info['link']}"
                )
                return
        except Exception as e:
            logger.error(f"评论解析出错: {e!r}", exc_info=True)
            yield event.plain_result("评论拉取失败，请稍后再试")
            return

        if self.enable_card_image:
            try:
                path = await bili_card.build_card(
                    session,
                    info,
                    comments,
                    total,
                    handle_num,
                    with_details=False,
                )
                yield event.image_result(path)
                return
            except Exception as e:
                logger.error(f"评论卡片生成失败: {e!r}", exc_info=True)

        head = f"🎬 {info['title']}\n{info['link']}\n"
        yield event.plain_result(head + format_comments_text(comments, total))

    @filter.command("下视频", alias={"下载视频", "b视频", "取视频"})
    async def download_video(self, event: AstrMessageEvent):
        """提取 B 站视频文件并直接发出来：/下视频 <链接 或 BV号>"""
        if not self.enable_video_download:
            yield event.plain_result(
                "视频下载功能已被关闭（插件配置 → 允许提取视频文件）"
            )
            return

        group_id = (
            event.message_obj.group_id if event.message_obj else None
        )
        if not self._check_group(group_id):
            return

        text = event.message_str.strip()
        if not BILI_PATTERN.search(text):
            yield event.plain_result(
                "用法：/下视频 <视频链接 或 BV号>\n例如：/下视频 BV1GJ411x7h7"
            )
            return

        event.stop_event()

        try:
            session = await self._get_session()
            if re.search(
                r"(b23\.tv)|(bili(22|23|33|2233)\.cn)", text, re.I
            ):
                text = await b23_extract(text, session=session)

            url, _page, _time = extract(text)
            if "view?" not in url:
                yield event.plain_result(
                    "目前只支持普通视频（番剧/专栏/动态暂不支持取流）。"
                )
                return

            if not self.bili_cookie:
                logger.info("B 站未登录，清晰度将以游客权限为准")

            info, _vurl, err = await video_info(url, session)
            if not info:
                yield event.plain_result(err or "视频解析失败")
                return
        except Exception as e:
            logger.error(f"取流前解析出错: {e!r}", exc_info=True)
            yield event.plain_result("解析失败，请稍后再试")
            return

        # 视频必须单独成一条消息，且要绕开 pipeline 的 yield 丢结果问题，统一直发
        for result in await self._video_file_reply(event, info, session):
            chain = getattr(result, "chain", None)
            if chain:
                await self._deliver(event, chain)
        event.stop_event()

    @filter.command("b站登录", alias={"bili登录", "登录b站", "b站登陆"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_qr_login(self, event: AstrMessageEvent):
        """扫码登录 B 站，登录态会持久化保存（4K 需要登录 + 大会员）"""
        event.stop_event()
        try:
            session = await self._get_session()
            qr = await bili_auth.generate_qr(session)
            png = bili_auth.make_qr_png(qr["url"])
        except Exception as e:
            logger.error(f"生成 B 站登录二维码失败: {e!r}", exc_info=True)
            yield event.plain_result(f"获取登录二维码失败：{e}")
            return

        yield event.image_result(png)
        yield event.plain_result(
            "请用 B 站手机客户端扫码，并在手机上点「确认登录」（3 分钟内有效）。"
        )

        info, cookie, message = await bili_auth.wait_login(
            session, qr["qrcode_key"], timeout=180
        )
        if not cookie:
            yield event.plain_result(message)
            return

        bili_auth.save_login(cookie, info)
        self.bili_cookie = cookie
        analysis_bilibili.bili_cookie = cookie
        vip = info.get("vip_label") or "普通用户"
        tip = (
            "已开启 4K 下载能力。"
            if info.get("vip")
            else "当前账号不是大会员，B 站最高只给 1080P（4K 会降级）。"
        )
        yield event.plain_result(
            f"✅ 登录成功：{info.get('uname', '')}（{vip}）\n{tip}\n"
            "登录状态已保存，插件重载/重启后自动复用，不会掉登录。"
        )

    @filter.command("b站状态", alias={"bili状态", "b站登录状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_login_status(self, event: AstrMessageEvent):
        """查看 B 站登录状态 / 大会员 / 4K 可用性"""
        event.stop_event()
        session = await self._get_session()
        login = await bili_auth.fetch_nav(session, self.bili_cookie)
        if not login.get("is_login"):
            yield event.plain_result(
                "当前未登录 B 站。发送 /b站登录 扫码登录（4K 需要登录 + 大会员）。"
            )
            return

        saved = bili_auth.load_login()
        saved_at = saved.get("saved_at")
        saved_text = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(saved_at))
            if saved_at
            else "未知（可能是配置里手填的 Cookie）"
        )
        vip = login.get("vip_label") or "普通用户"
        quality = "4K 可用" if login.get("vip") else "最高 1080P（非大会员）"
        yield event.plain_result(
            f"账号：{login.get('uname', '')}（UID {login.get('mid', '')}，"
            f"Lv{login.get('level', 0)}）\n"
            f"会员：{vip} → {quality}\n"
            f"Cookie 保存于：{saved_text}\n"
            f"当前取流清晰度设置：{self.video_quality}"
        )

    @filter.command("b站退出", alias={"bili退出", "退出b站"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def bili_logout(self, event: AstrMessageEvent):
        """清掉本地保存的 B 站登录态"""
        event.stop_event()
        bili_auth.clear_login()
        if not str(self.config.get("bili_cookie", "") or "").strip():
            self.bili_cookie = ""
            analysis_bilibili.bili_cookie = ""
        yield event.plain_result(
            "已清除本地保存的 B 站登录态。"
            + (
                "（配置里手填的 Cookie 仍然生效，需要的话去插件配置里删掉）"
                if str(self.config.get("bili_cookie", "") or "").strip()
                else ""
            )
        )

    async def terminate(self):
        """插件被卸载/停用时调用，关闭持久化 session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
