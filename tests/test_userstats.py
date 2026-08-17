"""用户画像：基础 / 活跃 / 风格 / 关键词 / 互动 / Bot / 昵称 / 群画像。

口径断言依据 conftest 合成数据（NOW=2026-08-15 12:00 +08，周六）。
"""

from __future__ import annotations

import pytest
from conftest import BOT, G1, G2, NOW, TZ, U1, U2, ts
from insight.service import ServiceError, _burst_sizes, _pctl, _streaks
from insight.timeutil import resolve_range


@pytest.fixture
def all_range():
    return resolve_range("all", TZ, now_ts=NOW)


# ---------- 基础 ----------

def test_user_basic_group_scope(service, all_range):
    p = service.user_summary(all_range, U1, G1)
    # G1 范围：19 条群消息（私聊排除，唤醒消息计入行为统计）
    assert p["message_count"] == 19
    assert p["text_message_count"] == 18  # 纯图空文本不计
    assert p["active_days"] == 4  # 08-12(坏JSON行) ~ 08-15
    assert p["active_groups"] == 1  # 全局属性：u1 只在 G1 发过言
    assert p["first_seen"] == ts(2026, 8, 12, 12, 30)
    assert p["last_seen"] == ts(2026, 8, 15, 12, 50)
    assert p["peak_hours"] == [10, 11, 12]


def test_user_basic_all_scope_includes_private(service, all_range):
    p = service.user_summary(all_range, U1, None)
    assert p["message_count"] == 20  # 19 群 + 1 私聊


def test_user_summary_unknown_user(service, all_range):
    with pytest.raises(ServiceError):
        service.user_summary(all_range, "404", G1)


def test_bot_messages_excluded(service, all_range):
    with pytest.raises(ServiceError):
        service.user_summary(all_range, BOT, G1)  # bot 无 user 消息记录


# ---------- 活跃 ----------

def test_user_activity(service, all_range):
    p = service.user_activity(all_range, U1, G1)
    assert p["total"] == 19
    assert p["hour_counts"][11] == 6
    assert p["peak_hours"] == [10, 11, 12]
    # 夜间 18:00–06:00：h23 睡觉 1 条 → 夜间 1/19
    assert p["day_ratio"] == pytest.approx(18 / 19)
    # 周末：08-15（周六）的 12 条
    assert p["weekend_ratio"] == pytest.approx(12 / 19)
    assert p["max_streak_days"] == 4  # 08-12 ~ 08-15 连续
    assert p["current_streak_days"] == 4
    assert p["active_days"] == 4


def test_streaks_helper():
    from datetime import date

    d = date.fromisoformat
    days = [d("2026-08-10"), d("2026-08-11"), d("2026-08-13")]
    # today=08-13：最长 2，当前 1
    assert _streaks(days, d("2026-08-13")) == (2, 1)
    # today=08-14（昨日活跃）：当前从昨日起算 1
    assert _streaks(days, d("2026-08-14")) == (2, 1)
    # today=08-15（隔两天）：当前 0
    assert _streaks(days, d("2026-08-15")) == (2, 0)
    assert _streaks([], d("2026-08-15")) == (0, 0)


# ---------- 风格 ----------

def test_user_style(service, all_range):
    p = service.user_style(all_range, U1, G1)
    assert p["total"] == 19
    assert p["text_total"] == 18
    assert p["p50"] >= 1 and p["p95"] >= p["p50"]
    assert p["long_ratio"] == 0.0  # u1 无 ≥100 字消息
    assert p["media_counts"]["image"] == 1
    assert p["media_counts"]["face"] == 2  # 两条 face 位消息
    assert p["media_counts"]["reply"] == 2
    assert p["burst_count"] == 19  # u1 无 <120s 连发 → 每条一轮
    assert p["max_burst"] == 1


def test_burst_and_pctl_helpers():
    t0 = 1000000
    # [1s, 2s, 130s, 200s, 260s]：1/2 同轮（间隔1s）；130 开新轮（128s>120s），
    # 200（70s）与 260（60s）并入该轮 → 轮大小 [2, 3]
    assert _burst_sizes([t0 + 1, t0 + 2, t0 + 130, t0 + 200, t0 + 260]) == [2, 3]
    assert _pctl([1, 2, 3, 4, 5], 0.5) == 3  # nearest-rank：int(5*0.5)=2 → 第 3 个
    assert _pctl([10], 0.95) == 10
    assert _pctl([], 0.5) == 0


# ---------- 关键词（恒排除唤醒消息） ----------

def test_user_keywords(service, all_range):
    p = service.user_keywords(all_range, U1, G1)
    words = dict(p["all_time"])
    assert words["AI"] == 19
    assert words["显卡"] == 12
    assert p["all_time"][0][0] == "AI"
    # 唤醒命令文本（/发言榜、/统计）与私聊不进入关键词
    assert all("发言榜" not in w and "统计" not in w for w in words)
    # 近 30 天覆盖全部数据 → 与全期一致
    assert dict(p["recent"]).get("AI") == 19


# ---------- 互动 ----------

