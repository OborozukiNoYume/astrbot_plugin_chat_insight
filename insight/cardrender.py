"""HTML 卡片渲染层：把画像 dict 组装成模板数据，经框架 html_render 截图。

设计约束：
- service/repository 零改动，本模块只消费 user_full/group_profile 的返回值；
- html_render 走 AstrBot 官方 t2i 服务（模板与统计数据会上传），模板数据必须
  全部 JSON 可序列化（TimeRange 等对象在此转成字符串）；
- 渲染失败一律返回 None，由调用方降级为既有文本输出（fmt_* 全部保留）；
- 远端 Jinja2 的 autoescape 配置未知：用户可控文本（昵称/关键词/互动对象）
  在此统一 html.escape，模板侧用 |safe 输出，两端皆安全且不会二次转义。
"""

from __future__ import annotations

import asyncio
import html
import logging
import tempfile
import time
import uuid
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment

from .service import ServiceError

# 与 AstrBot LogManager 同名 logger：宿主环境进统一日志，独立测试零依赖
logger = logging.getLogger("astrbot")

# 渲染偶发排队/挂起（云端 t2i 或本地 chromium 启动异常），超过该秒数立即
# 放弃并降级，避免群里长时间无响应。云端正常 5~12 秒，本地正常 <2 秒。
RENDER_TIMEOUT_SECONDS = 45

# ---------- 本地渲染（playwright + 内置中文字体，模板与数据不出本机） ----------

_FONT_PATH = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSansSC.ttf"
# as_uri() 自带百分号编码：路径含空格/#/中文时手拼 file:// URL 会被截断致字体静默失效
_FONT_INJECT = (
    '<style>@font-face{font-family:"NotoSansSC-Insight";'
    f'src:url("{_FONT_PATH.as_uri()}");}}'
    'body{font-family:"NotoSansSC-Insight","PingFang SC","Microsoft YaHei",'
    '"Noto Sans CJK SC",sans-serif !important;}</style>'
)
_CARD_DIR = Path(tempfile.gettempdir()) / "insight_cards"
_CARD_TTL_SECONDS = 86400


