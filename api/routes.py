"""
API routes. Single primary endpoint: GET /resolve.

Response shape is deliberately close to what your Android app's
StreamResult already looks like, so swapping the Android StreamEngine to
call this instead of (or alongside) its on-device WebView scan is a
small change, not a rewrite — see setup.md "Android integration" section
for the specific StreamEngine diff.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from adapters.generic import GenericAdapter
from adapters.registry import all_adapters, get_adapter
from core.models import MediaType, ResolveError, ResolveRequest

logger = logging.getLogger("resolver.api")
router = APIRouter()


class ResolveResponse(BaseModel):
    url: str
    is_hls: bool
    headers: dict
    referer: str
    origin: str
    source_id: str
    source_name: str
    cached: bool


@router.get("/health")
async def health(request: Request):
    stats = request.app.state.stats.snapshot()
    return {
        "status": "ok",
        "adapters": [a.source_id for a in all_adapters()],
        "stats": {
            sid: {
                "success_rate": round(s.success_rate, 3),
                "avg_ms": round(s.avg_ms),
                "total_count": s.total_count,
            }
            for sid, s in stats.items()
        },
    }


@router.get("/resolve", response_model=ResolveResponse)
async def resolve(
    request: Request,
    tmdb_id: int,
    media_type: str,
    season: Optional[int] = None,
    episode: Optional[int] = None,
    source_id: Optional[str] = None,
):
    try:
        mt = MediaType(media_type)
    except ValueError:
        raise HTTPException(400, f"invalid media_type: {media_type!r}, expected 'movie' or 'tv'")

    if mt == MediaType.TV and (season is None or episode is None):
        raise HTTPException(400, "season and episode are required for media_type=tv")

    req = ResolveRequest(tmdb_id=tmdb_id, media_type=mt, season=season, episode=episode, source_id=source_id)

    cache = request.app.state.cache
    engine = request.app.state.engine
    stats = request.app.state.stats
    coalescer = request.app.state.coalescer

    candidates = [get_adapter(source_id)] if source_id else all_adapters()

    # Collect EVERY adapter's outcome, not just the last one — with 5+
    # sources, losing 4/5 of the diagnostic picture behind "last_error"
    # makes it much harder to tell "source X is genuinely broken" from
    # "source X was merely skipped because Y went first and won". This
    # is what actually lets you (or a /health dashboard later) see
    # which specific sources need a look, source by source.
    attempts: dict[str, str] = {}
    skipped: list[str] = []

    for adapter in candidates:
        if source_id is None and stats.should_skip(adapter.source_id):
            skipped.append(adapter.source_id)
            continue

        key = req.cache_key(adapter.source_id)

        cached = await cache.get(key)
        if cached is not None:
            return ResolveResponse(**cached.to_cache_dict(), cached=True)

        async def _do_resolve(a=adapter):
            return await engine.resolve(a, tmdb_id, mt, season, episode)

        try:
            result = await coalescer.run(key, _do_resolve)
        except ResolveError as e:
            attempts[adapter.source_id] = e.reason
            continue
        except Exception as e:
            attempts[adapter.source_id] = f"unexpected: {e}"
            logger.exception("adapter=%s crashed", adapter.source_id)
            continue

        await cache.put(key, result)
        return ResolveResponse(**result.to_cache_dict(), cached=False)

    raise HTTPException(
        502,
        detail={
            "message": f"no adapter could resolve tmdb_id={tmdb_id} media_type={media_type}",
            "attempted": attempts,   # source_id -> failure reason, for EVERY source actually tried
            "skipped_unhealthy": skipped,  # source_id list, skipped due to should_skip() before even trying
        },
    )


class GenericResolveRequest(BaseModel):
    """
    Body for /resolve-generic — the honest 'try any source, even one
    with no dedicated adapter' endpoint. See adapters/generic.py's
    module docstring for exactly what this can and can't be expected to
    defeat. This is deliberately NOT cached the same way registered
    adapters are (no stable source_id/cache_key convention for an
    arbitrary one-off URL) — it always does a live resolve.
    """
    embed_url: str
    source_id: str = "generic_unknown"
    source_name: str = "Unknown Source"
    referer: str = ""
    origin: str = ""


@router.post("/resolve-generic", response_model=ResolveResponse)
async def resolve_generic(request: Request, body: GenericResolveRequest):
    """
    Try to resolve a stream URL from ANY embed URL, even one with no
    dedicated adapter written yet. See adapters/generic.py for exactly
    what this heuristic approach can and cannot be expected to defeat —
    it is a real, best-effort attempt, not a guarantee. Useful for:
      - Quickly testing whether a brand new source is even worth writing
        a dedicated adapter for, before investing that time
      - A genuinely long-tail source you'll only ever resolve rarely,
        where a dedicated adapter isn't worth maintaining
    """
    engine = request.app.state.engine
    adapter = GenericAdapter(
        embed_url=body.embed_url,
        source_id=body.source_id,
        source_name=body.source_name,
        referer=body.referer,
        origin=body.origin,
    )
    try:
        result = await engine.resolve(adapter, tmdb_id=0, media_type=MediaType.MOVIE)
    except ResolveError as e:
        raise HTTPException(502, detail={"message": str(e), "source_id": body.source_id})

    return ResolveResponse(**result.to_cache_dict(), cached=False)
