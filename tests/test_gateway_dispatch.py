"""Dispatch policy offline: health transitions with gradual readmission,
EWMA, bounded admission with a drain-estimate Retry-After, and choice that
never selects a down backend."""

import pytest

from resgraph.gateway.dispatch import Backend, ErrorWindow, Health, QueueFull, RollingWindow, choose


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


def test_percentiles_are_nearest_rank_over_the_window():
    w = RollingWindow(window_s=300.0)
    for i, v in enumerate([0.5, 0.5, 0.5, 12.0]):  # our measured bimodal shape
        w.observe(v, now=float(i))
    assert w.percentile(50, now=4.0) == 0.5
    assert w.percentile(99, now=4.0) == 12.0  # the mode a mean would erase
    assert w.snapshot(now=4.0) == {"p50": 0.5, "p75": 0.5, "p90": 12.0, "p99": 12.0}


def test_the_window_evicts_and_empty_reads_none():
    w = RollingWindow(window_s=10.0)
    w.observe(1.0, now=0.0)
    assert w.percentile(50, now=5.0) == 1.0
    assert w.percentile(50, now=11.0) is None
    assert w.snapshot(now=11.0) is None


def test_the_error_window_deprioritizes_then_forgets():
    e = ErrorWindow(window_s=30.0)
    e.observe(False, now=0.0)
    e.observe(False, now=1.0)
    assert not e.soft_deprioritized(now=2.0)  # below the event floor
    e.observe(True, now=2.0)
    e.observe(False, now=3.0)
    assert e.soft_deprioritized(now=4.0)  # 3/4 failed in-window
    assert not e.soft_deprioritized(now=40.0)  # the window forgot
    e.observe(True, now=40.0)  # observing also evicts the aged events
    assert len(e.events) == 1


def test_admission_is_bounded_and_carries_a_drain_estimate():
    b = Backend("local", concurrency=1, queue_max=2)
    b.ttft.observe(3.0)
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
    fast.ttft.observe(0.5)
    slow.ttft.observe(5.0)
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
    measured.ttft.observe(0.1)
    assert choose([measured, fresh]) is fresh


def test_choose_ranks_a_recently_erroring_backend_below_a_degraded_quiet_one():
    flaky = Backend("flaky", concurrency=1, queue_max=1)
    degraded = Backend("degraded", concurrency=1, queue_max=1)
    degraded.health.observe("slow")
    for ok in (False, False, True, False):
        flaky.errors.observe(ok)
    assert choose([flaky, degraded]) is degraded  # deprioritized, not eliminated
    degraded.health.observe("fail")
    assert choose([flaky, degraded]) is flaky  # still serves when the rest is down
