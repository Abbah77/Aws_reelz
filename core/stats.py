"""
Server-side per-source stats tracker.

Deliberately mirrors the Android app's SourceStatsTracker (same success
rate + speed scoring idea) — but now this data is shared across ALL
users instead of siloed per-device, which was the exact limitation
flagged when we built the client-only version. This is the piece that
lets the whole fleet benefit from one bad source being detected once,
not per-install.

In-memory by default (fast, resets on process restart — fine for a
single-instance EC2 box). Swap `InMemoryStats` for `PostgresStats` once
you want it to survive restarts / be shared across multiple Lambda
containers — the interface is identical either way.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class SourceStats:
    success_count: int = 0
    total_count: int = 0
    total_ms: int = 0

    @property
    def success_rate(self) -> float:
        return 0.5 if self.total_count == 0 else self.success_count / self.total_count

    @property
    def avg_ms(self) -> float:
        return 15_000.0 if self.success_count == 0 else self.total_ms / self.success_count

    @property
    def score(self) -> float:
        return self.success_rate * (1.0 / (self.avg_ms / 1000.0 + 1.0))


class StatsTracker:
    """In-memory implementation. Thread/async-safe via a plain lock —
    contention here is negligible compared to actual network/browser
    work, so a simple lock is the right level of complexity, not
    something fancier."""

    MIN_SAMPLE_SIZE = 4
    SKIP_SUCCESS_RATE = 0.15
    REPROBE_EVERY = 8

    def __init__(self):
        self._lock = threading.Lock()
        self._stats: dict[str, SourceStats] = {}
        self._probe_counts: dict[str, int] = {}

    def get(self, source_id: str) -> SourceStats:
        with self._lock:
            return self._stats.get(source_id, SourceStats())

    def record_success(self, source_id: str, elapsed_ms: int) -> None:
        with self._lock:
            s = self._stats.get(source_id, SourceStats())
            self._stats[source_id] = SourceStats(
                success_count=s.success_count + 1,
                total_count=s.total_count + 1,
                total_ms=s.total_ms + elapsed_ms,
            )

    def record_failure(self, source_id: str) -> None:
        with self._lock:
            s = self._stats.get(source_id, SourceStats())
            self._stats[source_id] = SourceStats(
                success_count=s.success_count,
                total_count=s.total_count + 1,
                total_ms=s.total_ms,
            )

    def should_skip(self, source_id: str) -> bool:
        with self._lock:
            s = self._stats.get(source_id, SourceStats())
            if s.total_count < self.MIN_SAMPLE_SIZE:
                return False
            if s.success_rate > self.SKIP_SUCCESS_RATE:
                return False
            # BUGFIX (caught by tests/test_core.py, not by inspection):
            # counting attempts from 0 meant `0 % REPROBE_EVERY == 0`,
            # so the FIRST check after a source crosses the bad-streak
            # threshold always fell through to "let it through" instead
            # of actually skipping — a source never got skipped on its
            # first real opportunity. Counting from 1 fixes this: the
            # 1st, 2nd, ... (REPROBE_EVERY-1)th checks skip normally,
            # and every REPROBE_EVERYth check is the deliberate re-probe.
            attempts = self._probe_counts.get(source_id, 0) + 1
            self._probe_counts[source_id] = attempts
            return (attempts % self.REPROBE_EVERY) != 0

    def snapshot(self) -> dict[str, SourceStats]:
        """For a /health or /stats debug endpoint."""
        with self._lock:
            return dict(self._stats)
