"""
Adapter for vidsrc.to.

THIS IS THE CONCRETE EXAMPLE MATCHING WHAT WE REVERSE-ENGINEERED FROM
YOUR DEVTOOLS CAPTURES EARLIER IN THIS CONVERSATION:
  - The site returns an index.m3u8 whose "segments" are actually
    page-N.html?token=<JWT> URLs, not real .ts/.mp4 chunks.
  - The JWT in the token= query param has a genuine `exp` claim — this
    is what we use for real cache expiry instead of guessing a fixed TTL.
  - A bare httpx GET (no JS) gets the same HTML-wrapped response a real
    ExoPlayer would — meaning this source is NOT resolvable via
    light_resolve; it needs the full render path so the adapter can
    watch what a real page load actually does with that token, and
    decide whether the wrapped response is ever unwrapped into a real
    media byte stream by their own JS.

HONEST STATUS: as of the DevTools captures we reviewed, this site's
current scheme does not appear to ever expose a real, ExoPlayer-playable
URL — the "segments" stay HTML-wrapped even after JS runs. This adapter
is written defensively: it looks for a genuine playable response
(content-type video/* or a .ts/.mp4/.m3u8 URL that actually returns
binary), and returns None (not a broken guess) if it never finds one.
If/when the site's scheme changes, this is the one place to update.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from playwright.async_api import Page

from core.base_adapter import InterceptedRequest, SiteAdapter
from core.models import MediaType, ResolveResult

_M3U8_RE = re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*")
_TOKEN_RE = re.compile(r"[?&]token=([^&\s\"'<>]+)")


class VidsrcToAdapter(SiteAdapter):
    source_id = "vidsrc_to"
    source_name = "vidsrc.to"
    requires_full_render = True
    prefers_light_resolution = False  # see module docstring — confirmed JS-gated
    render_timeout_seconds = 20

    def build_embed_url(
        self,
        tmdb_id: int,
        media_type: MediaType,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> str:
        if media_type == MediaType.TV:
            return f"https://vidsrc.to/embed/tv/{tmdb_id}/{season}/{episode}"
        return f"https://vidsrc.to/embed/movie/{tmdb_id}"

    async def extract_from_page(
        self,
        page: Page,
        intercepted: list[InterceptedRequest],
        embed_url: str,
    ) -> Optional[ResolveResult]:
        # 1) Look for a response that is GENUINELY playable — real
        #    video/binary content-type, not text/html wearing a .m3u8
        #    extension. This is the exact check that would have caught
        #    the page-N.html-disguised-as-a-segment problem from the
        #    DevTools captures: we don't trust the URL shape, we trust
        #    the actual response the browser got.
        for req in intercepted:
            if req.response_status != 200:
                continue
            ct = (req.response_content_type or "").lower()
            is_real_media = (
                "video/" in ct
                or "application/vnd.apple.mpegurl" in ct
                or "application/x-mpegurl" in ct
                or "application/octet-stream" in ct  # some CDNs mislabel but ARE binary
            )
            if is_real_media and (".m3u8" in req.url or ".mp4" in req.url or ".ts" in req.url):
                return self._build_result(req.url)

        # 2) Fallback: an .m3u8 URL was found in page JS state (e.g. a
        #    global var, not necessarily a network response yet) AND we
        #    can confirm its content-type independently. We deliberately
        #    do NOT just trust a regex match on page content the way the
        #    Android app's fast-path DirectScanner does — that's exactly
        #    what got fooled by this site's HTML-wrapped fake segments.
        #    Server-side, we have the luxury of verifying before trusting.
        try:
            page_content = await page.content()
        except Exception:
            page_content = ""
        candidates = _M3U8_RE.findall(page_content)
        for url in candidates:
            if await self._verify_playable(page, url):
                return self._build_result(url)

        # Nothing genuinely playable found — honest None, not a guess.
        return None

    async def _verify_playable(self, page: Page, url: str) -> bool:
        """Issue a HEAD-ish check via page.request (shares the page's
        cookies/session) rather than a fresh httpx client, since these
        tokens are often session-bound to whatever cookies the JS
        challenge set."""
        try:
            resp = await page.request.get(url, timeout=5000)
            ct = (resp.headers.get("content-type") or "").lower()
            return resp.status == 200 and (
                "video/" in ct or "mpegurl" in ct or "octet-stream" in ct
            )
        except Exception:
            return False

    def _build_result(self, url: str) -> ResolveResult:
        expires_at = self._expiry_from_url(url) or self.default_expiry()
        return ResolveResult(
            url=url,
            is_hls=".m3u8" in url,
            referer="https://vidsrc.to/",
            origin="https://vidsrc.to",
            headers={
                "Referer": "https://vidsrc.to/",
                "Origin": "https://vidsrc.to",
            },
            source_id=self.source_id,
            source_name=self.source_name,
            expires_at=expires_at,
        )

    def _expiry_from_url(self, url: str) -> Optional[datetime]:
        match = _TOKEN_RE.search(url)
        if not match:
            return None
        return self.parse_jwt_expiry(match.group(1))
