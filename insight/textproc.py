"""文本清洗 / 分词 / 停用词。

取词口径：只用 content_json 的 plain 段（结构化文本），不用 content
（content 混有 @名字(QQ号) 渲染文本，且空文本=纯媒体消息）。
关键词计数口径：token 出现次数（非包含该词的消息数）。
"""

from __future__ import annotations

import json
import re
import string
from collections import Counter
from pathlib import Path

import emoji as emoji_lib
import jieba

URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
AT_RE = re.compile(r"@[^\s@，。,：:；；!！?？]+")
# 纯数字/小数/百分号（含全角）一律过滤
NUMERIC_RE = re.compile(r"^[0-9０-９.．%％，，]+$")
WHITESPACE_RE = re.compile(r"\s+")

# 中英文常见标点，用于剥边与整词过滤
_PUNCT = set(
    string.punctuation
    + "！？｡。＂＃＄％＆＇（）＊＋，－／：；＜＝＞＠［＼］＾＿｀｛｜｝～"
    + "｟｠｢｣､、〃〈〉《》「」『』【】〔〕〖〗〘〙〚〛〜〝〞〟–—‘’‛“”„‟…‧﹏·"
)


def extract_plain_texts(content_json: str | None) -> list[str]:
    """content_json（白名单段数组）→ plain 段文本列表。坏 JSON / 非 plain 段跳过。"""
    if not content_json:
        return []
    try:
        segments = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(segments, list):
        return []
    texts = []
    for seg in segments:
        if isinstance(seg, dict) and seg.get("t") == "plain":
            x = seg.get("x")
            if isinstance(x, str) and x:
                texts.append(x)
    return texts


def clean_text(text: str) -> str:
    """去 URL / @提及 / Emoji，压缩空白。"""
    text = URL_RE.sub(" ", text)
    text = AT_RE.sub(" ", text)
    try:
        text = emoji_lib.replace_emoji(text, " ")
    except Exception:  # emoji 库异常不应阻断分词
        pass
    return WHITESPACE_RE.sub(" ", text).strip()


def tokenize(text: str, stopwords) -> list[str]:
    """jieba 分词 + 过滤规则。返回保留的 token（保留原大小写用于展示）。"""
    tokens = []
    for raw in jieba.cut(text):
        tok = raw.strip()
        # 剥除两端标点
        while tok and tok[0] in _PUNCT:
            tok = tok[1:]
        while tok and tok[-1] in _PUNCT:
            tok = tok[:-1]
        if len(tok) < 2:  # 单字 / 单字母 / 空串
            continue
        if all(ch in _PUNCT for ch in tok):
            continue
        if NUMERIC_RE.match(tok):
            continue
        if tok.lower() in stopwords:
            continue
        tokens.append(tok)
    return tokens


def count_keywords(plain_texts, stopwords) -> Counter:
    """关键词频次：token 出现次数。"""
    counter: Counter = Counter()
    for text in plain_texts:
        counter.update(tokenize(clean_text(text), stopwords))
    return counter


def load_stopwords(
    builtin_path: Path | None, extra_words=(), custom_path: str | None = None
) -> frozenset[str]:
    """内置停用词 + 配置附加词 + 用户自定义文件（每行一词，# 注释）。"""
    words: set[str] = set()
    for path in [builtin_path, Path(custom_path) if custom_path else None]:
        if not path:
            continue
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                w = line.strip().lower()
                if w and not w.startswith("#"):
                    words.add(w)
        except OSError:
            continue
    words.update(str(w).strip().lower() for w in extra_words if w)
    return frozenset(words)
