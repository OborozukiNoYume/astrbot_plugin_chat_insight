"""TTLCache（insight/cache.py）：画像内存缓存的开关 / 命中 / 过期 / 清空。"""

from __future__ import annotations

from insight.cache import TTLCache


def test_disabled_cache_is_noop():
    c = TTLCache(0)
    assert not c.enabled
    c.put(("k",), "v")
    assert c.get(("k",)) is None
    assert c.clear() == 0


def test_put_get_and_clear():
    c = TTLCache(60)
    assert c.enabled
    c.put(("card", "u1"), {"a": 1})
    assert c.get(("card", "u1")) == {"a": 1}
    assert c.get(("miss",)) is None
    assert c.clear() == 1
    assert c.get(("card", "u1")) is None


def test_expired_entry_returns_none(monkeypatch):
    c = TTLCache(60)
    t0 = 1_000_000.0
    clock = {"now": t0}
    monkeypatch.setattr("insight.cache.time.monotonic", lambda: clock["now"])
    c.put(("k",), "v")
    assert c.get(("k",)) == "v"
    clock["now"] = t0 + 61  # 超过 TTL
    assert c.get(("k",)) is None
    assert ("k",) not in c._store  # 过期条目顺带清除
