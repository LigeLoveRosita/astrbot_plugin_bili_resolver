import re
import json
import time
from collections import OrderedDict
from time import localtime, strftime
from typing import Dict, List, Optional, Tuple, Union

from aiohttp import ClientSession
from astrbot.api import logger

from .wbi import get_query


class BoundedDict(OrderedDict):
    """OrderedDict subclass with a max size, LRU eviction."""

    def __init__(self, max_size: int = 1024, *args, **kwargs):
        self._max_size = max_size
        super().__init__(*args, **kwargs)

    def __setitem__(self, key, value):
        if key in self:
            self.move_to_end(key)
        super().__setitem__(key, value)
        while len(self) > self._max_size:
            self.popitem(last=False)


# group_id : last_vurl
analysis_stat: BoundedDict = BoundedDict(1024)

# 默认配置
analysis_display_image = True
analysis_display_image_list = []
images_size = ""
cover_images_size = ""
analysis_video_template = ""


def resize_image(src: str, is_cover: bool = False) -> str:
    img_type = src[-3:]
    if cover_images_size and is_cover:
        return f"{src}@{cover_images_size}.{img_type}"
    if images_size:
        return f"{src}@{images_size}.{img_type}"
    return src


async def bili_keyword(
    group_id: Optional[str], text: str, session: ClientSession
) -> Union[List[Union[List[str], str]], str]:
    try:
        # 提取url
        url, page, time_location = extract(text)
        # 如果是小程序就去搜索标题
        if not url:
            if title := re.search(r'"desc":("[^"哔哩]+")', text):
                vurl = await search_bili_by_title(title[1], session)
                if vurl:
                    url, page, time_location = extract(vurl)

        # 获取视频详细信息
        msg, vurl = "", ""
        if "view?" in url:
            msg, vurl = await video_detail(
                url, page=page, time_location=time_location, session=session
            )
        elif "bangumi" in url:
            msg, vurl = await bangumi_detail(url, time_location, session)
        elif "xlive" in url:
            msg, vurl = await live_detail(url, session)
        elif "article" in url:
            msg, vurl = await article_detail(url, page, session)
        elif "dynamic" in url:
            msg, vurl = await dynamic_detail(url, session)

        # 避免多个机器人解析重复推送
        if group_id:
            if group_id in analysis_stat and analysis_stat[group_id] == vurl:
                return ""
            analysis_stat[group_id] = vurl
    except Exception as e:
        logger.error(f"bili_keyword Error: {e!r}", exc_info=True)
        msg = f"bili_keyword Error: {e!r}"
    return msg


async def b23_extract(text: str, session: ClientSession) -> str:
    b23 = re.compile(
        r"b23\.tv/(\w+)|(bili(22|23|33|2233)\.cn)/(\w+)", re.I
    ).search(text.replace("\\", ""))
    if not b23:
        return text

    url = f"https://{b23[0]}"
    async with session.get(url, allow_redirects=False) as resp:
        if resp.status in (301, 302, 303, 307, 308):
            return resp.headers.get("Location", text)
        # 没有重定向则尝试跟随
        return str(resp.url) if resp.status == 200 else text


