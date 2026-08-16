"""画像内存 TTL 缓存。

属于本插件自己，绝不写回 chatlog.db。首版不持久化，重启重算。
"""

from __future__ import annotations

import time
from typing import Any


class TTLCache:
    def __init__(self, ttl_seconds: int):
        self.ttl = int(ttl_seconds)
        self._store: dict[tuple, tuple[float, Any]] = {}

    @property
    def enabled(self) -> bool:
        return self.ttl > 0

    def get(self, key: tuple) -> Any | None:
        if not self.enabled:
            return None
        hit = self._store.get(key)
        if hit is None:
            return None
        expires_at, value = hit
        if time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def put(self, key: tuple, value: Any):
        if not self.enabled:
            return
        self._store[key] = (time.monotonic() + self.ttl, value)

    def clear(self) -> int:
        n = len(self._store)
        self._store.clear()
        return n
