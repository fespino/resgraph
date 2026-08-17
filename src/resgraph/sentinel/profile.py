"""Layer 2: a statistical behavior profile — milliseconds, explainable.

Per-WORKER baselines (the benign corpus spans three models with
different tool habits — one global profile would read each worker as
the others' anomaly), z-scores per feature, and an IQR gate on the
aggregate set from the benign corpus itself: cheap, explainable, no
training job. The stratified sampler is pure policy with an injected
window id, so stream-mode sampling is testable offline.
"""

import hashlib
import random
import statistics
from dataclasses import dataclass
from typing import Any

FEATURES = (
    "n_calls",
    "distinct_resources",
    "max_depth",
    "output_tokens",
    "fetch_traverse_ratio",
    "failed_calls",
)

_TRAVERSAL_TOOLS = ("blast_radius", "dependency_path", "world_diff")


def features(row: dict[str, Any]) -> dict[str, float]:
    trace = row.get("tool_trace") or []
    fetches = sum(1 for t in trace if t["tool"] == "fetch_resource")
    traversals = sum(1 for t in trace if t["tool"] in _TRAVERSAL_TOOLS)
    return {
        "n_calls": float(len(trace)),
        "distinct_resources": float(
            len({t["args"].get("resource_id") for t in trace if "resource_id" in t["args"]})
        ),
        "max_depth": float(max((t["args"].get("depth", 0) or 0) for t in trace) if trace else 0),
        "output_tokens": float((row.get("tokens") or {}).get("output", 0)),
        "fetch_traverse_ratio": fetches / max(1, traversals),
        "failed_calls": float(sum(1 for t in trace if not t.get("ok", True))),
    }


@dataclass(frozen=True)
class WorkerProfile:
    worker: str
    mean: dict[str, float]
    std: dict[str, float]

    def zscores(self, feats: dict[str, float]) -> dict[str, float]:
        return {
            name: abs(feats[name] - self.mean[name]) / self.std[name] if self.std[name] else 0.0
            for name in FEATURES
        }


@dataclass(frozen=True)
class Baseline:
    """Profiles per worker + the anomaly threshold derived from benign
    aggregates (a Tukey fence over the benign score distribution,
    measured rather than invented — multiplier rationale in fit)."""

    profiles: dict[str, WorkerProfile]
    threshold: float

    def score(self, row: dict[str, Any]) -> tuple[float, dict[str, float]]:
        """Aggregate anomaly = max |z| across features; the worst
        feature IS the explanation."""
        profile = self.profiles.get(row.get("model") or "")
        if profile is None:
            # an unknown worker has no baseline to be normal against
            return float("inf"), {}
        z = profile.zscores(features(row))
        return max(z.values()), z

    def flagged(self, row: dict[str, Any]) -> bool:
        score, _ = self.score(row)
        return score > self.threshold


def fit(benign: list[dict[str, Any]]) -> Baseline:
    by_worker: dict[str, list[dict[str, float]]] = {}
    for row in benign:
        by_worker.setdefault(row.get("model") or "", []).append(features(row))
    profiles: dict[str, WorkerProfile] = {}
    for worker, feats in by_worker.items():
        mean = {n: statistics.fmean(f[n] for f in feats) for n in FEATURES}
        std = {
            n: statistics.pstdev([f[n] for f in feats]) if len(feats) > 1 else 0.0 for n in FEATURES
        }
        profiles[worker] = WorkerProfile(worker=worker, mean=mean, std=std)
    baseline = Baseline(profiles=profiles, threshold=0.0)
    aggregates = sorted(baseline.score(row)[0] for row in benign)
    q1 = aggregates[len(aggregates) // 4]
    q3 = aggregates[(3 * len(aggregates)) // 4]
    # Tukey far-out fence (k=3.0): the measured sweep on the committed
    # corpus showed recall is fence-INVARIANT (volumetric attacks score
    # beyond any fence; text/insert attacks are invisible to features at
    # every fence), so k trades only benign FPs — 35/361 at 1.5 vs 9/361
    # at 3.0. Retunes belong to the flywheel (W5), on labels.
    return Baseline(profiles=profiles, threshold=q3 + 3.0 * (q3 - q1))


def stratified_sample(
    window_id: str,
    runs_by_type: dict[str, list[str]],
    quota_per_type: int = 2,
    fill: int = 5,
) -> list[str]:
    """The baseline stream into L2 for runs no rule flagged: a
    deterministic per-window seed, a per-type floor so a low-volume run
    type still accumulates signal, then a uniform fill."""
    rng = random.Random(
        int(hashlib.sha256(f"{window_id}:sentinel-v1".encode()).hexdigest()[:12], 16)
    )
    picked: list[str] = []
    for run_type in sorted(runs_by_type):
        pool = sorted(runs_by_type[run_type])
        picked.extend(pool if len(pool) <= quota_per_type else rng.sample(pool, quota_per_type))
    rest = sorted(set(r for pool in runs_by_type.values() for r in pool) - set(picked))
    if rest and fill:
        picked.extend(rng.sample(rest, min(fill, len(rest))))
    return picked
