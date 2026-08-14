"""Per-stream accounting: TTFT, throughput, reconciliation, death verdicts.

A state machine fed timestamped events by the relay — it holds no clock and
does no I/O, so every rule is exercised offline. TTFT is stamped on the first
CONTENT token (the relay decides what counts as content; headers and role
deltas must not be fed here). At stream end the gateway's own count is
reconciled against the provider's usage fields — drift beyond tolerance means
the accounting is wrong, and that is a test failure, not a rounding note.

A dead stream gets a verdict, not a retry: tokens already reached the client,
so a replay could diverge and splice two answers. Only a death at zero tokens
emitted — observably identical to an init failure — is eligible for one
silent restart elsewhere."""

from dataclasses import dataclass

DRIFT_TOLERANCE = 0.02


@dataclass(frozen=True)
class Reconciliation:
    counted: int
    reported: int
    drift: float
    within_tolerance: bool


@dataclass(frozen=True)
class StreamDeath:
    backend: str
    reason: str
    tokens_emitted: int
    restartable: bool


@dataclass
class StreamAccount:
    backend: str
    started_at: float
    first_token_at: float | None = None
    ended_at: float | None = None
    content_tokens: int = 0

    def content(self, at: float, tokens: int = 1) -> None:
        if self.first_token_at is None:
            self.first_token_at = at
        self.content_tokens += tokens

    @property
    def ttft(self) -> float | None:
        if self.first_token_at is None:
            return None
        return self.first_token_at - self.started_at

    @property
    def tokens_per_second(self) -> float | None:
        """Generation rate over the emission window, excluding the TTFT wait —
        a stream can start fast and starve, so the two are separate numbers."""
        if self.first_token_at is None or self.ended_at is None:
            return None
        window = self.ended_at - self.first_token_at
        if window <= 0:
            return None
        return self.content_tokens / window

    def finish(self, at: float, reported_output_tokens: int) -> Reconciliation:
        self.ended_at = at
        drift = abs(self.content_tokens - reported_output_tokens) / max(reported_output_tokens, 1)
        return Reconciliation(
            counted=self.content_tokens,
            reported=reported_output_tokens,
            drift=drift,
            within_tolerance=drift <= DRIFT_TOLERANCE,
        )

    def died(self, at: float, reason: str) -> StreamDeath:
        self.ended_at = at
        return StreamDeath(
            backend=self.backend,
            reason=reason,
            tokens_emitted=self.content_tokens,
            restartable=self.content_tokens == 0,
        )
