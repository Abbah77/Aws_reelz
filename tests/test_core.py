"""
Minimal tests covering the pieces testable without a live browser/DB —
JWT expiry parsing, stats scoring/skip logic, and coalescing. This is
NOT full coverage (adapter network behavior needs a real or mocked
browser, deliberately out of scope for a quick unit test) but it does
verify the parts most likely to have a silent logic bug.

Run with: pytest tests/ -v
"""
import asyncio
import base64
import json
import time

import pytest

from core.base_adapter import SiteAdapter
from core.coalescer import Coalescer
from core.stats import StatsTracker


class _DummyAdapter(SiteAdapter):
    source_id = "dummy"
    source_name = "Dummy"

    def build_embed_url(self, tmdb_id, media_type, season=None, episode=None):
        return "https://example.com"

    async def extract_from_page(self, page, intercepted, embed_url):
        return None


def _make_jwt(payload: dict) -> str:
    def b64(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    header = b64({"alg": "none", "typ": "JWT"})
    body = b64(payload)
    return f"{header}.{body}.sig"


def test_parse_jwt_expiry_valid():
    adapter = _DummyAdapter()
    future_exp = int(time.time()) + 3600
    token = _make_jwt({"exp": future_exp, "sub": "test"})
    expiry = adapter.parse_jwt_expiry(token)
    assert expiry is not None
    assert abs(expiry.timestamp() - future_exp) < 2


def test_parse_jwt_expiry_no_exp_claim():
    adapter = _DummyAdapter()
    token = _make_jwt({"sub": "test"})  # no 'exp'
    assert adapter.parse_jwt_expiry(token) is None


def test_parse_jwt_expiry_garbage_input():
    adapter = _DummyAdapter()
    assert adapter.parse_jwt_expiry("not-a-jwt-at-all") is None
    assert adapter.parse_jwt_expiry("") is None


def test_stats_skip_requires_min_sample():
    stats = StatsTracker()
    # 2 failures, well below MIN_SAMPLE_SIZE=4 -> must not skip yet
    stats.record_failure("src")
    stats.record_failure("src")
    assert stats.should_skip("src") is False


def test_stats_skip_after_bad_streak():
    stats = StatsTracker()
    for _ in range(5):
        stats.record_failure("src")
    assert stats.should_skip("src") is True


def test_stats_reprobe_lets_one_through():
    stats = StatsTracker()
    for _ in range(5):
        stats.record_failure("src")
    results = [stats.should_skip("src") for _ in range(StatsTracker.REPROBE_EVERY)]
    # exactly one False (the re-probe) among REPROBE_EVERY calls
    assert results.count(False) == 1


def test_stats_score_prefers_fast_reliable_source():
    stats = StatsTracker()
    stats.record_success("fast", elapsed_ms=500)
    stats.record_success("fast", elapsed_ms=500)
    stats.record_success("slow", elapsed_ms=8000)
    stats.record_success("slow", elapsed_ms=8000)
    assert stats.get("fast").score > stats.get("slow").score


@pytest.mark.asyncio
async def test_coalescer_dedupes_concurrent_calls():
    coalescer = Coalescer()
    call_count = 0

    async def factory():
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return "result"

    results = await asyncio.gather(*[
        coalescer.run("same-key", factory) for _ in range(10)
    ])
    assert all(r == "result" for r in results)
    assert call_count == 1  # only ONE actual resolution happened


@pytest.mark.asyncio
async def test_coalescer_allows_sequential_calls_after_completion():
    coalescer = Coalescer()
    call_count = 0

    async def factory():
        nonlocal call_count
        call_count += 1
        return call_count

    r1 = await coalescer.run("key", factory)
    r2 = await coalescer.run("key", factory)
    assert r1 == 1
    assert r2 == 2  # second call, key was cleared after first completed
