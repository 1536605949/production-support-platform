# -*- coding: utf-8 -*-
"""Fixed-window rate limiting with a Redis backend and an in-process fallback.

The limit is per call, not per instance, because each tenant carries its own
``rate_limit_per_minute``. When Redis is unreachable the limiter degrades to a
process-local bucket instead of failing open *or* failing closed — a single
node stays protected, and the degradation is visible in the logs.
"""
import logging
import time
from collections import defaultdict

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover
    redis_async = None

logger = logging.getLogger('gateway.rate_limit')

# When Redis is unreachable we fall back to an in-process bucket, but avoid
# paying connection/DNS cost on every request: retry Redis only after a pause.
REDIS_RETRY_AFTER_SECONDS = 30.0


class RateLimiter:
    def __init__(self, redis_url: str, limit: int):
        self.limit = limit
        self._memory = defaultdict(lambda: [0, 0])
        self.redis = redis_async.from_url(redis_url, decode_responses=True) if redis_async else None
        self._redis_down_until = 0.0
        self._warned = False

    async def allow(self, key: str, limit: int | None = None) -> bool:
        effective = int(limit if limit else self.limit)
        minute = int(time.time() // 60)
        now = time.time()

        if self.redis and now >= self._redis_down_until:
            try:
                redis_key = f'rl:{key}:{minute}'
                count = await self.redis.incr(redis_key)
                if count == 1:
                    await self.redis.expire(redis_key, 70)
                self._warned = False
                return count <= effective
            except Exception:
                self._redis_down_until = now + REDIS_RETRY_AFTER_SECONDS
                if not self._warned:
                    logger.warning('redis unavailable, falling back to in-process rate limit')
                    self._warned = True

        bucket = self._memory[key]
        if bucket[0] != minute:
            bucket[:] = [minute, 0]
        bucket[1] += 1
        return bucket[1] <= effective
