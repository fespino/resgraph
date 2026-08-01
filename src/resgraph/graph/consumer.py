"""Hot-store consumer — the generic stream loop applied via the watermark.

At-least-once delivery composed with the idempotent apply path yields
exactly-once store state. The loop mechanics (pending-first recovery,
ack-after-apply, poison handling) live in resgraph.consumer; this
wrapper binds them to a Bolt session.
"""

from resgraph.consumer import DEFAULT_STREAM, StreamConsumer
from resgraph.schema import UpdateMessage

from .ingest import apply_batch

__all__ = ["DEFAULT_STREAM", "Consumer"]


class Consumer(StreamConsumer):
    def __init__(
        self,
        redis_url: str,
        session,
        stream: str = DEFAULT_STREAM,
        group: str = "resgraph-ingest",
        name: str = "c1",
        batch: int = 1024,
        block_ms: int = 1000,
    ) -> None:
        def apply(msgs: list[UpdateMessage]) -> tuple[int, int]:
            return apply_batch(session, msgs)

        super().__init__(
            redis_url, apply, stream=stream, group=group, name=name, batch=batch, block_ms=block_ms
        )
