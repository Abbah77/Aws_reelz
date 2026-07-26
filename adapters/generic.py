"""
Generic heuristic adapter — the honest answer to "make it work with any
source."

WHAT THIS IS: a fallback adapter that can be pointed at ANY embed URL
without writing site-specific code first. It uses the same
"never trust a URL's file extension, verify the actual response
content-type" technique proven against vidsrc.to's tokenized
page-N.html scheme, generalized to look at every intercepted network
response rather than one site's specific quirks.

WHAT THIS IS NOT, AND WILL NEVER BE: a guarantee. This will work
reasonably well against sites that expose the real stream URL somewhere
in their normal page-load network traffic without additional
obfuscation beyond what a real browser executing real JS already
defeats (redirects, simple challenges, standard XHR/fetch patterns). It
will NOT reliably defeat:
  - Sites that require solving an actual CAPTCHA
  - Sites using canvas/WebGL fingerprinting to distinguish automation
    from a real user, and serving different content to each
  - Sites that encrypt the stream URL client-side with a key that
    itself requires solving a challenge Playwright's plain automation
    doesn't satisfy (some sites specifically detect `navigator.webdriver`
    or Playwright's other tells and branch behavior accordingly)
  - Sites where the real URL only appears after a genuine user gesture
    (a real click on a real "Play" button, sometimes with mouse-movement
    heuristics) that this adapter doesn't attempt to simulate

For any of the above, or simply for better reliability/speed on a site
you use a lot, write a dedicated adapter (see vidsrc_to.py, the ONE
adapter in this project actually verified against real captured
traffic). This generic adapter is meant as a bridge — try it on a new
source immediately, see what it catches, then graduate to a dedicated
adapter once you understand that source's specific pattern, the same
way vidsrc_to.py was built from real DevTools observation rather than
guesswork.

USAGE: this is not auto-registered in adapters/registry.py (a truly
unknown site has no `source_id`/URL-building convention to register
under). Use it directly via GenericAdapter(embed_url=..., source_id=...)
when you want to try an ad-hoc URL, e.g. from an admin tool or a
one-off exploratory script — see the bottom of this file for an example.
"""
from __future__ import annotations

import re
from typing import Optional

from playwright.async_api import Page

from core.base_adapter import InterceptedRequest, SiteAdapter
from core.models import MediaType, ResolveResult

_M3U8_RE = re.compile(r"https?://[^\s\"'<>]+\.m3u8[^\s\"'<>]*")
_MP4_RE = re.compile(r"https?://[^\s\"'<>]+\.mp4[^\s\"'<>]*")

# Content-types that genuinely indicate playable media, as opposed to a
# URL that merely LOOKS like a stream URL by file extension. This is the
# exact check that would have caught vidsrc.to's HTML-wrapped fake
# segments if we'd been relying on extension-matching alone.
_MEDIA_CONTENT_TYPES = (
    "video/",
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
)
# Some CDNs mislabel real binary segments as this — treated as a weaker,
# secondary signal, only trusted in combination with a matching URL
# extension (see _looks_genuinely_playable below).
_AMBIGUOUS_CONTENT_TYPES = ("application/octet-stream",)


class GenericAdapter(SiteAdapter):
    """
    Constructed per-use with an explicit embed URL, rather than a fixed
    URL-building pattern — this is what makes it usable against a
    source with no dedicated adapter yet.
    """

    def __init__(
        self,
        embed_url: str,
        source_id: str = "generic_unknown",
        source_name: str = "Unknown Source",
        referer: str = "",
        origin: str = "",
        render_timeout_seconds: int = 20,
    ):
        self.source_id = source_id
        self.source_name = source_name
        self._embed_url = embed_url
        self._referer = referer
        self._origin = origin
        self.render_timeout_seconds = render_timeout_seconds
        self.requires_full_render = True
        self.prefers_light_resolution = False

    def build_embed_url(
        self,
        tmdb_id: int,
        media_type: MediaType,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> str:
        # Ignores tmdb_id/media_type deliberately — this adapter is
        # constructed with a concrete URL already known (e.g. from an
        # admin tool pasting in a specific embed link to test), not from
        # a reusable per-title pattern the way registered adapters are.
        return self._embed_url

    async def extract_from_page(
        self,
        page: Page,
        intercepted: list[InterceptedRequest],
        embed_url: str,
    ) -> Optional[ResolveResult]:
        # Pass 1: trust only genuinely-verified media responses.
        best: Optional[str] = None
        for req in intercepted:
            if req.response_status != 200:
                continue
            if self._looks_genuinely_playable(req):
                best = req.url
                break

        # Pass 2: nothing verified via network interception — fall back
        # to scanning rendered page content for an m3u8/mp4 URL, but
        # verify it with a real request through the page's own session
        # (cookies/headers intact) before trusting it, same pattern as
        # vidsrc_to.py's fallback path.
        if best is None:
            try:
                content = await page.content()
            except Exception:
                content = ""
            candidates = _M3U8_RE.findall(content) + _MP4_RE.findall(content)
            for url in candidates:
                if await self._verify_via_page_request(page, url):
                    best = url
                    break

        if best is None:
            return None

        return ResolveResult(
            url=best,
            is_hls=".m3u8" in best,
            referer=self._referer,
            origin=self._origin,
            headers={
                k: v for k, v in {
                    "Referer": self._referer,
                    "Origin": self._origin,
                }.items() if v
            },
            source_id=self.source_id,
            source_name=self.source_name,
            expires_at=self.default_expiry(),  # no known token format to parse a real expiry from
        )

    def _looks_genuinely_playable(self, req: InterceptedRequest) -> bool:
        url_looks_like_stream = (
            ".m3u8" in req.url or ".mp4" in req.url or ".ts" in req.url
        )
        if not url_looks_like_stream:
            return False
        ct = (req.response_content_type or "").lower()
        if any(t in ct for t in _MEDIA_CONTENT_TYPES):
            return True
        if any(t in ct for t in _AMBIGUOUS_CONTENT_TYPES):
            return True  # secondary signal, already gated by URL shape above
        return False

    async def _verify_via_page_request(self, page: Page, url: str) -> bool:
        try:
            resp = await page.request.get(url, timeout=5000)
            ct = (resp.headers.get("content-type") or "").lower()
            return resp.status == 200 and (
                any(t in ct for t in _MEDIA_CONTENT_TYPES)
                or any(t in ct for t in _AMBIGUOUS_CONTENT_TYPES)
            )
        except Exception:
            return False


# Example ad-hoc usage (not executed on import):
#
#   from core.engine import ResolverEngine
#   from core.stats import StatsTracker
#   from adapters.generic import GenericAdapter
#
#   engine = ResolverEngine(stats=StatsTracker())
#   await engine.start()
#   adapter = GenericAdapter(
#       embed_url="https://some-new-site.example/embed/12345",
#       source_id="some_new_site",
#       referer="https://some-new-site.example/",
#   )
#   result = await engine.resolve(adapter, tmdb_id=0, media_type=MediaType.MOVIE)
#   print(result.url)
#
# If this works reliably for a source you'll use repeatedly, graduate
# it to a real adapter (adapters/your_site.py) with a proper
# build_embed_url pattern and register it in adapters/registry.py —
# that's the difference between "worked once in a test" and "part of
# the production fallback chain."
