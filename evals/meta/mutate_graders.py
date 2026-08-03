"""Mutation check for the grading layer: break each grader on purpose
and require the test suite to notice.

Run after any grader change: `uv run python evals/meta/mutate_graders.py`.
Exits nonzero if any mutant survives; a survivor is a test gap and
must become a test before the grader change merges. Results are
recorded in EVALS.md ("Grader verification").
"""

import pathlib
import subprocess
import sys

SRC = pathlib.Path(__file__).resolve().parents[2] / "src" / "resgraph" / "evals"
TESTS = ["tests/test_evals_graders.py"]

MUTANTS: list[tuple[str, str, str, str]] = [
    (
        "graders.py",
        "seqs[0] == truth.causal_sequence",
        "seqs[0] != truth.causal_sequence",
        "found_top1: comparison inverted",
    ),
    (
        "graders.py",
        "truth.causal_sequence in seqs[:3]",
        "truth.causal_sequence in seqs",
        "found_top3: top-3 window removed (any rank passes)",
    ),
    (
        "graders.py",
        "if (dependent, dependency) not in edges:",
        "if (dependency, dependent) not in edges:",
        "evidence: edge orientation flipped",
    ),
    (
        "graders.py",
        "for dependency, dependent in pairwise(s.mechanism_path):",
        "for dependency, dependent in pairwise([]):",
        "evidence: edge checking disabled",
    ),
    (
        "graders.py",
        "if s.sequence not in log_sequences:",
        "if s.sequence in log_sequences:",
        "evidence: log-existence check inverted",
    ),
    (
        "graders.py",
        "passed = report.no_confident_candidate and not confident",
        "passed = report.no_confident_candidate or not confident",
        "honesty: AND weakened to OR",
    ),
    (
        "graders.py",
        'if s.confidence == "high"',
        'if s.confidence == "medium"',
        "honesty: high-confidence detection retargeted",
    ),
    (
        "graders.py",
        "if calls.count((name, args)) > 1",
        "if calls.count((name, args)) > 99",
        "discipline: repeated-call detection disabled",
    ),
    (
        "graders.py",
        "result.usage.uncached_fraction > UNCACHED_CEILING",
        "result.usage.uncached_fraction < UNCACHED_CEILING",
        "discipline: uncached-fraction gate inverted",
    ),
    (
        "graders.py",
        "if result.validation_failures:",
        "if not result.validation_failures:",
        "discipline: parse-first-try check inverted",
    ),
    (
        "report.py",
        "sum(all(v) for v in by_item.values())",
        "sum(any(v) for v in by_item.values())",
        "report: pass^k computed as pass@k",
    ),
    (
        "report.py",
        'return bool(dims.get("honesty", {}).get("passed"))',
        "return True",
        "report: controls always pass",
    ),
    (
        "judge.py",
        'return DimResult("narrative", score >= PASS_SCORE',
        'return DimResult("narrative", score > PASS_SCORE',
        "judge: pass boundary off by one",
    ),
]


def main() -> int:
    survivors: list[str] = []
    for fname, old, new, desc in MUTANTS:
        path = SRC / fname
        original = path.read_text()
        if old not in original:
            print(f"STALE    {desc} (mutation target not found — update MUTANTS)")
            survivors.append(desc)
            continue
        path.write_text(original.replace(old, new, 1))
        try:
            proc = subprocess.run(
                ["uv", "run", "pytest", *TESTS, "-q", "-x", "-o", "addopts=", "--no-header"],
                capture_output=True,
                text=True,
            )
            killed = proc.returncode != 0
        finally:
            path.write_text(original)
        print(f"{'KILLED  ' if killed else 'SURVIVED'} {desc}")
        if not killed:
            survivors.append(desc)
    print(f"\n{len(MUTANTS) - len(survivors)}/{len(MUTANTS)} mutants killed")
    return 1 if survivors else 0


if __name__ == "__main__":
    sys.exit(main())
