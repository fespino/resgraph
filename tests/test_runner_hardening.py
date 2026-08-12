"""Runner hardening (#94): the guards that stand between the eval
loop and money — store preflight, the --max-cost brake, and the
secret scan on every row before it is written."""

from types import SimpleNamespace

import pytest

from resgraph.evals.runner import (
    PREFLIGHT_NODE_CAP,
    assert_row_clean,
    estimate_cost,
    preflight_store,
)


class FakeSession:
    def __init__(self, count):
        self.count = count

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def run(self, query):
        return SimpleNamespace(single=lambda: {"c": self.count})


def fake_driver(count):
    return SimpleNamespace(session=lambda: FakeSession(count))


def test_preflight_refuses_a_populated_store():
    with pytest.raises(SystemExit, match="looks like real data"):
        preflight_store(fake_driver(PREFLIGHT_NODE_CAP + 1))


def test_preflight_passes_a_scratch_store():
    preflight_store(fake_driver(0))
    preflight_store(fake_driver(180))


def test_estimate_cost_math_and_unknown_model():
    tokens = {
        "input": 1_000_000,
        "output": 1_000_000,
        "cache_read": 1_000_000,
        "cache_creation": 1_000_000,
    }
    # opus 4.8: 5 in + 25 out + 0.5 cache-read + 6.25 cache-write
    assert estimate_cost(tokens, "claude-opus-4-8") == pytest.approx(36.75)
    zero = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    assert estimate_cost(zero, "claude-haiku-4-5") == 0.0
    # a model with no price on file is unmetered (e.g. a self-hosted one), not an error
    assert estimate_cost(tokens, "claude-2") == 0.0


def test_row_scan_refuses_secret_shaped_content_without_echoing_it():
    token = "sk-ant-abcdef1234567890"
    with pytest.raises(SystemExit) as exc:
        assert_row_clean(f'{{"report": "my key is {token}"}}')
    assert token not in str(exc.value)
    assert "anthropic-key" in str(exc.value)


def test_row_scan_passes_a_normal_row():
    assert_row_clean('{"scenario_id": "decoy-s42007", "dims": {"found_top1": true}}')
