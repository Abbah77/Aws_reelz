"""
Request coalescing: if 50 users all ask to resolve the same trending
title in the same few seconds, this ensures only ONE actual resolution
happens — everyone else awaits the same in-flight result.

WHY THIS MATTERS (flagged earlier in the conversation before any code
was written): without this, a popular title trending at the exact
moment your cache entry expires causes a thundering herd — N simultaneous
Chromium launches against the same source, which is both wasteful and
exactly the kind of burst pattern that gets a single-IP resolver
rate-limited or Cloudflare-challenged by the source site.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Coroutine, TypeVar

T = TypeVar("T")


class Coalescer:
    def __init__(self):
        self._in_flight: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def run(self, key: str, coro_factory: Callable[[], Coroutine[None, None, T]]) -> T:
        async with self._lock:
            existing = self._in_flight.get(key)
            if existing is not None:
                fut = existing
                already_waiting = True
            else:
                fut = asyncio.ensure_future(coro_factory())
                self._in_flight[key] = fut
                already_waiting = False

        try:
            return await fut
        finally:
            if not already_waiting:
                async with self._lock:
                    self._in_flight.pop(key, None)
