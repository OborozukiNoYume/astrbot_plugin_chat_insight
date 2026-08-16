"""个人词云口语触发匹配。"""

from insight.colloquial import match_wordcloud


def test_personal_default_today():
    assert match_wordcloud("我的词云") == (True, "today", None)
    assert match_wordcloud("看看我的词云") == (True, "today", None)


def test_personal_with_time():
    assert match_wordcloud("我的历史词云") == (True, "all", None)
    assert match_wordcloud("我的总榜词云") == (True, "all", None)
    assert match_wordcloud("我的今日词云") == (True, "today", None)
    assert match_wordcloud("我的本周词云") == (True, "week", None)
    assert match_wordcloud("我的上周词云") == (True, "lastweek", None)
    assert match_wordcloud("我的今年词云") == (True, "year", None)
    assert match_wordcloud("我的半年词云") == (True, "halfyear", None)
    assert match_wordcloud("我的近7天词云") == (True, "7天", None)


def test_at_target_form():
    # personal=False：需要命令层从 At 组件取目标
    assert match_wordcloud("历史词云") == (False, "all", None)
    assert match_wordcloud("词云") == (False, "today", None)
    assert match_wordcloud("本周词云") == (False, "week", None)


def test_topn_extracted():
    assert match_wordcloud("我的词云 历史 30") == (True, "all", 30)
    assert match_wordcloud("我的词云 30") == (True, "today", 30)
    assert match_wordcloud("我的近7天词云") == (True, "7天", None)  # 7 属于时间词，不是词数
    assert match_wordcloud("我的词云") == (True, "today", None)
    assert match_wordcloud("历史词云 20") == (False, "all", 20)


def test_no_match():
    assert match_wordcloud("中午吃什么") is None
    assert match_wordcloud("哈哈哈") is None
    assert match_wordcloud("☁️ 词云 · 今日 · 全群 · 词次 169") is None  # 复读防护
    assert match_wordcloud("词云" + "好" * 40) is None  # 超长
    assert match_wordcloud("") is None
