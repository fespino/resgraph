"""Dispatch policy: admission, health, and backend choice, pure and offline.

The async server owns the sockets and the probe schedule; everything it must
decide — admit or 429, which backend serves an eligible request, what a probe
result does to health — lives here as plain state machines so the whole policy
is exercised without a server. Health is a generation probe, not a TCP ping: a
model server can 200 its health endpoint while generation has collapsed.

Speed is read as rolling percentiles (measured TTFT here is bimodal;
a mean describes no request that happened), and a short error window
deprioritizes a flaky backend without eliminating it."""

import time
from collections import deque
from dataclasses import dataclass, field
from math import ceil
from typing import Literal

HealthState = Literal["healthy", "degraded", "down"]
ProbeResult = Literal["ok", "slow", "fail"]

READMISSION_PROBES = 3
STATS_WINDOW_S = 300.0
ERROR_WINDOW_S = 30.0
SOFT_ERROR_RATE = 0.5
SOFT_ERROR_MIN_EVENTS = 3
PERCENTILES = (50, 75, 90, 99)


@dataclass
class Health:
    """Probe-driven health. Recovery from down is gradual: one passing probe
    under no load proves little, so readmission takes consecutive passes."""

    state: HealthState = "healthy"
    _streak: int = 0

    def observe(self, result: ProbeResult) -> HealthState:
        if result == "fail":
            self.state = "down"
            self._streak = 0
        elif self.state == "down":
            self._streak += 1
            if self._streak >= READMISSION_PROBES:
                self.state = "healthy" if result == "ok" else "degraded"
                self._streak = 0
        else:
            self.state = "healthy" if result == "ok" else "degraded"
        return self.state


@dataclass
class RollingWindow:
    """Timestamped samples over a sliding window, read as percentiles
    (nearest-rank). Empty window reads None — an unmeasured backend is a
    fact, not a zero."""

    window_s: float = STATS_WINDOW_S
    samples: deque[tuple[float, float]] = field(default_factory=deque)

    def _evict(self, now: float) -> None:
        while self.samples and now - self.samples[0][0] > self.window_s:
            self.samples.popleft()

    def observe(self, value: float, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.samples.append((now, value))
        self._evict(now)

    def percentile(self, p: int, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        self._evict(now)
        if not self.samples:
            return None
        ordered = sorted(v for _, v in self.samples)
        rank = max(1, ceil(p / 100 * len(ordered)))
        return ordered[rank - 1]

    def snapshot(self, now: float | None = None) -> dict[str, float] | None:
        now = time.monotonic() if now is None else now
        self._evict(now)
        if not self.samples:
            return None
        return {f"p{p}": self.percentile(p, now) or 0.0 for p in PERCENTILES}


@dataclass
class ErrorWindow:
    """Outcome events over a short window; the rate DEPRIORITIZES, never
    eliminates — the hard down/readmit machine owns elimination."""

    window_s: float = ERROR_WINDOW_S
    events: deque[tuple[float, bool]] = field(default_factory=deque)

    def observe(self, ok: bool, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.events.append((now, ok))
        while self.events and now - self.events[0][0] > self.window_s:
            self.events.popleft()

    def soft_deprioritized(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self.events and now - self.events[0][0] > self.window_s:
            self.events.popleft()
        if len(self.events) < SOFT_ERROR_MIN_EVENTS:
            return False
        failures = sum(1 for _, ok in self.events if not ok)
        return failures / len(self.events) >= SOFT_ERROR_RATE


class QueueFull(Exception):
    def __init__(self, backend: str, retry_after_s: int) -> None:
        self.backend = backend
        self.retry_after_s = retry_after_s
        super().__init__(f"{backend} queue full; retry after {retry_after_s}s")


@dataclass
class Backend:
    """One backend's dispatch state: bounded queue, rolling stats, health.

    The queue is bounded because a request's cost is unknown at admission
    (output length is not in the request) — a full queue rejects explicitly
    rather than growing."""

    name: str
    concurrency: int
    queue_max: int
    ttft: RollingWindow = field(default_factory=RollingWindow)
    tps: RollingWindow = field(default_factory=RollingWindow)
    errors: ErrorWindow = field(default_factory=ErrorWindow)
    health: Health = field(default_factory=Health)
    in_flight: int = 0

    def admit(self) -> None:
        """Admit one request or raise QueueFull with a drain-time estimate."""
        if self.in_flight >= self.queue_max:
            drain = (self.ttft.percentile(50) or 1.0) * self.in_flight / self.concurrency
            raise QueueFull(self.name, max(1, ceil(drain)))
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)

    def sort_key(self, now: float | None = None) -> tuple[bool, bool, bool, float]:
        """Selection order: recent errors deprioritize first (a backend
        failing half its last-30s attempts ranks below a quiet degraded
        one), then health, then TTFT p50 (unmeasured sorts first — a fresh
        backend deserves traffic to be measured at all)."""
        p50 = self.ttft.percentile(50, now)
        return (
            self.errors.soft_deprioritized(now),
            self.health.state != "healthy",
            p50 is not None,
            p50 or 0.0,
        )


def choose(backends: list[Backend], now: float | None = None) -> Backend | None:
    """Pick among eligible backends: never a down one, then by
    ``Backend.sort_key``. None means every backend is down; the caller
    answers 503, it does not queue blind."""
    eligible = [b for b in backends if b.health.state != "down"]
    if not eligible:
        return None
    return min(eligible, key=lambda b: b.sort_key(now))
