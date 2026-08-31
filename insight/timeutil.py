"""统一时间处理模块。

全插件只有这里允许做"时间规格 → epoch 边界"的换算，其他模块不得各自实现时间逻辑。
依据 ChatLogger QUERY_GUIDE 契约：chatlog.db 的 ts 为 UTC epoch **秒**，时区换算一律在应用层完成。

时间规格语法（命令层通用）：
    today | 今日      —— 本地今天 00:00 → 明天 00:00
    yesterday | 昨日  —— 昨天 00:00 → 今天 00:00
    week  | 本周      —— 本周一 00:00 → 明天 00:00（周为周一起始）
    month | 本月      —— 本月 1 号 00:00 → 下月 1 号 00:00
    N天 / N日 / Nd    —— 最近 N 个自然日（含今天），可带 近/最近 前缀（近7天）
    lastweek | 上周    —— 上个完整自然周（周一 → 周一）
    lastmonth| 上月    —— 上个完整自然月（1 号 → 1 号）
    quarter| 本季度    —— 本自然季度（1/4/7/10 月首日 → 下季度首日）
    lastquarter | 上季度
    halfyear | 半年    —— 最近 6 个自然月（滚动，含今天）
    year  | 今年      —— 本年 1 月 1 日 → 明年 1 月 1 日
    all   | 历史/总榜 —— 有记录以来全量（不受 max_query_days 限制）
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DEFAULT_DAYS = 7


class TimeRangeError(ValueError):
    """时间规格非法或超出允许的最大查询天数。"""


@dataclass(frozen=True)
class TimeRange:
    start_ts: int  # 含
    end_ts: int    # 不含
    tz: ZoneInfo
    label: str
    kind: str = "span"  # today / week / month / quarter / halfyear / year / ndays / all / span

    @property
    def duration(self) -> int:
        return self.end_ts - self.start_ts


_DAYS_RE = re.compile(r"^(?:近|最近)?(\d+)\s*(天|日|d|D)$")

_ALIASES = {
    "today": "today", "今日": "today", "今天": "today",
    "yesterday": "yesterday", "昨日": "yesterday", "昨天": "yesterday",
    "week": "week", "本周": "week", "这周": "week",
    "lastweek": "lastweek", "上周": "lastweek",
    "month": "month", "本月": "month", "这个月": "month",
    "lastmonth": "lastmonth", "上月": "lastmonth",
    "quarter": "quarter", "本季度": "quarter", "这季度": "quarter",
    "lastquarter": "lastquarter", "上季度": "lastquarter",
    "halfyear": "halfyear", "半年": "halfyear", "半年前": "halfyear",
    "近半年": "halfyear", "最近半年": "halfyear",
    "year": "year", "今年": "year", "本年": "year",
    "all": "all", "历史": "all", "全部": "all", "总榜": "all",
}


def resolve_range(
    spec: str | None,
    tz: ZoneInfo,
    max_days: int | None = None,
    *,
    now_ts: int | None = None,
    default_days: int = DEFAULT_DAYS,
) -> TimeRange:
    """把时间规格解析为 epoch 秒边界。now_ts 仅供测试注入。"""
    if tz is None:
        tz = ZoneInfo("Asia/Shanghai")
    now = datetime.fromtimestamp(now_ts, tz) if now_ts is not None else datetime.now(tz)
    today0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow0 = today0 + timedelta(days=1)

    key = _ALIASES.get((spec or "").strip().lower()) or _ALIASES.get((spec or "").strip())
    spec = (spec or "").strip()
    if not spec:
        spec = f"{default_days}天"

    if key == "today" or spec in ("today", "今日", "今天"):
        start, end, label, kind = today0, tomorrow0, "今日", "today"
    elif key == "yesterday" or spec in ("yesterday", "昨日", "昨天"):
        start, end, label, kind = today0 - timedelta(days=1), today0, "昨日", "today"
    elif key == "week" or spec in ("week", "本周", "这周"):
        start = today0 - timedelta(days=now.weekday())  # 周一=0
        end, label, kind = tomorrow0, "本周", "week"
    elif key == "lastweek" or spec in ("lastweek", "上周"):
        this_monday = today0 - timedelta(days=now.weekday())
        start, end, label, kind = this_monday - timedelta(days=7), this_monday, "上周", "week"
    elif key == "lastmonth" or spec in ("lastmonth", "上月"):
        this_month_start = today0.replace(day=1)
        start = _shift_months(this_month_start, -1)
        end, label, kind = this_month_start, "上月", "month"
    elif key == "quarter" or spec in ("quarter", "本季度", "这季度"):
        q_start = _quarter_start(today0)
        end = _shift_months(q_start, 3)
        start, label, kind = q_start, "本季度", "quarter"
    elif key == "lastquarter" or spec in ("lastquarter", "上季度"):
        q_start = _quarter_start(today0)
        start, end, label, kind = _shift_months(q_start, -3), q_start, "上季度", "quarter"
    elif key == "halfyear" or spec in ("halfyear", "半年", "半年前", "近半年", "最近半年"):
        # 滚动半年：最近 6 个自然月（含今天），不受 max_query_days 限制
        start, end, label, kind = _shift_months(tomorrow0, -6), tomorrow0, "半年", "halfyear"
    elif key == "year" or spec in ("year", "今年", "本年"):
        start = today0.replace(day=1, month=1)
        end = start.replace(year=start.year + 1)
        label, kind = "今年", "year"
    elif key == "all" or spec in ("all", "历史", "全部"):
        # 有记录以来：起点取 epoch 0（实际由库里最早消息决定），不受 max_query_days 限制
        start, end = datetime.fromtimestamp(0, tz), tomorrow0
        label, kind = "历史", "all"
    elif key == "month" or spec in ("month", "本月", "这个月"):
        start = today0.replace(day=1)
        end = _next_month_start(start)
        label, kind = "本月", "month"
    else:
        m = _DAYS_RE.match(spec)
        if not m:
            raise TimeRangeError(
                f"无法识别的时间参数「{spec}」，支持：today/今日、yesterday/昨日、week/本周、"
                "lastweek/上周、month/本月、lastmonth/上月、quarter/本季度、lastquarter/上季度、"
                "halfyear/半年、year/今年、all/历史/总榜、N天（如 7天/近7天）。"
            )
        n = int(m.group(1))
        if n < 1:
            raise TimeRangeError("时间范围至少为 1 天。")
        if max_days is not None and n > max_days:
            raise TimeRangeError(f"时间范围最多 {max_days} 天，当前请求 {n} 天。")
        start, end, label, kind = tomorrow0 - timedelta(days=n), tomorrow0, f"最近{n}天", "ndays"

    return TimeRange(int(start.timestamp()), int(end.timestamp()), tz, label, kind)


def _next_month_start(first_day: datetime) -> datetime:
    if first_day.month == 12:
        return first_day.replace(year=first_day.year + 1, month=1)
    return first_day.replace(month=first_day.month + 1)


def _shift_months(dt: datetime, n: int) -> datetime:
    """按自然月平移（n 可为负），月末日期自动收敛（8-31 → 2-28）。"""
    m = dt.month - 1 + n
    year = dt.year + m // 12
    month = m % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def _quarter_start(dt: datetime) -> datetime:
    """所在自然季度的第一天（Q1=1月, Q2=4月, Q3=7月, Q4=10月）。"""
    qm = (dt.month - 1) // 3 * 3 + 1
    return dt.replace(day=1, month=qm)


def tz_offset_seconds(tz: ZoneInfo, at_ts: int) -> int:
    """指定时刻的 UTC 偏移秒数，供 SQL 天/小时桶换算（(ts+off)//86400）。"""
    return int(datetime.fromtimestamp(at_ts, tz).utcoffset().total_seconds())


def day_bucket_to_date(bucket: int, tz: ZoneInfo, offset_seconds: int) -> str:
    """把 SQL 天桶还原为本地日期字符串 YYYY-MM-DD。"""
    epoch = bucket * 86400 - offset_seconds
    return datetime.fromtimestamp(epoch, tz).strftime("%Y-%m-%d")


def describe_span(r: TimeRange) -> str:
    if getattr(r, "kind", "") == "all":
        return "有记录以来"
    f = "%m-%d %H:%M"
    s = datetime.fromtimestamp(r.start_ts, r.tz)
    e = datetime.fromtimestamp(r.end_ts - 1, r.tz)
    if datetime.fromtimestamp(r.end_ts - 1, r.tz).date() == s.date():
        return f"{s.strftime('%Y-%m-%d')}"
    return f"{s.strftime(f)} ~ {e.strftime(f)}"
