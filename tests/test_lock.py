"""Tests for the optional Redis run lock (fake client; no Redis)."""

from __future__ import annotations

from quant_momentum.lock import run_lock


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)
        return 1


def test_noop_lock_without_redis() -> None:
    with run_lock(redis_url=None) as acquired:
        assert acquired is True


def test_acquire_then_release() -> None:
    redis = FakeRedis()
    with run_lock(redis_url="redis://x", client=redis, key="k") as acquired:
        assert acquired is True
        assert "k" in redis.store
    assert "k" not in redis.store  # released on exit


def test_second_acquire_blocked_while_held() -> None:
    redis = FakeRedis()
    with run_lock(redis_url="redis://x", client=redis, key="k") as first:
        assert first is True
        with run_lock(redis_url="redis://x", client=redis, key="k") as second:
            assert second is False
    assert "k" not in redis.store
