<div align="center">

![:name](https://count.getloli.com/@astrbot_plugin_bili_resolver?name=astrbot_plugin_bili_resolver&theme=minecraft&padding=6&offset=0&align=top&scale=1&pixelated=1&darkmode=auto)

# astrbot_plugin_bili_resolver

_✨ bilibili小组件等转链的工具 ✨_

[![License](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![AstrBot](https://img.shields.io/badge/AstrBot-4.0%2B-orange.svg)](https://github.com/Soulter/AstrBot)
[![GitHub](https://img.shields.io/badge/作者-chufeng-blue)](https://github.com/chufeng)

</div>

AstrBot 插件 —— 自动解析群聊/私聊中的 B 站链接，返回视频信息摘要。

> **本仓库是魔改版（v1.3.3）**，基于 [chufeng/astrbot_plugin_bili_resolver](https://github.com/chufeng/astrbot_plugin_bili_resolver)（GPLv3）修改。
> 相对上游新增：图片卡片排版、热门评论区与 `/评论`、扫码登录与登录态持久化、视频下载与 4K（DASH + ffmpeg 合轨）、`/下视频` 指令。
> 按 GPLv3 要求，本仓库同样以 GPLv3 发布，见 [LICENSE](LICENSE)。

> 本插件主要是为了避免转链容易被 QQ 踢下线的情况，本人从插件发布到现在还未被踢下线。

## 效果示例

群里有人发了一个 B 站链接或小程序卡片，机器人自动回复一张排版好的卡片：

- 封面（右下角时长）、标题、UP主 / 分区 / 发布时间
- 播放 / 点赞 / 投币 / 收藏 / 转发 / 评论 六项数据
- 简介（最多 2 行）
- **热门评论**：昵称、头像、正文、点赞数、楼中楼回复，UP主评论与置顶会打标
- 底部 BV 号与视频链接

图片在本地用 Pillow 绘制，不依赖任何在线 t2i 服务；生成失败时自动回退为下面的纯文字。

想看更多评论可以单独发指令 `/评论 <链接或BV号>`，会返回一张评论卡片。

纯文字（关闭图片卡片后）长这样：

```
https://www.bilibili.com/video/av114556558967080?p=1

标题："终于知道为什么听到某些歌，反派会愣住了。因为...这也是他们的童年啊..."
小标题：TG-2025-05-23-175551094
类型：XX | UP：一罐蠢乃酱 | https://space.bilibili.com/3546772907493433

播放：359.35万 | 弹幕：2350 | 收藏：12.97万
点赞：28.47万 | 硬币：4.07万 | 评论：2422

简介：-
```

## 支持的链接格式

| 类型 | 示例 |
|------|------|
| 短链 | `https://b23.tv/xxx` |
| 视频 | `bilibili.com/video/av...` 或 `BV...` |
| 番剧 | `bilibili.com/bangumi/play/ep...` / `ss...` / `md...` |
| 专栏文章 | `bilibili.com/read/cv...` |
| 动态 | `bilibili.com/opus/...` 或 `t.bilibili.com/...` |
| QQ 小程序卡片 | 分享 B 站内容到 QQ 的卡片消息 |

> 图片卡片与评论区目前只针对视频链接；番剧/专栏/动态仍走纯文字解析。

## 指令

| 指令 | 说明 |
|------|------|
| `/搜视频 关键词` | 搜索 B 站视频，返回第一个结果的解析信息 |
| `/评论 <链接或BV号>` | 查看该视频的热门评论（别名：`/看评论`、`/bili评论`） |
| `/下视频 <链接或BV号>` | 提取视频文件直接发出来（别名：`/下载视频`、`/b视频`、`/取视频`） |
| `/b站登录` | 管理员：发二维码扫码登录 B 站（别名：`/bili登录`、`/登录b站`） |
| `/b站状态` | 管理员：查看登录账号、大会员、4K 可用性 |
| `/b站退出` | 管理员：清除本地保存的登录态 |

## 登录与 4K

**为什么要登录**：B 站 4K / 1080P+ 只对登录的大会员开放，且必须走 DASH 分离流。

1. 群里发 `/b站登录`（需要管理员权限），机器人回一张二维码；
2. 用手机 B 站 App 扫码并在手机上点「确认登录」；
3. 机器人回复「登录成功：昵称（年度大会员）」，登录态写入 `data/plugin_data/astrbot_plugin_bili_resolver/bili_login.json`，**插件重载/重启后自动复用**；
4. `/b站状态` 可随时确认 Cookie 是否还有效，`/b站退出` 清除。

**4K 需要 ffmpeg 合轨**（DASH 的音视频是分开的）：

- 插件会自动在 `PATH`、winget 安装目录、`ffmpeg_path` 配置里找 ffmpeg；
- 找不到时会自动降级成 1080P（progressive MP4，单文件），并在消息里注明「已降级」；
- Windows 上一条命令即可：`winget install Gyan.FFmpeg`。

> 注意 4K 文件很大：实测 42 秒的 4K 视频约 **120MB**。自动发送 4K 时请配合 `video_max_mb` / `video_max_duration` 控制，别把群刷爆。

## 提取视频文件

`/下视频 BV1xxxxxxxxx` 会把视频下载下来，按消息直接发到群里，并附带标题、清晰度、体积和原链接。

- 走 B 站 `player/playurl`：≤1080P 用 **progressive MP4 直链**（单文件，不需要 ffmpeg）；4K / 1080P+ 用 **DASH 分离流 + ffmpeg 合轨**。
- 没有 ffmpeg 时，4K 会自动降级成 1080P，并在消息里注明，不会直接失败。
- 清晰度：不登录最高 720P；登录后普通账号 1080P，**大会员才有 4K / 1080P+**，实际以 B 站返回为准。
- 默认有保护：超过 `video_max_duration` 或 `video_max_mb` 就不下载，直接回复说明 + 原链接，避免一个 4 小时直播回放把你磁盘/流量吃满。
- 番剧、专栏、动态暂不支持取流。
- 想让它每次解析链接时自动跟着发视频：打开 `video_auto_send`（失败或超限会静默跳过）。
- 如果视频发不出去（例如 NapCat 不在同一台机器、读不到本地文件），在 AstrBot 全局配置里设置 `callback_api_base`，插件会自动改用 HTTP 链接分发文件。

## 安装

**推荐**：在 AstrBot WebUI 的插件管理页面，搜索 `astrbot_plugin_bili_resolver` 一键安装。

手动安装：将插件目录放入 AstrBot 的 `data/plugins/` 目录下，重启或热重载即可。

## 配置

安装后可在 AstrBot WebUI 插件管理面板中修改，无需编辑文件。

| 配置项 | 类型 | 默认值 | 说明 |
|-------|------|-------|------|
| `enable_auto_parse` | bool | `true` | 自动解析开关 |
| `enable_search` | bool | `true` | `/搜视频` 指令开关 |
| `enable_image` | bool | `true` | 纯文字模式下是否显示封面图 |
| `enable_card_image` | bool | `true` | 图片卡片模式（视频数据 + 评论一张图） |
| `enable_comments` | bool | `true` | 显示热门评论（自动解析 + `/评论`） |
| `comment_count` | int | `3` | 自动解析时附带的评论条数（0 = 不附带） |
| `comment_cmd_count` | int | `5` | `/评论` 指令展示的条数（最大 20） |
| `comment_sort` | string | `热门` | 评论排序：热门 / 最新 |
| `enable_video_download` | bool | `true` | 允许 `/下视频` 提取视频文件 |
| `video_auto_send` | bool | `false` | 解析链接时自动附带视频文件 |
| `video_quality` | string | `480P` | 取流清晰度：360P / 480P / 720P / 1080P / 1080P+ / 4K |
| `video_max_mb` | int | `0` | 视频体积上限（MB），**0 = 不限制** |
| `video_max_duration` | int | `300` | 视频时长上限（秒） |
| `bili_cookie` | string | `` | 可选，临时覆盖登录态（优先级高于扫码登录） |
| `ffmpeg_path` | string | `` | 可选，ffmpeg 路径；留空自动探测 |
| `group_whitelist_mode` | bool | `false` | 白名单模式（开启=仅列表中的群生效，关闭=黑名单模式） |
| `group_list` | list | `[]` | 群组 ID 列表 |
| `template_preset` | string | `原始格式` | 纯文字回退时的排版风格，见下方说明 |
| `video_template` | text | `` | 自定义排版模板，仅在 `template_preset` 为 `自定义` 时生效 |

**白名单模式**：只有列表中的群触发，其他群忽略。
**黑名单模式**（默认）：列表中的群不触发，其他群正常。列表为空则所有群生效。

### 视频排版风格（纯文字回退）

通过 `template_preset` 选择视频解析的输出格式：

**原始格式**：插件内置的纯文字格式。

```
https://www.bilibili.com/video/av114556558967080

标题："终于知道为什么听到某些歌，反派会愣住了"
类型：综合 | UP：一罐蠢乃酱 | https://space.bilibili.com/xxx

播放：359.35万 | 弹幕：2350 | 收藏：12.97万
点赞：28.47万 | 硬币：4.07万 | 评论：2422

简介：-
```

**简洁风格**：带 Emoji 的卡片格式，同时附带封面图。

```
🎬 标题：终于知道为什么听到某些歌，反派会愣住了
👤 UP主：一罐蠢乃酱
📝 简介：-
[封面图]
👍 点赞：28.47万 🪙 投币：4.07万
❤️ 收藏：12.97万 🔄 转发：1234
👀 观看：359.35万 💬 弹幕：2350
```

**自定义**：在 `video_template` 文本框中填入自定义模板，使用变量占位符自由排版。

支持的变量：

| 变量 | 说明 |
|------|------|
| `${标题}` | 视频标题 |
| `${UP主}` | UP 主名称 |
| `${UP主链接}` | UP 主空间链接 |
| `${简介}` | 视频简介（最多 3 行） |
| `${封面}` | 封面图（渲染为图片，受「显示封面」开关控制） |
| `${点赞}` | 点赞数 |
| `${投币}` | 投币数 |
| `${收藏}` | 收藏数 |
| `${转发}` | 转发数 |
| `${观看}` | 播放数 |
| `${弹幕数量}` | 弹幕数 |
| `${评论}` | 评论数 |
| `${链接}` | 视频链接 |
| `${发布时间}` | 发布时间 |
| `${类型}` | 视频分区 |
| `${BV号}` | BV 号 |
| `${时长}` | 视频时长（格式：`m:ss` / `h:mm:ss`） |
| `${版权}` | 原创 / 转载 |

## 依赖

- Python >= 3.10
- AstrBot
- aiohttp
- Pillow（图片卡片用；缺失时自动回退纯文字）

## 注意

- 评论接口偶尔会被 B 站风控（返回空），此时卡片只显示视频信息，不影响原解析。
- 同一群里同一个视频只会推送一次（原有去重逻辑保留）。
- 卡片里的评论条数越多图越长，建议 `comment_count` 保持 2~5。
