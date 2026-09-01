"""输出渲染：词云图片（无字体/无库时降级为文本）、文本条形图、榜单与画像格式化。

画像渲染的措辞边界：只描述统计事实——
- "主要讨论关键词"而非"兴趣"；"高频互动对象"而非"好友"；
- "22:00–01:00 占 43%"而非"夜猫子"。
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 与 AstrBot LogManager 同名 logger：宿主环境进统一日志，独立测试零依赖
logger = logging.getLogger("astrbot")

FONT_EXTS = (".ttf", ".ttc", ".otf")
# 系统字体探测候选（按优先级），命中名字子串即用
_CJK_CANDIDATES = (
    "notosanscjk",
    "notoserifcjk",
    "sourcehan",
    "wqy",
    "zenhei",
    "uming",
    "msyh",
    "simhei",
    "simsun",
    "pingfang",
    "deng",
)
_SYSTEM_FONT_ROOTS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    str(Path.home() / ".local/share/fonts"),
    str(Path.home() / ".fonts"),
)

# 画像媒体标签（中文展示名）
_MEDIA_LABELS = {
    "image": "图片",
    "voice": "语音",
    "video": "视频",
    "at": "At",
    "face": "QQ表情",
    "reply": "回复",
    "file": "文件",
}


def find_font(configured: str | None, plugin_dir: Path | str) -> Path | None:
    """字体探测链：配置 font_path → 插件内置 assets/fonts → 系统常见 CJK 字体。"""
    if configured:
        p = Path(configured).expanduser()
        if p.is_file():
            return p
    builtin_dir = Path(plugin_dir) / "assets" / "fonts"
    if builtin_dir.is_dir():
        for f in sorted(builtin_dir.iterdir()):
            if f.suffix.lower() in FONT_EXTS and f.is_file():
                return f
    for root in _SYSTEM_FONT_ROOTS:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        for f in sorted(root_path.rglob("*")):
            if f.suffix.lower() in FONT_EXTS and any(
                c in f.name.lower() for c in _CJK_CANDIDATES
            ):
                return f
    return None


def render_wordcloud(
    freq: dict[str, int] | Counter,
    out_path: Path | str,
    font_path: Path | None,
    max_words: int = 80,
    width: int = 1000,
    height: int = 560,
) -> Path | None:
    """生成词云 PNG。库缺失 / 无字体 / 无词频时返回 None（调用方降级为文本输出）。"""
    if not freq or font_path is None:
        return None
    try:
        from wordcloud import WordCloud
    except ImportError:
        return None
    try:
        wc = WordCloud(
            font_path=str(font_path),
            width=width,
            height=height,
            background_color="white",
            prefer_horizontal=0.9,
            max_words=min(max_words, len(freq)),
            random_state=42,
        ).generate_from_frequencies(freq)
        # 文件名按区间确定（供自动清理识别），写入必须原子落位：
        # 同（群,用户,区间）并发生成共写同一路径，直接 to_file 会让读取方拿到半截 PNG。
        # 临时名保留 .png 后缀——wordcloud 按扩展名选图片编码器
        tmp = out_path.with_suffix(".tmp.png")
        wc.to_file(str(tmp))
        os.replace(tmp, out_path)
        return Path(out_path)
    except Exception as e:
        # 渲染异常（字体损坏/磁盘满/PIL 崩溃等）降级文本，但必须留排障线索
        logger.warning(f"[insight] 词云渲染失败，降级文本词频: {e}")
        try:
            Path(str(out_path)).with_suffix(".tmp.png").unlink(missing_ok=True)
        except OSError:
            pass
        return None


# ---------- 文本渲染辅助 ----------

_BAR_FULL = "█"
_BAR_SEMI = "▉▊▋▌▍▎▏"


def bar(value: int, max_value: int, width: int = 12) -> str:
    """按最大值等比的方块条。"""
    if max_value <= 0 or value <= 0:
        return ""
    units = value / max_value * width
    full = int(units)
    frac = units - full
    s = _BAR_FULL * full
    if full < width and frac > 0.2:
        s += _BAR_SEMI[min(int(frac * len(_BAR_SEMI)), len(_BAR_SEMI) - 1)]
    return s


def fmt_ts(ts: int | None, tz: ZoneInfo) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %H:%M")


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _name_list(pairs, limit: int = 10) -> str:
    if not pairs:
        return "（暂无数据）"
    return "、".join(f"{n} ×{c}" for n, c in pairs[:limit])


def _media_line(ratios: dict) -> str:
    return " ".join(
        f"{_MEDIA_LABELS.get(k, k)} {_pct(r)}"
        for k, r in sorted(ratios.items(), key=lambda x: -x[1])
        if r > 0
    ) or "无媒体消息"


# ---------- 群统计格式化 ----------

def fmt_summary(s: dict) -> str:
    return (
        f"📊 群活跃度总览 · {s['range'].label}（{s['span']}）\n"
        f"消息量: {s['messages']} 条（日均 {s['avg_per_day']:.1f}）\n"
        f"活跃人数: {s['active_users']} 人（口径：范围内发过至少 1 条消息）\n"
        f"峰值日: {s['peak_date']}（{s['peak_messages']} 条）\n"
        f"有记录天数: {s['days_with_data']}/{s['span_days']} 天"
    )


def fmt_rank(title: str, entries, total: int) -> str:
    lines = [title]
    for i, e in enumerate(entries, 1):
        pct = f"{e.ratio * 100:.1f}%" if total else "-"
        name = e.user_name if e.user_name and e.user_name != e.user_id else e.user_id
        lines.append(f"{i}. {name}（{e.user_id}） {e.count} 条 · {pct}")
    if not entries:
        lines.append("该范围内没有发言记录")
    return "\n".join(lines)


def fmt_word_freq(title: str, pairs, empty_hint: str = "该范围内没有可用关键词") -> str:
    lines = [title]
    for i, (word, count) in enumerate(pairs, 1):
        lines.append(f"{i}. {word}  {count}")
    if not pairs:
        lines.append(empty_hint)
    return "\n".join(lines)


# ---------- 用户画像格式化（dict 输入） ----------

def fmt_user_card(name: str, p: dict, tz: ZoneInfo) -> str:
    """综合卡片（/用户画像 综合）。"""
    peak = "/".join(f"{h:02d}" for h in p["peak_hours"]) or "-"
    return (
        f"👤 用户画像 — {name}（{p['scope_label']} · {p['range'].label}）\n"
        f"消息 {p['message_count']} 条（文本 {p['text_message_count']}）"
        f"｜活跃 {p['active_days']} 天｜活跃群 {p['active_groups']} 个\n"
        f"首次 {fmt_ts(p['first_seen'], tz)}｜最近 {fmt_ts(p['last_seen'], tz)}\n"
        f"高峰 {peak} 时｜白天 {_pct(p['day_ratio'])}｜夜间 {_pct(1 - p['day_ratio'])}\n"
        f"均长 {p['avg_length_text']:.1f} 字｜长消息(≥100字) {_pct(p['long_ratio'])}\n"
        f"图片 {_pct(p['image_ratio'])}｜语音 {_pct(p['voice_ratio'])}"
    )


def fmt_user_activity(p: dict) -> str:
    peak = "/".join(f"{h:02d}" for h in p["peak_hours"]) or "-"
    lines = [
        f"⏰ 活跃规律（{p['scope_label']} · {p['range'].label}，共 {p['total']} 条）",
        f"高峰 {peak} 时（合计占 {_pct(p['peak_hours_ratio'])}）",
        f"白天(06–18) {_pct(p['day_ratio'])}｜夜间 {_pct(1 - p['day_ratio'])}"
        f"｜周末 {_pct(p['weekend_ratio'])}",
        f"活跃 {p['active_days']} 天｜最长连续 {p['max_streak_days']} 天"
        f"｜当前连续 {p['current_streak_days']} 天",
    ]
    nz = [(h, c) for h, c in enumerate(p["hour_counts"]) if c > 0]
    if nz:
        top = sorted(nz, key=lambda x: -x[1])[:12]
        top.sort()
        mx = max(c for _, c in top)
        lines.append("24h 分布：")
        lines.extend(f"{h:02d}时 {bar(c, mx, 10)} {c}" for h, c in top)
    return "\n".join(lines)


def fmt_user_keywords(p: dict) -> str:
    lines = [
        f"✍️ 讨论关键词（{p['scope_label']} · {p['range'].label}，"
        f"仅自然讨论，不含命令与 Bot 对话）",
        f"全期 Top: {_name_list(p['all_time'])}",
    ]
    if p["recent"]:
        lines.append(f"近{p['recent_window_days']}天 Top: {_name_list(p['recent'])}")
    return "\n".join(lines)


def fmt_user_style(p: dict) -> str:
    return (
        f"💡 消息风格（{p['scope_label']} · {p['range'].label}，共 {p['total']} 条）\n"
        f"长度分位 P50 {p['p50']} / P90 {p['p90']} / P95 {p['p95']}"
        f"（均 {p['avg_length']:.1f} 字，样本 {p['text_total']} 条文本）\n"
        f"短消息(<10字) {_pct(p['short_ratio'])}｜长消息(≥100字) {_pct(p['long_ratio'])}\n"
        f"连发：{p['burst_count']} 轮，均 {p['avg_burst']:.1f} 条/轮，单轮最多 {p['max_burst']} 条\n"
        f"媒体偏好：{_media_line(p['media_ratios'])}\n"
        f"QQ表情消息 {p['face_count']} 条"
    )


def fmt_user_social(p: dict) -> str:
    lines = [f"🔗 互动关系（{p['scope_label']} · {p['range'].label}，仅聊天系统内的互动频次）"]
    if p["reply_sent"]:
        lines.append("最常回复: " + _name_list(p["reply_sent"]))
    if p["reply_received"]:
        lines.append("最常被回复: " + _name_list(p["reply_received"]))
    if p["mutual"]:
        lines.append(
            "高频互动对象: " + "、".join(f"{n}（{a}↔{b}）" for n, a, b in p["mutual"])
        )
    if p["at_sent"]:
        lines.append("最常@: " + _name_list(p["at_sent"]))
    if p["at_received"]:
        lines.append(f"最常被@（近{p['at_window_days']}天）: " + _name_list(p["at_received"]))
    elif p["scope_label"] == "全部会话":
        lines.append("（谁最常@你仅在群范围内统计，当前为全量范围故略）")
    if len(lines) == 1:
        lines.append("（暂无互动记录）")
    return "\n".join(lines)


def fmt_user_bot(p: dict) -> str:
    lines = [
        f"🤖 Bot 互动（{p['scope_label']} · {p['range'].label}，统计指标非心理指标）",
        f"私聊会话 {p['private_session_count']} 个（私聊消息 {p['private_message_count']} 条，"
        "全局口径）",
        f"群内唤醒 {p['wake_count']} 次（占其群消息 {_pct(p['wake_ratio'])}）",
        f"@Bot {p['at_bot_count']} 次｜引用/回复 Bot {p['reply_bot_count']} 次",
    ]
    nz = [(h, c) for h, c in enumerate(p["wake_hour_counts"]) if c > 0]
    if nz:
        mx = max(c for _, c in nz)
        top = sorted(nz, key=lambda x: -x[1])[:6]
        top.sort()
        lines.append("互动时段：")
        lines.extend(f"{h:02d}时 {bar(c, mx, 10)} {c}" for h, c in top)
    return "\n".join(lines)


def fmt_user_full(name: str, p: dict, tz: ZoneInfo) -> str:
    """完整画像（/用户画像）：六视图合并输出。"""
    blocks = [
        fmt_user_card(name, p["card"], tz),
        fmt_user_activity(p["activity"]),
        fmt_user_keywords(p["keywords"]),
        fmt_user_style(p["style"]),
        fmt_user_social(p["social"]),
        fmt_user_bot(p["bot"]),
    ]
    return "\n\n————\n\n".join(b for b in blocks if b)


def fmt_group_profile(p: dict, tz: ZoneInfo) -> str:
    peak = "/".join(f"{h:02d}" for h in p["peak_hours"]) or "-"
    lines = [
        f"🏘️ 群画像 — {p['group_name']}（{p['group_id']}）",
        f"消息 {p['message_count']} 条｜发言成员 {p['active_members']} 人"
        f"（仅统计有发言者，非群成员总数）",
        f"跨度 {fmt_ts(p['first_seen'], tz)} ~ {fmt_ts(p['last_seen'], tz)}",
        f"近{p['trend_days']}天高峰 {peak} 时｜媒体：{_media_line(p['media_ratios'])}",
    ]
    if p["top_active"]:
        lines.append(f"近{p['trend_days']}天发言 Top: {_name_list(p['top_active'])}")
    if p["top_keywords"]:
        lines.append(f"近{p['keyword_window_days']}天关键词: {_name_list(p['top_keywords'])}")
    if p["daily_trend"]:
        mx = max(c for _, c in p["daily_trend"])
        trend = " ".join(f"{bar(c, mx, 4)}" for _, c in p["daily_trend"])
        lines.append(f"近{p['trend_days']}天日趋势: {trend}")
    if p["top_pairs"]:
        lines.append(
            "高频互动对: " + "、".join(f"{a}→{b} ×{c}" for a, b, c in p["top_pairs"][:5])
        )
    return "\n".join(lines)