def extract(text: str) -> Tuple[str, Optional[str], Optional[str]]:
    try:
        url = ""
        # 视频分p
        page = re.compile(r"([?&]|&amp;)p=\d+").search(text)
        # 视频播放定位时间
        time = re.compile(r"([?&]|&amp;)t=\d+").search(text)
        # 主站视频 av 号
        aid = re.compile(r"av\d+", re.I).search(text)
        # 主站视频 bv 号
        bvid = re.compile(r"BV([A-Za-z0-9]{10})+", re.I).search(text)
        # 番剧视频页
        epid = re.compile(r"ep\d+", re.I).search(text)
        # 番剧剧集ssid(season_id)
        ssid = re.compile(r"ss\d+", re.I).search(text)
        # 番剧详细页
        mdid = re.compile(r"md\d+", re.I).search(text)
        # 直播间
        room_id = re.compile(
            r"live.bilibili.com/(blanc/|h5/)?(\d+)", re.I
        ).search(text)
        # 文章
        cvid = re.compile(
            r"(/read/(cv|mobile|native)(/|\?id=)?|^cv)(\d+)", re.I
        ).search(text)
        # 动态 (opus 新格式)
        opus_id = re.compile(r"bilibili.com/opus/(\d+)", re.I).search(text)
        # 动态
        dynamic_id_type2 = re.compile(
            r"(t|m).bilibili.com/(\d+)\?(.*?)(&|&amp;)type=2", re.I
        ).search(text)
        # 动态
        dynamic_id = re.compile(r"(t|m).bilibili.com/(\d+)", re.I).search(
            text
        )
        if bvid:
            url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid[0]}"
        elif aid:
            url = f"https://api.bilibili.com/x/web-interface/view?aid={aid[0][2:]}"
        elif epid:
            url = f"https://bangumi.bilibili.com/view/web_api/season?ep_id={epid[0][2:]}"
        elif ssid:
            url = f"https://bangumi.bilibili.com/view/web_api/season?season_id={ssid[0][2:]}"
        elif mdid:
            url = f"https://bangumi.bilibili.com/view/web_api/season?media_id={mdid[0][2:]}"
        elif room_id:
            url = f"https://api.live.bilibili.com/xlive/web-room/v1/index/getInfoByRoom?room_id={room_id[2]}"
        elif cvid:
            page = cvid[4]
            url = f"https://api.bilibili.com/x/article/viewinfo?id={page}&mobi_app=pc&from=web"
        elif opus_id:
            url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={opus_id[1]}"
        elif dynamic_id_type2:
            url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?rid={dynamic_id_type2[2]}&type=2"
        elif dynamic_id:
            url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id={dynamic_id[2]}"
        return url, page, time
    except Exception:
        return "", None, None


async def search_bili_by_title(
    title: str, session: ClientSession
) -> Optional[str]:
    # set headers
    mainsite_url = "https://www.bilibili.com"
    async with session.get(mainsite_url) as resp:
        if resp.status != 200:
            logger.warning(
                f"search_bili_by_title: mainsite returned HTTP {resp.status}"
            )
            return None

    query = await get_query({"keyword": title}, session=session)
    search_url = (
        f"https://api.bilibili.com/x/web-interface/wbi/search/all/v2?{query}"
    )

    async with session.get(search_url) as resp:
        if resp.status != 200:
            logger.warning(
                f"search_bili_by_title: search API returned HTTP {resp.status}"
            )
            return None
        result = await resp.json()

    code = result.get("code", -1)
    if code != 0:
        logger.warning(
            f"analysis_bilibili search error: code={code}, "
            f"{result.get('message', '')}"
        )
        return None

    data = result.get("data")
    if not data or not isinstance(data.get("result"), list):
        logger.warning("analysis_bilibili search: 返回数据无 result 字段")
        return None

    for i in data["result"]:
        if i.get("result_type") != "video":
            continue
        items = i.get("data") or i.get("result")
        if items and len(items) > 0:
            return items[0].get("arcurl")
    return None


# 处理超过一万的数字
def handle_num(num: int) -> str:
    if num > 10000:
        return f"{num / 10000:.2f}万"
    return str(num)


def _format_duration(seconds: int) -> str:
    """将秒数格式化为 mm:ss 或 hh:mm:ss"""
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _truncate_desc(desc: str, max_lines: int = 3) -> str:
    """截断简介到最多 max_lines 行非空行"""
    lines = [line for line in desc.split("\n") if line.strip()]
    if len(lines) > max_lines:
        return "\n".join(lines[:max_lines]) + "……"
    return "\n".join(lines)


def _apply_template(template: str, data: dict, cover_url: str) -> list:
    """将模板中的变量替换为实际值，${封面} 拆分为独立的图片元素"""
    result = template
    for key, value in data.items():
        result = result.replace(f"${{{key}}}", str(value))

    if "${封面}" in result:
        parts = result.split("${封面}")
        msg_list = []
        for i, part in enumerate(parts):
            if part:
                msg_list.append(part)
            if i < len(parts) - 1 and cover_url:
                msg_list.append(cover_url)
        return msg_list
    return [result]


