"""Verify a run measured something before it is trusted or compared.

Pure over run rows; the CLI owns I/O. These are the pre-mortem gates for a
paid arm run (docs/drills/), promoted from ad-hoc snippets to a tested
command: a run that names numbers but fails a gate here measured noise, not
the thing it claims. A non-zero exit is the machine saying "do not compare
this yet."
"""

from dataclasses import dataclass
from typing import Any

from .report import aggregate


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def verify_run(
    rows: list[dict[str, Any]],
    *,
    expect_rows: int | None = None,
    expect_fingerprint: str | None = None,
    expect_items: int | None = None,
    min_trials: int | None = None,
) -> tuple[list[Check], bool]:
    """Gate an arm run against the invariants a comparison depends on.
    Universal checks always run; the expect_* are asserted only when given.
    Returns the checks and whether all passed."""
    if not rows:
        return [Check("non-empty", False, "no rows")], False

    checks: list[Check] = []
    agg = aggregate(rows)

    models = {m for r in rows if (m := r.get("model")) is not None}
    checks.append(Check("single model", len(models) == 1, ", ".join(sorted(models)) or "none"))

    fps = {f for r in rows if (f := r.get("cache_fingerprint")) is not None}
    # rows carry the full hash; callers pass its short prefix (b041069e)
    fp_ok = len(fps) == 1 and (
        expect_fingerprint is None or any(f.startswith(expect_fingerprint) for f in fps)
    )
    shown = ", ".join(sorted(f[:8] for f in fps)) or "none"
    if expect_fingerprint:
        shown += f" (expected {expect_fingerprint[:8]})"
    checks.append(Check("single fingerprint", fp_ok, shown))

    if expect_rows is not None:
        checks.append(
            Check("row count", len(rows) == expect_rows, f"{len(rows)} (expected {expect_rows})")
        )
    if expect_items is not None:
        checks.append(
            Check(
                "item count",
                agg["items"] == expect_items,
                f"{agg['items']} (expected {expect_items})",
            )
        )
    if min_trials is not None:
        checks.append(
            Check("trials", agg["trials"] >= min_trials, f"k={agg['trials']} (min {min_trials})")
        )

    notool = [r.get("scenario_id") for r in rows if not (r.get("tool_trace") or [])]
    checks.append(
        Check(
            "every row used tools",
            not notool,
            f"{len(notool)} row(s) with no tool call: {notool[:5]}",
        )
    )

    fab = agg["fabrication_count"]
    fab_detail = "; ".join(agg["fabrications"][:3]) if fab else "0"
    checks.append(Check("no fabrications (halt)", fab == 0, fab_detail))

    return checks, all(c.ok for c in checks)


def render(checks: list[Check], rows: list[dict[str, Any]]) -> str:
    lines = ["RUN VERIFY:"]
    for c in checks:
        lines.append(f"  {'PASS' if c.ok else 'FAIL'}  {c.name:<22} {c.detail}")
    agg = aggregate(rows)
    lines.append(
        f"  info  pass^k {agg['pass_all_trials']}  items {agg['items']}  "
        f"trials {agg['trials']}  fabrications {agg['fabrication_count']}"
    )
    return "\n".join(lines)
