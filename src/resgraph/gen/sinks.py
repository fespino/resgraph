"""Message sinks — stdout JSONL and Redis Stream (pipelined)."""

import sys

from resgraph.schema import UpdateMessage


class StdoutSink:
    def emit_many(self, msgs: list[UpdateMessage]) -> None:
        sys.stdout.write("".join(m.model_dump_json() + "\n" for m in msgs))

    def close(self) -> None:
        sys.stdout.flush()


class RedisSink:
    """Pipelined XADD — per-message round-trips cap at a few thousand msg/s;
    batching is not optional (measured in BENCHMARKS.md). maxlen~ keeps the
    stream bounded: it is transport, not storage."""

    def __init__(
        self,
        url: str,
        stream: str = "resgraph:updates",
        maxlen: int = 1_000_000,
    ) -> None:
        import redis

        self.r = redis.Redis.from_url(url)
        self.stream = stream
        self.maxlen = maxlen

    def emit_many(self, msgs: list[UpdateMessage]) -> None:
        pipe = self.r.pipeline(transaction=False)
        for m in msgs:
            pipe.xadd(
                self.stream,
                {"data": m.model_dump_json()},
                maxlen=self.maxlen,
                approximate=True,
            )
        pipe.execute()

    def close(self) -> None:
        self.r.close()
