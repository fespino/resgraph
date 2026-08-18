"""Routing economics (D41): the price lottery's measured shape and the
cost delta it buys. The seeded runs below ARE the measurement receipts —
deterministic, hardware-independent, methodology in the assertions."""

import random

import pytest

from resgraph.gateway import server

DRAWS = 6000
PRICES = {"cheap": 1.0, "mid": 2.0, "steep": 3.0}
# inverse-square weights 1 : 1/4 : 1/9, normalized
EXPECTED_SHARE = {"cheap": 36 / 49, "mid": 9 / 49, "steep": 4 / 49}


def _gw(prices: dict[str, float | None], seed: int = 7) -> server.Gateway:
    setups = {}
    for name, p in prices.items():
        setup: dict = {"provider": name, "base_url": "http://x", "model": "m"}
        if p is not None:
            setup["price_per_mtok"] = {"input": p / 2, "output": p / 2}
        setups[f"m@{name}"] = setup
    return server.Gateway(
        setups=setups,
        client_factory=lambda s: None,
        registry={},
        aliases={"m": list(setups)},
        rng=random.Random(seed),
    )


def _first_pick_shares(gw: server.Gateway) -> dict[str, float]:
    eids = gw.aliases["m"]
    counts: dict[str, int] = {}
    for _ in range(DRAWS):
        first = server._order_candidates(gw, list(eids))[0]
        counts[first] = counts.get(first, 0) + 1
    return {e: n / DRAWS for e, n in counts.items()}


def test_the_lottery_matches_the_documented_inverse_square_shape():
    shares = _first_pick_shares(_gw(dict(PRICES)))
    for name, expected in EXPECTED_SHARE.items():
        assert shares[f"m@{name}"] == pytest.approx(expected, abs=0.02)


def test_a_free_endpoint_preempts_the_lottery_entirely():
    gw = _gw({"free": None, **PRICES})
    shares = _first_pick_shares(gw)
    assert shares == {"m@free": 1.0}  # cheap-by-default is a tier, not a weight


def test_a_soft_deprioritized_priced_endpoint_leaves_its_group():
    gw = _gw(dict(PRICES))
    flaky = gw.backend("m@cheap")
    for ok in (False, False, False):
        flaky.errors.observe(ok)
    shares = _first_pick_shares(gw)
    assert "m@cheap" not in shares  # health prioritizes before cost weights
    total = EXPECTED_SHARE["mid"] + EXPECTED_SHARE["steep"]
    assert shares["m@mid"] == pytest.approx(EXPECTED_SHARE["mid"] / total, abs=0.02)


def test_the_measured_cost_delta_vs_latency_first_routing():
    """The exit-gate measurement (#263 item 3): the same request stream
    priced under both policies. Latency-first routing in the worst case —
    the priciest endpoint is the fastest — pays $3/mtok on every request;
    the lottery pays the share-weighted price. The delta is the cost
    clause's whole argument, and it is a measured number, not a claim."""
    shares = _first_pick_shares(_gw(dict(PRICES)))
    lottery_price = sum(shares[f"m@{n}"] * p for n, p in PRICES.items())
    latency_first_price = PRICES["steep"]
    ratio = lottery_price / latency_first_price
    # expectation: (36·1 + 9·2 + 4·3) / 49 / 3 = 66/147 ≈ 0.449
    assert ratio == pytest.approx(0.449, abs=0.02)


def test_the_sampler_survives_rounding_overshoot():
    """The defensive else: if accumulated weights undershoot the drawn
    point (float addition), the last candidate is taken, never dropped."""

    class _Over:
        def random(self):
            return 1.5  # beyond any [0,1) draw: forces draw > sum(weights)

    gw = _gw(dict(PRICES))
    gw.rng = _Over()
    ordered = server._sample_by_inverse_square_price(gw, list(gw.aliases["m"]))
    assert sorted(ordered) == sorted(gw.aliases["m"])  # all served, none lost
