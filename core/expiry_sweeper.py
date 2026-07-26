"""
Background task that periodically deletes expired cache rows.

NOTE ON EC2 vs LAMBDA:
- On EC2 (long-running process): run this as an asyncio background task
  started alongside the API — see api/main.py's lifespan hook. Simple,
  no extra moving parts.
- On Lambda: there is no long-running process to host a background loop
  in. Use EventBridge Scheduler to invoke a separate small Lambda
  (or the same one with a different event source) on a cron schedule
  (e.g. every 15 minutes) that just calls `StreamCache.sweep_expired()`
  once and exits. See setup.md for the exact EventBridge config.
Either way, this is pure housekeeping — read correctness never depends
on the sweep having run recently, because `StreamCache.get()` already
filters `expires_at > now()` on every read.
"""
from __future__ import annotations

import asyncio
import logging

from core.cache import StreamCache

logger = logging.getLogger("resolver.sweeper")


async def run_forever(cache: StreamCache, interval_seconds: int = 900) -> None:
    """For EC2 / long-running deployment: call this as a background task."""
    while True:
        try:
            deleted = await cache.sweep_expired()
            if deleted:
                logger.info("expiry sweep deleted %d expired rows", deleted)
        except Exception:
            logger.exception("expiry sweep failed")
        await asyncio.sleep(interval_seconds)


async def run_once(cache: StreamCache) -> int:
    """For Lambda: call this from a scheduled-event handler, once, then exit."""
    deleted = await cache.sweep_expired()
    logger.info("expiry sweep (single run) deleted %d expired rows", deleted)
    return deleted
