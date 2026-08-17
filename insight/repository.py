"""SQL 集中管理：本插件全部 SQL 都在这个模块，命令层与业务层不得直接写 SQL。

约定：
- 只读（连接由 db.ChatlogDB 以 mode=ro 提供），参数绑定，禁止拼接值
- 统计用户行为恒过滤 sender_type='user'，bot 消息不计（口径见 README）
- 时间过滤 ts >= ? AND ts < ?，配合 (group_id, ts)/(user_id, ts)/(ts) 索引
- @网络 使用 JSON 结构化函数（json_each/json_extract），
  且一律先经 json_valid 内层过滤——content_json 可能被 chatlogger 截断为非法 JSON

唤醒消息（waked_bot）过滤分场景：
- 群统计（排行/总览/关键词等）：默认排除（防命令文本污染统计），受 exclude_waked 配置控制
- 用户画像的行为统计（活跃/风格/互动/Bot）：不排除——唤醒 Bot 本身是用户行为
- 用户画像的关键词：恒排除（防命令文本刷屏）
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .db import ChatlogDB
from .timeutil import TimeRange, day_bucket_to_date

# media_flags 位掩码（QUERY_GUIDE 契约）：1图片 2语音 4视频 8At 16表情 32回复 64文件
MEDIA_BITS = {
    "image": 1,
    "voice": 2,
    "video": 4,
    "at": 8,
    "face": 16,
    "reply": 32,
    "file": 64,
}

# json_each 查询的公共防护：content_json 非空且合法
_JSON_OK = "content_json IS NOT NULL AND json_valid(content_json)"


@dataclass
class RankEntry:
    user_id: str
    user_name: str
    count: int
    ratio: float  # 占范围内用户消息总数比例 0~1


@dataclass
class DayActivity:
    date: str
    messages: int
    active_users: int


@dataclass
class MediaStats:
    messages: int = 0
    counts: dict = field(default_factory=dict)  # {"image": n, ...}

    def count(self, key: str) -> int:
        return self.counts.get(key, 0)


class ChatlogRepository:
    def __init__(self, db: ChatlogDB, exclude_waked: bool = True):
        """exclude_waked: 群统计是否排除唤醒消息（waked_bot=1：/命令、@Bot、引用Bot、私聊）。
        仅作用于群统计口径；用户画像的行为统计不受此开关影响（见模块 docstring）。"""
        self.db = db
        self.exclude_waked = bool(exclude_waked)
        self._bot_ids: set[str] | None = None

    # ---------- 内部：公共 WHERE 构造（值全部参数绑定） ----------

    def _where(
        self,
        r: TimeRange,
        group_id=None,
        user_id=None,
        extra: list[str] | None = None,
        waked: bool | None = None,
    ):
        """waked=None 用全局配置（群统计口径）；True/False 强制覆盖（用户画像口径）。"""
        conds: list[str] = []
        params: list = []
        if group_id is not None:
            conds.append("group_id = ?")
            params.append(str(group_id))
        if user_id is not None:
            conds.append("user_id = ?")
            params.append(str(user_id))
        conds.append("sender_type = 'user'")
        if waked if waked is not None else self.exclude_waked:
            conds.append("waked_bot = 0")
        conds.append("ts >= ?")
        params.append(r.start_ts)
        conds.append("ts < ?")
        params.append(r.end_ts)
        if extra:
            conds.extend(extra)
        return " AND ".join(conds), params

    def _query(self, sql: str, params: list):
        with self.db.connect() as conn:
            return conn.execute(sql, params).fetchall()

    # ---------- 维度辅助（昵称 / Bot 识别） ----------

    def resolve_names(self, user_ids: list[str]) -> dict[str, str]:
        """user_id → 最近昵称。优先 users 表（可重建缓存），
        查不到时回退到 messages 里该 ID 最新一条的 user_name，仍无则用 ID 本身。"""
        ids = [str(u) for u in dict.fromkeys(user_ids) if u]
        out: dict[str, str] = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            ph = ",".join("?" * len(chunk))
            rows = self._query(
                f"SELECT user_id, NULLIF(NULLIF(user_name, ''), '　') "
                f"FROM users WHERE user_id IN ({ph})",
                chunk,
            )
            for uid, name in rows:
                if name:
                    out[str(uid)] = name
        missing = [u for u in ids if u not in out]
        for i in range(0, len(missing), 100):
            chunk = missing[i : i + 100]
            ph = ",".join("?" * len(chunk))
            # SQLite 裸列 + MAX 的组合保证取到 ts 最大那一行的 user_name
            rows = self._query(
                f"SELECT user_id, NULLIF(NULLIF(user_name, ''), '　'), MAX(ts) "
                f"FROM messages WHERE user_id IN ({ph}) GROUP BY user_id",
                chunk,
            )
            for uid, name, _ts in rows:
                if name:
                    out[str(uid)] = name
        for uid in ids:
            out.setdefault(uid, uid)
        return out

    def get_display_name(self, user_id, group_id=None) -> str:
        """用户最新昵称（群维度优先），无记录时回退为 ID。"""
        if group_id is not None:
            row = self._query(
                "SELECT user_name FROM messages WHERE user_id = ? AND group_id = ? "
                "AND sender_type = 'user' AND user_name IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1",
                [str(user_id), str(group_id)],
            )
        else:
            row = self._query(
                "SELECT user_name FROM messages WHERE user_id = ? "
                "AND sender_type = 'user' AND user_name IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1",
                [str(user_id)],
            )
        return (row[0][0] if row else "") or str(user_id)

    def bot_self_ids(self) -> set[str]:
        """机器人自身 ID 集合（sender_type='bot' 消息的 user_id 存的是 self_id）。"""
        if self._bot_ids is None:
            rows = self._query(
                "SELECT DISTINCT user_id FROM messages WHERE sender_type='bot'", []
            )
            self._bot_ids = {str(r[0]) for r in rows}
        return self._bot_ids

    def refresh_bot_ids(self):
        self._bot_ids = None

    def group_exists(self, group_id) -> bool:
        row = self._query(
            "SELECT 1 FROM messages WHERE group_id = ? LIMIT 1", [str(group_id)]
        )
        return bool(row)

    def user_exists(self, user_id, group_id=None) -> bool:
        if group_id is not None:
            row = self._query(
                "SELECT 1 FROM messages WHERE user_id = ? AND group_id = ? LIMIT 1",
                [str(user_id), str(group_id)],
            )
        else:
            row = self._query(
                "SELECT 1 FROM messages WHERE user_id = ? LIMIT 1", [str(user_id)]
            )
        return bool(row)

    def db_stats(self) -> dict:
        """库概况（/用户画像 状态 用）。"""
        total, oldest, newest = self._query(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM messages", []
        )[0]
        by_type = dict(
            self._query(
                "SELECT sender_type, COUNT(*) FROM messages GROUP BY sender_type", []
            ),
        )
        return {"total": total or 0, "oldest": oldest, "newest": newest, "by_type": by_type}

    # ---------- 计数与排行 ----------

    def get_message_count(self, r: TimeRange, group_id=None, user_id=None) -> int:
        where, params = self._where(r, group_id, user_id)
        row = self._query(f"SELECT COUNT(*) FROM messages WHERE {where}", params)
        return int(row[0][0])

    def get_active_user_count(self, r: TimeRange, group_id=None) -> int:
        """活跃用户口径：时间范围内发送过至少 1 条 sender_type='user' 消息的用户数。"""
        where, params = self._where(r, group_id)
        row = self._query(
            f"SELECT COUNT(DISTINCT user_id) FROM messages WHERE {where}", params
        )
        return int(row[0][0])

    def get_message_rank(self, r: TimeRange, group_id, limit: int = 10) -> list[RankEntry]:
        """群内发言排行。昵称取该用户范围内最新一条消息的 user_name
        （SQLite 裸列 + MAX(ts) 语义：返回最大值所在行的其他列）。"""
        where, params = self._where(r, group_id)
        rows = self._query(
            f"""
            SELECT user_id, COUNT(*) AS c, MAX(ts), user_name
            FROM messages WHERE {where}
            GROUP BY user_id
            ORDER BY c DESC, user_id
            LIMIT ?
            """,
            params + [limit],
        )
        total = self.get_message_count(r, group_id=group_id)
        entries = []
        for user_id, c, _, name in rows:
            entries.append(
                RankEntry(
                    user_id=str(user_id),
                    user_name=(name or "") or str(user_id),
                    count=int(c),
                    ratio=(int(c) / total) if total else 0.0,
                )
            )
        return entries

    # ---------- 活跃度趋势（群与用户共用聚合形态） ----------

    def get_activity_by_day(
        self, r: TimeRange, group_id=None, user_id=None, offset_seconds: int = 0,
        waked: bool | None = None,
    ) -> list[DayActivity]:
        """按本地自然日聚合消息量（与活跃用户数，仅群维度时有意义）。天桶 = (ts+off)//86400。"""
        where, params = self._where(r, group_id, user_id, waked=waked)
        rows = self._query(
            f"""
            SELECT CAST((ts + ?) / 86400 AS INTEGER) AS d,
                   COUNT(*) AS c, COUNT(DISTINCT user_id) AS u
            FROM messages WHERE {where}
            GROUP BY d ORDER BY d
            """,
            [offset_seconds] + params,
        )
        tz = r.tz
        return [
            DayActivity(
                date=day_bucket_to_date(int(d), tz, offset_seconds),
                messages=int(c),
                active_users=int(u),
            )
            for d, c, u in rows
        ]

    def get_activity_by_hour(
        self, r: TimeRange, group_id=None, user_id=None, offset_seconds: int = 0,
        waked: bool | None = None,
    ) -> list[int]:
        """24 小时消息量分布（本地时区），返回长度 24 的列表。"""
        where, params = self._where(r, group_id, user_id, waked=waked)
        rows = self._query(
            f"""
            SELECT CAST((ts + ?) % 86400 / 3600 AS INTEGER) AS h, COUNT(*) AS c
            FROM messages WHERE {where}
            GROUP BY h
            """,
            [offset_seconds] + params,
        )
        buckets = [0] * 24
        for h, c in rows:
            buckets[int(h)] = int(c)
        return buckets

    # ---------- 词云 / 关键词取文本 ----------

    def fetch_texts(
        self, r: TimeRange, group_id=None, user_id=None, limit: int = 50000,
        waked: bool | None = None,
    ) -> list[str]:
        """按时间+范围取 content_json（白名单段数组），分词在 Python 侧做。
        ORDER BY ts DESC：超上限时保留最新部分。"""
        where, params = self._where(
            r, group_id, user_id, extra=["content_json IS NOT NULL"], waked=waked
        )
        rows = self._query(
            f"SELECT content_json FROM messages WHERE {where} ORDER BY ts DESC LIMIT ?",
            params + [limit],
        )
        return [row[0] for row in rows]

    # ---------- 媒体（media_flags 位聚合，群画像与用户风格共用） ----------

    def _media_aggregate_sql(self) -> str:
        return ", ".join(f"SUM(media_flags & {bit} != 0)" for bit in MEDIA_BITS.values())

    def get_media_stats(self, r: TimeRange, group_id=None, user_id=None) -> MediaStats:
        where, params = self._where(r, group_id, user_id)
        row = self._query(
            f"SELECT COUNT(*), {self._media_aggregate_sql()} FROM messages WHERE {where}",
            params,
        )[0]
        counts = {k: int(v or 0) for (k, v) in zip(MEDIA_BITS, row[1:])}
        return MediaStats(messages=int(row[0]), counts=counts)

    # ==================== 用户画像（行为统计不排除唤醒消息） ====================

    def get_user_basic(
        self, r: TimeRange, user_id, group_id=None, offset_seconds: int = 0
    ) -> dict:
        """基础画像：首末发言/消息量/文本量/活跃天数/均长/最长。
        活跃天数用 SQL 天桶去重，不拉全量 ts。"""
        where, params = self._where(r, group_id, user_id, waked=False)
        row = self._query(
            f"""
            SELECT MIN(ts), MAX(ts), COUNT(*),
                   SUM(CASE WHEN content != '' THEN 1 ELSE 0 END),
                   AVG(LENGTH(content)), MAX(LENGTH(content)),
                   COUNT(DISTINCT CAST((ts + ?) / 86400 AS INTEGER))
            FROM messages WHERE {where}
            """,
            [offset_seconds] + params,
        )[0]
        first, last, count, text_count, avg_len, max_len, active_days = row
        # 活跃群数是用户的全局属性，不受 scope 限制
        active_groups = self._query(
            "SELECT COUNT(DISTINCT group_id) FROM messages "
            "WHERE user_id = ? AND sender_type = 'user' AND group_id IS NOT NULL",
            [str(user_id)],
        )[0][0]
        return {
            "first_seen": first,
            "last_seen": last,
            "message_count": int(count or 0),
            "text_message_count": int(text_count or 0),
            "active_days": int(active_days or 0),
            "active_groups": int(active_groups or 0),
            "span_days": (int((last - first) / 86400) + 1) if first and last else 0,
            "avg_length": float(avg_len or 0),
            "max_length": int(max_len or 0),
        }

    def get_user_activity(
        self, r: TimeRange, user_id, group_id=None, offset_seconds: int = 0
    ) -> dict:
        """活跃规律原料：24 小时分布 + 按天 (date, count) 列表（量级=活跃天数）。
        周几分布与连续活跃由 service 从日期列表计算。行为统计不排除唤醒消息。"""
        hours = self.get_activity_by_hour(
            r, group_id=group_id, user_id=user_id, offset_seconds=offset_seconds,
            waked=False,
        )
        by_day = self.get_activity_by_day(
            r, group_id=group_id, user_id=user_id, offset_seconds=offset_seconds,
            waked=False,
        )
        return {"hour_counts": hours, "by_day": [(d.date, d.messages) for d in by_day]}

    def get_user_style(
        self, r: TimeRange, user_id, group_id=None, limit: int = 50000
    ) -> dict:
        """消息风格原料：长度样本（不拉 content 本体）+ ts 序列（连发轮次，
        走 (user_id, ts) 索引的纯整数序列）+ 媒体/face 位聚合。"""
        where, params = self._where(r, group_id, user_id, extra=["content != ''"], waked=False)
        lengths = [
            int(v)
            for (v,) in self._query(
                f"SELECT LENGTH(content) FROM messages WHERE {where} LIMIT ?",
                params + [limit],
            )
        ]
        where, params = self._where(r, group_id, user_id, waked=False)
        ts_list = [
            int(v)
            for (v,) in self._query(
                f"SELECT ts FROM messages WHERE {where} ORDER BY ts", params
            )
        ]
        row = self._query(
            f"SELECT COUNT(*), {self._media_aggregate_sql()} FROM messages WHERE {where}",
            params,
        )[0]
        media = MediaStats(
            messages=int(row[0]),
            counts={k: int(v or 0) for (k, v) in zip(MEDIA_BITS, row[1:])},
        )
        return {"lengths": lengths, "ts_list": ts_list, "media": media}

    def get_user_reply_network(self, r: TimeRange, user_id, group_id=None) -> tuple[dict, dict]:
        """回复网络：(最常回复谁, 最常被谁回复)。均为频次事实。
        「被谁回复」走 reply_user_id 全扫描（无索引，个人部署规模可接受）。"""
        uid = str(user_id)
        where, params = self._where(
            r, group_id, user_id, extra=["reply_user_id IS NOT NULL", "reply_user_id != user_id"],
            waked=False,
        )
        sent = self._query(
            f"SELECT reply_user_id, COUNT(*) c FROM messages WHERE {where} "
            f"GROUP BY 1 ORDER BY c DESC LIMIT 10",
            params,
        )
        recv_conds = ["reply_user_id = ?", "sender_type = 'user'", "user_id != reply_user_id"]
        recv_params: list = [uid]
        if group_id is not None:
            recv_conds.append("group_id = ?")
            recv_params.append(str(group_id))
        recv_conds += ["ts >= ?", "ts < ?"]
        recv_params += [r.start_ts, r.end_ts]
        received = self._query(
            f"SELECT user_id, COUNT(*) c FROM messages WHERE {' AND '.join(recv_conds)} "
            f"GROUP BY 1 ORDER BY c DESC LIMIT 10",
            recv_params,
        )
        return {str(k): int(c) for k, c in sent}, {str(k): int(c) for k, c in received}

    def get_user_at_sent(self, r: TimeRange, user_id, group_id=None) -> dict[str, int]:
        """@谁最多：个人索引扫描内 json_each 展开（json_valid 防护）。"""
        uid = str(user_id)
        where, params = self._where(r, group_id, user_id, extra=[_JSON_OK], waked=False)
        rows = self._query(
            f"""
            SELECT json_extract(seg.value, '$.qq') AS qq, COUNT(*) c
            FROM (SELECT content_json FROM messages WHERE {where}) m,
                 json_each(m.content_json) seg
            WHERE json_extract(seg.value, '$.t') = 'at'
                  AND qq IS NOT NULL AND qq != '' AND qq != ?
            GROUP BY 1 ORDER BY c DESC LIMIT 10
            """,
            params + [uid],
        )
        return {str(q): int(c) for q, c in rows}

    def get_user_at_received(
        self, r: TimeRange, user_id, group_id, window_days: int = 90
    ) -> list[tuple[str, int]]:
        """谁最常@我：群范围 + 时间窗内 json_each 扫描（json_valid 防护）。仅群范围可用。"""
        cutoff = r.end_ts - min(window_days * 86400, r.duration)
        rows = self._query(
            f"""
            SELECT m.user_id, COUNT(*) c
            FROM (SELECT user_id, content_json FROM messages
                  WHERE group_id = ? AND sender_type = 'user' AND ts >= ? AND ts < ?
                    AND {_JSON_OK}) m,
                 json_each(m.content_json) seg
            WHERE json_extract(seg.value, '$.t') = 'at'
                  AND json_extract(seg.value, '$.qq') = ?
            GROUP BY 1 ORDER BY c DESC LIMIT 10
            """,
            [str(group_id), cutoff, r.end_ts, str(user_id)],
        )
        return [(str(u), int(c)) for u, c in rows]

    def get_user_bot_interaction(
        self, r: TimeRange, user_id, group_id=None, bot_ids: set[str] | None = None,
        offset_seconds: int = 0,
    ) -> dict:
        """Bot 互动画像。私聊统计恒为全局口径（私聊本身即与 Bot 的对话），
        群内唤醒统计受 scope 限制。"""
        uid = str(user_id)
        p = {
            "private_message_count": 0,
            "private_session_count": 0,
            "group_message_count": 0,
            "wake_count": 0,
            "reply_bot_count": 0,
            "at_bot_count": 0,
        }
        p["private_message_count"], p["private_session_count"] = self._query(
            "SELECT COUNT(*), COUNT(DISTINCT session_id) FROM messages "
            "WHERE user_id = ? AND sender_type = 'user' AND message_type = 'private' "
            "AND ts >= ? AND ts < ?",
            [uid, r.start_ts, r.end_ts],
        )[0]
        where, params = self._where(
            r, group_id, user_id, extra=["message_type = 'group'"], waked=False
        )
        p["group_message_count"] = self._query(
            f"SELECT COUNT(*) FROM messages WHERE {where}", params
        )[0][0]
        wake_where, wake_params = self._where(
            r, group_id, user_id, extra=["message_type = 'group'", "waked_bot = 1"],
            waked=False,
        )
        p["wake_count"] = self._query(
            f"SELECT COUNT(*) FROM messages WHERE {wake_where}", wake_params
        )[0][0]
        bots = bot_ids if bot_ids is not None else self.bot_self_ids()
        if bots:
            ph = ",".join("?" * len(bots))
            sorted_bots = sorted(bots)
            reply_where, reply_params = self._where(
                r, group_id, user_id,
                extra=[f"reply_user_id IN ({ph})", "reply_user_id IS NOT NULL"],
                waked=False,
            )
            p["reply_bot_count"] = self._query(
                f"SELECT COUNT(*) FROM messages WHERE {reply_where}",
                [*reply_params, *sorted_bots],
            )[0][0]
            at_where, at_params = self._where(r, group_id, user_id, extra=[_JSON_OK], waked=False)
            p["at_bot_count"] = self._query(
                f"""
                SELECT COUNT(*) FROM (
                  SELECT content_json FROM messages WHERE {at_where}
                ) m, json_each(m.content_json) seg
                WHERE json_extract(seg.value, '$.t') = 'at'
                      AND json_extract(seg.value, '$.qq') IN ({ph})
                """,
                [*at_params, *sorted_bots],
            )[0][0]
        wake_hours = self._query(
            f"""
            SELECT CAST((ts + ?) % 86400 / 3600 AS INTEGER) AS h, COUNT(*) c
            FROM messages WHERE {wake_where}
            GROUP BY h
            """,
            [offset_seconds] + wake_params,
        )
        hour_counts = [0] * 24
        for h, c in wake_hours:
            hour_counts[int(h)] = int(c)
        p["wake_hour_counts"] = hour_counts
        return p

    def get_user_names(self, user_id) -> list[tuple[str, int, int, int]]:
        """昵称历史（全局属性，不受 scope/时间限制）。
        user_name 每条消息冗余存储（契约），GROUP BY 即可还原变迁；
        全角空格 '　' 是部分平台的"空昵称"，按 NULLIF 处理。"""
        rows = self._query(
            """
            SELECT NULLIF(NULLIF(user_name, ''), '　') AS name,
                  MIN(ts), MAX(ts), COUNT(*)
            FROM messages
            WHERE user_id = ? AND sender_type = 'user' AND user_name IS NOT NULL
            GROUP BY name
            ORDER BY MIN(ts)
            """,
            [str(user_id)],
        )
        return [(n, int(a), int(b), int(c)) for n, a, b, c in rows if n]

    # ---------- 群画像 ----------

    def get_group_meta(self, group_id) -> tuple[str, int, int, int, int]:
        """(群名, 消息量, 活跃人数, 首条, 末条)。活跃人数=有过发言的 user 身份数，
        不等于平台群成员总数——数据库没有成员列表，绝不伪造。
        口径随群统计（默认排除唤醒消息，受 exclude_waked 配置控制）。"""
        gid = str(group_id)
        name_row = self._query(
            "SELECT group_name FROM groups WHERE group_id = ?", [gid]
        )
        name = (name_row[0][0] if name_row else "") or gid
        waked_cond = "AND waked_bot = 0" if self.exclude_waked else ""
        row = self._query(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT user_id), MIN(ts), MAX(ts)
            FROM messages WHERE group_id = ? AND sender_type = 'user' {waked_cond}
            """,
            [gid],
        )[0]
        return name, int(row[0] or 0), int(row[1] or 0), row[2], row[3]

    def get_group_reply_pairs(self, group_id, r: TimeRange, limit: int = 10):
        """群内高频回复互动对（reply_user_id 冗余列，无需 join）。"""
        rows = self._query(
            f"""
            SELECT user_id, reply_user_id, COUNT(*) c FROM messages
            WHERE group_id = ? AND sender_type = 'user' AND ts >= ? AND ts < ?
              AND reply_user_id IS NOT NULL AND reply_user_id != user_id
            GROUP BY 1, 2 ORDER BY c DESC LIMIT ?
            """,
            [str(group_id), r.start_ts, r.end_ts, limit],
        )
        return [(str(a), str(b), int(c)) for a, b, c in rows]
