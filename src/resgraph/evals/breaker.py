"""D29a — the judge's daily spend circuit breaker.

Eval infrastructure has a bill, and a retry loop shows up on the
invoice before it shows up anywhere else unless something counts.
The breaker keeps a per-UTC-day ledger of estimated judge spend,
warns once at 90% of the cap, and trips loudly at the cap — a
SystemExit, not a skipped dimension: a run silently missing its
judge rows would poison comparisons against fully-judged baselines.

Laptop-honest atomicity: one process, a JSON file rewritten per
charge. The day a second concurrent runner exists, this moves to a
real atomic counter alongside D27's served-store reversal.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path("data") / "judge-spend.json"
WARN_FRACTION = 0.9


class JudgeSpendBreaker:
    def __init__(
        self,
        *,
        cap_usd: float,
        model: str,
        prices_per_mtok: dict[str, tuple[float, float]],
        ledger: Path = DEFAULT_LEDGER,
    ) -> None:
        if model not in prices_per_mtok:
            raise SystemExit(f"judge breaker has no pricing for {model}; extend PRICES_PER_MTOK")
        self.cap_usd = cap_usd
        self.model = model
        self._input_rate, self._output_rate = prices_per_mtok[model]
        self.ledger = ledger

    def _load(self) -> dict[str, Any]:
        today = datetime.now(UTC).date().isoformat()
        try:
            state = json.loads(self.ledger.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            state = {}
        if state.get("date") != today:
            state = {"date": today, "spent_usd": 0.0, "warned": False}
        return state

    def spent_today(self) -> float:
        return float(self._load()["spent_usd"])

    def check(self) -> None:
        """Call before a judge call: trips if today's spend is at cap."""
        spent = self.spent_today()
        if spent >= self.cap_usd:
            raise SystemExit(
                f"judge daily spend breaker tripped: ${spent:.2f} of "
                f"${self.cap_usd:.2f} cap spent today. Completed rows are "
                "banked; resume tomorrow or raise --judge-daily-cap."
            )

    def charge(self, usage: Any) -> None:
        """Call after a judge call with its response usage."""
        cost = (
            (getattr(usage, "input_tokens", 0) or 0) * self._input_rate
            + (getattr(usage, "output_tokens", 0) or 0) * self._output_rate
        ) / 1_000_000
        state = self._load()
        state["spent_usd"] = float(state["spent_usd"]) + cost
        if not state["warned"] and state["spent_usd"] >= WARN_FRACTION * self.cap_usd:
            state["warned"] = True
            print(
                f"judge spend warning: ${state['spent_usd']:.2f} of "
                f"${self.cap_usd:.2f} daily cap (90% threshold crossed)"
            )
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text(json.dumps(state))
