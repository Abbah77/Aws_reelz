"""
Base interface every site adapter implements.

WHY THIS EXISTS (read before adding a new adapter):
There is no such thing as a truly generic, site-agnostic stream resolver
for this class of embed site — every site hides the real URL differently
(plain XHR, blob URL, tokenized HTML-wrapped "segments" like vidsrc.to's
current scheme, WebSocket push, etc). Claiming otherwise would be
dishonest. What IS reusable is everything AROUND the site-specific part:
browser lifecycle, network interception plumbing, timeout handling,
caching, expiry parsing, stats. This base class owns all of that; a
concrete adapter only has to answer three questions:
  1. What URL do I load for this title?
  2. Given the page + intercepted network traffic, what's the real
     stream URL?
  3. When does that URL expire?

Adding support for a new site = writing one small adapter class, not
touching the engine, cache, or API.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from playwright.async_api import Page

from core.models import MediaType, ResolveResult


@dataclass
class InterceptedRequest:
    url: str
    resource_type: str        # 'xhr', 'fetch', 'media', 'document', ...
    method: str
    response_status: Optional[int] = None
    response_content_type: Optional[str] = None


class SiteAdapter(abc.ABC):
    """Subclass this once per site. See adapters/ for real examples."""

    # Unique, stable id — used as the Postgres/cache key prefix and in
    # stats tracking. Change this and you orphan existing cache rows.
    source_id: str = "override_me"
    source_name: str = "Override Me"

    # Sites that only expose the real URL via document.write, blob URLs,
    # or fully client-rendered players need the full page load + JS
    # execution. Sites that expose it in a plain fetch/XHR response body
    # can often be resolved with a lighter, faster path — see
    # `prefers_light_resolution`.
    requires_full_render: bool = True

    # If True, the engine will first try a plain httpx GET + adapter's
    # `try_light_resolve` before spinning up Playwright at all. Cheaper
    # and faster when it works. Most tokenized/JS-gated sites will
    # return False here because there's nothing to find without JS.
    prefers_light_resolution: bool = False

    # Hard per-adapter timeout for the Playwright resolution path.
    # Kept adapter-level (not global) because some sites are just
    # slower than others by nature of their challenge/redirect chains.
    render_timeout_seconds: int = 20

    @abc.abstractmethod
    def build_embed_url(
        self,
        tmdb_id: int,
        media_type: MediaType,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> str:
        """Return the embed page URL for this title."""
        raise NotImplementedError

    async def try_light_resolve(self, embed_url: str) -> Optional[ResolveResult]:
        """
        Optional fast path: plain HTTP GET + regex/parse, no browser.
        Only called if `prefers_light_resolution` is True. Return None
        to signal 'fall through to full render' rather than raising —
        this path failing is expected and normal, not an error.
        """
        return None

    @abc.abstractmethod
    async def extract_from_page(
        self,
        page: Page,
        intercepted: list[InterceptedRequest],
        embed_url: str,
    ) -> Optional[ResolveResult]:
        """
        Called after the embed page has loaded (and after
        `on_page_ready` hooks, if any) with the full list of network
        requests/responses the engine observed. Return the resolved
        ResolveResult, or None if nothing was found (NOT an exception —
        "found nothing" is a normal, expected outcome, not a bug).
        """
        raise NotImplementedError

    async def before_navigate(self, page: Page) -> None:
        """
        Optional hook: inject scripts, set extra headers, cookies, etc.
        BEFORE navigation starts. Default no-op. Override for sites that
        need something set up before the page even begins loading (e.g.
        a document-start interceptor script, like the Android app's
        WebViewScanner already does for on-device resolution).
        """
        return None

    def default_expiry(self) -> datetime:
        """
        Fallback expiry when the site doesn't tell us one (no token exp
        claim, no explicit TTL in the URL/response). Deliberately
        conservative — better to re-resolve a bit early than serve a
        dead link. Override per-adapter if a site's real-world token
        lifetime is known to be longer or shorter than this default.
        """
        return datetime.now(timezone.utc) + timedelta(minutes=20)

    def parse_jwt_expiry(self, token: str) -> Optional[datetime]:
        """
        Convenience helper: many of these sites embed a JWT in the query
        string with a genuine `exp` claim (this is exactly what we saw
        in the vidsrc.to DevTools captures — a `token=eyJ...` JWT).
        Adapters can call this to get a real expiry instead of guessing.
        Returns None if the token isn't a parseable JWT or has no `exp`.
        """
        import base64
        import json

        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            payload_b64 = parts[1]
            padding = "=" * (-len(payload_b64) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
            exp = payload.get("exp")
            if exp is None:
                return None
            return datetime.fromtimestamp(exp, tz=timezone.utc)
        except Exception:
            return None
