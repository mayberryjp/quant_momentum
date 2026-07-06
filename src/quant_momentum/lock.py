"""Optional Redis-based run lock (spec §13, decision: Postgres-first, Redis optional).

When ``QUANT_REDIS_URL`` is unset the lock is a no-op that always "acquires", so
single-instance deployments need no Redis. When configured, it prevents
overlapping scheduled runs across instances using ``SET key token NX EX ttl``.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager

log = logging.getLogger("quant_momentum.lock")

DEFAULT_LOCK_KEY = "quant_momentum:run_lock"
DEFAULT_TTL_SECONDS = 3600


@contextmanager
def run_lock(
    *,
    redis_url: str | None,
    key: str = DEFAULT_LOCK_KEY,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    client=None,
):
    """Context manager yielding ``True`` if the run may proceed.

    Yields ``True`` when no Redis is configured (no-op) or the lock was
    acquired; ``False`` when another instance currently holds the lock.
    """
    if not redis_url and client is None:
        yield True
        return

    if client is None:
        import redis  # imported lazily; optional dependency at runtime

        client = redis.from_url(redis_url)

    token = str(uuid.uuid4())
    acquired = bool(client.set(key, token, nx=True, ex=ttl_seconds))
    if not acquired:
        log.warning("run lock %s is held by another instance", key)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                current = client.get(key)
                if current in (token, token.encode()):
                    client.delete(key)
            except Exception:  # releasing is best-effort
                log.warning("failed to release run lock %s", key, exc_info=True)
