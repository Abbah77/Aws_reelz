"""
Core data models for the resolver.

Kept dependency-light (dataclasses only) so these can be imported by
adapters, the cache layer, and the API layer without pulling in heavy
deps like Playwright or SQLAlchemy just to read a type.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class MediaType(str, Enum):
    MOVIE = "movie"
    TV = "tv"


@dataclass
class ResolveRequest:
    tmdb_id: int
    media_type: MediaType
    season: Optional[int] = None
    episode: Optional[int] = None
    # Explicit source id to try, or None to try all adapters in priority order
    source_id: Optional[str] = None

    def cache_key(self, source_id: str) -> str:
        if self.media_type == MediaType.TV:
            return f"{source_id}:tv:{self.tmdb_id}:{self.season}:{self.episode}"
        return f"{source_id}:movie:{self.tmdb_id}"


@dataclass
class ResolveResult:
    url: str
    is_hls: bool
    headers: dict = field(default_factory=dict)
    referer: str = ""
    origin: str = ""
    source_id: str = ""
    source_name: str = ""
    resolved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # When the token/session embedded in `url` stops being valid. This is
    # what drives Postgres row expiry — NOT a fixed TTL guess, but read
    # from whatever the source itself told us (query param, response
    # header, or a conservative adapter-specific default when the source
    # doesn't say).
    expires_at: Optional[datetime] = None

    def to_cache_dict(self) -> dict:
        return {
            "url": self.url,
            "is_hls": self.is_hls,
            "headers": self.headers,
            "referer": self.referer,
            "origin": self.origin,
            "source_id": self.source_id,
            "source_name": self.source_name,
        }


class ResolveError(Exception):
    """Raised by an adapter when it definitively fails to resolve.
    Distinguished from a timeout/crash so the engine can record a clean
    failure stat rather than an ambiguous exception."""
    def __init__(self, source_id: str, reason: str):
        self.source_id = source_id
        self.reason = reason
        super().__init__(f"[{source_id}] {reason}")
