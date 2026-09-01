"""chatlog.db 定位与只读访问层。

契约（见 ChatLogger 插件 QUERY_GUIDE.md）：
- 只读连接：sqlite3.connect(f"file:{db}?mode=ro", uri=True)，WAL 下与写入方并发安全
- schema user_version = 3；低于该版本提示升级 ChatLogger
- 消费插件严格只读，写路径归 ChatLogger 独占
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

CONTRACT_USER_VERSION = 3


class DatabaseNotAvailable(Exception):
    """数据库不存在 / 路径错误 / ChatLogger 未安装。"""


class SchemaIncompatible(Exception):
    """schema 版本过低或 SQLite 能力不足。"""


class ChatlogDB:
    """只读数据库门面。查询用短连接（connect 上下文），用完即释放，不持有长事务。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_file():
            raise DatabaseNotAvailable(
                f"未找到 chatlog.db（{self.path}）。"
                "请确认已安装 ChatLogger 插件并已产生聊天记录，"
                "或在聊天洞察插件配置中填写正确的 database_path。"
            )
        try:
            with self.connect() as conn:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                conn.execute("SELECT json_extract('{\"t\":\"x\"}', '$.t')").fetchone()
        except sqlite3.DatabaseError as e:
            raise DatabaseNotAvailable(f"chatlog.db 无法读取（{e}），数据库可能已损坏。") from e
        if version < CONTRACT_USER_VERSION:
            raise SchemaIncompatible(
                f"chatlog.db schema 版本过低（user_version={version}，契约要求 >={CONTRACT_USER_VERSION}）。"
                "请先升级 ChatLogger 插件后再使用聊天洞察。"
            )
        self.user_version = version

    @contextmanager
    def connect(self):
        # as_uri() 自带百分号编码：路径含 ?/#/空格时手拼 file: URI 会被截断，
        # 误报 user_version=0（把路径问题伪装成 schema 契约问题）
        conn = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5.0
        )
        try:
            yield conn
        finally:
            conn.close()

    def status_line(self) -> str:
        return f"chatlog.db: {self.path} (user_version={self.user_version})"
