"""Apply-failure containment logic, unit level (D14 addendum, #44).

The retry ladder and the binary split are pure control flow; a stub
client records acks and DLQ writes without any store. What genuinely
needs a real Redis (XPENDING delivery-counter semantics) stays in
test_consumer_dlq.py under the integration marker.
"""

from resgraph.consumer import StreamConsumer
from resgraph.gen.churn import Churn
from resgraph.gen.world import World


class StubRedis:
    def __init__(self):
        self.acked = []
        self.dlq = []

    def xack(self, stream, group, *ids):
        self.acked.extend(ids)

    def xadd(self, stream, fields):
        self.dlq.append((stream, fields))

    def xpending_range(self, *args, **kwargs):
        return []


def _consumer(apply_fn) -> tuple[StreamConsumer, StubRedis]:
    c = StreamConsumer("redis://localhost:1", apply_fn, backoff_s=0)
    stub = StubRedis()
    c.r = stub
    return c, stub


def _entries(n=16):
    churn = Churn(World(3, 20))
    msgs = (list(churn.snapshot()) + [churn.next_message() for _ in range(n)])[:n]
    return [
        (f"{i}-0".encode(), {b"data": m.model_dump_json().encode()}) for i, m in enumerate(msgs)
    ], msgs


def _counters():
    return {"read": 0, "applied": 0, "skipped": 0, "invalid": 0, "dead_lettered": 0}


def test_split_isolates_one_poison_in_log_applies():
    entries, msgs = _entries(16)
    poison = (msgs[5].resource_id, msgs[5].sequence)
    calls = []

    def apply(batch):
        calls.append(len(batch))
        if any((m.resource_id, m.sequence) == poison for m in batch):
            raise RuntimeError("rejected")
        return (len(batch), 0)

    c, stub = _consumer(apply)
    counters = _counters()
    c._apply_batch(entries, counters)

    assert counters["dead_lettered"] == 1
    assert counters["applied"] == 15
    assert len(stub.dlq) == 1
    assert stub.dlq[0][0] == c.dlq and "rejected" in stub.dlq[0][1]["error"]
    assert sorted(stub.acked) == sorted(eid for eid, _ in entries)
    # retry ladder (4 attempts) + binary descent, nowhere near 16 applies
    assert len(calls) <= 4 + 2 * 4 + 1


def test_split_isolates_multiple_poisons():
    entries, msgs = _entries(16)
    poisons = {(msgs[2].resource_id, msgs[2].sequence), (msgs[11].resource_id, msgs[11].sequence)}

    def apply(batch):
        if any((m.resource_id, m.sequence) in poisons for m in batch):
            raise RuntimeError("rejected")
        return (len(batch), 0)

    c, stub = _consumer(apply)
    counters = _counters()
    c._apply_batch(entries, counters)

    assert counters["dead_lettered"] == 2
    assert counters["applied"] == 14
    assert len(stub.dlq) == 2


def test_transient_failure_never_reaches_the_split():
    entries, _ = _entries(8)
    calls = {"n": 0}

    def apply(batch):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return (len(batch), 0)

    c, stub = _consumer(apply)
    counters = _counters()
    c._apply_batch(entries, counters)

    assert counters["dead_lettered"] == 0 and counters["applied"] == 8
    assert calls["n"] == 2
    assert stub.dlq == []


def test_deterministic_failure_exhausts_exactly_the_retry_ladder():
    entries, _ = _entries(1)
    calls = {"n": 0}

    def apply(batch):
        calls["n"] += 1
        raise RuntimeError("always")

    c, stub = _consumer(apply)
    counters = _counters()
    c._apply_batch(entries, counters)

    assert calls["n"] == c.max_retries + 1
    assert counters["dead_lettered"] == 1 and len(stub.dlq) == 1