def _parse_video_payload(
    res: dict, page, time_location
) -> dict:
    """把 view 接口返回整理成统一结构，供文字与图片卡片共用。"""
    aid = res["aid"]
    bvid = res.get("bvid", "")
    text_vurl = f"https://www.bilibili.com/video/av{aid}"
    link = f"https://www.bilibili.com/video/{bvid or 'av' + str(aid)}"
    part = ""
    pages = res.get("pages") or []
    cid = res.get("cid") or (pages[0].get("cid") if pages else 0) or 0
    duration_sec = int(res.get("duration") or 0)
    if page:
        page = page[0].replace("&amp;", "&")
        try:
            p = int(page[3:])
        except ValueError:
            p = 0
        if p and p <= len(pages):
            text_vurl += f"?p={p}"
            link += f"?p={p}"
            part = pages[p - 1].get("part", "")
            if part == res["title"]:
                part = ""
            cid = pages[p - 1].get("cid") or cid
            duration_sec = int(pages[p - 1].get("duration") or duration_sec)
    if time_location:
        t = time_location[0].replace("&amp;", "&")[3:]
        joiner = "&" if "?" in text_vurl else "?"
        text_vurl += f"{joiner}t={t}"
        joiner = "&" if "?" in link else "?"
        link += f"{joiner}t={t}"

    stat = res.get("stat") or {}
    owner = res.get("owner") or {}
    return {
        "aid": aid,
        "bvid": bvid,
        "title": res.get("title", ""),
        "part": part,
        "tname": res.get("tname", ""),
        "desc": res.get("desc", "") or "",
        "pubdate": strftime("%Y-%m-%d %H:%M:%S", localtime(res.get("pubdate", 0))),
        "duration": _format_duration(res.get("duration", 0) or 0),
        "duration_sec": duration_sec,
        "cid": cid,
        "pic": res.get("pic", ""),
        "link": link,
        "text_vurl": text_vurl,
        "owner": {
            "mid": owner.get("mid", 0),
            "name": owner.get("name", ""),
            "face": owner.get("face", ""),
        },
        "stat": {
            "view": stat.get("view", 0),
            "danmaku": stat.get("danmaku", 0),
            "like": stat.get("like", 0),
            "coin": stat.get("coin", 0),
            "favorite": stat.get("favorite", 0),
            "share": stat.get("share", 0),
            "reply": stat.get("reply", 0),
        },
        "copyright": res.get("copyright", 1),
        "raw": res,
    }


async def video_info(
    url: str, session: ClientSession, **kwargs
) -> Tuple[Optional[dict], str, Optional[str]]:
    """请求视频详情。

    Returns:
        (info, vurl, err) —— err 非空表示失败，调用方直接把 err 当文字回执。
    """
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None, "", f"视频请求失败: HTTP {resp.status}"
            res = (await resp.json()).get("data")
            if not res:
                return None, "", "解析到视频被删了/稿件不可见或审核中/权限不足"
    except Exception as e:
        logger.error(f"视频解析出错: {e!r}", exc_info=True)
        return None, "", f"视频解析出错--Error: {e!r}"

    info = _parse_video_payload(
        res, kwargs.get("page"), kwargs.get("time_location")
    )
    return info, info["text_vurl"], None


