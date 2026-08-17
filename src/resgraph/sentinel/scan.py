"""Score the combined corpus through layers 1+2 and report the funnel.

Pure measurement over committed data: per-rule and per-layer confusion
against ground truth, and the funnel count (what would reach the paid
layer 3). The benign false-positive rate is printed first — it is the
headline, not attack recall.
"""

from dataclasses import dataclass
from typing import Any

from . import corpus, profile, queue, rules


@dataclass(frozen=True)
class Verdict:
    run_key: str
    malicious: bool
    attack_type: str | None
    l1: rules.RuleVerdict
    l2_score: float
    l2_flagged: bool
    l2_z: dict[str, float]

    @property
    def reaches_l3(self) -> bool:
        return self.l1.flagged or self.l2_flagged


@dataclass(frozen=True)
class Report:
    verdicts: list[Verdict]

    def confusion(self, layer: str) -> dict[str, int]:
        out = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
        for v in self.verdicts:
            hit = v.l1.flagged if layer == "l1" else v.l2_flagged if layer == "l2" else v.reaches_l3
            key = ("tp" if hit else "fn") if v.malicious else ("fp" if hit else "tn")
            out[key] += 1
        return out

    def per_rule(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {name: {"tp": 0, "fp": 0} for name in rules.RULES}
        for v in self.verdicts:
            for flag in v.l1.flags:
                out[flag.rule]["tp" if v.malicious else "fp"] += 1
        return out

    def recall_by_type(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for v in self.verdicts:
            if not v.malicious or v.attack_type is None:
                continue
            caught, total = out.get(v.attack_type, (0, 0))
            out[v.attack_type] = (caught + (1 if v.reaches_l3 else 0), total + 1)
        return out


def _run_key(row: dict[str, Any]) -> str:
    sid = (row.get("sentinel") or {}).get("id")
    return sid or f"{row.get('run_id')}/{row.get('scenario_id')}/t{row.get('trial')}"


def scan_corpus() -> Report:
    benign = corpus.iter_benign()
    attacks = corpus.load_attacks()
    baseline = profile.fit(benign)
    exclusions = queue.load_exclusions()
    verdicts = [
        Verdict(
            run_key=_run_key(row),
            malicious="sentinel" in row,
            attack_type=(row.get("sentinel") or {}).get("attack_type"),
            l1=_excluded(rules.scan_rules(row), _run_key(row), exclusions),
            l2_score=(scored := baseline.score(row))[0],
            l2_flagged=baseline.flagged(row),
            l2_z=scored[1],
        )
        for row in [*benign, *attacks]
    ]
    return Report(verdicts=verdicts)


def _excluded(
    verdict: rules.RuleVerdict, run_key: str, exclusions: set[tuple[str, str]]
) -> rules.RuleVerdict:
    """A scoped exclusion suppresses one rule for one reviewed run —
    the rule stays live everywhere else (never a rule disable)."""
    if not exclusions:
        return verdict
    kept = tuple(f for f in verdict.flags if (f.rule, run_key) not in exclusions)
    return verdict if len(kept) == len(verdict.flags) else rules.RuleVerdict(flags=kept)
