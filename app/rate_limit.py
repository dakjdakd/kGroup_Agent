from __future__ import annotations

import time
import threading
from collections import defaultdict, deque
from typing import Protocol


class RateLimiter(Protocol):
    def allow(self, customer_id: str, action_id: str, now: float | None = None) -> bool: ...


class SQLiteRateLimiter:
    def __init__(self, store) -> None:
        self.store = store

    def allow(self, customer_id: str, action_id: str, now: float | None = None) -> bool:
        return self.store.try_record_outbound(customer_id, action_id, time.time() if now is None else now)

    def release(self, customer_id: str, action_id: str) -> None:
        self.store.release_outbound_reservation(action_id)


class InMemoryRateLimiter:
    """Offline implementation; process-local only. Use Redis for multiple workers."""

    def __init__(self, window_seconds: float = 60.0) -> None:
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, customer_id: str, action_id: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            events = self._events[customer_id]
            while events and events[0] < now - self.window_seconds:
                events.popleft()
            if events:
                return False
            events.append(now)
            return True


class RedisRateLimiter:
    """Optional production adapter. Requires redis-py and an atomic Lua script."""

    SCRIPT = """
    local key = KEYS[1]
    local now = tonumber(ARGV[1])
    local cutoff = now - tonumber(ARGV[2])
    redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
    if redis.call('ZCARD', key) >= 1 then return 0 end
    redis.call('ZADD', key, now, ARGV[3])
    redis.call('EXPIRE', key, math.ceil(tonumber(ARGV[2])))
    return 1
    """

    def __init__(self, redis_client, window_seconds: int = 60) -> None:
        self.redis = redis_client
        self.window_seconds = window_seconds
        self._script = self.redis.register_script(self.SCRIPT)

    def allow(self, customer_id: str, action_id: str, now: float | None = None) -> bool:
        result = self._script(keys=[f"outbound:{customer_id}"], args=[time.time() if now is None else now, self.window_seconds, action_id])
        return bool(result)
