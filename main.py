"""astrbot_plugin_chat_insight — 聊天洞察（只读统计前端）。

建立在 ChatLogger 数据底座之上的统计/聚合/画像插件，合并并取代
astrbot_plugin_chat_statistics 与 astrbot_plugin_user_profile：

    AstrBot → ChatLogger → chatlog.db → Chat Insight（本插件）

硬边界（详见 README 负面清单）：
- ChatLogger 是唯一聊天数据来源，本插件对 chatlog.db 严格只读（mode=ro，
  契约见 ChatLogger 的 QUERY_GUIDE.md，user_version >= 3），不监听消息、不建第二套聊天库；
- 不做 Memory / RAG / 搜索 / LLM 画像，不推断敏感属性，只描述聊天系统内的公开行为；
- 查询他人画像需要管理员权限；用户画像默认仅统计当前群（防跨群数据泄露）。

架构：commands(本文件) → service → repository → SQLite(mode=ro)。
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .insight import colloquial, render
from .insight.cache import TTLCache
from .insight.db import ChatlogDB, DatabaseNotAvailable, SchemaIncompatible
from .insight.repository import ChatlogRepository
from .insight.service import ServiceError, StatisticsService
from .insight.timeutil import describe_span

PLUGIN_NAME = "astrbot_plugin_chat_insight"

TIME_WORDS = {
    "today", "今日", "今天",
    "yesterday", "昨日", "昨天",
    "week", "本周", "这周", "lastweek", "上周",
    "month", "本月", "这个月", "lastmonth", "上月",
    "quarter", "本季度", "这季度", "lastquarter", "上季度",
    "halfyear", "半年", "半年前", "近半年", "最近半年",
    "year", "今年", "本年",
    "all", "历史", "全部", "总榜",
}
_TIME_RE = re.compile(r"^(?:近|最近)?\d+[天日dD]$")
_NUM_RE = re.compile(r"^\d+$")

_MEDIA_LABELS = {
    "image": "图片",
    "voice": "语音",
    "video": "视频",
    "at": "At",
    "face": "QQ表情",
    "reply": "回复",
    "file": "文件",
}
_TYPE_LABELS = {"group": "群聊", "private": "私聊", "other": "其他"}

_USER_ERRORS = (ServiceError, DatabaseNotAvailable, SchemaIncompatible)

# 本插件命令词：带唤醒前缀/@Bot 时让命令通道处理，避免双响应
_PLUGIN_COMMAND_WORDS = frozenset(
    {"统计", "chatstats", "聊天统计", "发言榜", "发言排行", "rank", "词云", "wordcloud",
     "用户统计", "user", "用户画像", "profile", "群画像"}
)

PROFILE_HELP = """📊 用户画像 — 子命令（时间参数可选，默认「历史」全期）
/用户统计 [@某人] [时间]   综合卡片（查他人需管理员）
/用户画像 综合 [@某人] [时间] 综合画像
/用户画像 活跃 [时间]      24h 分布 / 昼夜 / 连续活跃
/用户画像 关键词 [时间]    讨论关键词（全期 vs 近 30 天）
/用户画像 风格 [时间]      长度分位 / 连发 / 媒体偏好
/用户画像 互动 [时间]      回复 / @ 互动网络
/用户画像 机器人 [时间]    Bot 互动画像
/用户画像 昵称             昵称历史
/群画像                    当前群画像
/用户画像 刷新             清空画像缓存（管理员）
/用户画像 状态             数据库契约检查（管理员）

