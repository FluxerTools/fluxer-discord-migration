"""Proactive rate limiting for Fluxer's creation endpoints.

Measured against the live API (2026-10): POST /guilds/{id}/roles and
POST /guilds/{id}/channels each allow **10 requests per 60 second window**,
per guild. Two quirks make the stock client handling insufficient:

1. Rate limit headers are only returned on the 429 response itself. Successful
   responses carry no X-RateLimit-* headers, so a reactive limiter has nothing
   to work with until it has already been blocked.
2. The 429 body's ``retry_after`` is far shorter than the real window. A 429 at
   the start of a window reports retry_after≈2.5s while the header says
   X-RateLimit-Reset-After≈56s. Sleeping retry_after simply earns another 429,
   which is why a migration can stall in a retry loop.

This module paces requests so the limit is not hit in the first place, and
treats X-RateLimit-Reset-After as the source of truth when it is.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

# Measured server-side limit for creation endpoints
DEFAULT_LIMIT = 10
DEFAULT_WINDOW = 60.0

# Small cushion so clock drift and in-flight requests don't push us over
SAFETY_MARGIN = 0.5


class CreationRateLimiter:
    """Sliding-window pacer for a single creation bucket.

    Records the timestamp of each request and, once ``limit`` requests have been
    made inside ``window`` seconds, waits for the oldest one to age out before
    allowing the next.
    """

    def __init__(
        self,
        limit: int = DEFAULT_LIMIT,
        window: float = DEFAULT_WINDOW,
        logger: Any = None,
    ):
        self.limit = limit
        self.window = window
        self.logger = logger
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()
        # Set when a 429 told us the real reset time
        self._blocked_until: float = 0.0

    def _log(self, message: str, level: str = "INFO"):
        if self.logger is not None:
            self.logger.log(message, level)

    async def acquire(self) -> None:
        """Wait until another request may be sent."""
        async with self._lock:
            while True:
                now = time.monotonic()

                # Honour a server-reported reset first
                if now < self._blocked_until:
                    delay = self._blocked_until - now
                    self._log(
                        f"Rate limit window full - waiting {delay:.0f}s "
                        f"(Fluxer allows {self.limit} per {self.window:.0f}s)"
                    )
                    await asyncio.sleep(delay)
                    continue

                # Drop timestamps that have aged out of the window
                cutoff = now - self.window
                self._timestamps = [t for t in self._timestamps if t > cutoff]

                if len(self._timestamps) < self.limit:
                    self._timestamps.append(now)
                    return

                # Window is full: wait for the oldest request to expire
                delay = self._timestamps[0] + self.window - now + SAFETY_MARGIN
                self._log(
                    f"Rate limit reached ({self.limit}/{self.window:.0f}s) - "
                    f"pausing {delay:.0f}s before continuing..."
                )
                await asyncio.sleep(delay)

    def note_rate_limited(self, reset_after: float) -> None:
        """Record a server-reported reset window from a 429 response.

        ``reset_after`` is X-RateLimit-Reset-After, not the body's retry_after:
        the latter under-reports the wait and causes repeated 429s.
        """
        self._blocked_until = max(
            self._blocked_until, time.monotonic() + reset_after + SAFETY_MARGIN
        )
        # The window is known to be exhausted
        self._timestamps = [time.monotonic()] * self.limit


class RateLimitedClient:
    """Wraps a Fluxer client and paces calls to the creation endpoints.

    Non-creation calls (permission overwrites, lookups, edits) are passed
    straight through; those were measured as unthrottled.
    """

    # Methods that consume the 10-per-60s creation budget, and the bucket each
    # one belongs to. Roles and channels are separate server-side buckets.
    THROTTLED: dict[str, str] = {
        "create_guild_role": "roles",
        "create_guild_channel": "channels",
    }

    def __init__(self, client: Any, logger: Any = None):
        self._client = client
        self._logger = logger
        self._limiters: dict[str, CreationRateLimiter] = {}

    def _limiter(self, bucket: str) -> CreationRateLimiter:
        if bucket not in self._limiters:
            self._limiters[bucket] = CreationRateLimiter(logger=self._logger)
        return self._limiters[bucket]

    def __getattr__(self, name: str):
        attr = getattr(self._client, name)

        bucket = self.THROTTLED.get(name)
        if bucket is None or not callable(attr):
            return attr

        limiter = self._limiter(bucket)

        async def throttled(*args: Any, **kwargs: Any):
            await limiter.acquire()
            try:
                return await attr(*args, **kwargs)
            except Exception as e:
                # If the server still rate limited us, trust its reset window
                reset_after = _reset_after_from_error(e)
                if reset_after is not None:
                    limiter.note_rate_limited(reset_after)
                raise

        return throttled


def _reset_after_from_error(error: Exception) -> float | None:
    """Extract X-RateLimit-Reset-After (or retry_after) from a rate limit error."""
    for attr in ("reset_after", "retry_after"):
        value = getattr(error, attr, None)
        if isinstance(value, (int, float)) and value > 0:
            return float(value)
    return None
