"""个人词云口语触发（极窄模式，仅此一处口语入口）。

支持形态：
    我的词云            → /词云 user me 今日
    我的<时间>词云      → /词云 user me <时间>（如 我的历史词云 / 我的本周词云）
    @某人 <时间>词云    → /词云 user <被@者> <时间>（@ 由命令层从消息组件提取）

其余情况一律不触发（包括裸"词云"），群友普通聊天零干扰。
"""

from __future__ import annotations

import re

# 平台把消息链里的 At 段渲染成 "@名字(QQ号)" 文本，可能混入命令参数；
# 命令解析遇到该形态时提取 QQ 号作为目标用户（兼容全角括号）
AT_RENDER_RE = re.compile(r"^@.+?[(（](\d{1,20})[)）]$")

# 时间关键词（长词在前，防"上月"吃掉"上上月"类歧义）
_TIME_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?:最近|近)\s*(\d{1,3})\s*[天日]"), "n_days"),
    (re.compile(r"历史|总榜|全部"), "all"),
    (re.compile(r"今年|本年"), "year"),
    (re.compile(r"最近半年|近半年|半年前|半年"), "halfyear"),
    (re.compile(r"本季度|这季度"), "quarter"),
    (re.compile(r"上季度"), "lastquarter"),
    (re.compile(r"本月|这个月"), "month"),
    (re.compile(r"上月"), "lastmonth"),
    (re.compile(r"本周|这周"), "week"),
    (re.compile(r"上周"), "lastweek"),
    (re.compile(r"昨日|昨天"), "yesterday"),
    (re.compile(r"今日|今天"), "today"),
]

_DEFAULT_SPEC = "today"


def match_wordcloud(text: str, max_len: int = 40) -> tuple[bool, str, int | None] | None:
    """识别个人词云口语说法。

    返回 (personal, time_spec, top_n)：personal=True 表示"我的词云"（当前用户），
    False 表示需要配合消息中的 @目标（由命令层解析 At 组件）；
    top_n 为可选词数上限（独立数字，如「我的词云 历史 30」的 30）。
    不匹配返回 None。
    """
    text = (text or "").strip()
    if "词云" not in text or len(text) > max_len or "·" in text:
        return None
    personal = "我的" in text
    rest = text
    spec = _DEFAULT_SPEC
    for pattern, kind in _TIME_KEYWORDS:
        m = pattern.search(rest)
        if m:
            spec = f"{m.group(1)}天" if kind == "n_days" else kind
            rest = rest[: m.start()] + " " + rest[m.end() :]
            break
    # 时间词剔除后的独立数字（1-50）视为词数上限
    top_n = None
    m = re.search(r"(?:^|\s)(\d{1,2})(?:\s|$)", rest)
    if m and 1 <= int(m.group(1)) <= 50:
        top_n = int(m.group(1))
    return personal, spec, top_n