数据来自 astrbot_plugin_chatlogger（只读），默认仅统计当前群。"""


@register(
    PLUGIN_NAME,
    "OborozukiNoYume",
    "聊天洞察：基于 ChatLogger 的群聊统计、发言排行、词云关键词、用户画像（只读）",
    "0.1.0",
)
class ChatInsight(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.service: StatisticsService | None = None
        self._init_error: str | None = None
        self._wc_trigger_enabled = True
        self._profile_scope = "current_group"
        self.cache = TTLCache(0)

    async def initialize(self):
        """定位并校验 chatlog.db。失败不抛出：插件可加载，命令给出清晰提示。"""
        try:
            configured = str(self.config.get("database_path", "") or "").strip()
            if configured:
                db_path = Path(configured).expanduser()
            else:
                db_path = (
                    Path(get_astrbot_data_path())
                    / "plugin_data"
                    / "astrbot_plugin_chatlogger"
                    / "chatlog.db"
                )
            db = ChatlogDB(db_path)
            exclude_waked = bool(self.config.get("exclude_waked_messages", True))
            from .insight import textproc

            stopwords = textproc.load_stopwords(
                Path(__file__).parent / "assets" / "stopwords.txt",
                self.config.get("extra_stopwords", []) or [],
                self.config.get("stopwords_path", "") or None,
            )
            output_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
            self.service = StatisticsService(
                ChatlogRepository(db, exclude_waked=exclude_waked),
                tz_name=self.config.get("timezone", "Asia/Shanghai"),
                default_top_n=int(self.config.get("default_top_n", 10)),
                max_query_days=int(self.config.get("max_query_days", 90)),
                max_messages_scan=int(self.config.get("max_messages_scan", 50000)),
                stopwords=stopwords,
                output_dir=output_dir,
                plugin_dir=Path(__file__).parent,
                font_path_config=self.config.get("font_path", "") or None,
                wordcloud_enabled=bool(self.config.get("wordcloud_enabled", True)),
            )
            self._wc_trigger_enabled = bool(self.config.get("wordcloud_trigger_enabled", True))
            self._profile_scope = str(self.config.get("profile_scope", "current_group"))
            self.cache = TTLCache(
                int(self.config.get("cache_ttl_minutes", 30) or 0) * 60
            )
            logger.info(f"[insight] 唤醒消息排除（群统计口径）: {'开' if exclude_waked else '关'}")
            logger.info(f"[insight] 画像范围: {self._profile_scope} | 就绪: {db.status_line()}")
        except _USER_ERRORS as e:
            self._init_error = str(e)
            logger.warning(f"[insight] 初始化未完成: {e}")
        except Exception as e:
            self._init_error = f"初始化失败：{e}"
            logger.error(f"[insight] 初始化异常: {e}", exc_info=True)

    async def terminate(self):
        self.service = None
        self.cache.clear()

    # ---------- 工具 ----------

    def _svc(self) -> StatisticsService:
        if self.service is None:
            raise DatabaseNotAvailable(
                self._init_error or "统计服务不可用，请检查插件日志。"
            )
        return self.service

    @staticmethod
    def _normalize_args(time_spec, top_n):
        """(time_spec, top_n) 灵活归一：`/排行 10` 里的 10 视为条数而非时间。"""
        if time_spec is not None and _NUM_RE.match(str(time_spec).strip()):
            top_n = int(str(time_spec).strip())
            time_spec = None
        n = max(1, min(int(top_n), 50)) if top_n else None
        spec = str(time_spec).strip() if time_spec else None
        return spec, n

    @staticmethod
    def _parse_scope_tokens(tokens: list[str]):
        """词云/排行参数解析：用户 <QQ号|我> / 我 / 全群 / 群 <群号> / 时间 / 纯数字(条数)。"""
        user_id = group_id = time_spec = None
        top_n = None
        all_group = False
        i = 0
        while i < len(tokens):
            t = str(tokens[i])
            low = t.lower()
            if low in ("user", "用户", "u", "谁") and i + 1 < len(tokens):
                v = str(tokens[i + 1])
                user_id = "me" if v.lower() in ("me", "我") else v
                i += 2
            elif low in ("me", "我", "自己"):
                user_id = "me"
                i += 1
            elif t in ("全群", "本群"):
                all_group = True
                i += 1
            elif low in ("group", "群", "g") and i + 1 < len(tokens):
                group_id = str(tokens[i + 1])
                i += 2
            elif low in TIME_WORDS or t in TIME_WORDS or _TIME_RE.match(t):
                time_spec = t
                i += 1
            elif _NUM_RE.match(t):
                top_n = int(t)
                i += 1
            else:
                raise ServiceError(
                    f"无法识别的参数「{t}」。支持：用户 <QQ号|我>（或 user me）、全群、"
                    "群 <群号>（或 group）、时间（今日/昨日/本周/上月/本季度/半年/今年/历史/N天）、条数（数字）。"
                )
        return user_id, group_id, time_spec, top_n, all_group

    @staticmethod
    def _resolve_user(event: AstrMessageEvent, user_id: str | None) -> str | None:
        if user_id == "me":
            uid = event.get_sender_id()
            if not uid:
                raise ServiceError("无法识别你的用户 ID，请显式指定 用户 <QQ号>。")
            return uid
        return user_id

    # ---------- 用户画像：守卫与解析 ----------

    def _profile_group_id(self, event: AstrMessageEvent) -> str | None:
        """画像统计范围：current_group（默认）→ 当前群号；私聊或 all → None（全部）。

        默认限定当前群，避免在群 A 查询暴露某用户在群 B 的活跃数据。
        """
        if self._profile_scope != "current_group":
            return None
        gid = event.get_group_id()
        return str(gid) if gid else None

    def _resolve_target(self, event: AstrMessageEvent, arg: str = "") -> tuple[str, bool]:
        """(目标 user_id, 是否查询他人)。At 段 > 纯数字参数 > 发送者自己。

        跳过指向 Bot 自身的 At 段——"@Bot 用户画像 me" 是唤醒方式，
        不是要查 Bot 的画像（Bot 消息被 sender_type 过滤，永远是空画像）。
        """
        sender = str(event.get_sender_id() or "unknown")
        self_id = str(event.get_self_id() or "")
        for comp in event.message_obj.message or []:
            if isinstance(comp, At) and comp.qq:
                tid = str(comp.qq)
                if tid == self_id:
                    continue  # @Bot 是唤醒段，不是查询目标
                return (tid, tid != sender)
        if arg and arg.isdigit() and arg != sender:
            return arg, True
        return sender, False

    @staticmethod
    def _guard_other(event: AstrMessageEvent, is_other: bool) -> str | None:
        """查自己随意，查他人需要管理员。"""
        if not is_other:
            return None
        if not event.is_admin():
            return "查询他人画像需要管理员权限"
        return None

    def _parse_user_tokens(self, event: AstrMessageEvent, tokens: list[str]):
        """用户画像参数解析：[@目标已由 At 段处理] user <id|me> / 时间。
        返回 (user_id 显式值或 None, time_spec)。"""
        user_id = None
        time_spec = None
        i = 0
        while i < len(tokens):
            t = str(tokens[i])
            low = t.lower()
            if low in ("user", "用户", "u", "谁") and i + 1 < len(tokens):
                v = str(tokens[i + 1])
                user_id = event.get_sender_id() if v.lower() in ("me", "我") else v
                i += 2
            elif low in TIME_WORDS or t in TIME_WORDS or _TIME_RE.match(t):
                time_spec = t
                i += 1
            else:
                raise ServiceError(
                    f"无法识别的参数「{t}」。支持：用户 <QQ号|我>（或 user me）、"
                    "时间（今日/本周/本月/今年/历史/N天）。"
                )
        return user_id, time_spec

    async def _prepare_user(self, event: AstrMessageEvent, tokens: list[str]):
        """公共前置：服务可用 + 目标解析 + 权限 + 范围 + 时间。
        失败返回 (None, 提示)；成功返回 ((uid, gid, r), None)。"""
        svc = self._svc()
        explicit_uid, time_spec = self._parse_user_tokens(event, tokens)
        if explicit_uid is not None:
            uid, is_other = explicit_uid, explicit_uid != str(event.get_sender_id() or "")
        else:
            arg = next((t for t in tokens if str(t).isdigit()), "")
            uid, is_other = self._resolve_target(event, arg)
        err = self._guard_other(event, is_other)
        if err:
            return None, f"⛔ {err}"
        gid = self._profile_group_id(event)
        r = svc.resolve(time_spec or "all")  # 用户画像默认全期，群统计默认近 7 天
        return (uid, gid, r), None

    async def _build_user(self, kind: str, fn_name: str, *args):
        """画像统一走 to_thread + TTL 缓存（key 含维度与参数）。"""
        svc = self._svc()
        key = (kind, *map(str, args))
        if self.cache.enabled:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        value = await asyncio.to_thread(getattr(svc, fn_name), *args)
        if self.cache.enabled:
            self.cache.put(key, value)
        return value

    # ---------- 群统计共享实现（顶层命令与 /聊天统计 组内入口共用） ----------

    async def _summary_impl(self, event: AstrMessageEvent, time_spec, top_n):
        try:
            svc = self._svc()
            spec, _ = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            s = await asyncio.to_thread(svc.summary, r, event.get_group_id())
            yield event.plain_result(render.fmt_summary(s))
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] summary 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    async def _keywords_impl(self, event: AstrMessageEvent, time_spec, top_n):
        try:
            svc = self._svc()
            spec, n = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            pairs, total = await asyncio.to_thread(
                svc.keywords, r, event.get_group_id(), None, n
            )
            yield event.plain_result(
                render.fmt_word_freq(f"🔑 高频关键词 · {r.label}（{describe_span(r)}）", pairs)
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] keywords 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    async def _wordcloud_impl(self, event: AstrMessageEvent, tokens: list[str]):
        try:
            svc = self._svc()
            user_id, group_id, time_spec, top_n, all_group = self._parse_scope_tokens(tokens)
            user_id = self._resolve_user(event, user_id)
            # @目标 优先：`@某人 /词云 7天` = 查被@者的词云
            at_target = self._at_target(event)
            if at_target:
                user_id = at_target
            # 默认查自己；「全群」或显式群号才是群维度词云
            if user_id is None and not all_group:
                user_id = self._resolve_user(event, "me")
            spec, n = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            scope_gid = group_id if group_id is not None else event.get_group_id()
            image_path, pairs, total = await asyncio.to_thread(
                svc.wordcloud, r, scope_gid, user_id, n
            )
            if user_id:
                who = await asyncio.to_thread(svc.repo.get_display_name, user_id, scope_gid)
            else:
                who = "全群"
            title = f"☁️ 词云 · {r.label}（{describe_span(r)}）· {who} · 词次 {total}"
            if image_path:
                yield event.image_result(str(image_path))
                yield event.plain_result(title)
            else:
                yield event.plain_result(
                    render.fmt_word_freq(title, pairs, "范围内没有可用于词云的文本")
                )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] wordcloud 失败: {e}", exc_info=True)
            yield event.plain_result("词云生成失败，请稍后重试（详情见日志）。")

    async def _rank_impl(self, event: AstrMessageEvent, tokens: list[str]):
        try:
            svc = self._svc()
            _, group_id, time_spec, top_n, _all_group = self._parse_scope_tokens(tokens)
            spec, n = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            scope_gid = group_id if group_id is not None else event.get_group_id()
            entries, total = await asyncio.to_thread(svc.rank, r, scope_gid, n)
            yield event.plain_result(
                render.fmt_rank(
                    f"🏆 发言排行 · {r.label}（{describe_span(r)}）· 共 {total} 条",
                    entries,
                    total,
                )
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] rank 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    # ---------- 命令入口①：/聊天统计 指令组（管理员） ----------

    @filter.command_group("聊天统计", alias={"统计", "chatstats"})
    def chatstats(self):
        pass

    @chatstats.command("总览", alias={"summary"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_summary(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """群活跃度总览：/聊天统计 总览 [时间]（管理员）"""
        async for r in self._summary_impl(event, time_spec, top_n):
            yield r

    @chatstats.command("趋势", alias={"trend"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_trend(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """按天趋势：/聊天统计 趋势 [时间]（管理员）"""
        try:
            svc = self._svc()
            spec, _ = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            days = await asyncio.to_thread(svc.trend, r, event.get_group_id())
            yield event.plain_result(
                render.fmt_day_trend(f"📈 按天趋势 · {r.label}（{describe_span(r)}）", days)
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] trend 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("时段", alias={"hours"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_hours(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """24 小时分布：/聊天统计 时段 [时间]（管理员）"""
        try:
            svc = self._svc()
            spec, _ = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            buckets = await asyncio.to_thread(svc.hours, r, event.get_group_id())
            yield event.plain_result(
                render.fmt_hours(f"🕐 24 小时分布 · {r.label}（{describe_span(r)}）", buckets)
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] hours 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("关键词", alias={"keywords"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_keywords(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """群高频关键词：/聊天统计 关键词 [时间] [N]（管理员）"""
        async for r in self._keywords_impl(event, time_spec, top_n):
            yield r

    @chatstats.command("emoji")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_emoji(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """Emoji 统计：/聊天统计 emoji [时间] [N]（管理员）"""
        try:
            svc = self._svc()
            spec, n = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            pairs = await asyncio.to_thread(svc.emoji_stats, r, event.get_group_id(), n)
            yield event.plain_result(
                render.fmt_emoji_freq(f"😄 Emoji 统计 · {r.label}（{describe_span(r)}）", pairs)
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] emoji 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("表情", alias={"face"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_face(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """QQ 表情统计：/聊天统计 表情 [时间] [N]（管理员，face ID 口径）"""
        try:
            svc = self._svc()
            spec, n = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            rows = await asyncio.to_thread(svc.face_stats, r, event.get_group_id(), n)
            yield event.plain_result(
                render.fmt_face_freq(f"😚 QQ 表情统计 · {r.label}（{describe_span(r)}）", rows)
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] face 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("类型", alias={"type"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_type(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """消息类型：/聊天统计 类型 [时间]（管理员）"""
        try:
            svc = self._svc()
            spec, _ = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            media, types = await asyncio.to_thread(svc.media_stats, r, event.get_group_id())
            type_line = " / ".join(
                f"{_TYPE_LABELS.get(k, k)} {v} 条" for k, v in sorted(types.items())
            )
            media_line = (
                " · ".join(
                    f"{_MEDIA_LABELS[k]} {media.count(k)}"
                    for k in _MEDIA_LABELS
                    if media.count(k) > 0
                )
                or "无媒体消息"
            )
            yield event.plain_result(
                f"📦 消息类型 · {r.label}（{describe_span(r)}）\n"
                f"场景: {type_line}\n"
                f"媒体构成（按消息数，一条消息可含多种）: {media_line}"
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] type 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("长度", alias={"length"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_length(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """消息长度：/聊天统计 长度 [时间]（管理员，字符长度口径）"""
        try:
            svc = self._svc()
            spec, _ = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            stats = await asyncio.to_thread(svc.length_stats, r, event.get_group_id())
            yield event.plain_result(
                render.fmt_length(f"📏 消息长度 · {r.label}（{describe_span(r)}）", stats)
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] length 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("转发", alias={"forward"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_forward(self, event: AstrMessageEvent, time_spec: str = None, top_n: int = None):
        """转发统计：/聊天统计 转发 [时间] [N]（管理员）"""
        try:
            svc = self._svc()
            spec, n = self._normalize_args(time_spec, top_n)
            r = svc.resolve(spec)
            fwd_total, msg_total, entries = await asyncio.to_thread(
                svc.forward_stats, r, event.get_group_id(), n
            )
            yield event.plain_result(
                render.fmt_forward(
                    f"📤 转发统计 · {r.label}（{describe_span(r)}）",
                    fwd_total,
                    msg_total,
                    entries,
                )
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] forward 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("关键词趋势", alias={"kw-trend"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_kw_trend(self, event: AstrMessageEvent, time_spec: str = None):
        """关键词趋势：/聊天统计 关键词趋势 [时间]（管理员），当前区间 vs 上一区间"""
        try:
            svc = self._svc()
            spec, _ = self._normalize_args(time_spec, None)
            rows, cur, prev = await asyncio.to_thread(
                svc.kw_trend, spec, event.get_group_id(), None
            )
            yield event.plain_result(
                render.fmt_kw_trend(
                    f"📈 关键词趋势 · {cur.label} vs {prev.label}", rows, cur.label, prev.label
                )
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] kw-trend 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @chatstats.command("昼夜", alias={"daynight"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_daynight(self, event: AstrMessageEvent, time_spec: str = None):
        """昼夜差异：/聊天统计 昼夜 [时间]（管理员）"""
        try:
            svc = self._svc()
            spec, _ = self._normalize_args(time_spec, None)
            r = svc.resolve(spec)
            result = await asyncio.to_thread(svc.daynight, r, event.get_group_id(), None)
            yield event.plain_result(
                render.fmt_daynight(f"🌗 昼夜关键词差异 · {r.label}（{describe_span(r)}）", result)
            )
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] daynight 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    # ---------- 命令入口②③：/发言排行 与 /词云（公开） ----------

    @filter.command("发言榜", alias={"rank", "发言排行"})
    async def cmd_rank(
        self, event: AstrMessageEvent, a: str = None, b: str = None, c: str = None
    ):
        """发言榜：/发言榜 [群 <群号>] [时间] [条数]，群内默认当前群"""
        async for r in self._rank_impl(event, [t for t in (a, b, c) if t]):
            yield r

    @filter.command("词云", alias={"wordcloud"})
    async def cmd_wordcloud(
        self,
        event: AstrMessageEvent,
        a: str = None,
        b: str = None,
        c: str = None,
        d: str = None,
        e: str = None,
    ):
        """生成词云：/词云 [时间]（默认自己）；@某人 查他人；「全群」查整群"""
        async for r in self._wordcloud_impl(event, [t for t in (a, b, c, d, e) if t]):
            yield r

    # ---------- 命令入口④：/用户统计 综合卡片（查他人需管理员） ----------

    @filter.command("用户统计", alias={"user"})
    async def cmd_user_card(
        self, event: AstrMessageEvent, a: str = None, b: str = None, c: str = None
    ):
        """用户统计综合卡片：/用户统计 [@某人] [用户 <QQ号|我>] [时间]"""
        try:
            svc = self._svc()
            ctx, err = await self._prepare_user(event, [t for t in (a, b, c) if t])
            if err:
                yield event.plain_result(err)
                return
            uid, gid, r = ctx
            p = await self._build_user("card", "user_summary", r, uid, gid)
            name = await asyncio.to_thread(svc.repo.get_display_name, uid, gid)
            yield event.plain_result(render.fmt_user_card(name, p, svc.tz))
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] 用户统计失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    # ---------- 命令入口⑤：/用户画像 指令组 ----------

    @filter.command_group("用户画像", alias={"profile"})
    def profile(self):
        """用户画像指令组"""

    @profile.command("帮助", alias={"help"})
    async def profile_help(self, event: AstrMessageEvent):
        """查看子命令列表"""
        yield event.plain_result(PROFILE_HELP)

    @profile.command("综合", alias={"me"})
    async def profile_me(self, event: AstrMessageEvent, a: str = None, b: str = None):
        """综合画像（默认自己；@他人需管理员）"""
        try:
            svc = self._svc()
            ctx, err = await self._prepare_user(event, [t for t in (a, b) if t])
            if err:
                yield event.plain_result(err)
                return
            uid, gid, r = ctx
            p = await self._build_user("card", "user_summary", r, uid, gid)
            name = await asyncio.to_thread(svc.repo.get_display_name, uid, gid)
            yield event.plain_result(render.fmt_user_card(name, p, svc.tz))
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] profile me 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    async def _user_view(self, event: AstrMessageEvent, tokens: list[str], view: str):
        """用户画像子命令公共实现：view ∈ {activity, keywords, style, social, bot, names}。"""
        try:
            svc = self._svc()
            ctx, err = await self._prepare_user(event, tokens)
            if err:
                yield event.plain_result(err)
                return
            uid, gid, r = ctx
            fn = {
                "activity": ("user_activity", lambda p: render.fmt_user_activity(p)),
                "keywords": ("user_keywords", lambda p: render.fmt_user_keywords(p)),
                "style": ("user_style", lambda p: render.fmt_user_style(p)),
                "social": ("user_social", lambda p: render.fmt_user_social(p)),
                "bot": ("user_bot", lambda p: render.fmt_user_bot(p)),
            }[view]
            p = await self._build_user(view, fn[0], r, uid, gid)
            yield event.plain_result(fn[1](p))
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] profile {view} 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    @profile.command("活跃", alias={"activity"})
    async def profile_activity(self, event: AstrMessageEvent, a: str = None, b: str = None):
        """活跃规律：24h 分布 / 昼夜 / 连续活跃"""
        async for r in self._user_view(event, [t for t in (a, b) if t], "activity"):
            yield r

    @profile.command("关键词", alias={"keywords"})
    async def profile_keywords(self, event: AstrMessageEvent, a: str = None, b: str = None):
        """讨论关键词（全期 vs 近 30 天）"""
        async for r in self._user_view(event, [t for t in (a, b) if t], "keywords"):
            yield r

    @profile.command("风格", alias={"style"})
    async def profile_style(self, event: AstrMessageEvent, a: str = None, b: str = None):
        """消息风格：长度分位 / 连发 / 媒体偏好"""
        async for r in self._user_view(event, [t for t in (a, b) if t], "style"):
            yield r

    @profile.command("互动", alias={"social"})
    async def profile_social(self, event: AstrMessageEvent, a: str = None, b: str = None):
        """互动关系：回复 / @ 网络"""
        async for r in self._user_view(event, [t for t in (a, b) if t], "social"):
            yield r

    @profile.command("机器人", alias={"bot"})
    async def profile_bot(self, event: AstrMessageEvent, a: str = None, b: str = None):
        """Bot 互动画像"""
        async for r in self._user_view(event, [t for t in (a, b) if t], "bot"):
            yield r

    @profile.command("昵称", alias={"names"})
    async def profile_names(self, event: AstrMessageEvent, a: str = None):
        """昵称历史（全局属性，不受群范围限制）"""
        try:
            svc = self._svc()
            ctx, err = await self._prepare_user(event, [t for t in (a,) if t])
            if err:
                yield event.plain_result(err)
                return
            uid, _gid, _r = ctx
            p = await self._build_user("names", "user_names", uid)
            yield event.plain_result(render.fmt_user_names(p, svc.tz))
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] profile names 失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    # ---------- 群画像（公开，仅群聊） ----------

    @filter.command("群画像", alias={"group_profile"})
    async def cmd_group_profile(self, event: AstrMessageEvent):
        """当前群画像（发言成员数 ≠ 群成员总数）"""
        try:
            svc = self._svc()
            gid = event.get_group_id()
            if not gid:
                yield event.plain_result("⛔ 该命令仅在群聊中可用")
                return
            p = await self._build_user("group", "group_profile", str(gid))
            yield event.plain_result(render.fmt_group_profile(p, svc.tz))
        except _USER_ERRORS as e:
            yield event.plain_result(f"⚠️ {e}")
        except Exception as e:
            logger.error(f"[insight] 群画像失败: {e}", exc_info=True)
            yield event.plain_result("查询失败，请稍后重试（详情见日志）。")

    # ---------- 管理子命令 ----------

    @profile.command("状态", alias={"status"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def profile_status(self, event: AstrMessageEvent):
        """数据库契约检查：路径 / schema 版本 / 消息量 / 时间跨度"""
        try:
            svc = self._svc()
        except _USER_ERRORS as e:
            yield event.plain_result(f"❌ {e}")
            return
        s = await asyncio.to_thread(svc.repo.db_stats)
        bot_ids = await asyncio.to_thread(svc.repo.bot_self_ids)
        yield event.plain_result(
            f"📋 聊天洞察数据源\n"
            f"库: {svc.repo.db.path}\n"
            f"消息总数: {s['total']} "
            f"(用户 {s['by_type'].get('user', 0)} / 机器人 {s['by_type'].get('bot', 0)})\n"
            f"时间跨度: {render.fmt_ts(s['oldest'], svc.tz)} ~ {render.fmt_ts(s['newest'], svc.tz)}\n"
            f"识别到 Bot ID: {len(bot_ids)} 个\n"
            f"时区: {svc.tz.key} | 画像范围: {self._profile_scope} | 缓存: "
            f"{'开 ' + str(self.cache.ttl // 60) + ' 分钟' if self.cache.enabled else '关'}"
        )

    @profile.command("刷新", alias={"refresh"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def profile_refresh(self, event: AstrMessageEvent):
        """清空画像缓存并刷新 Bot ID 识别"""
        try:
            svc = self._svc()
        except _USER_ERRORS as e:
            yield event.plain_result(f"❌ {e}")
            return
        n = self.cache.clear()
        await asyncio.to_thread(svc.repo.refresh_bot_ids)
        yield event.plain_result(f"🧹 已清空 {n} 条画像缓存，Bot ID 将重新识别")

    def _wake_prefixes(self) -> list[str]:
        """AstrBot 配置的唤醒前缀列表（可多个、可变更，禁止硬编码）。

        框架唤醒后会从 message_str 剥离前缀；未唤醒消息保留原样。
        """
        try:
            prefixes = self.context.get_config()["wake_prefix"]
        except Exception:
            prefixes = ["/"]
        if isinstance(prefixes, str):
            prefixes = [prefixes]
        return [str(p) for p in prefixes if p]

    # ---------- 口语触发：个人词云（我的词云 / @某人 词云） ----------

    @filter.event_message_type(filter.EventMessageType.ALL, priority=99)
    async def wordcloud_colloquial(self, event: AstrMessageEvent):
        """口语触发：@Bot 我的词云 / @Bot @某人 历史词云（均需 @机器人或唤醒前缀）"""
        try:
            if not self._wc_trigger_enabled or self.service is None:
                return
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return
            text = (event.message_str or "").strip()
            if not text:
                return
            first = text.split()[0]
            if first in _PLUGIN_COMMAND_WORDS:
                return  # 命令通道处理，避免双响应
            hit = colloquial.match_wordcloud(text)
            if hit is None:
                return
            personal, spec, top_n = hit
            if not event.is_at_or_wake_command:
                # 框架规则：群聊里首段 @普通人 的消息即使带唤醒前缀也不唤醒
                # （防止抢答别人被 @ 的消息），「@某人 /词云」因此进不了命令通道。
                # 「@某人 词云」「@某人 <唤醒前缀>词云」是明确的指向性请求，放行；
                # 判定用配置的真实唤醒前缀剥离（前缀可变更，不硬编码）；
                # 其余未唤醒消息一律不响应，防止日常聊天误触。
                stripped_texts = {text, text.lower()}
                for p in self._wake_prefixes():
                    if p and text.startswith(p):
                        s = text[len(p):].strip()
                        stripped_texts.update({s, s.lower()})
                firsts = {t.split()[0] for t in stripped_texts if t.split()}
                if not firsts & {"词云", "wordcloud"}:
                    return
            if personal:
                tokens = ["user", "me", spec]
            else:
                target_qq = self._at_target(event)
                if not target_qq:
                    return  # 裸「词云」无 @目标：不触发，走斜杠命令
                tokens = ["user", target_qq, spec]
            if top_n:
                tokens.append(str(top_n))
            event.stop_event()  # 阻断后续（含 LLM）
            async for r in self._wordcloud_impl(event, tokens):
                yield r
        except Exception as e:
            logger.error(f"[insight] 词云口语触发失败: {e}", exc_info=True)

    @staticmethod
    def _at_target(event: AstrMessageEvent) -> str | None:
        """取消息里 @ 的第一个非机器人成员。"""
        self_id = str(event.get_self_id() or "")
        for comp in event.get_messages() or []:
            if isinstance(comp, At):
                qq = str(getattr(comp, "qq", "") or "")
                if qq and qq not in (self_id, "all"):
                    return qq
        return None
