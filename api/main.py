"""
FastAPI app entrypoint. Runs directly via uvicorn — this is the ONLY
deployment path now (Render, or any other long-running host). The
earlier Lambda-specific handler files (api/lambda_handler.py,
api/lambda_sweep_handler.py) and the mangum dependency have been
removed — they're not needed here and would just be dead weight.

RESOLVER_DEPLOYMENT env var still exists and still defaults to "ec2",
kept as the generic name for "I am a long-running process, run the
in-process background sweeper" — this is correct for Render's normal
web service model too, not just literal EC2. No change needed to this
env var for Render; the default already does the right thing.
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

    # Render (and any long-running host) keeps this process alive
    # indefinitely, so an in-process background sweeper loop is the
    # right approach — no external scheduler needed, unlike the Lambda
    # path this project no longer uses.
    sweeper_task = asyncio.create_task(sweep_forever(cache, interval_seconds=900))
    logger.info("started in-process expiry sweeper")

    logger.info("resolver ready with %d adapters", len(app.state.adapters))
    yield

    sweeper_task.cancel()
    await engine.stop()
    await cache.stop()


app = FastAPI(title="Reelz Stream Resolver", lifespan=lifespan)
app.include_router(router)
