"""
Adapter for vidlink.pro.

HONEST CAVEAT: I have not reverse-engineered this site's actual network
behavior the way we did for vidsrc.to from your real DevTools captures.
This adapter is written as a STRUCTURAL TEMPLATE showing how a
lighter-weight site would plug into the framework via
`prefers_light_resolution` — the httpx-only fast path, no Chromium spin-
up. You said your app currently gets vidlink.pro working via WebView, so
it likely DOES expose the real URL directly in page content or an early
XHR, similar to what your existing Android DirectScanner already
successfully matches against.

BEFORE TRUSTING THIS IN PRODUCTION: capture real DevTools traffic
against vidlink.pro the same way you did for vidsrc.to, confirm exactly
which response contains the real URL and what its content-type/expiry
look like, then adjust `try_light_resolve`'s regex/parsing to match
reality. Shipping this unverified would repeat the same mistake as
guessing at implementation details without checking — don't do that
here either.
"""
from __future__ import annotations

import re
from typing import Optional

from playwright.async_api import Page

from core.base_adapter import InterceptedRequest, SiteAdapter
from core.engine import light_http_get
from core.models import MediaType, ResolveResult

_M3U8_RE = re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*")


class VidlinkProAdapter(SiteAdapter):
    source_id = "vidlink_pro"
    source_name = "vidlink.pro"
    requires_full_render = True   # fallback path if light resolution misses
    prefers_light_resolution = True
    render_timeout_seconds = 15

    def build_embed_url(
        self,
        tmdb_id: int,
        media_type: MediaType,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> str:
        if media_type == MediaType.TV:
            return f"https://vidlink.pro/tv/{tmdb_id}/{season}/{episode}"
        return f"https://vidlink.pro/movie/{tmdb_id}"

    async def try_light_resolve(self, embed_url: str) -> Optional[ResolveResult]:
        try:
            resp = await light_http_get(
                embed_url,
                headers={
                    "Referer": "https://vidlink.pro/",
                    "Origin": "https://vidlink.pro",
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                },
            )
        except Exception:
            return None

        if resp.status_code != 200:
            return None

        match = _M3U8_RE.search(resp.text)
        if not match:
            return None  # normal miss -> engine falls through to full render

        return ResolveResult(
            url=match.group(0),
            is_hls=True,
            referer="https://vidlink.pro/",
            origin="https://vidlink.pro",
            headers={"Referer": "https://vidlink.pro/", "Origin": "https://vidlink.pro"},
            source_id=self.source_id,
            source_name=self.source_name,
            expires_at=self.default_expiry(),  # no known JWT here yet — verify and replace
        )

    async def extract_from_page(
        self,
        page: Page,
        intercepted: list[InterceptedRequest],
        embed_url: str,
    ) -> Optional[ResolveResult]:
        # Full-render fallback if the light path missed. Same idea as
        # vidsrc_to's extract_from_page: only trust genuinely playable
        # responses, not URL shape.
        for req in intercepted:
            if req.response_status != 200:
                continue
            ct = (req.response_content_type or "").lower()
            if ".m3u8" in req.url and ("mpegurl" in ct or "octet-stream" in ct or ct == ""):
                return ResolveResult(
                    url=req.url,
                    is_hls=True,
                    referer="https://vidlink.pro/",
                    origin="https://vidlink.pro",
                    headers={"Referer": "https://vidlink.pro/", "Origin": "https://vidlink.pro"},
                    source_id=self.source_id,
                    source_name=self.source_name,
                    expires_at=self.default_expiry(),
                )
        return None
