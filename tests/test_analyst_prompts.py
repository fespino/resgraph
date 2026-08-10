"""Prompt assembly invariants — stable prefix, one breakpoint, synced audit."""

import json
from datetime import UTC, datetime
from pathlib import Path

from resgraph.analyst.harness import MAX_RUN_TOKENS, MAX_TOOL_CALLS, cache_fingerprint
from resgraph.analyst.prompts import (
    AUDITED_SECTIONS,
    WorldSummary,
    build_prompt,
    prefix_text,
)

AUDIT_DOC = Path(__file__).resolve().parents[1] / "docs" / "prompt-audit.md"

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)

SUMMARY = WorldSummary(
    resource_counts={"vm": 18, "host": 3},
    neighborhood=("host-000001 (host) — state=up, rack=r1",),
    window_start=T0,
    window_end=T1,
)
OTHER_SUMMARY = WorldSummary(
    resource_counts={"vm": 2},
    neighborhood=(),
    window_start=T0,
    window_end=T0,
)


def prompt(summary=SUMMARY, rid="vm-000002"):
    return build_prompt(resource_id=rid, symptom="unreachable", fired_at=T1, summary=summary)


def test_prefix_bytes_identical_across_runs():
    a, b = prompt(), prompt(OTHER_SUMMARY, rid="db-000001")
    assert a.system[0] == b.system[0]
    assert a.system[1] != b.system[1]
    assert a.user != b.user


def test_cache_breakpoint_only_ends_the_prefix():
    p = prompt()
    assert p.system[0]["cache_control"] == {"type": "ephemeral"}
    assert all("cache_control" not in block for block in p.system[1:])


def test_prefix_carries_discipline_and_contract():
    text = prefix_text()
    assert "Bracket the window tightly" in text  # change-forensics body
    assert "Intersect before you inspect" in text
    for field in ("suspects", "mechanism_path", "no_confident_candidate", "degraded"):
        assert field in text
    assert "read-only" in text


def test_prefix_names_no_budget_numbers():
    # Budgets are enforced in the harness; the prompt describes the
    # behavior on exhaustion but never a number a code change could orphan.
    text = prefix_text()
    assert f"{MAX_TOOL_CALLS} tool" not in text
    assert str(MAX_RUN_TOKENS) not in text
    assert f"{MAX_RUN_TOKENS:,}" not in text


def test_suffix_and_user_carry_the_runtime_facts():
    p = prompt()
    suffix = p.system[1]["text"]
    assert "host=3, vm=18" in suffix
    assert "host-000001" in suffix
    assert "2026-01-01T00:00:00+00:00 .. 2026-01-01T00:05:00+00:00" in suffix
    assert "unreachable on vm-000002" in p.user


def test_prefix_json_schema_is_canonical():
    text = prefix_text()
    start = text.index('{\n  "$defs"')
    depth = 0
    end = start
    for i, ch in enumerate(text[start:], start):
        depth += ch == "{"
        depth -= ch == "}"
        if depth == 0:
            end = i + 1
            break
    schema = text[start:end]
    assert schema == json.dumps(json.loads(schema), indent=2, sort_keys=True)


def test_audit_table_covers_every_section():
    doc = AUDIT_DOC.read_text()
    for section in AUDITED_SECTIONS:
        assert section in doc, f"docs/prompt-audit.md is missing a verdict for {section!r}"


def test_cache_fingerprint_stable_across_runs():
    class Tools:
        def __init__(self, description="d"):
            self._description = description

        def blocks(self):
            return [{"name": "fetch_resource", "description": self._description}]

        def execute(self, name, args):
            raise AssertionError("fingerprint must not execute tools")

    a = cache_fingerprint(prompt(), Tools())
    b = cache_fingerprint(prompt(OTHER_SUMMARY, rid="db-000001"), Tools())
    assert a == b
    assert cache_fingerprint(prompt(), Tools(description="edited")) != a


def test_worked_examples_include_a_labeled_negative():
    text = prefix_text()
    assert "do not copy" in text
    assert "sequence 0 is the initial snapshot" in text
    assert "derived arithmetic" in " ".join(text.split())


def test_contract_carries_the_deferral_rule():
    text = prefix_text()
    assert "deferral is for running out of EVIDENCE" in text
    assert '"deferral"' in text
