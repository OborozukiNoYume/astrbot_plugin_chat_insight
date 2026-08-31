"""定时群报（insight/report.py）：触发时间计算与报告拼装。

时间锚点：夹具数据落在 2026-08-13~15。
weekly 用 now=08-22（上周=08-10~16）、daily 用 now=08-16（昨日=08-15）、
monthly 用 now=09-01（上月=八月），三种频率的统计区间均覆盖数据。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from conftest import G1, NOW, PLUGIN_DIR, TZ
from insight import report
from insight.service import StatisticsService


def dt(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


# ---------- next_report_dt ----------

def test_next_daily_before_and_after_time():
    assert report.next_report_dt(dt(2026, 8, 15, 7), "daily", 1, 1, "08:00").date() == date(2026, 8, 15)
    assert report.next_report_dt(dt(2026, 8, 15, 12), "daily", 1, 1, "08:00").date() == date(2026, 8, 16)


def test_next_weekly_boundaries():
    now = dt(2026, 8, 15, 12)  # 周六
    t = report.next_report_dt(now, "weekly", 1, 1, "08:00")  # 本周一(08-10)已过
    assert (t.year, t.month, t.day, t.hour, t.minute) == (2026, 8, 17, 8, 0)
    assert report.next_report_dt(now, "weekly", 7, 1, "09:30").date() == date(2026, 8, 16)
    assert report.next_report_dt(dt(2026, 8, 15, 7), "weekly", 6, 1, "08:00").date() == date(2026, 8, 15)
    assert report.next_report_dt(dt(2026, 8, 15, 12), "weekly", 6, 1, "08:00").date() == date(2026, 8, 22)


def test_next_monthly_future_passed_and_clamp():
    now = dt(2026, 1, 20, 10)
    assert report.next_report_dt(now, "monthly", 1, 25, "08:00").date() == date(2026, 1, 25)
    assert report.next_report_dt(dt(2026, 1, 26, 10), "monthly", 1, 25, "08:00").date() == date(2026, 2, 25)
    # 31 日在 2 月钳制为当月最后一天（2026 非闰年 → 28）
    assert report.next_report_dt(dt(2026, 2, 1, 10), "monthly", 1, 31, "08:00").date() == date(2026, 2, 28)
    # 12 月顺延到次年 1 月
    assert report.next_report_dt(dt(2026, 12, 31, 23), "monthly", 1, 15, "08:00").date() == date(2027, 1, 15)


def test_next_report_invalid_args():
    now = dt(2026, 8, 15, 12)
    with pytest.raises(ValueError, match="频率"):
        report.next_report_dt(now, "hourly", 1, 1, "08:00")
    with pytest.raises(ValueError, match="星期"):
        report.next_report_dt(now, "weekly", 0, 1, "08:00")
    with pytest.raises(ValueError, match="日期"):
        report.next_report_dt(now, "monthly", 1, 32, "08:00")
    with pytest.raises(ValueError, match="时间"):
        report.next_report_dt(now, "daily", 1, 1, "8点")


def test_hhmm_flexible_forms():
    # 单数字时/分也接受（线上实测有用户填 "18:5"）：18:5 = 18:05
    t = report.next_report_dt(dt(2026, 8, 15, 7), "daily", 1, 1, "18:5")
    assert t.strftime("%H:%M") == "18:05"
    assert report.next_report_dt(dt(2026, 8, 15, 7), "daily", 1, 1, "9:5").strftime("%H:%M") == "09:05"
    with pytest.raises(ValueError, match="时间"):
        report.next_report_dt(dt(2026, 8, 15, 7), "daily", 1, 1, "24:00")


# ---------- build_report ----------

def make_svc(repo, stopwords, tmp_path, now_ts):
    return StatisticsService(
        repo,
        stopwords=stopwords,
        output_dir=tmp_path / "out",
        plugin_dir=PLUGIN_DIR,
        now_ts=now_ts,
    )


def test_build_report_weekly(repo, stopwords, tmp_path):
    svc = make_svc(repo, stopwords, tmp_path, NOW + 7 * 86400)  # 08-22
    title, text, image_path = report.build_report(svc, G1, report.SECTIONS, None, 0, "weekly")
    assert "上周群报" in title
    assert "发言榜" in text and "高频关键词" not in text  # 关键词分节已砍：词云即其可视化
    assert image_path is not None and image_path.exists()


def test_build_report_daily_and_monthly(repo, stopwords, tmp_path):
    svc_d = make_svc(repo, stopwords, tmp_path, NOW + 86400)  # 08-16：昨日=08-15
    title, text, image = report.build_report(svc_d, G1, ["summary", "rank"], None, 0, "daily")
    assert "昨日群报" in title and "发言榜" in text and image is None
    svc_m = make_svc(repo, stopwords, tmp_path, NOW + 17 * 86400)  # 09-01：上月=八月
    title, m_text, _image = report.build_report(svc_m, G1, ["summary"], None, 0, "monthly")
    assert "上月群报" in title
    assert "消息量" in m_text and "峰值日" in m_text  # monthly 正文渲染不可只断标题


def test_build_report_section_selection_and_skip(repo, stopwords, tmp_path):
    svc = make_svc(repo, stopwords, tmp_path, NOW + 7 * 86400)
    _t, text, image_path = report.build_report(svc, G1, ["rank"], None, 0, "weekly")
    assert "发言榜" in text and "高频关键词" not in text and image_path is None
    assert report.build_report(svc, G1, report.SECTIONS, None, 100000, "weekly") is None
    assert report.build_report(svc, G1, report.SECTIONS, None, 0, "weekly") is not None
    title, text, image_path = report.build_report(svc, G1, [], None, 0, "weekly")
    assert "上周群报" in title and text == "" and image_path is None
