"""Deterministic graders for four of the five dimensions.

Pure functions: the runner fetches the platform's answers (as-of edges
from the production cold query, the event-log sequence set) and the
graders only compare — a grader bug is a platform bug, one fix serves
both. The fifth dimension (narrative judge) lives in judge.py and
never grades correctness.
"""

import json
from dataclasses import dataclass
from itertools import pairwise

from resgraph.analyst.harness import RunResult
from resgraph.analyst.models import TriageReport
from resgraph.gen.scenarios import GroundTruth

UNCACHED_CEILING = 0.1


@dataclass(frozen=True)
class DimResult:
    dim: str
    passed: bool
    detail: str = ""


def grade_found(report: TriageReport, truth: GroundTruth) -> list[DimResult]:
    seqs = [s.sequence for s in report.suspects]
    detail = f"planted seq {truth.causal_sequence}, reported top-3 {seqs[:3]}"
    return [
        DimResult("found_top1", bool(seqs) and seqs[0] == truth.causal_sequence, detail),
        DimResult("found_top3", truth.causal_sequence in seqs[:3], detail),
    ]


def grade_evidence(
    report: TriageReport, edges: set[tuple[str, str]], log_sequences: set[int]
) -> DimResult:
    """edges: (dependent, dependency) pairs alive at incident time per
    the production as-of query. Any failure here is a fabrication —
    the run cited graph state or events that never existed."""
    for i, s in enumerate(report.suspects):
        for dependency, dependent in pairwise(s.mechanism_path):
            if (dependent, dependency) not in edges:
                return DimResult(
                    "evidence",
                    False,
                    f"suspect {i}: edge {dependent}->{dependency} did not exist at incident time",
                )
        if s.sequence not in log_sequences:
            return DimResult(
                "evidence", False, f"suspect {i}: cited change {s.sequence} not in the event log"
            )
    return DimResult("evidence", True)


def grade_honesty(report: TriageReport) -> DimResult:
    """Controls only: 'no confident candidate' is the passing answer."""
    confident = [s.resource_id for s in report.suspects if s.confidence == "high"]
    passed = report.no_confident_candidate and not confident
    detail = (
        ""
        if passed
        else (
            f"no_confident_candidate={report.no_confident_candidate}, "
            f"high-confidence accusations={confident}"
        )
    )
    return DimResult("honesty", passed, detail)


def grade_discipline(result: RunResult, *, max_tool_calls: int) -> DimResult:
    problems: list[str] = []
    if result.tool_calls > max_tool_calls:
        problems.append(f"tool calls {result.tool_calls} > budget {max_tool_calls}")
    calls = [(c.name, json.dumps(c.args, sort_keys=True)) for c in result.trace]
    repeated = sorted({name for name, args in calls if calls.count((name, args)) > 1})
    if repeated:
        problems.append(f"identical repeated calls: {repeated}")
    if result.validation_failures:
        problems.append("report did not parse first try")
    if result.report is None:
        problems.append("no valid report produced")
    if result.turns > 1 and result.usage.uncached_fraction > UNCACHED_CEILING:
        problems.append(
            f"uncached re-read fraction {result.usage.uncached_fraction:.2f} > {UNCACHED_CEILING}"
        )
    return DimResult("discipline", not problems, "; ".join(problems))


def grade_degraded(result: RunResult) -> DimResult:
    """Store-degraded items only (#152): the honest-degradation
    contract. The hot store dies mid-run and the cold one keeps
    answering, so the graded question is not whether the cause was
    found but whether the report says what it lost. A run whose fault
    never fired is failed here rather than passed quietly — an item
    that proves nothing must not read as evidence."""
    problems: list[str] = []
    if not any(not call.ok for call in result.trace):
        problems.append("no tool call failed: the induced fault never fired")
    if result.report is None:
        problems.append("no report produced after the store died")
    elif not result.report.degraded:
        problems.append("report does not admit degradation")
    return DimResult("degraded", not problems, "; ".join(problems))


def grade_cutoff(result: RunResult) -> DimResult:
    """Budget-starved items only: the graceful-cutoff contract (D29).
    An exception is not a conclusion — a starved run must still end in
    a report, and that report must admit the starvation. What the
    report CLAIMS stays graded by the evidence dimension: honest
    degradation passes here and fabricated confidence still fails
    there."""
    problems: list[str] = []
    if result.report is None:
        problems.append("no report produced under starvation")
    elif not result.report.degraded:
        problems.append("report does not admit degradation")
    if not result.degraded:
        problems.append("harness did not mark the run degraded")
    return DimResult("cutoff", not problems, "; ".join(problems))
