"""The verify command: the pre-mortem gates for a paid arm run as tested
code — one model, one fingerprint, every row used tools, zero fabrications,
and the expected shape when asserted. A gate that fails means the run
measured noise, so verify exits non-zero."""

import json

from typer.testing import CliRunner

from resgraph.evals.cli import app
from resgraph.evals.verify import render, verify_run

runner = CliRunner()


# rows carry the full 64-char hash; callers assert with its short prefix
FULL_FP = "b041069e807c50f959bece42827286587a33b883cb44bac722341fb1eb18cd1b"


def _row(sid, *, model="claude-opus-4-8", fp=FULL_FP, tools=1, evidence=True, stype="direct"):
    return {
        "scenario_id": sid,
        "scenario_type": stype,
        "model": model,
        "cache_fingerprint": fp,
        "tool_trace": [{"tool": "world_diff", "ok": True}] * tools,
        "dims": {
            "found_top3": {"passed": True},
            "evidence": {"passed": evidence, "detail": "" if evidence else "fabricated edge"},
        },
        "tokens": {"total": 100},
    }


def _run(n=3, **kw):
    return [_row(f"s{i}", **kw) for i in range(n)]


def test_clean_run_passes_all_gates():
    checks, ok = verify_run(
        _run(3), expect_rows=3, expect_fingerprint="b041069e", expect_items=3, min_trials=1
    )
    assert ok and all(c.ok for c in checks)


def test_multiple_fingerprints_fail():
    checks, ok = verify_run(_run(2) + [_row("s2", fp="deadbeef")])
    assert not ok
    assert any(c.name == "single fingerprint" and not c.ok for c in checks)


def test_expected_fingerprint_mismatch_fails():
    checks, ok = verify_run(_run(2, fp="aaaa1111"), expect_fingerprint="b041069e")
    assert not ok
    assert any(c.name == "single fingerprint" and not c.ok for c in checks)


def test_multiple_models_fail():
    checks, ok = verify_run(_run(2) + [_row("s2", model="claude-haiku-4-5")])
    assert not ok
    assert any(c.name == "single model" and not c.ok for c in checks)


def test_row_with_no_tools_fails():
    checks, ok = verify_run(_run(2) + [_row("s2", tools=0)])
    assert not ok
    assert any(c.name == "every row used tools" and not c.ok for c in checks)


def test_fabrication_is_a_halt():
    checks, ok = verify_run(_run(2) + [_row("s2", evidence=False)])
    assert not ok
    assert any("fabrication" in c.name and not c.ok for c in checks)


def test_expected_shape_mismatch_fails():
    checks, ok = verify_run(_run(3), expect_rows=90, expect_items=30)
    assert not ok
    assert any(c.name == "row count" and not c.ok for c in checks)
    assert any(c.name == "item count" and not c.ok for c in checks)


def test_empty_run_fails():
    checks, ok = verify_run([])
    assert not ok and not checks[0].ok


def test_render_marks_pass_and_fail():
    checks, _ = verify_run(_run(2) + [_row("s2", evidence=False)])
    text = render(checks, _run(2) + [_row("s2", evidence=False)])
    assert text.startswith("RUN VERIFY:")
    assert "FAIL" in text and "PASS" in text


def test_cli_verify_exit_codes(tmp_path):
    good = tmp_path / "good.jsonl"
    good.write_text("".join(json.dumps(r) + "\n" for r in _run(3)))
    assert runner.invoke(app, ["verify", str(good), "--fingerprint", "b041069e"]).exit_code == 0

    bad = tmp_path / "bad.jsonl"
    bad.write_text("".join(json.dumps(r) + "\n" for r in _run(2) + [_row("s2", evidence=False)]))
    result = runner.invoke(app, ["verify", str(bad)])
    assert result.exit_code == 1
    assert "FAIL" in result.output
