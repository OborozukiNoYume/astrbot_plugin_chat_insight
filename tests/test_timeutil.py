"""timeutil：边界换算 / 别名 / 上限 / 上一区间。"""

from datetime import datetime

import pytest
from conftest import NOW, TZ, ts
from insight.timeutil import (
    TimeRangeError,
    day_bucket_to_date,
    resolve_range,
    tz_offset_seconds,
)


def test_today():
    r = resolve_range("today", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 8, 15, 0), ts(2026, 8, 16, 0))
    assert r.label == "今日"


def test_yesterday():
    r = resolve_range("昨日", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 8, 14, 0), ts(2026, 8, 15, 0))
    assert r.label == "昨日"
    for spec in ("昨天", "yesterday", "YESTERDAY"):
        assert resolve_range(spec, TZ, now_ts=NOW).start_ts == ts(2026, 8, 14, 0)


def test_n_days_with_prefix():
    for spec in ["近7天", "最近7天"]:
        r = resolve_range(spec, TZ, now_ts=NOW)
        assert (r.start_ts, r.end_ts) == (ts(2026, 8, 9, 0), ts(2026, 8, 16, 0))


def test_today_alias():
    for spec in ("今日", "今天", "TODAY"):
        r = resolve_range(spec, TZ, now_ts=NOW)
        assert r.start_ts == ts(2026, 8, 15, 0)


def test_week_starts_monday():
    # 2026-08-15 是周六 → 本周从 08-10（周一）开始
    r = resolve_range("week", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 8, 10, 0), ts(2026, 8, 16, 0))


def test_month():
    r = resolve_range("month", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 8, 1, 0), ts(2026, 9, 1, 0))


def test_month_year_boundary():
    dec = int(datetime(2026, 12, 15, 12, 0, tzinfo=TZ).timestamp())
    r = resolve_range("month", TZ, now_ts=dec)
    assert (r.start_ts, r.end_ts) == (
        int(datetime(2026, 12, 1, tzinfo=TZ).timestamp()),
        int(datetime(2027, 1, 1, tzinfo=TZ).timestamp()),
    )


@pytest.mark.parametrize("spec", ["7天", "7日", "7d", " 7天 "])
def test_last_n_days(spec):
    r = resolve_range(spec, TZ, now_ts=NOW)
    # 最近 7 个自然日（含今天）：08-09 00:00 → 08-16 00:00
    assert (r.start_ts, r.end_ts) == (ts(2026, 8, 9, 0), ts(2026, 8, 16, 0))
    assert r.label == "最近7天"


def test_default_is_7_days():
    r = resolve_range(None, TZ, now_ts=NOW)
    assert r.start_ts == ts(2026, 8, 9, 0)


def test_max_days_exceeded():
    with pytest.raises(TimeRangeError, match="最多 3 天"):
        resolve_range("7天", TZ, max_days=3, now_ts=NOW)


def test_invalid_spec():
    with pytest.raises(TimeRangeError, match="无法识别"):
        resolve_range("foo", TZ, now_ts=NOW)


def test_year():
    r = resolve_range("今年", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 1, 1, 0), ts(2027, 1, 1, 0))
    assert r.label == "今年" and r.kind == "year"
    assert resolve_range("本年", TZ, now_ts=NOW).start_ts == ts(2026, 1, 1, 0)


def test_all_history():
    r = resolve_range("历史", TZ, now_ts=NOW)
    assert r.start_ts == 0
    assert r.end_ts == ts(2026, 8, 16, 0)
    assert r.label == "历史" and r.kind == "all"
    assert resolve_range("全部", TZ, now_ts=NOW).kind == "all"
    # 历史不受 max_query_days 限制
    assert resolve_range("历史", TZ, max_days=7, now_ts=NOW).kind == "all"


def test_last_week():
    # NOW 是周六 2026-08-15，本周一为 08-10
    r = resolve_range("上周", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 8, 3, 0), ts(2026, 8, 10, 0))
    assert r.label == "上周"


def test_last_month():
    r = resolve_range("上月", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 7, 1, 0), ts(2026, 8, 1, 0))


def test_quarter():
    # 8 月属于 Q3：07-01 → 10-01
    r = resolve_range("本季度", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 7, 1, 0), ts(2026, 10, 1, 0))
    assert r.label == "本季度"


def test_last_quarter():
    r = resolve_range("上季度", TZ, now_ts=NOW)
    assert (r.start_ts, r.end_ts) == (ts(2026, 4, 1, 0), ts(2026, 7, 1, 0))


def test_quarter_year_boundary():
    feb = int(datetime(2026, 2, 15, tzinfo=TZ).timestamp())
    r = resolve_range("本季度", TZ, now_ts=feb)
    assert (r.start_ts, r.end_ts) == (
        int(datetime(2026, 1, 1, tzinfo=TZ).timestamp()),
        int(datetime(2026, 4, 1, tzinfo=TZ).timestamp()),
    )


def test_halfyear():
    for spec in ("半年", "半年前", "近半年", "最近半年"):
        r = resolve_range(spec, TZ, now_ts=NOW)
        # 滚动 6 个月：2026-02-16 → 2026-08-16
        assert (r.start_ts, r.end_ts) == (ts(2026, 2, 16, 0), ts(2026, 8, 16, 0)), spec
        assert r.label == "半年"


def test_zongbang_alias_is_all():
    r = resolve_range("总榜", TZ, now_ts=NOW)
    assert r.kind == "all" and r.label == "历史"


def test_tz_offset_and_day_bucket_roundtrip():
    off = tz_offset_seconds(TZ, NOW)
    assert off == 8 * 3600
    bucket = (NOW + off) // 86400
    assert day_bucket_to_date(bucket, TZ, off) == "2026-08-15"
