"""The fall-forward spend budget: the failure path's money, bounded.

The walk buys availability with money in one direction only (local-down
makes every free call a paid one), so fallback-served paid traffic gets
a per-UTC-day cap: warn once at 90%, then paid candidates leave the
walk and the refusal is explicit and distinct. Routed paid traffic, pins,
and unpriced backends are out of scope by construction."""

import logging
from pathlib import Path

from resgraph.evals.breaker import WARN_FRACTION, DailyLedger

log = logging.getLogger("resgraph.gateway")

DEFAULT_LEDGER = Path("data") / "gateway-fallback-spend.json"


class FallForwardBudget:
    def __init__(self, *, cap_usd: float, ledger: Path = DEFAULT_LEDGER) -> None:
        self.cap_usd = cap_usd
        self._ledger = DailyLedger(ledger)

    def spent_today(self) -> float:
        return self._ledger.spent_today()

    def allows(self) -> bool:
        return self.spent_today() < self.cap_usd

    def charge(self, cost_usd: float) -> None:
        if self._ledger.add(cost_usd, warn_at_usd=WARN_FRACTION * self.cap_usd):
            log.warning(
                "[gateway:fallback-budget] $%.2f of $%.2f daily cap (90%% crossed)",
                self.spent_today(),
                self.cap_usd,
            )
