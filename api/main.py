"""
FastAPI app entrypoint. Run directly (EC2 / local dev) via uvicorn, or
wrap with Mangum for Lambda (see setup.md — Lambda deployment section).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from adapters.registry import all_adapters
from api.routes import router
from core.cache import StreamCache
from core.coalescer import Coalescer
from core.engine import ResolverEngine
from core.expiry_sweeper import run_forever as sweep_forever
from core.stats import StatsTracker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("resolver.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    dsn = os.environ["DATABASE_URL"]
    stats = StatsTracker()
    cache = StreamCache(dsn=dsn)
    engine = ResolverEngine(stats=stats, headless=True)
    coalescer = Coalescer()

    await cache.start()
    await engine.start()

    app.state.cache = cache
    app.state.engine = engine
    app.state.stats = stats
    app.state.coalescer = coalescer
    app.state.adapters = all_adapters()

    sweeper_task = None
    # Only run the in-process sweeper loop on a long-running deployment
    # (EC2). On Lambda, RESOLVER_DEPLOYMENT=lambda and expiry sweeping is
    # driven by a separate EventBridge-scheduled invocation instead (see
    # setup.md) — a Lambda handler that returns has no business starting
    # an infinite background loop that will just get frozen/killed.
    if os.environ.get("RESOLVER_DEPLOYMENT", "ec2") == "ec2":
        sweeper_task = asyncio.create_task(sweep_forever(cache, interval_seconds=900))
        logger.info("started in-process expiry sweeper (EC2 mode)")

    logger.info("resolver ready with %d adapters", len(app.state.adapters))
    yield

    if sweeper_task:
        sweeper_task.cancel()
    await engine.stop()
    await cache.stop()


app = FastAPI(title="Reelz Stream Resolver", lifespan=lifespan)
app.include_router(router)
