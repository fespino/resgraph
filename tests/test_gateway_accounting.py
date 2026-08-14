"""Stream accounting offline: TTFT stamping, the throughput window,
usage reconciliation, and the death verdicts."""

import pytest

from resgraph.gateway.accounting import StreamAccount


def test_ttft_is_none_until_the_first_content_token():
    a = StreamAccount(backend="local", started_at=10.0)
    assert a.ttft is None
    a.content(at=12.5)
    assert a.ttft == 2.5


def test_ttft_stamps_once_and_does_not_move():
    a = StreamAccount(backend="local", started_at=10.0)
    a.content(at=12.0)
    a.content(at=20.0)
    assert a.ttft == 2.0


def test_throughput_measures_the_emission_window_not_the_wait():
    a = StreamAccount(backend="local", started_at=0.0)
    a.content(at=5.0)
    a.content(at=6.0, tokens=9)
    a.finish(at=10.0, reported_output_tokens=10)
    assert a.tokens_per_second == pytest.approx(2.0)


def test_throughput_is_none_without_tokens_or_before_the_end():
    a = StreamAccount(backend="local", started_at=0.0)
    assert a.tokens_per_second is None
    a.content(at=1.0)
    assert a.tokens_per_second is None


def test_reconciliation_within_tolerance():
    a = StreamAccount(backend="local", started_at=0.0)
    a.content(at=1.0, tokens=100)
    r = a.finish(at=2.0, reported_output_tokens=101)
    assert r.within_tolerance
    assert r.drift == pytest.approx(1 / 101)


def test_reconciliation_beyond_tolerance_is_flagged():
    a = StreamAccount(backend="local", started_at=0.0)
    a.content(at=1.0, tokens=90)
    r = a.finish(at=2.0, reported_output_tokens=100)
    assert not r.within_tolerance
    assert r.drift == pytest.approx(0.10)


def test_reconciliation_of_an_empty_stream():
    a = StreamAccount(backend="local", started_at=0.0)
    r = a.finish(at=1.0, reported_output_tokens=0)
    assert r.within_tolerance
    assert r.drift == 0.0


def test_death_at_zero_tokens_is_restartable():
    a = StreamAccount(backend="local", started_at=0.0)
    d = a.died(at=1.0, reason="connection reset")
    assert d.restartable
    assert d.tokens_emitted == 0


def test_death_after_tokens_must_surface_not_restart():
    a = StreamAccount(backend="local", started_at=0.0)
    a.content(at=1.0, tokens=500)
    d = a.died(at=2.0, reason="backend died")
    assert not d.restartable
    assert d.tokens_emitted == 500
    assert d.backend == "local"
    assert d.reason == "backend died"
