"""统计业务聚合层：命令层只调这里，这里只调 repository / textproc / render。

全部方法为同步实现（SQL/分词均为阻塞操作），命令层用 asyncio.to_thread 调用，
避免阻塞 AstrBot 事件循环。

用户画像的时间模型与群统计一致：全部接受 TimeRange，命令层默认解析为「历史」
（全期）；画像的措辞纪律：只呈现可验证的频次/分布事实，不推导心理标签
（「高频互动对象」而非「好友」，「主要讨论关键词」而非「兴趣」）。
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import render, textproc
from .repository import ChatlogRepository, MediaStats, RankEntry
from .timeutil import (
    TimeRange,
    TimeRangeError,
    describe_span,
    resolve_range,
    tz_offset_seconds,
)


class ServiceError(Exception):
    """面向用户的可读错误（命令层直接展示 message）。"""


_LONG_THRESHOLD = 100
_SHORT_THRESHOLD = 10
# 用户行为口径常量
BURST_GAP_SECONDS = 120  # 相邻消息间隔小于该值视为同一轮发言
DAY_HOURS = range(6, 18)  # 06:00–17:59 白天，其余夜间
AT_WINDOW_DAYS = 90  # 被@网络统计时间窗
GROUP_KEYWORD_WINDOW_DAYS = 7  # 群画像关键词时间窗
GROUP_TREND_DAYS = 30  # 群画像日趋势天数


def _pctl(sorted_vals: list[int], q: float) -> int:
    if not sorted_vals:
        return 0
    idx = min(int(len(sorted_vals) * q), len(sorted_vals) - 1)
    return sorted_vals[idx]


def _burst_sizes(ts_sorted: list[int]) -> list[int]:
    sizes: list[int] = []
    cur = 0
    prev = None
    for ts in ts_sorted:
        if prev is not None and ts - prev < BURST_GAP_SECONDS:
            cur += 1
        else:
            if cur:
                sizes.append(cur)
            cur = 1
        prev = ts
    if cur:
        sizes.append(cur)
    return sizes


def _streaks(days: list[date], today: date) -> tuple[int, int]:
    """最长连续活跃天数 + 截至今日的连续天数（今日无活跃但昨日有则从昨日起算）。"""
    if not days:
        return 0, 0
    max_streak = cur = 1
    for a, b in zip(days, days[1:]):
        cur = cur + 1 if (b - a).days == 1 else 1
        max_streak = max(max_streak, cur)
    current = 0
    if (today - days[-1]).days <= 1:
        current = 1
        i = len(days) - 1
        while i > 0 and (days[i] - days[i - 1]).days == 1:
            current += 1
            i -= 1
    return max_streak, current


class StatisticsService:
    def __init__(
        self,
        repository: ChatlogRepository,
        *,
        tz_name: str = "Asia/Shanghai",
        default_top_n: int = 10,
        max_query_days: int = 90,
        max_messages_scan: int = 50000,
        stopwords: frozenset[str] = frozenset(),
        output_dir: Path | None = None,
        plugin_dir: Path | None = None,
        font_path_config: str | None = None,
        wordcloud_enabled: bool = True,
        wordcloud_max_words: int = 80,
        wordcloud_retention_days: int = 7,
        now_ts: int | None = None,
    ):
        self.repo = repository
        self.tz_name = tz_name
        try:
            self.tz = ZoneInfo(tz_name)
        except Exception as e:
            raise ServiceError(f"配置的时区无效：{tz_name}（{e}）") from e
        self.default_top_n = max(1, int(default_top_n))
        self.max_query_days = int(max_query_days)
        self.max_messages_scan = int(max_messages_scan)
        self.stopwords = stopwords
        self.output_dir = Path(output_dir) if output_dir else None
        self.plugin_dir = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parent.parent
        self.font_path_config = font_path_config
        self.wordcloud_enabled = bool(wordcloud_enabled)
        self.wordcloud_max_words = int(wordcloud_max_words)
        self.wordcloud_retention_days = int(wordcloud_retention_days)
        self._now_ts = now_ts  # 固定时钟（测试注入），None=真实时间
        self._font: Path | None | bool = False  # False=未探测

    # ---------- 基础 ----------

    @property
    def _now_dt(self) -> datetime:
        return datetime.fromtimestamp(self._now_ts, self.tz) if self._now_ts is not None else datetime.now(self.tz)

    def resolve(self, spec: str | None) -> TimeRange:
        try:
            return resolve_range(spec, self.tz, self.max_query_days, now_ts=self._now_ts)
        except TimeRangeError as e:
            raise ServiceError(str(e)) from e

    def _off(self, r: TimeRange) -> int:
        return tz_offset_seconds(self.tz, r.start_ts)

    def _require_group(self, group_id) -> str:
        gid = str(group_id) if group_id not in (None, "") else ""
        if not gid:
            raise ServiceError("当前是私聊，请用 group <群号> 指定要统计的群。")
        if not self.repo.group_exists(gid):
            raise ServiceError(f"群 {gid} 在 ChatLogger 中还没有聊天记录。")
        return gid

    def _require_user(self, user_id, group_id=None) -> str:
        uid = str(user_id) if user_id not in (None, "") else ""
        if not uid:
            raise ServiceError("无法识别目标用户，请用 user <QQ号> 或 @目标 指定。")
        if not self.repo.user_exists(uid, group_id):
            scope = f"（群 {group_id} 内）" if group_id else ""
            raise ServiceError(f"用户 {uid} {scope}在 ChatLogger 中还没有聊天记录。")
        return uid

    def _plain_texts(self, r: TimeRange, group_id=None, user_id=None, waked: bool | None = None) -> list[str]:
        raw = self.repo.fetch_texts(
            r, group_id=group_id, user_id=user_id, limit=self.max_messages_scan, waked=waked
        )
        texts: list[str] = []
        for content_json in raw:
            texts.extend(textproc.extract_plain_texts(content_json))
        return texts

    def _keywords(self, r: TimeRange, group_id=None, user_id=None, waked: bool | None = None) -> Counter:
        return textproc.count_keywords(self._plain_texts(r, group_id, user_id, waked=waked), self.stopwords)

    def _font_path(self) -> Path | None:
        if self._font is False:
            self._font = render.find_font(self.font_path_config, self.plugin_dir)
        return self._font

    # ---------- 群统计：总览 / 趋势 / 排行 / 关键词 / 词云 ----------

    def summary(self, r: TimeRange, group_id) -> dict:
        gid = self._require_group(group_id)
        total = self.repo.get_message_count(r, group_id=gid)
        if total == 0:
            raise ServiceError(f"{r.label}（{describe_span(r)}）该群没有用户消息记录。")
        active = self.repo.get_active_user_count(r, group_id=gid)
        by_day = self.repo.get_activity_by_day(r, gid, offset_seconds=self._off(r))
        # 「历史」区间按有记录的天数算日均，避免除以 1970 年以来的天数
        days = max(1, len(by_day)) if r.kind == "all" else max(1, (r.end_ts - r.start_ts) // 86400)
        peak = max(by_day, key=lambda d: d.messages, default=None)
        return {
            "range": r,
            "span": describe_span(r),
            "messages": total,
            "active_users": active,
            "avg_per_day": total / days,
            "peak_date": peak.date if peak else "-",
            "peak_messages": peak.messages if peak else 0,
            "days_with_data": len(by_day),
            "span_days": days,
        }

    def rank(self, r: TimeRange, group_id, top_n: int | None = None) -> tuple[list[RankEntry], int]:
        gid = self._require_group(group_id)
        n = top_n or self.default_top_n
        entries = self.repo.get_message_rank(r, group_id=gid, limit=n)
        total = self.repo.get_message_count(r, group_id=gid)
        if not entries:
            raise ServiceError(f"{r.label}（{describe_span(r)}）该群没有用户发言记录。")
        return entries, total

    def wordcloud(self, r: TimeRange, group_id=None, user_id=None, top_n: int | None = None):
        """返回 (图片路径|None, 词频对, 样本消息数)。图片为 None 时命令层降级文本输出。"""
        gid = self._require_group(group_id)
        n = top_n or self.default_top_n
        counter = self._keywords(r, group_id=gid, user_id=user_id)
        if not counter:
            raise ServiceError(f"{r.label}（{describe_span(r)}）范围内没有可用于词云的文本。")
        pairs = counter.most_common(n)
        # N 同时限制图片词数（上限不超过 wordcloud_max_words 配置）
        img_words = min(n, self.wordcloud_max_words) if n else self.wordcloud_max_words
        image_path = None
        if self.wordcloud_enabled:
            font = self._font_path()
            if font is None:
                raise ServiceError(
                    "未找到可用中文字体，无法生成词云图片。"
                    "请在插件配置 font_path 指定字体文件，或在 assets/fonts/ 放入字体后重试。"
                )
            if self.output_dir is not None:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                scope = f"g{gid}" if user_id is None else f"g{gid}_u{user_id}"
                out = self.output_dir / f"wc_{scope}_{r.start_ts}.png"
                image_path = render.render_wordcloud(
                    dict(counter.most_common(img_words)),
                    out,
                    font,
                    max_words=img_words,
                )
        # 顺手清理过期词云图：仅删本插件命名的 wc_*.png，按 mtime 判断（<=0 关闭）
        if self.wordcloud_retention_days > 0 and self.output_dir is not None:
            cutoff = time.time() - self.wordcloud_retention_days * 86400
            for old_png in self.output_dir.glob("wc_*.png"):
                try:
                    if old_png.stat().st_mtime < cutoff:
                        old_png.unlink()
                except OSError:
                    pass
        return image_path, pairs, sum(counter.values())

    # ==================== 用户画像 ====================

    def user_summary(self, r: TimeRange, user_id, group_id=None) -> dict:
        """综合卡片：基础 + 活跃摘要 + 风格摘要。"""
        uid = self._require_user(user_id, group_id)
        off = self._off(r)
        basic = self.repo.get_user_basic(r, uid, group_id, off)
        if basic["message_count"] == 0:
            raise ServiceError(f"{r.label}（{describe_span(r)}）范围内该用户没有消息记录。")
        act = self.repo.get_user_activity(r, uid, group_id, off)
        style = self.repo.get_user_style(r, uid, group_id, limit=self.max_messages_scan)
        lengths = sorted(style["lengths"])
        hours = act["hour_counts"]
        total = basic["message_count"]
        peak = sorted((h for h in range(24) if hours[h] > 0), key=lambda h: -hours[h])[:3]
        peak.sort()
        media = style["media"]
        day = sum(hours[h] for h in DAY_HOURS)
        return {
            "range": r,
            "span": describe_span(r),
            "scope_label": f"群 {group_id}" if group_id else "全部会话",
            **basic,
            "peak_hours": peak,
            "day_ratio": day / total,
            "avg_length_text": sum(lengths) / len(lengths) if lengths else 0.0,
            "long_ratio": (sum(1 for v in lengths if v >= _LONG_THRESHOLD) / len(lengths)) if lengths else 0.0,
            "image_ratio": media.count("image") / total if total else 0.0,
            "voice_ratio": media.count("voice") / total if total else 0.0,
        }

    def user_activity(self, r: TimeRange, user_id, group_id=None) -> dict:
        """活跃规律：24h 分布 / 昼夜 / 工作日周末 / 峰值 / 连续活跃。"""
        uid = self._require_user(user_id, group_id)
        raw = self.repo.get_user_activity(r, uid, group_id, self._off(r))
        hours = raw["hour_counts"]
        total = sum(hours)
        if total == 0:
            raise ServiceError(f"{r.label}（{describe_span(r)}）范围内该用户没有消息记录。")
        weekdays = [0] * 7
        days: list[date] = []
        by_day = raw["by_day"]
        for date_str, c in by_day:
            d = date.fromisoformat(date_str)
            days.append(d)
            weekdays[d.weekday()] += c  # 周几分布按消息数加权，非天数
        day = sum(hours[h] for h in DAY_HOURS)
        peak = sorted((h for h in range(24) if hours[h] > 0), key=lambda h: -hours[h])[:3]
        peak.sort()
        max_streak, current_streak = _streaks(sorted(days), self._now_dt.date())
        return {
            "range": r,
            "span": describe_span(r),
            "scope_label": f"群 {group_id}" if group_id else "全部会话",
            "total": total,
            "hour_counts": hours,
            "weekday_counts": weekdays,
            "day_ratio": day / total,
            "weekend_ratio": sum(weekdays[5:7]) / total,
            "peak_hours": peak,
            "peak_hours_ratio": sum(hours[h] for h in peak) / total if peak else 0.0,
            "max_streak_days": max_streak,
            "current_streak_days": current_streak,
            "active_days": len(days),
        }

    def user_style(self, r: TimeRange, user_id, group_id=None) -> dict:
        """消息风格：长度分位 / 长短比 / 连发轮次 / 媒体偏好 / QQ表情次数。"""
        uid = self._require_user(user_id, group_id)
        raw = self.repo.get_user_style(r, uid, group_id, limit=self.max_messages_scan)
        media: MediaStats = raw["media"]
        total = media.messages
        if total == 0:
            raise ServiceError(f"{r.label}（{describe_span(r)}）范围内该用户没有消息记录。")
        lengths = sorted(raw["lengths"])
        ts_list = raw["ts_list"]
        bursts = _burst_sizes(ts_list)
        return {
            "range": r,
            "span": describe_span(r),
            "scope_label": f"群 {group_id}" if group_id else "全部会话",
            "total": total,
            "text_total": len(lengths),
            "p50": _pctl(lengths, 0.50),
            "p90": _pctl(lengths, 0.90),
            "p95": _pctl(lengths, 0.95),
            "avg_length": sum(lengths) / len(lengths) if lengths else 0.0,
            "short_ratio": (sum(1 for v in lengths if v < _SHORT_THRESHOLD) / len(lengths)) if lengths else 0.0,
            "long_ratio": (sum(1 for v in lengths if v >= _LONG_THRESHOLD) / len(lengths)) if lengths else 0.0,
            "burst_count": len(bursts),
            "avg_burst": len(ts_list) / len(bursts) if bursts else 0.0,
            "max_burst": max(bursts) if bursts else 0,
            "media_counts": media.counts,
            "media_ratios": {k: v / total for k, v in media.counts.items()},
            "face_count": media.count("face"),
        }

    def user_keywords(self, r: TimeRange, user_id, group_id=None, top_n: int | None = None):
        """个人讨论关键词（全期 vs 近 30 天）。恒排除唤醒消息，防命令文本刷屏。"""
        uid = self._require_user(user_id, group_id)
        n = top_n or self.default_top_n
        counter = self._keywords(r, group_id=group_id, user_id=uid, waked=True)
        if not counter:
            raise ServiceError(f"{r.label}（{describe_span(r)}）范围内没有可统计的文本关键词。")
        all_time = counter.most_common(n)
        # 近 30 天窗口：以区间末尾为锚
        recent_days = 30
        cutoff_ts = r.end_ts - recent_days * 86400
        recent_r = TimeRange(
            max(r.start_ts, cutoff_ts), r.end_ts, r.tz, f"近{recent_days}天", "ndays"
        )
        recent_counter = self._keywords(recent_r, group_id=group_id, user_id=uid, waked=True)
        recent = recent_counter.most_common(n)
        return {
            "range": r,
            "span": describe_span(r),
            "scope_label": f"群 {group_id}" if group_id else "全部会话",
            "total_words": sum(counter.values()),
            "all_time": all_time,
            "recent": recent,
            "recent_window_days": recent_days,
        }

    def user_social(self, r: TimeRange, user_id, group_id=None) -> dict:
        """互动关系：回复网络 + @ 网络。只呈现"高频互动对象"这一频次事实。"""
        uid = self._require_user(user_id, group_id)
        sent, received = self.repo.get_user_reply_network(r, uid, group_id)
        at_sent = self.repo.get_user_at_sent(r, uid, group_id)
        at_received = None
        if group_id is not None:
            at_received = self.repo.get_user_at_received(
                r, uid, group_id, window_days=AT_WINDOW_DAYS
            )
        names = self.repo.resolve_names(
            [*sent, *received, *at_sent, *[u for u, _ in (at_received or [])]]
        )
        mutual: dict[str, list[int]] = {}
        for i, c in sent.items():
            mutual.setdefault(i, [0, 0])[0] = c
        for i, c in received.items():
            mutual.setdefault(i, [0, 0])[1] = c
        top_mutual = sorted(mutual.items(), key=lambda kv: -(kv[1][0] + kv[1][1]))[:8]
        return {
            "range": r,
            "span": describe_span(r),
            "scope_label": f"群 {group_id}" if group_id else "全部会话",
            "reply_sent": [(names[i], c) for i, c in sorted(sent.items(), key=lambda kv: -kv[1])[:10]],
            "reply_received": [(names[i], c) for i, c in sorted(received.items(), key=lambda kv: -kv[1])[:10]],
            "mutual": [(names[i], out_, inn) for i, (out_, inn) in top_mutual],
            "at_sent": [(names[q], c) for q, c in sorted(at_sent.items(), key=lambda kv: -kv[1])[:10]],
            "at_received": [(names[u], c) for u, c in (at_received or [])],
            "at_window_days": AT_WINDOW_DAYS,
        }

    def user_bot(self, r: TimeRange, user_id, group_id=None) -> dict:
        """Bot 互动画像。私聊统计恒为全局口径（输出注明）。"""
        uid = self._require_user(user_id, group_id)
        p = self.repo.get_user_bot_interaction(
            r, uid, group_id, bot_ids=self.repo.bot_self_ids(), offset_seconds=self._off(r)
        )
        if p["group_message_count"] == 0 and p["private_message_count"] == 0:
            raise ServiceError(f"{r.label}（{describe_span(r)}）范围内该用户没有消息记录。")
        p["wake_ratio"] = (
            p["wake_count"] / p["group_message_count"] if p["group_message_count"] else 0.0
        )
        p["range"] = r
        p["span"] = describe_span(r)
        p["scope_label"] = f"群 {group_id}" if group_id else "全部会话"
        return p

    def user_full(self, r: TimeRange, user_id, group_id=None) -> dict:
        """完整画像：六视图合一（综合/活跃/关键词/风格/互动/机器人）。

        综合卡片内部会重复取活跃与风格原料，量级小且结果整体进 TTL 缓存，不做去重。
        """
        return {
            "card": self.user_summary(r, user_id, group_id),
            "activity": self.user_activity(r, user_id, group_id),
            "keywords": self.user_keywords(r, user_id, group_id),
            "style": self.user_style(r, user_id, group_id),
            "social": self.user_social(r, user_id, group_id),
            "bot": self.user_bot(r, user_id, group_id),
        }

    # ---------- 群画像 ----------

    def group_profile(self, group_id, top_n: int | None = None) -> dict:
        """群画像：全期概况 + 近 30 天趋势 + 7 天关键词 + 媒体构成 + 高频互动对。
        复用群统计的 SQL 侧聚合，不做全量 ts 拉取。"""
        gid = self._require_group(group_id)
        n = top_n or self.default_top_n
        tz = self.tz
        now = self._now_dt
        day0 = datetime.combine(now.date(), datetime.min.time(), tzinfo=tz)
        trend_r = TimeRange(
            int((day0 - timedelta(days=GROUP_TREND_DAYS - 1)).timestamp()),
            int((day0 + timedelta(days=1)).timestamp()),
            tz, f"近{GROUP_TREND_DAYS}天", "ndays",
        )
        kw_r = TimeRange(
            trend_r.end_ts - GROUP_KEYWORD_WINDOW_DAYS * 86400, trend_r.end_ts,
            tz, f"近{GROUP_KEYWORD_WINDOW_DAYS}天", "ndays",
        )
        name, msg_count, members, first, last = self.repo.get_group_meta(gid)
        if msg_count == 0:
            raise ServiceError(f"群 {gid} 在 ChatLogger 中还没有聊天记录。")
        hour_counts = self.repo.get_activity_by_hour(
            trend_r, gid, offset_seconds=self._off(trend_r)
        )
        by_day = self.repo.get_activity_by_day(
            trend_r, gid, offset_seconds=self._off(trend_r)
        )
        peak = sorted((h for h in range(24) if hour_counts[h] > 0), key=lambda h: -hour_counts[h])[:3]
        peak.sort()
        top_active = self.repo.get_message_rank(trend_r, gid, limit=10)
        kw_counter = self._keywords(kw_r, group_id=gid)
        media = self.repo.get_media_stats(trend_r, gid)
        pairs = self.repo.get_group_reply_pairs(gid, trend_r)
        pair_ids = [a for a, _, _ in pairs] + [b for _, b, _ in pairs]
        pair_names = self.repo.resolve_names(pair_ids)
        active_names = self.repo.resolve_names([e.user_id for e in top_active])
        return {
            "group_id": gid,
            "group_name": name,
            "message_count": msg_count,
            "active_members": members,  # 发言成员数 ≠ 平台群成员总数
            "first_seen": first,
            "last_seen": last,
            "hour_counts": hour_counts,
            "peak_hours": peak,
            "daily_trend": [(d.date, d.messages) for d in by_day],
            "trend_days": GROUP_TREND_DAYS,
            "top_active": [(active_names[e.user_id], e.count) for e in top_active],
            "top_keywords": kw_counter.most_common(n),
            "keyword_window_days": GROUP_KEYWORD_WINDOW_DAYS,
            "media_ratios": {k: v / media.messages for k, v in media.counts.items()} if media.messages else {},
            "top_pairs": [(pair_names[a], pair_names[b], c) for a, b, c in pairs],
        }
