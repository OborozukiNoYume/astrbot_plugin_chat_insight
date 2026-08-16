"""service：聚合口径 / 业务规则（低基数、昼夜归一化）/ 词云与降级。"""

import pytest

from insight.service import ServiceError, StatisticsService

from conftest import G1, PLUGIN_DIR, NOW, build_db


@pytest.fixture
def week(service):
    return service.resolve("7天")


def test_resolve_rejects_over_max(repo, stopwords, tmp_path):
    svc = StatisticsService(repo, stopwords=stopwords, max_query_days=3)
    with pytest.raises(ServiceError, match="最多 3 天"):
        svc.resolve("7天")


def test_summary(service, week):
    s = service.summary(week, G1)
    assert s["messages"] > 0
    assert s["active_users"] == 2
    assert s["peak_date"] == "2026-08-15"
    assert s["span_days"] == 7


def test_summary_requires_group(service, week):
    with pytest.raises(ServiceError, match="group"):
        service.summary(week, "")


def test_summary_unknown_group(service, week):
    with pytest.raises(ServiceError, match="没有聊天记录"):
        service.summary(week, "99999")


def test_trend_fills_zero_days(service, week):
    days = service.trend(week, G1)
    assert len(days) == 7
    assert days[0].date == "2026-08-09" and days[0].messages == 0
    assert {d.date for d in days if d.messages > 0} == {"2026-08-12", "2026-08-13", "2026-08-14", "2026-08-15"}


def test_hours(service, week):
    buckets = service.hours(week, G1)
    assert len(buckets) == 24 and sum(buckets) > 0


def test_rank(service, week):
    entries, total = service.rank(week, G1, 10)
    assert entries[0].user_name == "三哥"
    assert sum(e.count for e in entries) == total


def test_keywords_topn(service, week):
    pairs, total = service.keywords(week, G1, None, 5)
    assert len(pairs) <= 5
    # "AI" 仅出现在 u1 的用户消息里：13/14/15 三天 6+6+7=19 次
    d = dict(pairs)
    assert d["AI"] == 19


def test_keywords_user_scope(service, week):
    pairs, _ = service.keywords(week, G1, "222", 10)
    assert dict(pairs).get("记录") == 1


def test_kw_trend_percentage_and_low_base(service):
    # 今日(08-15) vs 昨日(08-14)：
    #   AI 7 vs 6 → +16.7%；显卡 6 vs 1 → 低基数；模型 0 vs 6 → 归零
    rows, cur, prev = service.kw_trend("today", G1, 10)
    by_word = {w: (c, p, ch) for w, c, p, ch in rows}
    assert by_word["AI"] == (7, 6, "+16.7%")
    assert by_word["显卡"] == (7, 1, "低基数")
    assert by_word["模型"] == (0, 6, "归零")
    assert cur.label == "今日" and prev.label == "昨日"


def test_kw_trend_new_word(service):
    rows, _, _ = service.kw_trend("today", G1, 50)
    by_word = {w: ch for w, _, _, ch in rows}
    assert by_word["好文"] == "新增"  # 只在 08-15 出现（URL 消息）


def test_daynight_normalized(service, week):
    result = service.daynight(week, G1, 10)
    # 白天词只在白天出现、夜话只在夜间出现
    day_words = {w for w, _ in result["day_distinctive"]}
    night_words = {w for w, _ in result["night_distinctive"]}
    assert "显卡" in day_words
    assert "睡觉" in night_words
    assert "睡觉" not in day_words
    # 归一化数值口径：每千条消息出现次数
    day_top = dict(result["day_top"])
    if "显卡" in day_top:
        assert day_top["显卡"] > 0


def test_trend_rejects_huge_range(service):
    r = service.resolve("历史")
    with pytest.raises(ServiceError, match="范围太长"):
        service.trend(r, G1)


def test_summary_history_uses_days_with_data(service):
    r = service.resolve("历史")
    s = service.summary(r, G1)
    assert s["span"] == "有记录以来"
    assert s["span_days"] == s["days_with_data"]
    assert s["messages"] > 0


def test_kw_trend_history_rejected(service):
    with pytest.raises(ServiceError, match="没有上一对比区间"):
        service.kw_trend("历史", G1, 10)


def test_emoji_stats(service, week):
    pairs = service.emoji_stats(week, G1, 10)
    d = dict(pairs)
    assert d["😂"] == 2 and d["🤣"] == 1 and d["👨‍👩‍👧‍👦"] == 1


def test_face_stats(service, week):
    rows = service.face_stats(week, G1, 10)
    assert dict(rows).get(123) == 2


def test_media_and_type_stats(service, week):
    media, types = service.media_stats(week, G1)
    assert media.count("image") == 1
    assert types.get("group") == media.messages


def test_length_stats(service, week):
    s = service.length_stats(week, G1)
    assert s["n"] > 0
    assert s["max"] == 108
    assert s["long_ratio"] > 0
    assert sum(s["distribution"].values()) == s["n"]


def test_forward_stats(service, week):
    fwd_total, msg_total, entries = service.forward_stats(week, G1, 10)
    assert fwd_total == 3
    assert {e.user_id: e.count for e in entries} == {"111": 2, "222": 1}


def test_wordcloud_renders_png(service, week, tmp_path):
    image_path, pairs, total = service.wordcloud(week, G1, None, 30)
    assert image_path is not None and image_path.exists()
    assert image_path.suffix == ".png"
    assert pairs and total > 0


def test_wordcloud_user_scope(service, week):
    image_path, pairs, _ = service.wordcloud(week, G1, "222", 30)
    assert dict(pairs).get("记录") == 1


def test_wordcloud_topn_limits_image_words(service, week):
    from unittest.mock import patch
    from insight import render as render_mod
    captured = {}
    real = render_mod.render_wordcloud

    def spy(freq, out_path, font_path, max_words=80, **kw):
        captured["n_words"] = len(freq)
        captured["max_words"] = max_words
        return real(freq, out_path, font_path, max_words=max_words, **kw)

    with patch.object(render_mod, "render_wordcloud", spy):
        service.wordcloud(week, G1, None, 5)  # top_n=5（小于默认 80）
    assert captured["n_words"] <= 5 and captured["max_words"] == 5


def test_wordcloud_no_font_falls_back_with_error(repo, stopwords, tmp_path, week):
    svc = StatisticsService(
        repo,
        stopwords=stopwords,
        output_dir=tmp_path / "out",
        plugin_dir=tmp_path,  # 无 assets/fonts
    )
    svc._font = None  # 跳过探测，视为未找到字体（避免依赖宿主机系统字体）
    with pytest.raises(ServiceError, match="字体"):
        svc.wordcloud(week, G1, None, 10)


def test_wordcloud_disabled_returns_text_only(repo, stopwords, tmp_path, week):
    svc = StatisticsService(
        repo,
        stopwords=stopwords,
        output_dir=tmp_path / "out",
        plugin_dir=tmp_path,
        wordcloud_enabled=False,
    )
    image_path, pairs, total = svc.wordcloud(week, G1, None, 10)
    assert image_path is None
    assert pairs


def test_wordcloud_empty_range(service):
    r = service.resolve("1天")  # 08-15 当天有数据，改用未来空区间
    from insight.timeutil import TimeRange

    future = TimeRange(r.start_ts + 86400 * 30, r.start_ts + 86400 * 31, service.tz, "未来")
    with pytest.raises(ServiceError, match="没有可用于词云"):
        service.wordcloud(future, G1, None, 10)
