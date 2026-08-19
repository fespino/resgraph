"""Billing: named accounts (keys in the environment), prepaid wallets
decremented from the meter, and a usage surface over the meter's own
records."""

import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

ACCOUNTS_PATH = Path("evals/gateway-accounts.yaml")
BALANCES_PATH = Path("data") / "gateway-balances.json"
USAGE_PATH = Path("data") / "gateway-usage.jsonl"
MGMT_KEY_ENV = "RESGRAPH_GATEWAY_MGMT_KEY"


def load_accounts(text: str) -> dict[str, dict[str, Any]]:
    """Accounts from the committed file: each names the env var holding
    its key (secrets never live in the file) and its prepaid grant."""
    doc = yaml.safe_load(text) or {}
    out: dict[str, dict[str, Any]] = {}
    for name, entry in (doc.get("accounts") or {}).items():
        entry = entry or {}
        if "key" in entry:
            raise SystemExit(
                f"account {name!r} carries an inline key; keep the secret in the "
                "environment and name it with key_env instead"
            )
        if not entry.get("key_env"):
            raise SystemExit(f"account {name!r} needs key_env (the env var holding its key)")
        out[name] = {
            "key_env": entry["key_env"],
            "granted_usd": float(entry.get("granted_usd", 0.0)),
        }
    return out


def resolve_account(accounts: dict[str, dict[str, Any]], presented: str) -> str | None:
    """The account whose key matches, or None; an unset env var never
    authenticates."""
    for name, entry in accounts.items():
        expected = os.environ.get(entry["key_env"])
        if expected and hmac.compare_digest(expected, presented):
            return name
    return None


@dataclass(frozen=True)
class BalanceState:
    granted_usd: float
    spent_usd: float

    @property
    def remaining_usd(self) -> float:
        return self.granted_usd - self.spent_usd


class Wallet:
    """Prepaid balances: the grant lives in the accounts file, the
    cumulative spend here."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or BALANCES_PATH

    def _spent(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        return {k: float(v) for k, v in json.loads(self.path.read_text()).items()}

    def state(self, account: str, granted_usd: float) -> BalanceState:
        return BalanceState(granted_usd=granted_usd, spent_usd=self._spent().get(account, 0.0))

    def charge(self, account: str, cost_usd: float) -> None:
        if cost_usd <= 0:
            return
        spent = self._spent()
        spent[account] = spent.get(account, 0.0) + cost_usd
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(spent, sort_keys=True))


class UsageLedger:
    """The meter's own records: one appended row per served response.
    The usage surface aggregates these and nothing else."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or USAGE_PATH

    def record(
        self,
        *,
        account: str | None,
        endpoint: str,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        cached: bool = False,
    ) -> None:
        row = {
            "day": datetime.now(UTC).date().isoformat(),
            "account": account or "anonymous",
            "endpoint": endpoint,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost_usd, 8),
            "cached": cached,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(row, sort_keys=True) + "\n")

    def aggregate(self, account: str | None = None) -> list[dict[str, Any]]:
        """Per (day, account, endpoint): request count, tokens, cost —
        sorted newest day first."""
        if not self.path.exists():
            return []
        buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
        for line in self.path.read_text().splitlines():
            row = json.loads(line)
            if account is not None and row["account"] != account:
                continue
            key = (row["day"], row["account"], row["endpoint"])
            b = buckets.setdefault(
                key,
                {
                    "day": row["day"],
                    "account": row["account"],
                    "endpoint": row["endpoint"],
                    "model": row["model"],
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                },
            )
            b["requests"] += 1
            b["input_tokens"] += row["input_tokens"]
            b["output_tokens"] += row["output_tokens"]
            b["cost_usd"] = round(b["cost_usd"] + row["cost_usd"], 8)
        return sorted(buckets.values(), key=lambda b: (b["day"], b["account"]), reverse=True)


def mgmt_authorized(presented: str | None) -> bool:
    """Open when no management key is configured; strict when one is."""
    expected = os.environ.get(MGMT_KEY_ENV)
    if not expected:
        return True
    return presented is not None and hmac.compare_digest(expected, presented)
