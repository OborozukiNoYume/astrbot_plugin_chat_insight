"""HTML 卡片渲染层：数据组装 / HTML 转义 / 本地模板渲染 / 失败降级。"""

from __future__ import annotations

import asyncio
from zoneinfo import ZoneInfo

import pytest
from conftest import G1, NOW, TZ, U1
from insight import cardrender
from insight.timeutil import resolve_range
from jinja2 import Environment

TZ_FIXED = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def user_full(service):
    r = resolve_range("all", TZ, now_ts=NOW)
    return service.user_full(r, U1, G1)


# ---------- 数据组装 ----------

def test_build_user_card_data(user_full):
    data = cardrender.build_user_card_data("三哥", U1, user_full, TZ)
    assert data["name"] == "三哥"
    assert data["uid"] == U1
    assert data["scope"] == f"群 {G1}"
    assert data["stats"][0]["value"] == "19"  # 消息数与文本版口径一致
    # 24 小时柱：24 根、最高柱 56px、峰时柱打标
    assert len(data["hours"]) == 24
    assert max(h["hpx"] for h in data["hours"]) == 56
    assert {h["h"] for h in data["hours"] if h["peak"]} == {10, 11, 12}
    assert data["hours"][0]["tick"] == "00"
    assert data["hours"][1]["tick"] == ""
    assert len(data["weekdays"]) == 7
    # 媒体构成：0 占比类型被过滤，按占比降序
    keys = [m["label"] for m in data["media"]]
    assert keys and keys == sorted(
        keys, key=lambda k: -next(m["ratio"] for m in data["media"] if m["label"] == k)
    )
    assert all(m["pct"].endswith("%") for m in data["media"])
    # 互动行：有数据的维度才出现
    labels = [r["label"] for r in data["social_rows"]]
    assert "最常回复" in labels and "高频互动对象" in labels


def test_build_uses_text_pipeline_percentiles(user_full):
    """卡片与文本版同源同口径：P50/短消息率直接来自 style 视图。"""
    data = cardrender.build_user_card_data("三哥", U1, user_full, TZ)
    style = user_full["style"]
    by_label = {c["label"]: c["value"] for c in data["style_grid"]}
    assert by_label["P50 长度"] == f"{style['p50']} 字"
    assert by_label["短消息 <10字"] == f"{style['short_ratio'] * 100:.1f}%"


# ---------- 转义：昵称/关键词/互动对象是用户可控文本 ----------

def _fake_full(name: str) -> dict:
    """最小六视图假数据（含待转义的昵称与关键词）。"""
    r = resolve_range("all", TZ, now_ts=NOW)

    def view(**kw):
        return {"range": r, "span": "有记录以来", "scope_label": "全部会话", **kw}

    return {
        "card": view(
            message_count=10, text_message_count=9, active_days=2, active_groups=1,
            span_days=2, first_seen=None, last_seen=None, peak_hours=[12],
            day_ratio=0.5, avg_length_text=5.0, long_ratio=0.0,
            image_ratio=0.1, voice_ratio=0.0,
        ),
        "activity": view(
            total=10, hour_counts=[0] * 24, weekday_counts=[1] * 7, day_ratio=0.5,
            weekend_ratio=0.5, peak_hours=[12], peak_hours_ratio=0.2,
            max_streak_days=2, current_streak_days=1, active_days=2,
        ),
        "keywords": view(all_time=[(f"<b>{name}</b>", 3)], recent=[], recent_window_days=30),
        "style": view(
            total=10, text_total=9, p50=5, p90=20, p95=30, avg_length=6.0,
            short_ratio=0.6, long_ratio=0.0, burst_count=3, avg_burst=2.0,
            max_burst=5, media_counts={}, media_ratios={"at": 0.5}, face_count=0,
        ),
        "social": view(
            reply_sent=[], reply_received=[],
            mutual=[(f"<i>{name}</i>", 2, 1)], at_sent=[], at_received=[],
            at_window_days=90,
        ),
        "bot": view(
            private_message_count=0, private_session_count=0, group_message_count=10,
            wake_count=1, reply_bot_count=0, at_bot_count=0, wake_ratio=0.1,
            wake_hour_counts=[0] * 24,
        ),
    }


def test_escapes_user_controlable_text():
    data = cardrender.build_user_card_data("<script>x", U1, _fake_full("毒"), TZ_FIXED)
    assert data["name"] == "&lt;script&gt;x"
    assert "keywords" not in data  # 关键词已砍：词云覆盖
    assert data["social_rows"][0]["items"][0]["name"] == "&lt;i&gt;毒&lt;/i&gt;"


def test_empty_social_renders_placeholder():
    p = _fake_full("甲")
    p["social"]["mutual"] = []
    data = cardrender.build_user_card_data("甲", U1, p, TZ_FIXED)
    assert data["social_rows"] == []


# ---------- 模板本地渲染（不依赖网络渲染服务） ----------

def test_template_renders_locally(user_full):
    data = cardrender.build_user_card_data("三哥", U1, user_full, TZ)
    html = Environment(autoescape=False).from_string(
        cardrender.load_template("user_profile")
    ).render(data)
    for block in ("活跃规律", "消息风格", "互动关系", "Bot 互动"):
        assert block in html
    assert "讨论关键词" not in html  # 关键词区块已砍：个人词云即其可视化
    assert "三哥" in html and "暂无互动记录" not in html  # 合成数据有互动


