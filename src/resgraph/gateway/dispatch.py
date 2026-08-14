"""Dispatch policy: admission, health, and backend choice, pure and offline.

The async server owns the sockets and the probe schedule; everything it must
decide — admit or 429, which backend serves an eligible request, what a probe
result does to health — lives here as plain state machines so the whole policy
is exercised without a server. Health is a generation probe, not a TCP ping: a
model server can 200 its health endpoint while generation has collapsed."""

from dataclasses import dataclass, field
from math import ceil
from typing import Literal

HealthState = Literal["healthy", "degraded", "down"]
ProbeResult = Literal["ok", "slow", "fail"]

READMISSION_PROBES = 3


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
class Ewma:
    """Exponentially weighted moving average; None until the first sample."""

    alpha: float = 0.3
    value: float | None = None

    def update(self, sample: float) -> float:
        self.value = (
            sample if self.value is None else (self.alpha * sample + (1 - self.alpha) * self.value)
        )
        return self.value


class QueueFull(Exception):
    def __init__(self, backend: str, retry_after_s: int) -> None:
        self.backend = backend
        self.retry_after_s = retry_after_s
        super().__init__(f"{backend} queue full; retry after {retry_after_s}s")


@dataclass
class Backend:
    """One backend's dispatch state: bounded queue, TTFT average, health.

    The queue is bounded because a request's cost is unknown at admission
    (output length is not in the request) — a full queue rejects explicitly
    rather than growing."""

    name: str
    concurrency: int
    queue_max: int
    ttft_ewma: Ewma = field(default_factory=Ewma)
    health: Health = field(default_factory=Health)
    in_flight: int = 0

    def admit(self) -> None:
        """Admit one request or raise QueueFull with a drain-time estimate."""
        if self.in_flight >= self.queue_max:
            drain = (self.ttft_ewma.value or 1.0) * self.in_flight / self.concurrency
            raise QueueFull(self.name, max(1, ceil(drain)))
        self.in_flight += 1

    def release(self) -> None:
        self.in_flight = max(0, self.in_flight - 1)


def choose(backends: list[Backend]) -> Backend | None:
    """Pick among eligible backends: never a down one, healthy before
    degraded, then lowest observed TTFT (unmeasured sorts first — a fresh
    backend deserves traffic to be measured at all). None means every
    backend is down; the caller answers 503, it does not queue blind."""
    eligible = [b for b in backends if b.health.state != "down"]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda b: (
            b.health.state != "healthy",
            b.ttft_ewma.value is not None,
            b.ttft_ewma.value or 0.0,
        ),
    )
