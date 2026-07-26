"""
Core resolution engine. This is the reusable part described in
base_adapter.py's module docstring — adapters plug into this, they don't
reimplement it.

Responsibilities:
  - Own a shared Playwright browser instance (NOT one per request — that
    is what actually kills performance/cost on Lambda/EC2, launching
    Chromium fresh per request is the single most expensive mistake
    possible here)
  - Run the light-resolution path first when an adapter opts into it
  - Run the full-render path with network interception otherwise
  - Enforce per-adapter timeouts AND a hard global wall-clock ceiling
  - Report outcomes to the stats tracker (mirrors what the Android app's
    SourceStatsTracker already does, same idea, server-side)

LAMBDA COST MODEL — READ THIS, IT IS NOT AUTOMATIC:
Lambda bills duration × memory. A Chromium resolve is the most expensive
kind of Lambda invocation you can run — long-ish duration AND high
memory, the two dials that directly multiply cost. "Pay as you go" does
NOT mean cheap here; it means cost scales linearly with how often this
path actually runs. The two things that keep this under control are:
  1. The Postgres cache (core/cache.py) — most requests should be cache
     hits that never touch this engine at all.
  2. The request coalescer (core/coalescer.py) — prevents N simultaneous
     users from each triggering their own Chromium launch for the same
     title.
This engine adds a THIRD control: a hard `GLOBAL_RESOLVE_CEILING_SECONDS`
wall-clock cutoff independent of any single adapter's own timeout, so a
pathological page (infinite redirect loop, hung challenge script) can't
silently run up Lambda duration billing past a sane ceiling even if an
adapter's own timeout logic has a bug.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx
from playwright.async_api import Browser, Page, Playwright, async_playwright

from core.base_adapter import InterceptedRequest, SiteAdapter
from core.models import MediaType, ResolveError, ResolveResult
from core.stats import StatsTracker

logger = logging.getLogger("resolver.engine")

# Resource types we never need for a stream URL and which cost real
# bandwidth/time to load — blocking these mirrors exactly what the
# Android app's WebViewScanner already does on-device, same reasoning:
# images/fonts/css/ads are pure waste for THIS specific task.
_BLOCKED_RESOURCE_TYPES = {"image", "font", "stylesheet", "media"}
_BLOCKED_URL_SUBSTRINGS = (
    "google-analytics", "googletagmanager", "doubleclick", "facebook.com/tr",
    "googlesyndication", "adservice", "/ads/", "adsbygoogle",
)

# Hard wall-clock ceiling for a SINGLE adapter's full-render attempt,
# independent of that adapter's own render_timeout_seconds. This exists
# specifically for Lambda cost protection: if an adapter's own timeout
# logic ever has a bug (or a site's challenge script hangs in a way that
# doesn't respect page.goto's timeout), this is the backstop that
# guarantees no single resolve attempt can run past this ceiling and
# rack up Lambda duration billing indefinitely. Deliberately set higher
# than any individual adapter's render_timeout_seconds so it's a true
# backstop, not the normal path.
GLOBAL_RESOLVE_CEILING_SECONDS = 35


class ResolverEngine:
    def __init__(self, stats: StatsTracker, headless: bool = True):
        self._stats = stats
        self._headless = headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._launch_lock = asyncio.Lock()

    async def start(self) -> None:
        """Call once at process startup (or lazily on first request)."""
        async with self._launch_lock:
            if self._browser is not None:
                return
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",  # avoids /dev/shm OOM in containers
                    "--no-sandbox",             # required in most container/Lambda envs
                    "--disable-setuid-sandbox",
                ],
            )
            logger.info("Playwright browser launched")

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        self._browser = None
        self._playwright = None

    async def resolve(
        self,
        adapter: SiteAdapter,
        tmdb_id: int,
        media_type: MediaType,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> ResolveResult:
        """
        Resolve a single title against a single adapter. Raises
        ResolveError on a clean "this source doesn't have it" failure;
        lets timeouts/crashes propagate as-is so the caller (usually the
        multi-adapter fan-out in api/routes.py) can distinguish "tried
        and failed" from "broke".
        """
        embed_url = adapter.build_embed_url(tmdb_id, media_type, season, episode)
        started = time.monotonic()

        used_full_render = False
        try:
            if adapter.prefers_light_resolution:
                light = await adapter.try_light_resolve(embed_url)
                if light is not None:
                    self._stats.record_success(
                        adapter.source_id, elapsed_ms=int((time.monotonic() - started) * 1000)
                    )
                    logger.info(
                        "resolved source=%s via LIGHT path in %dms (no Chromium cost)",
                        adapter.source_id, int((time.monotonic() - started) * 1000),
                    )
                    return light
                # Falls through to full render deliberately — a light-path
                # miss is not a failure, some titles/sources genuinely
                # need JS even on sites that usually don't.

            used_full_render = True
            result = await self._resolve_full_render(adapter, embed_url)
            if result is None:
                self._stats.record_failure(adapter.source_id)
                raise ResolveError(adapter.source_id, "no stream URL found on page")

            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._stats.record_success(adapter.source_id, elapsed_ms=elapsed_ms)
            # COST VISIBILITY: every full-render (Chromium) resolve is
            # logged with its actual duration. On Lambda, this duration
            # is directly what you're billed for (× memory). Grepping
            # CloudWatch logs for "FULL_RENDER" gives you a real, honest
            # picture of what's driving your bill — not a guess.
            logger.info(
                "resolved source=%s via FULL_RENDER in %dms (Chromium cost incurred)",
                adapter.source_id, elapsed_ms,
            )
            return result

        except ResolveError:
            raise
        except Exception as exc:
            self._stats.record_failure(adapter.source_id)
            logger.warning("adapter=%s embed_url=%s failed: %s", adapter.source_id, embed_url, exc)
            raise ResolveError(adapter.source_id, f"unexpected error: {exc}") from exc

    async def _resolve_full_render(
        self, adapter: SiteAdapter, embed_url: str
    ) -> Optional[ResolveResult]:
        if self._browser is None:
            await self.start()
        assert self._browser is not None

        try:
            return await asyncio.wait_for(
                self._resolve_full_render_inner(adapter, embed_url),
                timeout=GLOBAL_RESOLVE_CEILING_SECONDS,
            )
        except asyncio.TimeoutError:
            # This firing means an adapter's own timeout logic did NOT
            # save us in time — treat it as a clean failure (not a
            # crash) so stats/skip logic records it correctly, but log
            # loudly since this indicates a specific adapter needs its
            # render_timeout_seconds or extract_from_page loop looked at.
            logger.warning(
                "adapter=%s hit GLOBAL_RESOLVE_CEILING_SECONDS=%ds — "
                "its own timeout did not fire in time, investigate this adapter",
                adapter.source_id, GLOBAL_RESOLVE_CEILING_SECONDS,
            )
            return None

    async def _resolve_full_render_inner(
        self, adapter: SiteAdapter, embed_url: str
    ) -> Optional[ResolveResult]:
        assert self._browser is not None

        context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        intercepted: list[InterceptedRequest] = []

        async def on_route(route, request):
            if request.resource_type in _BLOCKED_RESOURCE_TYPES:
                await route.abort()
                return
            if any(s in request.url for s in _BLOCKED_URL_SUBSTRINGS):
                await route.abort()
                return
            await route.continue_()

        async def on_response(response):
            try:
                intercepted.append(InterceptedRequest(
                    url=response.url,
                    resource_type=response.request.resource_type,
                    method=response.request.method,
                    response_status=response.status,
                    response_content_type=response.headers.get("content-type"),
                ))
            except Exception:
                pass  # response can vanish mid-read on redirect chains; non-fatal

        page: Page = await context.new_page()
        await page.route("**/*", on_route)
        page.on("response", on_response)

        try:
            await adapter.before_navigate(page)
            await page.goto(
                embed_url,
                wait_until="domcontentloaded",
                timeout=adapter.render_timeout_seconds * 1000,
            )
            # Give the page's own JS a window to run whatever
            # challenge/redirect/player-init logic it needs. Polling
            # short-circuits early once the adapter finds something,
            # rather than always waiting the full timeout.
            deadline = time.monotonic() + adapter.render_timeout_seconds
            result = None
            while time.monotonic() < deadline:
                result = await adapter.extract_from_page(page, intercepted, embed_url)
                if result is not None:
                    break
                await asyncio.sleep(0.5)
            return result
        finally:
            await context.close()


async def light_http_get(url: str, headers: Optional[dict] = None, timeout: float = 8.0) -> httpx.Response:
    """
    Shared helper for adapters' try_light_resolve — plain HTTP client,
    no browser. Kept here (not duplicated per-adapter) so timeout/retry
    behavior is consistent across all light-path adapters.
    """
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
        return await client.get(url, headers=headers or {})
