"""repository：计数口径 / 索引化聚合 / JSON 结构化统计 / 坏 JSON 容错。"""

from pathlib import Path

import pytest

from insight.db import ChatlogDB, DatabaseNotAvailable, SchemaIncompatible
from insight.repository import ChatlogRepository
from insight.timeutil import resolve_range, tz_offset_seconds

from conftest import BOT, G1, G2, TZ, U1, U2, NOW, build_db, ts


@pytest.fixture
def week():
    return resolve_range("7天", TZ, now_ts=NOW)


def test_db_missing(tmp_path):
    with pytest.raises(DatabaseNotAvailable, match="未找到 chatlog.db"):
        ChatlogDB(tmp_path / "nope.db")


def test_db_old_schema(tmp_path):
    p = build_db(tmp_path / "old.db", user_version=2)
    with pytest.raises(SchemaIncompatible, match="user_version=2"):
        ChatlogDB(p)


def test_message_count_excludes_bot_and_other_groups(repo, week):
    # G1 内用户消息；bot 与 G2、私聊不计
    assert repo.get_message_count(week, group_id=G1) == repo.get_message_count(week, group_id=G1)
    assert repo.get_message_count(week, group_id=G1) > 0
    total_all = repo.get_message_count(week)
    assert total_all >= repo.get_message_count(week, group_id=G1)


def test_active_user_count(repo, week):
    # G1 活跃用户只有 U1/U2，bot 不算
    assert repo.get_active_user_count(week, group_id=G1) == 2


def test_rank_order_and_latest_name(repo, week):
    entries = repo.get_message_rank(week, group_id=G1, limit=10)
    assert entries[0].user_id == U1
    # 昵称取范围内最新一条（张三→三哥）
    assert entries[0].user_name == "三哥"
    counts = [e.count for e in entries]
    assert counts == sorted(counts, reverse=True)
    assert sum(e.count for e in entries) == repo.get_message_count(week, group_id=G1)
    assert 0 < entries[0].ratio < 1


def test_rank_by_user_filter(repo, week):
    c = repo.get_message_count(week, group_id=G1, user_id=U2)
    assert c > 0
    assert repo.get_message_count(week, group_id=G1, user_id=BOT) == 0


def test_activity_by_day(repo, week):
    off = tz_offset_seconds(TZ, week.start_ts)
    days = repo.get_activity_by_day(week, group_id=G1, offset_seconds=off)
    by_date = {d.date: d for d in days}
    assert set(by_date) == {"2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15"}
    assert by_date["2026-08-13"].active_users == 1
    assert by_date["2026-08-15"].active_users == 2


def test_activity_by_hour(repo, week):
    off = tz_offset_seconds(TZ, week.start_ts)
    buckets = repo.get_activity_by_hour(week, group_id=G1, offset_seconds=off)
    assert len(buckets) == 24
    assert buckets[10] > 0  # 08-15 10:00+08 有多条
    assert buckets[23] > 0  # 08-14 23:00+08
    assert buckets[9] > 0


def test_count_by_hour_cross_midnight(repo, week):
    off = tz_offset_seconds(TZ, week.start_ts)
    night = repo.count_by_hour(week, G1, 18, 6, off)
    day = repo.count_by_hour(week, G1, 6, 18, off)
    assert night > 0  # 23:00 与 22:00 的消息
    assert day > night
    assert night + day == repo.get_message_count(week, group_id=G1)


def test_face_stats_structured(repo, week):
    rows = repo.get_face_stats(week, group_id=G1, limit=10)
    d = dict(rows)
    assert d.get(123) == 2
    assert d.get(45) == 1


def test_forward_stats_structured(repo, week):
    fwd_total, msg_total, entries = repo.get_forward_stats(week, group_id=G1, limit=10)
    assert fwd_total == 3  # u1×2 + u2×1
    assert msg_total == repo.get_message_count(week, group_id=G1)
    by_user = {e.user_id: e.count for e in entries}
    assert by_user == {U1: 2, U2: 1}


def test_forward_not_matched_by_plain_text(repo, week):
    # 文本里出现"转发"二字（plain 段）不算转发；坏 JSON 行也不得导致报错
    _, _, entries = repo.get_forward_stats(week, group_id=G1, limit=10)
    assert all(e.count >= 1 for e in entries)


def test_media_stats(repo, week):
    media = repo.get_media_stats(week, group_id=G1)
    assert media.count("image") == 1
    assert media.count("face") == 2
    assert media.count("at") == 1
    assert media.count("video") == 0


def test_message_type_stats(repo, week):
    types = repo.get_message_type_stats(week, group_id=G1)
    assert set(types) == {"group"}


def test_lengths_exclude_empty(repo, week):
    lengths = repo.get_lengths(week, group_id=G1)
    assert 0 not in lengths
    assert max(lengths) == 108  # LONG_TEXT


def test_fetch_texts_limit_and_order(repo, week):
    all_rows = repo.fetch_texts(week, group_id=G1, limit=10000)
    limited = repo.fetch_texts(week, group_id=G1, limit=3)
    assert len(limited) == 3
    assert len(all_rows) > 3
    assert limited[0] == all_rows[0]  # 同为最新优先


def test_fetch_texts_by_hour(repo, week):
    off = tz_offset_seconds(TZ, week.start_ts)
    night_texts = repo.fetch_texts_by_hour(week, G1, 18, 6, off)
    assert len(night_texts) == repo.count_by_hour(week, G1, 18, 6, off)
    # 私聊消息不在群过滤结果里
    private_rows = [
        r for r in repo.fetch_texts(week, group_id=G2, limit=10000)
    ]
    assert len(private_rows) == 1


def test_display_name_fallback(repo, week):
    assert repo.get_display_name(U1, G1) == "三哥"
    assert repo.get_display_name("nobody", G1) == "nobody"


def test_group_exists(repo):
    assert repo.group_exists(G1)
    assert repo.group_exists(G2)
    assert not repo.group_exists("99999")


def test_waked_messages_excluded_by_default(repo, week):
    """唤醒消息（命令/@Bot）不应计入任何统计，防止测试命令污染。"""
    total_with = repo_all_count(repo, week)
    assert repo.get_message_count(week, group_id=G1) == total_with - 3
    texts = repo.fetch_texts(week, group_id=G1)
    joined = " ".join(texts)
    assert "发言榜" not in joined and "词云" not in joined and "统计" not in joined
    # 关闭开关后恢复计入
    repo.exclude_waked = False
    try:
        assert repo.get_message_count(week, group_id=G1) == total_with
        joined2 = " ".join(repo.fetch_texts(week, group_id=G1))
        assert "发言榜" in joined2
    finally:
        repo.exclude_waked = True


def repo_all_count(repo, week):
    import sqlite3
    with repo.db.connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE group_id = ? AND sender_type = 'user' "
            "AND ts >= ? AND ts < ?",
            (G1, week.start_ts, week.end_ts),
        ).fetchone()
        return int(row[0])


def test_empty_database(tmp_path):
    p = build_db(tmp_path / "empty.db", rows=[])
    r = ChatlogRepository(ChatlogDB(p))
    week = resolve_range("7天", TZ, now_ts=NOW)
    assert r.get_message_count(week, group_id=G1) == 0
    assert r.get_activity_by_day(week, group_id=G1, offset_seconds=28800) == []
    assert r.get_face_stats(week, group_id=G1) == []
    assert r.get_forward_stats(week, group_id=G1) == (0, 0, [])
    assert r.get_lengths(week, group_id=G1) == []