def test_template_renders_escaped_name_safely():
    data = cardrender.build_user_card_data("<script>x", U1, _fake_full("毒"), TZ_FIXED)
    html = Environment(autoescape=False).from_string(
        cardrender.load_template("user_profile")
    ).render(data)
    assert "<script>" not in html
    assert "&lt;script&gt;x" in html


# ---------- render_user_card：降级路径 ----------

class _FakeStar:
    def __init__(self, result=None, error: Exception | None = None):
        self.result, self.error = result, error
        self.calls = []

    async def html_render(self, tmpl, data, return_url=True, options=None):
        self.calls.append((tmpl, data, return_url, options))
        if self.error:
            raise self.error
        return self.result


def test_render_user_card_returns_path(user_full, tmp_path):
    real_png = tmp_path / "card.png"
    real_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)  # 魔数校验需真图片头
    star = _FakeStar(result=str(real_png))
    path = asyncio.run(
        cardrender.render_user_card(star, "三哥", U1, user_full, TZ)
    )
    assert path == str(real_png)
    tmpl, data, return_url, options = star.calls[0]
    assert "消息风格" in tmpl  # 模板本体被上传
    assert data["uid"] == U1
    assert return_url is False  # 下载到本地文件而非远端 URL
    assert options == {"type": "png"}


def test_render_user_card_falls_back_on_error(user_full):
    star = _FakeStar(error=RuntimeError("all endpoints failed"))
    assert asyncio.run(
        cardrender.render_user_card(star, "三哥", U1, user_full, TZ)
    ) is None
    assert asyncio.run(
        cardrender.render_user_card(_FakeStar(result=""), "三哥", U1, user_full, TZ)
    ) is None  # 空路径同样视为失败


# ---------- 渲染服务"假图片"响应：错误文本落盘不抛异常 ----------

def test_is_image_file(tmp_path):
    png = tmp_path / "ok.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    jpg = tmp_path / "ok.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 32)
    txt = tmp_path / "err.png"
    txt.write_text("no available server\n")
    assert cardrender._is_image_file(str(png))
    assert cardrender._is_image_file(str(jpg))
    assert not cardrender._is_image_file(str(txt))
    assert not cardrender._is_image_file(str(tmp_path / "absent.png"))


def test_render_rejects_non_image_response(user_full):
    # 渲染服务过载返回 200+文本时框架不抛异常，必须按魔数识别并降级
    bad = _FakeStar(result="/tmp/not_exist.png")  # 文件不存在
    assert asyncio.run(
        cardrender.render_user_card(bad, "三哥", U1, user_full, TZ)
    ) is None


# ---------- 群报卡片 ----------

ALL_SECTIONS = ("summary", "rank", "wordcloud")


def test_build_report_card_data(service):
    data = cardrender.build_report_card_data(
        service, G1, ALL_SECTIONS, None, 0, "daily"
    )
    assert data["group_name"] == "测试群一"
    assert data["period_label"] == "昨日"
    # 发言榜：u1 三哥榜首，条形宽度最大；关键词已并入词云，卡片不再输出
    assert data["rank_rows"][0]["name"] == "三哥"
    assert data["rank_rows"][0]["wpx"] == 100
    assert data["rank_rows"][0]["pct"].endswith("%")
    assert "keywords" not in data


def test_build_report_card_data_skips_ranks_section(service):
    data = cardrender.build_report_card_data(
        service, G1, ("summary",), None, 0, "daily"
    )
    assert data["rank_rows"] == []


def test_build_report_card_data_silent_group(service):
    assert cardrender.build_report_card_data(
        service, G1, ALL_SECTIONS, None, min_messages=10_000, frequency="daily"
    ) is None


def test_report_card_template_renders_locally(service):
    data = cardrender.build_report_card_data(
        service, G1, ALL_SECTIONS, None, 0, "daily"
    )
    html = Environment(autoescape=False).from_string(
        cardrender.load_template("report_card")
    ).render(data)
    assert "发言榜" in html and "高频关键词" not in html and "三哥" in html


def test_render_report_card_paths(service):
    data = cardrender.build_report_card_data(
        service, G1, ALL_SECTIONS, None, 0, "daily"
    )
    assert asyncio.run(cardrender.render_report_card(_FakeStar(result="x"), data)) is None


# ---------- 群画像卡片 ----------

def test_build_group_card_data(service):
    p = service.group_profile(G1)
    data = cardrender.build_group_card_data(G1, p, TZ)
    assert data["group_name"] == "测试群一"
    assert data["stats"][0]["value"] == f"{p['message_count']:,}"
    assert len(data["hours"]) == 24
    # 榜首条形满宽；互动对结构完整
    assert data["top_active"][0]["wpx"] == 100
    assert all({"a", "b", "count"} <= set(pair) for pair in data["top_pairs"])


def test_group_card_template_renders_locally(service):
    p = service.group_profile(G1)
    data = cardrender.build_group_card_data(G1, p, TZ)
    html = Environment(autoescape=False).from_string(
        cardrender.load_template("group_profile")
    ).render(data)
    for block in ("活跃分布", "日趋势", "发言榜", "媒体构成", "高频互动对"):
        assert block in html
    assert "群关键词" not in html  # 关键词区块已砍：群词云即其可视化


def test_render_group_card_paths(service):
    p = service.group_profile(G1)
    assert asyncio.run(
        cardrender.render_group_card(_FakeStar(result="x"), G1, p, TZ)
    ) is None
