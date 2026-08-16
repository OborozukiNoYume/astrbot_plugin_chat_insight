"""textproc：清洗 / 分词过滤规则 / 关键词口径 / Emoji 图形簇 / 停用词加载。"""

import json

from insight import textproc

STOP = frozenset({"我们", "哈哈", "the", "ai"})
STOP_NO_AI = frozenset({"我们", "哈哈", "the"})


def test_extract_plain_texts():
    cj = json.dumps(
        [{"t": "at", "qq": "1"}, {"t": "plain", "x": "捏你"}, {"t": "face", "id": "5"}, {"t": "plain", "x": "揉你"}],
        ensure_ascii=False,
    )
    assert textproc.extract_plain_texts(cj) == ["捏你", "揉你"]


def test_extract_plain_texts_tolerates_bad_json():
    assert textproc.extract_plain_texts('[{"t": "plai') == []
    assert textproc.extract_plain_texts(None) == []
    assert textproc.extract_plain_texts("not json") == []
    assert textproc.extract_plain_texts(json.dumps({"t": "plain"})) == []


def test_clean_text_removes_url_at_emoji():
    s = textproc.clean_text("看 https://example.com/a 和 @某人 😂好文")
    assert "https" not in s
    assert "@" not in s
    assert "😂" not in s
    assert "好文" in s


def test_tokenize_filters():
    tokens = textproc.tokenize(
        "我们 AI ai 今天666 3.14 测试！！！ --- 哈哈", STOP
    )
    assert "我们" not in tokens
    assert "AI" not in tokens      # ai 在停用词（小写匹配），展示层保留原大小写
    assert "666" not in tokens     # 纯数字
    assert "3.14" not in tokens
    assert "哈哈" not in tokens
    assert "测试" in tokens
    assert all(len(t) >= 2 for t in tokens)


def test_tokenize_keeps_cjk_len2_and_drops_single():
    tokens = textproc.tokenize("显卡 好 显 卡", STOP)
    assert tokens == ["显卡"]


def test_tokenize_strips_edge_punct():
    assert "测试" in textproc.tokenize("「测试」", STOP)


def test_count_keywords_occurrence_semantics():
    # 口径：token 出现次数（"AI AI AI" = 3，而不是 1 条消息）
    counter = textproc.count_keywords(["AI AI AI", "AI"], STOP_NO_AI)
    assert counter["AI"] == 4


def test_count_emoji_clusters():
    counter = textproc.count_emoji(["😂😂🤣👨‍👩‍👧‍👦"])
    assert counter["😂"] == 2
    assert counter["🤣"] == 1
    assert counter["👨‍👩‍👧‍👦"] == 1  # ZWJ 组合算一个 emoji


def test_count_emoji_mixed_text():
    counter = textproc.count_emoji(["吃饭了吗😂和🤣🤣"])
    assert counter["😂"] == 1
    assert counter["🤣"] == 2


def test_load_stopwords(tmp_path):
    custom = tmp_path / "extra.txt"
    custom.write_text("# 注释\n自定义词\nThe\n", encoding="utf-8")
    sw = textproc.load_stopwords(None, ["临时词"], str(custom))
    assert "自定义词" in sw
    assert "the" in sw
    assert "临时词" in sw
    assert "#" not in sw
