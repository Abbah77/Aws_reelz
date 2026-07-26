"""
Postgres-backed resolved-URL cache.

WHY POSTGRES OVER REDIS HERE: you said you're already using Postgres, and
for this workload (low write volume, need for a real expiry timestamp
column you can query/inspect, no need for sub-millisecond reads) plain
Postgres is completely sufficient — no need to introduce Redis just to
add a moving part. If read volume gets heavy later, this same table
design translates directly to Redis with TTL if you ever want to switch.

Auto-delete-on-expire is implemented two ways, deliberately BOTH:
  1. Every read filters `WHERE expires_at > now()` — so an expired row
     is functionally invisible immediately, even if the delete sweep
     hasn't run yet. This is what actually matters for correctness.
  2. A periodic sweep DELETEs rows past expiry, so the table doesn't
     grow forever. This is just housekeeping — the WHERE clause in (1)
     is your real safety net, the sweep is cleanup, not correctness.

Uses asyncpg directly rather than an ORM — this table is simple enough
(one table, no relations to speak of) that SQLAlchemy would be pure
overhead here.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import asyncpg

from core.models import ResolveResult

logger = logging.getLogger("resolver.cache")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS resolved_streams (
    cache_key    TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    url          TEXT NOT NULL,
    is_hls       BOOLEAN NOT NULL,
    headers      JSONB NOT NULL DEFAULT '{}'::jsonb,
    referer      TEXT NOT NULL DEFAULT '',
    origin       TEXT NOT NULL DEFAULT '',
    resolved_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resolved_streams_expires_at
    ON resolved_streams (expires_at);
"""


class StreamCache:
    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool: Optional[asyncpg.Pool] = None

    async def start(self) -> None:
        self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
        async with self._pool.acquire() as conn:
            await conn.execute(_CREATE_TABLE_SQL)
        logger.info("StreamCache connected and schema ensured")

    async def stop(self) -> None:
        if self._pool:
            await self._pool.close()

    async def get(self, cache_key: str) -> Optional[ResolveResult]:
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT source_id, source_name, url, is_hls, headers,
                       referer, origin, resolved_at, expires_at
                FROM resolved_streams
                WHERE cache_key = $1 AND expires_at > now()
                """,
                cache_key,
            )
        if row is None:
            return None
        return ResolveResult(
            url=row["url"],
            is_hls=row["is_hls"],
            headers=json.loads(row["headers"]) if isinstance(row["headers"], str) else dict(row["headers"]),
            referer=row["referer"],
            origin=row["origin"],
            source_id=row["source_id"],
            source_name=row["source_name"],
            resolved_at=row["resolved_at"],
            expires_at=row["expires_at"],
        )

    async def put(self, cache_key: str, result: ResolveResult) -> None:
        assert self._pool is not None
        expires_at = result.expires_at or datetime.now(timezone.utc)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO resolved_streams
                    (cache_key, source_id, source_name, url, is_hls,
                     headers, referer, origin, resolved_at, expires_at)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, now(), $9)
                ON CONFLICT (cache_key) DO UPDATE SET
                    source_id   = EXCLUDED.source_id,
                    source_name = EXCLUDED.source_name,
                    url         = EXCLUDED.url,
                    is_hls      = EXCLUDED.is_hls,
                    headers     = EXCLUDED.headers,
                    referer     = EXCLUDED.referer,
                    origin      = EXCLUDED.origin,
                    resolved_at = now(),
                    expires_at  = EXCLUDED.expires_at
                """,
                cache_key,
                result.source_id,
                result.source_name,
                result.url,
                result.is_hls,
                json.dumps(result.headers),
                result.referer,
                result.origin,
                expires_at,
            )

    async def sweep_expired(self) -> int:
        """
        Housekeeping only — reads already exclude expired rows via the
        WHERE clause in get(). Call this periodically (see
        core/expiry_sweeper.py) just to keep the table from growing
        unbounded, not because correctness depends on it.
        """
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            result = await conn.execute("DELETE FROM resolved_streams WHERE expires_at <= now()")
        # asyncpg returns a string like "DELETE 3"
        try:
            return int(result.split()[-1])
        except Exception:
            return 0