def format_video_text(info: dict) -> list:
    """按配置里的排版风格生成纯文字消息链（不含封面以外的图片）。"""
    res = info.get("raw") or {}
    has_image = bool(
        analysis_display_image or "video" in analysis_display_image_list
    )
    cover = resize_image(info["pic"]) if has_image and info.get("pic") else ""
    vurl = info["text_vurl"]
    vurl = "\n" + vurl if cover else vurl

    if analysis_video_template:
        data = {
            "标题": info["title"],
            "UP主": info["owner"]["name"],
            "UP主链接": f"https://space.bilibili.com/{info['owner']['mid']}",
            "简介": _truncate_desc(info["desc"]),
            "点赞": handle_num(info["stat"]["like"]),
            "投币": handle_num(info["stat"]["coin"]),
            "收藏": handle_num(info["stat"]["favorite"]),
            "转发": handle_num(info["stat"]["share"]),
            "观看": handle_num(info["stat"]["view"]),
            "弹幕数量": handle_num(info["stat"]["danmaku"]),
            "评论": handle_num(info["stat"]["reply"]),
            "链接": vurl.lstrip("\n"),
            "发布时间": info["pubdate"],
            "类型": info["tname"],
            "BV号": info["bvid"],
            "时长": info["duration"],
            "版权": "原创" if info.get("copyright") == 1 else "转载",
        }
        return _apply_template(analysis_video_template, data, cover)

    title = f"\n标题：{info['title']}\n"
    if info.get("part"):
        title += f"小标题：{info['part']}\n"
    up_line = (
        f"类型：{info['tname']} | UP：" if info.get("tname") else "UP："
    )
    tname = (
        f"{up_line}{info['owner']['name']} "
        f"| https://space.bilibili.com/{info['owner']['mid']}\n"
    )
    stat = (
        f"\n播放：{handle_num(info['stat']['view'])} | "
        f"弹幕：{handle_num(info['stat']['danmaku'])} | "
        f"收藏：{handle_num(info['stat']['favorite'])}\n"
    )
    stat += (
        f"点赞：{handle_num(info['stat']['like'])} | "
        f"硬币：{handle_num(info['stat']['coin'])} | "
        f"评论：{handle_num(info['stat']['reply'])}\n"
    )
    desc = f"\n简介：{info['desc']}"
    desc = "".join(i + "\n" for i in desc.split("\n") if i)
    desc_list = desc.split("\n")
    if len(desc_list) > 4:
        desc = desc_list[0] + "\n" + desc_list[1] + "\n" + desc_list[2] + "……"
    return [cover, vurl, title, tname, stat, desc]


async def video_detail(
    url: str, session: ClientSession, **kwargs
) -> Tuple[Union[List[str], str], Optional[str]]:
    info, vurl, err = await video_info(url, session, **kwargs)
    if not info:
        return err or "视频解析失败", vurl or url
    return format_video_text(info), vurl


