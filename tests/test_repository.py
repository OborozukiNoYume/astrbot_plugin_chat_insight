"""repository：计数口径 / 索引化聚合 / JSON 结构化统计 / 坏 JSON 容错。"""


import pytest
from conftest import BOT, G1, G2, NOW, TZ, U1, U2, build_db, ts
from insight.db import ChatlogDB, DatabaseNotAvailable, SchemaIncompatible
from insight.repository import ChatlogRepository
from insight.timeutil import resolve_range, tz_offset_seconds


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
    # 合成库 G1 内：user 非唤醒 23（u1 20 + u2 3... 见 conftest）；
    # bot 1 条、G2 1 条、私聊 1 条、G1 唤醒 3 条——任何一维泄漏计数都会偏离 23
    assert repo.get_message_count(week, group_id=G1) == 23


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





def test_media_stats(repo, week):
    media = repo.get_media_stats(week, group_id=G1)
    assert media.count("image") == 1
    assert media.count("face") == 2
    assert media.count("at") == 1
    assert media.count("video") == 0




def test_fetch_texts_limit_and_order(repo, week):
    all_rows = repo.fetch_texts(week, group_id=G1, limit=10000)
    limited = repo.fetch_texts(week, group_id=G1, limit=3)
    assert len(limited) == 3
    assert len(all_rows) > 3
    assert limited[0] == all_rows[0]  # 同为最新优先



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


def test_reply_pairs_follow_waked_config(tmp_path):
    """互动对口径随群统计：默认排除唤醒消息（含唤醒式回复），关闭开关恢复计入。"""
    import json as json_mod

    def row(n, user, reply_user, waked):
        content = json_mod.dumps([{"t": "plain", "x": "r"}], ensure_ascii=False)
        t = NOW - 100 + n
        return (
            f"rp{n}", "test", "group", f"test:GroupMessage:{G1}", G1, user, user,
            "user", waked, "r", content, 32, "mX", reply_user, t, t,
        )

    rows = [
        row(1, U1, U2, 0),   # 普通回复：恒计入
        row(2, U1, U2, 1),   # 唤醒式回复：默认排除
        row(3, U2, U1, 0),
    ]
    r = ChatlogRepository(ChatlogDB(build_db(tmp_path / "pairs.db", rows=rows)))
    week = resolve_range("7天", TZ, now_ts=NOW)
    # 平局顺序不保证，按键值断言
    assert {(a, b): c for a, b, c in r.get_group_reply_pairs(G1, week)} == {
        (U1, U2): 1, (U2, U1): 1,
    }
    r.exclude_waked = False
    assert {(a, b): c for a, b, c in r.get_group_reply_pairs(G1, week)} == {
        (U1, U2): 2, (U2, U1): 1,
    }


def test_db_stats(repo):
    """库概况（/画像维护 状态 的数据源）。"""
    s = repo.db_stats()
    with repo.db.connect() as conn:
        total = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
    assert s["total"] == total
    assert s["by_type"]["user"] > 0 and s["by_type"]["bot"] == 1
    # 最早行为 08-12 的坏 JSON 样本，最晚行 08-15 12:50
    assert s["oldest"] == ts(2026, 8, 12, 12, 30)
    assert s["newest"] == ts(2026, 8, 15, 12, 50)


def repo_all_count(repo, week):
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