def _cleanup_old_cards() -> None:
    if not _CARD_DIR.is_dir():
        return
    cutoff = time.time() - _CARD_TTL_SECONDS
    for f in _CARD_DIR.glob("card_*.png"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


async def _local_screenshot(template: str, data: dict, label: str) -> str | None:
    """playwright 本地截图：Jinja2 渲染 → chromium 加载 → 截卡片元素。

    playwright 未安装 / chromium 启动失败 / 任何异常均返回 None（调用方转云端）。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None
    try:
        html_str = Environment(autoescape=False).from_string(
            load_template(template)
        ).render(data)
        html_str = html_str.replace("</head>", _FONT_INJECT + "</head>", 1)
        _cleanup_old_cards()
        _CARD_DIR.mkdir(parents=True, exist_ok=True)
        # 毫秒时间戳 + 随机后缀：并发渲染同毫秒不再互相覆盖
        out = _CARD_DIR / f"card_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}.png"
        async with async_playwright() as p:
            browser = await p.chromium.launch(args=["--disable-dev-shm-usage"])
            try:
                page = await browser.new_page(
                    viewport={"width": 760, "height": 800}, device_scale_factor=2,
                )
                await page.set_content(html_str)
                await page.evaluate("document.fonts.ready")
                # 头像等外链图:最多等 3 秒,加载失败由模板 onerror 回退首字
                await page.evaluate(
                    "Promise.race([Promise.all([...document.images].map(i =>"
                    " i.complete || new Promise(r => { i.onload = i.onerror = r; }))),"
                    " new Promise(r => setTimeout(r, 3000))])"
                )
                el = await page.query_selector(".card")
                await el.screenshot(path=str(out))
            finally:
                await browser.close()
        if _is_image_file(str(out)):
            logger.info(f"[insight] {label}卡片已本地渲染: {out}")
            return str(out)
        return None
    except Exception as e:
        logger.warning(f"[insight] {label}卡片本地渲染失败，转云端: {e}")
        return None

_TEMPLATES_DIR = Path(__file__).parent / "templates"

# 媒体类型 → (中文标签, 卡片用色)，与文本版 _MEDIA_LABELS 的措辞保持一致
_MEDIA_COLORS = {
    "image": ("图片", "#f59e0b"),
    "at": ("At", "#4f6ef7"),
    "reply": ("回复", "#10b981"),
    "video": ("视频", "#ef4444"),
    "file": ("文件", "#8b5cf6"),
    "face": ("QQ表情", "#ec4899"),
    "voice": ("语音", "#06b6d4"),
}

_WEEKDAY_LABELS = "一二三四五六日"


@lru_cache(maxsize=8)
def load_template(name: str) -> str:
    return (_TEMPLATES_DIR / f"{name}.html").read_text(encoding="utf-8")


def _esc(value) -> str:
    return html.escape(str(value), quote=False)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _ts(value: int | None, tz: ZoneInfo) -> str:
    if not value:
        return "-"
    return datetime.fromtimestamp(value, tz).strftime("%Y-%m-%d %H:%M")


def _qq_avatar_url(uid) -> str | None:
    """QQ 个人头像（腾讯公开 CDN）。非纯数字 ID（非 QQ 平台）返回 None → 模板回退首字。"""
    s = str(uid or "")
    return f"https://q1.qlogo.cn/g?b=qq&nk={s}&s=640" if s.isdigit() else None


def _qq_group_avatar_url(gid) -> str | None:
    """QQ 群头像（腾讯公开 CDN）。"""
    s = str(gid or "")
    return f"https://p.qlogo.cn/gh/{s}/{s}/640" if s.isdigit() else None


def _is_image_file(path: str) -> bool:
    """渲染服务过载时会以 200 返回 "no available server" 文本，且框架的
    download_image_by_url 不校验内容直接落盘——按魔数确认拿到的是真图片。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    return head.startswith(b"\x89PNG") or head.startswith(b"\xff\xd8")


def _name_pairs(pairs, limit: int = 8) -> list[dict]:
    """[(名字, 次数)] → 模板友好的已转义列表。"""
    return [{"name": _esc(n), "count": c} for n, c in pairs[:limit]]


def build_user_card_data(name: str, uid: str, p: dict, tz: ZoneInfo) -> dict:
    """user_full 六视图 dict → 用户画像卡片模板数据（纯函数，全部 JSON 可序列化）。
    关键词视图不进卡片：个人词云（/词云）即其可视化。"""
    card, act = p["card"], p["activity"]
    style, social, bot = p["style"], p["social"], p["bot"]

    hour_max = max(act["hour_counts"]) or 1
    hours = [
        {
            "hpx": round(c / hour_max * 54) + (2 if c else 0),
            "peak": h in card["peak_hours"],
            "tick": f"{h:02d}" if h % 3 == 0 else "",
        }
        for h, c in enumerate(act["hour_counts"])
    ]
    wd_max = max(act["weekday_counts"]) or 1
    weekdays = [
        {
            "label": _WEEKDAY_LABELS[i],
            "hpx": round(c / wd_max * 54) + (2 if c else 0),
        }
        for i, c in enumerate(act["weekday_counts"])
    ]

    media = [
        {
            "label": _MEDIA_COLORS.get(k, (k, "#94a3b8"))[0],
            "color": _MEDIA_COLORS.get(k, (k, "#94a3b8"))[1],
            "pct": _pct(v),
        }
        for k, v in sorted(style["media_ratios"].items(), key=lambda kv: -kv[1])
        if v > 0
    ]

    social_rows = []
    if social["reply_sent"]:
        social_rows.append(("最常回复", _name_pairs(social["reply_sent"])))
    if social["reply_received"]:
        social_rows.append(("最常被回复", _name_pairs(social["reply_received"])))
    if social["mutual"]:
        social_rows.append((
            "高频互动对象",
            [{"name": _esc(n), "count": f"{a}↔{b}"} for n, a, b in social["mutual"][:6]],
        ))
    if social["at_sent"]:
        social_rows.append(("最常@", _name_pairs(social["at_sent"])))
    if social["at_received"]:
        social_rows.append((
            f"最常被@（近{social['at_window_days']}天）",
            _name_pairs(social["at_received"]),
        ))

    span_days = max(card["span_days"], 1)
    return {
        "name": _esc(name),
        "uid": _esc(uid),
        "avatar_url": _qq_avatar_url(uid),
        "scope": _esc(card["scope_label"]),
        "range_label": card["range"].label,
        "span": card["span"],
        "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
        "first_seen": _ts(card["first_seen"], tz),
        "last_seen": _ts(card["last_seen"], tz),
        "stats": [
            {"label": "消息", "value": f"{card['message_count']:,}",
             "sub": f"文本 {card['text_message_count']:,}"},
            {"label": "活跃天数", "value": str(card["active_days"]),
             "sub": f"跨度 {card['span_days']} 天"},
            {"label": "活跃群", "value": str(card["active_groups"]), "sub": "全局口径"},
            {"label": "日均消息", "value": f"{card['message_count'] / span_days:.1f}",
             "sub": "按跨度天数"},
        ],
        "hours": hours,
        "weekdays": weekdays,
        "peak_hours": "/".join(f"{h:02d}" for h in card["peak_hours"]) or "-",
        "peak_ratio": _pct(act["peak_hours_ratio"]),
        "day_night": [
            {"label": "白天 06–18", "value": _pct(act["day_ratio"])},
            {"label": "夜间", "value": _pct(1 - act["day_ratio"])},
            {"label": "周末", "value": _pct(act["weekend_ratio"])},
            {"label": "最长连续", "value": f"{act['max_streak_days']} 天"},
            {"label": "当前连续", "value": f"{act['current_streak_days']} 天"},
        ],
        "style_hint": f"文本样本 {style['text_total']:,} 条",
        "style_grid": [
            {"label": "P50 长度", "value": f"{style['p50']} 字"},
            {"label": "P90 长度", "value": f"{style['p90']} 字"},
            {"label": "P95 长度", "value": f"{style['p95']} 字"},
            {"label": "平均长度", "value": f"{style['avg_length']:.1f} 字"},
            {"label": "短消息 <10字", "value": _pct(style["short_ratio"])},
            {"label": "长消息 ≥100字", "value": _pct(style["long_ratio"])},
        ],
        "burst": (
            f"连发 {style['burst_count']} 轮 · 均 {style['avg_burst']:.1f} 条/轮"
            f" · 单轮最多 {style['max_burst']} 条"
        ),
        "media": media,
        "social_rows": [
            {"label": label, "items": items} for label, items in social_rows
        ],
        "bot_grid": [
            {"label": "私聊会话", "value": str(bot["private_session_count"]),
             "sub": f"私聊消息 {bot['private_message_count']} 条 · 全局口径"},
            {"label": "群内唤醒", "value": f"{bot['wake_count']} 次",
             "sub": f"占其群消息 {_pct(bot['wake_ratio'])}"},
            {"label": "@Bot", "value": f"{bot['at_bot_count']} 次", "sub": ""},
            {"label": "回复/引用 Bot", "value": f"{bot['reply_bot_count']} 次", "sub": ""},
        ],
    }


async def render_user_card(star, name: str, uid: str, p: dict, tz: ZoneInfo) -> str | None:
    """渲染用户画像卡片，返回图片路径；模板缺失或渲染失败返回 None（降级文本）。"""
    try:
        data = build_user_card_data(name, uid, p, tz)
    except Exception as e:
        logger.warning(f"[insight] 用户画像卡片数据组装失败，回退文本: {e}")
        return None
    return await _render_card(star, "user_profile", data, "用户画像")


# ---------- 群报卡片 ----------

def build_report_card_data(service, group_id, sections, top_n=None,
                           min_messages: int = 0, frequency: str = "weekly") -> dict | None:
    """群报卡片数据（与 report.build_report 同口径取数）。

    静默群（区间消息量低于 min_messages）返回 None；群无记录由
    service.summary 抛 ServiceError（调用方记日志跳过），与文本路径一致。
    rank 分节独立容错：无数据跳过该区块，不影响整张卡片。
    """
    from . import report as report_mod

    r = service.resolve(report_mod.PERIOD_SPEC[frequency])
    s = service.summary(r, group_id)
    if s["messages"] < min_messages:
        return None
    group_name = service.repo.get_group_meta(str(group_id))[0]

    entries: list = []
    total = 0
    if "rank" in sections:
        try:
            entries, total = service.rank(r, group_id, top_n)
        except ServiceError:
            entries, total = [], 0
    rank_max = max((e.count for e in entries), default=0)

    return {
        "group_name": _esc(group_name),
        "group_id": _esc(group_id),
        "avatar_url": _qq_group_avatar_url(group_id),
        "period_label": report_mod.PERIOD_LABEL[frequency],
        "title_badge": f"{report_mod.PERIOD_LABEL[frequency]}群报",
        "range_label": s["range"].label,
        "span": s["span"],
        "generated_at": datetime.now(service.tz).strftime("%Y-%m-%d %H:%M"),
        "stats": [
            {"label": "消息", "value": f"{s['messages']:,}", "sub": ""},
            {"label": "日均", "value": f"{s['avg_per_day']:.1f}", "sub": ""},
            {"label": "活跃人数", "value": str(s["active_users"]), "sub": "发过至少 1 条"},
            {"label": "峰值日", "value": str(s["peak_messages"]), "sub": s["peak_date"]},
        ],
        # 峰值日独立命名键：模板正文按名引用，避免 stats 位置重排后静默错值
        "peak_date": s["peak_date"],
        "peak_value": str(s["peak_messages"]),
        "days_with_data": f"{s['days_with_data']}/{s['span_days']} 天",
        "rank_total": total,
        "rank_rows": [
            {
                "i": i,
                "name": _esc(e.user_name if e.user_name and e.user_name != e.user_id else e.user_id),
                "count": e.count,
                "pct": f"{e.ratio * 100:.1f}%" if total else "-",
                "wpx": round(e.count / rank_max * 100) if rank_max else 0,
            }
            for i, e in enumerate(entries, 1)
        ],
    }


async def render_report_card(star, data: dict) -> str | None:
    """渲染群报卡片，失败返回 None（降级文本群报）。"""
    return await _render_card(star, "report_card", data, "群报")


# ---------- 群画像卡片 ----------

def build_group_card_data(group_id: str, p: dict, tz: ZoneInfo) -> dict:
    """group_profile dict → 群画像卡片模板数据。"""
    hour_max = max(p["hour_counts"]) or 1
    hours = [
        {
            "hpx": round(c / hour_max * 54) + (2 if c else 0),
            "peak": h in p["peak_hours"],
            "tick": f"{h:02d}" if h % 3 == 0 else "",
        }
        for h, c in enumerate(p["hour_counts"])
    ]
    trend_max = max((c for _, c in p["daily_trend"]), default=0)
    trend = [
        {"date": d, "hpx": round(c / trend_max * 54) + (2 if c else 0)}
        for d, c in p["daily_trend"]
    ]
    act_max = max((c for _, c in p["top_active"]), default=0)
    top_active = [
        {"i": i, "name": _esc(n), "count": c,
         "wpx": round(c / act_max * 100) if act_max else 0}
        for i, (n, c) in enumerate(p["top_active"], 1)
    ]
    media = [
        {
            "label": _MEDIA_COLORS.get(k, (k, "#94a3b8"))[0],
            "color": _MEDIA_COLORS.get(k, (k, "#94a3b8"))[1],
            "pct": _pct(v),
        }
        for k, v in sorted(p["media_ratios"].items(), key=lambda kv: -kv[1])
        if v > 0
    ]
    span_days = max(
        (p["last_seen"] - p["first_seen"]) // 86400 + 1
        if p["first_seen"] and p["last_seen"] else 1,
        1,
    )
    return {
        "group_name": _esc(p["group_name"]),
        "group_id": _esc(group_id),
        "avatar_url": _qq_group_avatar_url(group_id),
        "generated_at": datetime.now(tz).strftime("%Y-%m-%d %H:%M"),
        "first_seen": _ts(p["first_seen"], tz),
        "last_seen": _ts(p["last_seen"], tz),
        "stats": [
            {"label": "消息", "value": f"{p['message_count']:,}", "sub": "全期"},
            {"label": "发言成员", "value": str(p["active_members"]),
             "sub": "有发言者，非群成员总数"},
            {"label": "跨度", "value": f"{span_days} 天", "sub": "有记录以来"},
            {"label": "日均", "value": f"{p['message_count'] / span_days:.1f}", "sub": ""},
        ],
        "hours": hours,
        "peak_hours": "/".join(f"{h:02d}" for h in p["peak_hours"]) or "-",
        "trend_days": p["trend_days"],
        "trend": trend,
        "top_active": top_active,
        "media": media,
        "top_pairs": [
            {"a": _esc(a), "b": _esc(b), "count": c}
            for a, b, c in p["top_pairs"][:5]
        ],
    }


async def render_group_card(star, group_id: str, p: dict, tz: ZoneInfo) -> str | None:
    """渲染群画像卡片，失败返回 None（降级文本）。"""
    try:
        data = build_group_card_data(group_id, p, tz)
    except Exception as e:
        logger.warning(f"[insight] 群画像卡片数据组装失败，回退文本: {e}")
        return None
    return await _render_card(star, "group_profile", data, "群画像")


async def _render_card(star, template: str, data: dict, label: str) -> str | None:
    """全部卡片的共用渲染封装。

    渲染链：本地 chromium（模板与数据不出本机）→ 云端 t2i（本地不可用时回退，
    数据需上传）→ None（调用方降级文本）。各级超时/异常/非图片响应均向后回退。
    """
    try:
        path = await asyncio.wait_for(
            _local_screenshot(template, data, label),
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        if path:
            return path
    except Exception as e:
        logger.warning(f"[insight] {label}卡片本地渲染超时，转云端: {e}")
    try:
        # png 保证小字号清晰（框架默认 jpeg q40 对文字过糊）
        path = await asyncio.wait_for(
            star.html_render(
                load_template(template), data, return_url=False,
                options={"type": "png"},
            ),
            timeout=RENDER_TIMEOUT_SECONDS,
        )
        if not path or not _is_image_file(str(path)):
            return None
        logger.info(f"[insight] {label}卡片已渲染(云端): {path}")
        return str(path)
    except Exception as e:
        logger.warning(f"[insight] {label}卡片渲染失败，回退文本: {e}")
        return None
