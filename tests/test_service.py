"""service：聚合口径 / 业务规则（低基数、昼夜归一化）/ 词云与降级。"""

import pytest
from conftest import G1
from insight.service import ServiceError, StatisticsService


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


def test_rank(service, week):
    entries, total = service.rank(week, G1, 10)
    assert entries[0].user_name == "三哥"
    assert sum(e.count for e in entries) == total


def test_summary_history_uses_days_with_data(service):
    r = service.resolve("历史")
    s = service.summary(r, G1)
    assert s["span"] == "有记录以来"
    assert s["span_days"] == s["days_with_data"]
    assert s["messages"] > 0




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


def test_wordcloud_prunes_expired_pngs(repo, stopwords, tmp_path, week):
    import os
    import time as time_mod

    out = tmp_path / "out"
    out.mkdir()
    expired = out / "wc_g10001_0.png"
    fresh = out / "wc_g10001_9999999999.png"
    for f in (expired, fresh):
        f.write_bytes(b"x")
    past = time_mod.time() - 30 * 86400
    os.utime(expired, (past, past))  # 30 天前，超出默认 7 天保留期

    svc = StatisticsService(
        repo,
        stopwords=stopwords,
        output_dir=out,
        plugin_dir=tmp_path,
        wordcloud_enabled=False,  # 文本模式同样执行清理
    )
    svc.wordcloud(week, G1, None, 10)
    assert not expired.exists()
    assert fresh.exists()


def test_wordcloud_prune_disabled_keeps_all(repo, stopwords, tmp_path, week):
    import os
    import time as time_mod

    out = tmp_path / "out"
    out.mkdir()
    expired = out / "wc_g10001_0.png"
    expired.write_bytes(b"x")
    past = time_mod.time() - 30 * 86400
    os.utime(expired, (past, past))

    svc = StatisticsService(
        repo,
        stopwords=stopwords,
        output_dir=out,
        plugin_dir=tmp_path,
        wordcloud_enabled=False,
        wordcloud_retention_days=0,  # 关闭自动清理
    )
    svc.wordcloud(week, G1, None, 10)
    assert expired.exists()


def test_wordcloud_empty_range(service):
    r = service.resolve("1天")  # 08-15 当天有数据，改用未来空区间
    from insight.timeutil import TimeRange

    future = TimeRange(r.start_ts + 86400 * 30, r.start_ts + 86400 * 31, service.tz, "未来")
    with pytest.raises(ServiceError, match="没有可用于词云"):
        service.wordcloud(future, G1, None, 10)