def test_user_social(service, all_range):
    p = service.user_social(all_range, U1, G1)
    sent = dict(p["reply_sent"])
    recv = dict(p["reply_received"])
    assert sent.get("李四") == 1 and sent.get("AstrBot") == 1  # u1 回复 u2 和 bot
    assert recv.get("李四") == 1  # u2 回复 u1
    mutual = {n: (a, b) for n, a, b in p["mutual"]}
    assert mutual.get("李四") == (1, 1)
    assert dict(p["at_sent"]) == {}  # u1 没发过 at
    assert p["at_received"] == [("李四", 1)]  # u2 at 过 u1


def test_user_social_out_of_scope(service, all_range):
    # u1 在 G2 没有消息记录：范围校验直接给出可读错误
    with pytest.raises(ServiceError):
        service.user_social(all_range, U1, G2)


# ---------- Bot 互动 ----------

def test_user_bot(service, all_range):
    p = service.user_bot(all_range, U1, G1)
    assert p["private_message_count"] == 1
    assert p["private_session_count"] == 1
    assert p["group_message_count"] == 19
    assert p["wake_count"] == 2  # 12:40 / 12:50 两条唤醒
    assert p["reply_bot_count"] == 1
    assert p["at_bot_count"] == 0
    assert p["wake_ratio"] == pytest.approx(2 / 19)
    assert p["wake_hour_counts"][12] == 2


def test_user_bot_at_detection(service, all_range):
    p = service.user_bot(all_range, U2, G1)
    assert p["at_bot_count"] == 1  # u2 的「@AstrBot 我的词云」
    assert p["wake_count"] == 1


# ---------- 昵称 ----------

def test_user_names(service):
    p = service.user_names(U1)
    assert p["current"] == "三哥"
    assert p["change_count"] == 1
    names = {n: (first, last, count) for n, first, last, count in p["entries"]}
    assert names["张三"] == (ts(2026, 8, 13, 10, 0), ts(2026, 8, 13, 11, 0), 2)
    assert names["三哥"][2] == 18


# ---------- 群画像 ----------

def test_group_profile(service):
    p = service.group_profile(G1)
    assert p["group_name"] == "测试群一"
    # 群画像统一群统计口径（默认排除唤醒消息）：26 全量 - 3 唤醒（u1×2 + u2×1）= 23
    assert p["message_count"] == 23
    assert p["active_members"] == 2
    assert p["top_active"][0][0] == "三哥"
    assert p["top_active"][0][1] == 17  # 19 - 2 唤醒
    assert p["top_keywords"][0][0] == "AI"
    pairs = {(a, b): c for a, b, c in p["top_pairs"]}
    assert pairs.get(("李四", "三哥")) == 1
    assert pairs.get(("三哥", "李四")) == 1
    assert pairs.get(("三哥", "AstrBot")) == 1  # 互动对不排除唤醒（含对 Bot 的回复）


def test_group_profile_empty_group(service):
    with pytest.raises(ServiceError):
        service.group_profile("99999")


# ---------- 渲染冒烟 ----------

def test_render_user_outputs(service, all_range):
    from insight import render

    p = service.user_summary(all_range, U1, G1)
    text = render.fmt_user_card("三哥", p, service.tz)
    assert "用户画像 — 三哥" in text and "群 10001" in text
    a = render.fmt_user_activity(service.user_activity(all_range, U1, G1))
    assert "活跃规律" in a and "高峰" in a
    s = render.fmt_user_style(service.user_style(all_range, U1, G1))
    assert "消息风格" in s and "连发" in s
    k = render.fmt_user_keywords(service.user_keywords(all_range, U1, G1))
    assert "讨论关键词" in k and "AI" in k
    soc = render.fmt_user_social(service.user_social(all_range, U1, G1))
    assert "互动关系" in soc and "高频互动对象" in soc
    b = render.fmt_user_bot(service.user_bot(all_range, U1, G1))
    assert "Bot 互动" in b and "私聊会话" in b
    n = render.fmt_user_names(service.user_names(U1), service.tz)
    assert "昵称历史" in n and "张三" in n
    g = render.fmt_group_profile(service.group_profile(G1), service.tz)
    assert "群画像" in g and "非群成员总数" in g


def test_user_full_merges_all_views(service, all_range):
    """user_full 六视图齐全，fmt_user_full 逐段包含各视图标题。"""
    from insight import render

    p = service.user_full(all_range, U1, G1)
    assert set(p) == {"card", "activity", "keywords", "style", "social", "bot"}
    text = render.fmt_user_full("三哥", p, service.tz)
    assert "用户画像 — 三哥" in text
    assert "活跃规律" in text and "讨论关键词" in text
    assert "消息风格" in text and "互动关系" in text and "Bot 互动" in text
    assert text.count("————") == 5  # 六段以分隔线相连


# ---------- json_valid 防护（坏 JSON 行不得使统计崩溃） ----------

def test_bad_json_rows_do_not_crash(service, all_range):
    # conftest 含一行被截断的非法 content_json（08-12），
    # @网络 / face / 转发相关统计均已跑通即证明防护生效
    assert service.user_social(all_range, U1, G1)["at_received"] == [("李四", 1)]
    rows = service.repo.get_user_at_sent(all_range, U1, G1)
    assert rows == {}
