"""测试夹具：在临时目录构建与 ChatLogger 同构（user_version=3）的 chatlog.db。

数据为固定时间戳（Asia/Shanghai），保证天/小时桶断言稳定。
覆盖：中英混合 / URL / 数字 / Emoji / face / at / reply / node 转发 / 纯图 /
bot / 私聊 / 昵称变更 / 跨天 / 坏 JSON / 唤醒消息（画像行为统计不排除的口径样本）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from insight import textproc
from insight.db import ChatlogDB
from insight.repository import ChatlogRepository
from insight.service import StatisticsService

TZ = ZoneInfo("Asia/Shanghai")
PLUGIN_DIR = Path(__file__).resolve().parent.parent

G1 = "10001"
G2 = "20002"
U1 = "111"
U2 = "222"
BOT = "9999"

# 固定"现在"：2026-08-15 12:00 +08（周六）
NOW = int(datetime(2026, 8, 15, 12, 0, 0, tzinfo=TZ).timestamp())


def ts(y, m, d, h, mi=0):
    return int(datetime(y, m, d, h, mi, 0, tzinfo=TZ).timestamp())


def segs(*segments):
    return json.dumps(list(segments), ensure_ascii=False)


def plain(x):
    return {"t": "plain", "x": x}


# 与 chatlogger main.py 相同的 DDL（契约 schema v3）
DDL = """
CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_id    TEXT,
  platform      TEXT NOT NULL,
  message_type  TEXT NOT NULL CHECK (message_type IN ('group','private','other')),
  session_id    TEXT NOT NULL,
  group_id      TEXT,
  user_id       TEXT NOT NULL,
  user_name     TEXT,
  sender_type   TEXT NOT NULL DEFAULT 'user'
                CHECK (sender_type IN ('user','bot','system')),
  waked_bot     INTEGER NOT NULL DEFAULT 0 CHECK (waked_bot IN (0,1)),
  content       TEXT NOT NULL DEFAULT '',
  content_json  TEXT,
  media_flags   INTEGER NOT NULL DEFAULT 0 CHECK (media_flags BETWEEN 0 AND 127),
  reply_to      TEXT,
  reply_user_id TEXT,
  raw_json      TEXT,
  ts            INTEGER NOT NULL,
  ingested_at   INTEGER NOT NULL,
  UNIQUE (platform, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_group      ON messages(group_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_group_user ON messages(group_id, user_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_user       ON messages(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_session    ON messages(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts         ON messages(ts);
CREATE TABLE IF NOT EXISTS users (
  user_id   TEXT PRIMARY KEY,
  user_name TEXT,
  first_seen INTEGER,
  last_seen  INTEGER,
  msg_count  INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS groups (
  group_id   TEXT PRIMARY KEY,
  group_name TEXT,
  first_seen INTEGER,
  last_seen  INTEGER,
  msg_count  INTEGER NOT NULL DEFAULT 0
);
"""

INSERT_SQL = """
INSERT OR IGNORE INTO messages
  (message_id, platform, message_type, session_id, group_id, user_id, user_name,
   sender_type, waked_bot, content, content_json, media_flags, reply_to, reply_user_id,
   ts, ingested_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

LONG_TEXT = "这是一条很长的消息" * 12  # 108 字符


def default_rows():
    """覆盖群统计与用户画像双口径的合成消息。"""
    n = [0]

    def row(user, uname, content, content_json, flags, t, group=G1, sender="user",
            mtype="group", waked=0, reply_to=None, reply_user=None):
        n[0] += 1
        session = f"test:GroupMessage:{group}" if group else "test:FriendMessage:priv"
        return (
            f"m{n[0]}", "test", mtype, session, group, user, uname, sender, waked,
            content, content_json, flags, reply_to, reply_user, t, t,
        )

    return [
        # --- 08-13（白天 10:00）：u1 两条，包含本周对比用词 ---
        row(U1, "张三", "AI AI AI AI AI AI 显卡", segs(plain("AI AI AI AI AI AI 显卡")), 0, ts(2026, 8, 13, 10, 0)),
        row(U1, "张三", "显卡 显卡 显卡", segs(plain("显卡 显卡 显卡")), 0, ts(2026, 8, 13, 11, 0)),
        # --- 08-14（u1 昵称已改为"三哥"）：关键词趋势基准 ---
        row(U1, "三哥", "AI AI AI AI AI AI 模型 模型 模型 模型 模型 模型", segs(plain("AI AI AI AI AI AI 模型 模型 模型 模型 模型 模型")), 0, ts(2026, 8, 14, 9, 30)),
        row(U1, "三哥", "显卡", segs(plain("显卡")), 0, ts(2026, 8, 14, 10, 0)),
        # --- 08-15（今日）：u1 ---
        row(U1, "三哥", "AI AI AI AI AI AI AI", segs(plain("AI AI AI AI AI AI AI")), 0, ts(2026, 8, 15, 9, 0)),
        row(U1, "三哥", "显卡 显卡 显卡 显卡 显卡 显卡", segs(plain("显卡 显卡 显卡 显卡 显卡 显卡")), 0, ts(2026, 8, 15, 10, 0)),
        row(U1, "三哥", "看这个 https://example.com/a 好文", segs(plain("看这个 https://example.com/a 好文")), 0, ts(2026, 8, 15, 11, 0)),
        # QQ 表情（id 为字符串，与 chatlogger 序列化一致）+ face 位
        row(U1, "三哥", "笑死", segs({"t": "face", "id": "123"}, plain("笑死")), 16, ts(2026, 8, 15, 11, 30)),
        row(U1, "三哥", "笑死", segs({"t": "face", "id": "123"}, {"t": "face", "id": "45"}), 16, ts(2026, 8, 15, 11, 40)),
        # 合并转发 node 段 ×2（u1）
        row(U1, "三哥", "转发", segs({"t": "node"}, plain("转发")), 0, ts(2026, 8, 15, 12, 0)),
        row(U1, "三哥", "转发", segs({"t": "node"}), 0, ts(2026, 8, 14, 15, 0)),
        # 纯图片消息（content 空，不参与长度统计）
        row(U1, "三哥", "", segs({"t": "image", "u": "http://example.com/i.jpg"}), 1, ts(2026, 8, 15, 8, 0)),
        # --- 08-15 夜间消息 ---
        row(U1, "三哥", "睡觉 睡觉 睡觉", segs(plain("睡觉 睡觉 睡觉")), 0, ts(2026, 8, 14, 23, 0)),
        row(U1, "三哥", "显卡", segs(plain("显卡")), 0, ts(2026, 8, 15, 7, 0)),
        # --- u2 李四 ---
        row(U2, "李四", "😂😂🤣👨‍👩‍👧‍👦 666 3.14", segs(plain("😂😂🤣👨‍👩‍👧‍👦 666 3.14")), 0, ts(2026, 8, 15, 10, 30)),
        row(U2, "李四", "记录", segs({"t": "at", "qq": "111"}, plain("记录")), 8, ts(2026, 8, 15, 10, 45)),
        row(U2, "李四", LONG_TEXT, segs(plain(LONG_TEXT)), 0, ts(2026, 8, 15, 11, 0)),
        # 合并转发 node 段 ×1（u2）
        row(U2, "李四", "转发", segs({"t": "node"}, {"t": "forward"}), 0, ts(2026, 8, 15, 9, 40)),
        row(U2, "李四", "哈哈哈", segs(plain("哈哈哈")), 0, ts(2026, 8, 14, 22, 0)),
        # --- 回复网络：u2 回复 u1 / u1 回复 u2 ---
        row(U2, "李四", "回复你", segs(plain("回复你"), {"t": "reply", "id": "m1"}), 32, ts(2026, 8, 15, 10, 50), reply_to="m1", reply_user=U1),
        row(U1, "三哥", "不客气", segs(plain("不客气"), {"t": "reply", "id": "mX"}), 32, ts(2026, 8, 15, 11, 5), reply_to="mX", reply_user=U2),
        # --- bot 消息：一切用户统计均应排除 ---
        row(BOT, "AstrBot", "AI AI AI 我是机器人", segs(plain("AI AI AI 我是机器人")), 0, ts(2026, 8, 15, 10, 50), sender="bot"),
        # u1 回复 bot（Bot 互动）
        row(U1, "三哥", "谢谢Bot", segs(plain("谢谢Bot"), {"t": "reply", "id": "mB"}), 32, ts(2026, 8, 15, 11, 10), reply_to="mB", reply_user=BOT),
        # --- 私聊消息：群统计应排除；画像私聊会话数应计入 ---
        row(U1, "三哥", "私聊 AI", segs(plain("私聊 AI")), 0, ts(2026, 8, 15, 10, 55), group=None, mtype="private"),
        # --- 其他群：G1 统计应排除 ---
        row(U2, "李四", "别的群 AI", segs(plain("别的群 AI")), 0, ts(2026, 8, 15, 10, 20), group=G2),
        # --- 坏 JSON 行（模拟 4096 截断）：face/转发/@ 统计不得报错 ---
        row(U1, "三哥", "x", '[{"t": "plai', 0, ts(2026, 8, 15, 12, 30) - 86400 * 3),
        # --- 唤醒消息（命令/@Bot）：群统计口径下应被排除；画像行为统计不排除 ---
        row(U1, "三哥", "/发言榜 历史", segs(plain("/发言榜 历史")), 0, ts(2026, 8, 15, 12, 40), waked=1),
        row(U2, "李四", "@AstrBot 我的词云", segs({"t": "at", "qq": "9999"}, plain("我的词云")), 8, ts(2026, 8, 15, 12, 45), waked=1),
        row(U1, "三哥", "/统计 关键词", segs(plain("/统计 关键词")), 0, ts(2026, 8, 15, 12, 50), waked=1),
    ]


USER_ROWS = [
    (U1, "三哥", ts(2026, 8, 13, 10, 0), ts(2026, 8, 15, 12, 50), 0),
    (U2, "李四", ts(2026, 8, 14, 22, 0), ts(2026, 8, 15, 12, 45), 0),
    (BOT, "AstrBot", ts(2026, 8, 15, 10, 50), ts(2026, 8, 15, 10, 50), 0),
]

GROUP_ROWS = [
    (G1, "测试群一", ts(2026, 8, 13, 10, 0), ts(2026, 8, 15, 12, 50), 0),
    (G2, "测试群二", ts(2026, 8, 15, 10, 20), ts(2026, 8, 15, 10, 20), 0),
]


def build_db(path: Path, rows=None, user_version: int = 3):
    conn = sqlite3.connect(path)
    try:
        conn.executescript(DDL)
        conn.execute(f"PRAGMA user_version = {user_version}")
        conn.executemany(INSERT_SQL, default_rows() if rows is None else rows)
        conn.executemany(
            "INSERT OR IGNORE INTO users VALUES (?,?,?,?,?)", USER_ROWS
        )
        conn.executemany(
            "INSERT OR IGNORE INTO groups VALUES (?,?,?,?,?)", GROUP_ROWS
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def db_path(tmp_path):
    return build_db(tmp_path / "chatlog.db")


@pytest.fixture
def repo(db_path):
    return ChatlogRepository(ChatlogDB(db_path))


@pytest.fixture
def stopwords():
    return textproc.load_stopwords(PLUGIN_DIR / "assets" / "stopwords.txt")


@pytest.fixture
def service(repo, stopwords, tmp_path):
    return StatisticsService(
        repo,
        stopwords=stopwords,
        output_dir=tmp_path / "out",
        plugin_dir=PLUGIN_DIR,
        wordcloud_enabled=True,
        now_ts=NOW,
    )


def range_days(repo_days: int = 7):
    """构造与 resolve_range('N天', TZ, now_ts=NOW) 一致的区间断言用值。"""
    from insight.timeutil import resolve_range

    return resolve_range(f"{repo_days}天", TZ, now_ts=NOW)
