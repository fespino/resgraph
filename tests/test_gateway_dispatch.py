"""Dispatch policy offline: health transitions with gradual readmission,
EWMA, bounded admission with a drain-estimate Retry-After, and choice that
never selects a down backend."""

import pytest

from resgraph.gateway.dispatch import Backend, Ewma, Health, QueueFull, choose


def test_health_degrades_on_slow_and_drops_on_fail():
    h = Health()
    assert h.observe("slow") == "degraded"
    assert h.observe("ok") == "healthy"
    assert h.observe("fail") == "down"


def test_readmission_takes_three_consecutive_passes():
    h = Health()
    h.observe("fail")
    assert h.observe("ok") == "down"
    assert h.observe("ok") == "down"
    assert h.observe("ok") == "healthy"


def test_a_fail_mid_readmission_resets_the_streak():
    h = Health()
    h.observe("fail")
    h.observe("ok")
    h.observe("ok")
    assert h.observe("fail") == "down"
    h.observe("ok")
    h.observe("ok")
    assert h.state == "down"
    assert h.observe("ok") == "healthy"


def test_slow_readmission_lands_degraded_not_healthy():
    h = Health()
    h.observe("fail")
    h.observe("ok")
    h.observe("ok")
    assert h.observe("slow") == "degraded"


def test_ewma_first_sample_then_weighted():
    e = Ewma(alpha=0.5)
    assert e.value is None
    assert e.update(2.0) == 2.0
    assert e.update(4.0) == pytest.approx(3.0)


def test_admission_is_bounded_and_carries_a_drain_estimate():
    b = Backend("local", concurrency=1, queue_max=2)
    b.ttft_ewma.update(3.0)
    b.admit()
    b.admit()
    with pytest.raises(QueueFull) as exc:
        b.admit()
    assert exc.value.retry_after_s == 6
    b.release()
    b.admit()


def test_admission_before_any_sample_still_rejects_with_a_floor():
    b = Backend("local", concurrency=1, queue_max=1)
    b.admit()
    with pytest.raises(QueueFull) as exc:
        b.admit()
    assert exc.value.retry_after_s >= 1


def test_choose_prefers_healthy_then_lowest_ttft():
    fast = Backend("fast", concurrency=1, queue_max=1)
    slow = Backend("slow", concurrency=1, queue_max=1)
    fast.ttft_ewma.update(0.5)
    slow.ttft_ewma.update(5.0)
    assert choose([slow, fast]) is fast
    fast.health.observe("slow")
    assert choose([slow, fast]) is slow


def test_choose_never_picks_a_down_backend_and_none_when_all_down():
    a = Backend("a", concurrency=1, queue_max=1)
    b = Backend("b", concurrency=1, queue_max=1)
    a.health.observe("fail")
    assert choose([a, b]) is b
    b.health.observe("fail")
    assert choose([a, b]) is None


def test_choose_gives_an_unmeasured_backend_traffic_first():
    fresh = Backend("fresh", concurrency=1, queue_max=1)
    measured = Backend("measured", concurrency=1, queue_max=1)
    measured.ttft_ewma.update(0.1)
    assert choose([measured, fresh]) is fresh
