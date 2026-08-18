"""定时群报：下一次触发时间计算与报告内容拼装（纯函数，可独立测试）。

频率三档（report_frequency）与统计区间、标题联动：
    daily   每日 → 昨日     weekly 每周 → 上周（周一起始自然周）     monthly 每月 → 上月（自然月）
调度循环与平台推送在命令层（main.py），本模块不依赖 astrbot。
"""

from __future__ import annotations

import calendar
import re
from datetime import datetime, timedelta

from . import render
from .service import ServiceError

# 频率 → (统计区间 spec, 报告标题前缀)
FREQUENCIES = ("daily", "weekly", "monthly")
FREQ_LABELS = {"daily": "每日", "weekly": "每周", "monthly": "每月"}
PERIOD_SPEC = {"daily": "yesterday", "weekly": "lastweek", "monthly": "lastmonth"}
PERIOD_LABEL = {"daily": "昨日", "weekly": "上周", "monthly": "上月"}

# 播报内容分节（顺序即输出顺序；标识进入配置 report_sections 的 options）
SECTIONS = ("summary", "rank", "keywords", "wordcloud")
SECTION_LABELS = {"summary": "总览", "rank": "发言榜", "keywords": "关键词", "wordcloud": "词云"}

_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    m = _HHMM_RE.match(str(hhmm).strip())
    if not m:
        raise ValueError(f"时间配置非法：{hhmm!r}（应为 HH:MM 24 小时制）")
    return int(m.group(1)), int(m.group(2))


def next_report_dt(
    now: datetime, frequency: str, day: int, day_of_month: int, hhmm: str
) -> datetime:
    """下一次群报触发时间。

    Args:
        now: 当前时刻（带时区）。
        frequency: daily / weekly / monthly。
        day: 每周模式的星期，1=周一 … 7=周日。
        day_of_month: 每月模式的日期，1-31（超出当月天数取当月最后一天）。
        hhmm: "HH:MM" 24 小时制。

    Returns:
        下一次触发的本地时间（不早于 now，等于/早于时顺延一个周期）。

    Raises:
        ValueError: 频率/星期/日期/时间配置非法。
    """
    if frequency not in FREQUENCIES:
        raise ValueError(f"频率配置非法：{frequency!r}（应为 {'/'.join(FREQUENCIES)}）")
    hour, minute = _parse_hhmm(hhmm)
    if frequency == "daily":
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if frequency == "weekly":
        if not 1 <= int(day) <= 7:
            raise ValueError(f"星期配置非法：{day}（1=周一 … 7=周日）")
        monday = now.date() - timedelta(days=now.weekday())  # weekday(): 周一=0
        target = datetime.combine(
            monday + timedelta(days=int(day) - 1), datetime.min.time(), tzinfo=now.tzinfo
        ).replace(hour=hour, minute=minute)
        if target <= now:
            target += timedelta(days=7)
        return target
    # monthly
    if not 1 <= int(day_of_month) <= 31:
        raise ValueError(f"日期配置非法：{day_of_month}（1-31）")

    def month_candidate(year: int, month: int) -> datetime:
        d = min(int(day_of_month), calendar.monthrange(year, month)[1])
        return datetime(year, month, d, hour, minute, tzinfo=now.tzinfo)

    target = month_candidate(now.year, now.month)
    if target <= now:
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        target = month_candidate(year, month)
    return target


def build_report(service, group_id, sections, top_n=None, min_messages: int = 0,
                 frequency: str = "weekly"):
    """按频率对应的统计区间拼装群报。

    Args:
        service: StatisticsService（同步调用，命令层放 to_thread）。
        group_id: 目标群号。
        sections: 启用的分节标识集合（见 SECTIONS）。
        top_n: 榜单/关键词条数，None 用服务默认。
        min_messages: 区间消息量低于该值返回 None（静默群自动跳过）。
        frequency: 报告频率，决定统计区间（见 PERIOD_SPEC）。

    Returns:
        (标题, 正文, 词云图路径 | None)；群不活跃返回 None。

    Raises:
        ValueError: 频率非法。ServiceError: 群无任何记录（由调用方记录并跳过）。
    """
    if frequency not in FREQUENCIES:
        raise ValueError(f"频率配置非法：{frequency!r}")
    r = service.resolve(PERIOD_SPEC[frequency])
    s = service.summary(r, group_id)  # 群无记录时由此抛 ServiceError
    if s["messages"] < min_messages:
        return None
    lines = [f"📋 {PERIOD_LABEL[frequency]}群报 · {s['span']}"]
    if "summary" in sections:
        lines.append(render.fmt_summary(s))
    # 各分节独立容错：某分节无数据（如全媒体群无文本）跳过，不影响整份报告
    if "rank" in sections:
        try:
            entries, total = service.rank(r, group_id, top_n)
            lines.append(render.fmt_rank("🏆 发言榜", entries, total))
        except ServiceError:
            pass
    if "keywords" in sections:
        try:
            pairs, _total = service.keywords(r, group_id, None, top_n)
            lines.append(render.fmt_word_freq("🔑 高频关键词", pairs))
        except ServiceError:
            pass
    image_path = None
    if "wordcloud" in sections:
        try:
            image_path, pairs, _total = service.wordcloud(r, group_id, None, None)
            if image_path is None and pairs:
                lines.append(render.fmt_word_freq("☁️ 词云（文本版）", pairs))
        except ServiceError:
            pass
    return lines[0], "\n\n".join(lines[1:]) if len(lines) > 1 else "", image_path
