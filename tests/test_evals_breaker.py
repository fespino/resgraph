"""D29a judge spend breaker: a per-day ledger that warns at 90% and
trips loudly at the cap — a retry loop noticed before the invoice."""

from types import SimpleNamespace

import pytest

from resgraph.evals.breaker import JudgeSpendBreaker

PRICES = {"judge-m": (5.0, 25.0)}


def breaker(tmp_path, cap=1.0):
    return JudgeSpendBreaker(
        cap_usd=cap, model="judge-m", prices_per_mtok=PRICES, ledger=tmp_path / "spend.json"
    )


def usage(inp, out):
    return SimpleNamespace(input_tokens=inp, output_tokens=out)


def test_charge_accumulates_within_the_day(tmp_path):
    b = breaker(tmp_path)
    b.charge(usage(100_000, 10_000))  # 0.5 + 0.25 = 0.75
    assert b.spent_today() == pytest.approx(0.75)
    b.charge(usage(0, 0))
    assert b.spent_today() == pytest.approx(0.75)


def test_check_trips_at_cap(tmp_path):
    b = breaker(tmp_path, cap=0.5)
    b.charge(usage(100_000, 0))  # 0.5 exactly
    with pytest.raises(SystemExit, match="breaker tripped"):
        b.check()


def test_check_passes_below_cap(tmp_path):
    b = breaker(tmp_path, cap=1.0)
    b.charge(usage(10_000, 0))  # 0.05
    b.check()  # no raise


def test_warns_once_at_ninety_percent(tmp_path, capsys):
    b = breaker(tmp_path, cap=1.0)
    b.charge(usage(180_000, 0))  # 0.90 → crosses threshold
    b.charge(usage(1_000, 0))  # already warned; no second warning
    out = capsys.readouterr().out
    assert out.count("judge spend warning") == 1


def test_new_day_resets_the_ledger(tmp_path):
    path = tmp_path / "spend.json"
    path.write_text('{"date": "2000-01-01", "spent_usd": 5.0, "warned": true}')
    b = JudgeSpendBreaker(cap_usd=1.0, model="judge-m", prices_per_mtok=PRICES, ledger=path)
    assert b.spent_today() == 0.0  # stale day ignored
    b.check()  # would trip if the 5.0 carried over


def test_unknown_model_refused(tmp_path):
    with pytest.raises(SystemExit, match="no pricing"):
        JudgeSpendBreaker(
            cap_usd=1.0, model="mystery", prices_per_mtok=PRICES, ledger=tmp_path / "s.json"
        )
