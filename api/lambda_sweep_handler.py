"""
Standalone Lambda handler for the EventBridge-scheduled expiry sweep.
Deliberately separate from lambda_handler.py: this one does NOT boot
Playwright/Chromium at all — it only touches Postgres — so it can run
on minimal memory (128-256MB) and finish in well under a second,
completely decoupled from the resolver Lambda's cost profile.

EventBridge Scheduler config: see setup.md, "Lambda deployment" section.
Suggested schedule: rate(15 minutes).
"""
from __future__ import annotations

import asyncio
import os

from core.cache import StreamCache
from core.expiry_sweeper import run_once


def handler(event, context):
    dsn = os.environ["DATABASE_URL"]

    async def _run():
        cache = StreamCache(dsn=dsn)
        await cache.start()
        try:
            deleted = await run_once(cache)
            return {"deleted_rows": deleted}
        finally:
            await cache.stop()

    return asyncio.run(_run())