async def bangumi_detail(
    url: str,
    time_location: Optional[re.Match],
    session: ClientSession,
) -> Tuple[List[str], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"番剧请求失败: HTTP {resp.status}", None
            res = (await resp.json()).get("result")
            if not res:
                return None, None

        has_image = False
        if analysis_display_image or "bangumi" in analysis_display_image_list:
            has_image = True

        cover = (
            resize_image(res["cover"], is_cover=True) if has_image else ""
        )
        title = f"番剧：{res['title']}\n"
        desc = f"{res['newest_ep']['desc']}\n"
        index_title = ""
        style = "".join(f"{i}," for i in res["style"])
        style = f"类型：{style[:-1]}\n"
        evaluate = f"简介：{res['evaluate']}\n"
        if "season_id" in url:
            vurl = (
                f"https://www.bilibili.com/bangumi/play/ss{res['season_id']}"
            )
        elif "media_id" in url:
            vurl = (
                f"https://www.bilibili.com/bangumi/media/md{res['media_id']}"
            )
        else:
            epid = re.compile(r"ep_id=\d+").search(url)[0][len("ep_id="):]
            for i in res["episodes"]:
                if str(i["ep_id"]) == epid:
                    index_title = f"标题：{i['index_title']}\n"
                    break
            vurl = f"https://www.bilibili.com/bangumi/play/ep{epid}"
        if time_location:
            time_location = time_location[0].replace("&amp;", "&")[3:]
            vurl += f"?t={time_location}"
        vurl = "\n" + vurl if cover else vurl
        msg = [cover, f"{vurl}\n", title, index_title, desc, style, evaluate]
        return msg, vurl
    except Exception as e:
        logger.error(f"番剧解析出错: {e!r}", exc_info=True)
        msg = f"番剧解析出错--Error: {e!r}"
        msg += f"\n{url}"
        return msg, None


async def live_detail(
    url: str, session: ClientSession
) -> Tuple[List[str], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"直播间请求失败: HTTP {resp.status}", None
            res = await resp.json()
            if res["code"] != 0:
                return None, None
        res = res["data"]
        uname = res["anchor_info"]["base_info"]["uname"]
        room_id = res["room_info"]["room_id"]
        title = res["room_info"]["title"]

        has_image = False
        if analysis_display_image or "live" in analysis_display_image_list:
            has_image = True

        cover = (
            resize_image(res["room_info"]["cover"], is_cover=True)
            if has_image
            else ""
        )
        live_status = res["room_info"]["live_status"]
        lock_status = res["room_info"]["lock_status"]
        parent_area_name = res["room_info"]["parent_area_name"]
        area_name = res["room_info"]["area_name"]
        online = res["room_info"]["online"]
        tags = res["room_info"]["tags"]
        watched_show = res["watched_show"]["text_large"]
        vurl = f"https://live.bilibili.com/{room_id}\n"
        if lock_status:
            lock_time = res["room_info"]["lock_time"]
            lock_time = strftime(
                "%Y-%m-%d %H:%M:%S", localtime(lock_time)
            )
            title = f"[已封禁]直播间封禁至：{lock_time}\n"
        elif live_status == 1:
            title = f"[直播中]标题：{title}\n"
        elif live_status == 2:
            title = f"[轮播中]标题：{title}\n"
        else:
            title = f"[未开播]标题：{title}\n"
        up = f"主播：{uname}  当前分区：{parent_area_name}-{area_name}\n"
        watch = (
            f"观看：{watched_show}  "
            f"直播时的人气上一次刷新值：{handle_num(online)}\n"
        )
        if tags:
            tags = f"标签：{tags}\n"
        if live_status:
            player = (
                f"独立播放器：https://www.bilibili.com/blackboard/live/"
                f"live-activity-player.html?enterTheRoom=0&cid={room_id}"
            )
        else:
            player = ""
        vurl = "\n" + vurl if cover else vurl
        msg = [cover, vurl, title, up, watch, tags, player]
        return msg, vurl
    except Exception as e:
        logger.error(f"直播间解析出错: {e!r}", exc_info=True)
        msg = f"直播间解析出错--Error: {e!r}"
        return msg, None


async def article_detail(
    url: str, cvid: str, session: ClientSession
) -> Tuple[List[Union[List[str], str]], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"专栏请求失败: HTTP {resp.status}", None
            res = (await resp.json()).get("data")
            if not res:
                return None, None

        has_image = False
        if analysis_display_image or "article" in analysis_display_image_list:
            has_image = True

        images = (
            [resize_image(i) for i in res["origin_image_urls"]]
            if has_image
            else []
        )
        vurl = f"https://www.bilibili.com/read/cv{cvid}"
        title = f"标题：{res['title']}\n"
        up = (
            f"作者：{res['author_name']} "
            f"(https://space.bilibili.com/{res['mid']})\n"
        )
        view = f"阅读数：{handle_num(res['stats']['view'])} "
        favorite = f"收藏数：{handle_num(res['stats']['favorite'])} "
        coin = f"硬币数：{handle_num(res['stats']['coin'])}"
        share = f"分享数：{handle_num(res['stats']['share'])} "
        like = f"点赞数：{handle_num(res['stats']['like'])} "
        dislike = f"不喜欢数：{handle_num(res['stats']['dislike'])}"
        desc = view + favorite + coin + "\n" + share + like + dislike + "\n"
        msg = [images, title, up, desc, vurl]
        return msg, vurl
    except Exception as e:
        logger.error(f"专栏解析出错: {e!r}", exc_info=True)
        msg = f"专栏解析出错--Error: {e!r}"
        return msg, None


async def dynamic_detail(
    url: str, session: ClientSession
) -> Tuple[List[Union[List[str], str]], str]:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return f"动态请求失败: HTTP {resp.status}", None
            res = await resp.json()
            if res["code"] != 0:
                return None, None

        data = res.get("data")
        if not data:
            return "动态数据为空", None
        item = data.get("item")
        if not item:
            return "动态内容为空", None

        dynamic_id = item["id_str"]
        vurl = f"https://t.bilibili.com/{dynamic_id}\n"

        # 动态内容
        module_dynamic = item["modules"]["module_dynamic"]
        module_type = item["type"]

        # 文字信息
        desc = module_dynamic["desc"]
        content = desc.get("text").replace("\r", "\n").replace("\n\n", "\n")

        has_image = False
        if analysis_display_image or "dynamic" in analysis_display_image_list:
            has_image = True

        # 额外信息(会员购)
        additional_msg = []
        additional = module_dynamic.get("additional")
        if isinstance(additional, dict):
            additional_type = additional.get("type")
            if additional_type == "ADDITIONAL_TYPE_GOODS":
                items = additional.get("goods", {}).get("items", [])
                for goods_item in items:
                    additional_msg.append(
                        f"{goods_item.get('name')}"
                        f"（{goods_item.get('price')}）\n"
                    )

        # DRAW图片/ARCHIVE转发视频/null纯文字
        draws = []
        archive_cover = ""
        archive_msg = ""
        split = "\n----------------------------------------\n"
        major = module_dynamic["major"]
        if isinstance(major, dict):
            if module_type == "DYNAMIC_TYPE_DRAW":
                split = split if additional_msg else ""
                if has_image:
                    draws = [
                        resize_image(i.get("src"))
                        for i in major.get("draw", {}).get("items", [])
                    ]
                else:
                    items_len = len(
                        major.get("draw", {}).get("items", [])
                    )
                    content += f"\nPS：动态中包含{items_len}张图片"

            elif module_type == "DYNAMIC_TYPE_AV":
                archive = major.get("archive", {})
                jump_url = archive.get("jump_url", "")
                archive_cover = (
                    resize_image(archive.get("cover", ""))
                    if has_image
                    else ""
                )
                archive_msg += f"转发视频：https:{jump_url}\n"
                archive_msg += f"简介：{archive.get('desc', '')}"

        elif module_type == "DYNAMIC_TYPE_FORWARD":
            orig = item.get("orig")
            orig_id = orig.get("id_str") if orig else ""
            archive_msg += f"转发动态：https://t.bilibili.com/{orig_id}\n"
        else:
            split = ""

        msg = [
            content,
            draws,
            split,
            archive_cover,
            archive_msg,
            additional_msg,
            f"\n动态链接：{vurl}",
        ]
        return msg, vurl
    except Exception as e:
        logger.error(f"动态解析出错: {e!r}", exc_info=True)
        msg = f"动态解析出错--Error: {e!r}"
        return msg, None


# ---------------------------------------------------------------- 评论区

REFERER = "https://www.bilibili.com/"
BILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 可选：用户在配置里填的 Cookie（SESSDATA 等），用于 1080P 取流和更稳的评论接口
bili_cookie = ""

# 主站 cookie（buvid3）准备时间戳，避免每次解析都打一次首页
_buvid_ready_at: float = 0.0

_SORT_MAP = {"热门": 2, "最新": 0, "默认": 0}


async def _ensure_buvid(session: ClientSession) -> None:
    """没有 buvid3 时 B 站评论接口容易返回风控错误，先访问一次主站拿 cookie。"""
    global _buvid_ready_at
    if time.time() - _buvid_ready_at < 1800:
        return
    try:
        headers = {"User-Agent": BILI_UA, "Referer": REFERER}
        if bili_cookie:
            headers["Cookie"] = bili_cookie
        async with session.get(REFERER, headers=headers) as resp:
            await resp.read()
        _buvid_ready_at = time.time()
    except Exception as e:
        logger.debug(f"bili buvid 获取失败: {e!r}")


def _clean_comment_text(msg: str, emotes: Optional[dict]) -> str:
    """去掉表情占位符，压缩空白。"""
    msg = (msg or "").replace("\r\n", "\n").replace("\r", "\n")
    for tag in emotes or {}:
        msg = msg.replace(tag, "")
    msg = re.sub(r"[ \t\u3000]+", " ", msg)
    msg = re.sub(r"\n{2,}", "\n", msg)
    return msg.strip()


def _avatar_url(url: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if "@" not in url:
        url += "@96w_96h_1c"
    return url


def _parse_reply(r: dict, owner_mid) -> dict:
    member = r.get("member") or {}
    content = r.get("content") or {}
    subs = []
    for sub in (r.get("replies") or [])[:2]:
        sm = sub.get("member") or {}
        sc = sub.get("content") or {}
        stext = _clean_comment_text(sc.get("message", ""), sc.get("emote"))
        if stext:
            subs.append(
                {
                    "uname": sm.get("uname", ""),
                    "message": stext,
                    "like": sub.get("like", 0),
                }
            )
    ctime = r.get("ctime") or 0
    try:
        ctime_text = strftime("%m-%d %H:%M", localtime(int(ctime)))
    except (ValueError, OSError):
        ctime_text = ""
    return {
        "uname": member.get("uname", ""),
        "mid": member.get("mid", 0),
        "avatar": _avatar_url(member.get("avatar", "")),
        "message": _clean_comment_text(
            content.get("message", ""), content.get("emote")
        ),
        "like": r.get("like", 0),
        "rcount": r.get("rcount", 0),
        "ctime": ctime_text,
        "is_up": bool(owner_mid)
        and str(member.get("mid", "")) == str(owner_mid),
        "pinned": False,
        "replies": subs,
    }


async def fetch_comments(
    aid: int,
    session: ClientSession,
    limit: int = 3,
    bvid: str = "",
    owner_mid=0,
    sort: str = "热门",
) -> Tuple[List[dict], int]:
    """拉取视频评论。

    Returns:
        (评论列表, 评论总数)；失败时返回 ([], 0) 且只记日志。
    """
    if not aid or limit <= 0:
        return [], 0

    try:
        oid = int(aid)
    except (TypeError, ValueError):
        return [], 0
    if oid <= 0:
        return [], 0

    await _ensure_buvid(session)

    limit = max(1, min(int(limit), 20))
    params = {
        "type": 1,
        "oid": oid,
        "sort": _SORT_MAP.get(sort, 2),
        "ps": limit,
        "pn": 1,
    }
    referer = (
        f"https://www.bilibili.com/video/{bvid}"
        if bvid
        else REFERER
    )
    try:
        headers = {"User-Agent": BILI_UA, "Referer": referer}
        if bili_cookie:
            headers["Cookie"] = bili_cookie
        async with session.get(
            "https://api.bilibili.com/x/v2/reply",
            params=params,
            headers=headers,
        ) as resp:
            if resp.status != 200:
                logger.warning(f"评论接口 HTTP {resp.status}")
                return [], 0
            payload = await resp.json(content_type=None)
    except Exception as e:
        logger.warning(f"评论获取失败: {e!r}")
        return [], 0

    if payload.get("code") != 0:
        logger.warning(
            f"评论接口 code={payload.get('code')} "
            f"msg={payload.get('message')}"
        )
        return [], 0

    data = payload.get("data") or {}
    page_info = data.get("page") or {}
    try:
        total = int(page_info.get("count") or 0)
    except (TypeError, ValueError):
        total = 0

    pinned = ((data.get("upper") or {}).get("top") or {})
    pinned_rpid = str(pinned.get("rpid_str") or pinned.get("rpid") or "")

    comments: List[dict] = []
    for r in data.get("replies") or []:
        item = _parse_reply(r, owner_mid)
        item["pinned"] = bool(pinned_rpid) and str(
            r.get("rpid_str") or r.get("rpid") or ""
        ) == pinned_rpid
        if item["message"]:
            comments.append(item)
        if len(comments) >= limit:
            break

    return comments, total


def format_comments_text(comments: List[dict], total: int = 0) -> str:
    """纯文字兜底用的评论块。"""
    if not comments:
        return ""
    lines = ["", "———— 热门评论 ————"]
    if total:
        lines.append(f"共 {handle_num(total)} 条")
    for i, c in enumerate(comments, 1):
        who = c.get("uname") or "匿名用户"
        if c.get("is_up"):
            who += "（UP主）"
        like = c.get("like", 0)
        lines.append(f"{i}. {who}　赞{handle_num(like)}")
        for msg_line in (c.get("message") or "").split("\n"):
            if msg_line.strip():
                lines.append(f"　 {msg_line.strip()}")
        for sub in c.get("replies") or []:
            lines.append(
                f"　 └ {sub.get('uname', '')}：{sub.get('message', '')}"
            )
    return "\n".join(lines)


def check_duplicate(group_id: Optional[str], vurl: str) -> bool:
    """同一群同一个视频只推送一次。返回 True 表示重复。"""
    if not group_id or not vurl:
        return False
    if group_id in analysis_stat and analysis_stat[group_id] == vurl:
        return True
    analysis_stat[group_id] = vurl
    return False
