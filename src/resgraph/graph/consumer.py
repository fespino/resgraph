"""Hot-store consumer — the generic stream loop applied via the watermark.

At-least-once delivery composed with the idempotent apply path yields
exactly-once store state. The loop mechanics (pending-first recovery,
ack-after-apply, poison handling) live in resgraph.consumer; this
wrapper binds them to a Bolt session.
"""

from typing import Any

from neo4j import Session
from neo4j.exceptions import ServiceUnavailable, SessionExpired, TransientError

from resgraph.consumer import DEFAULT_STREAM, StreamConsumer
from resgraph.schema import UpdateMessage

from .ingest import apply_batch

__all__ = ["DEFAULT_STREAM", "Consumer"]

# Connection-class errors: the store is down or the session broke —
# retry forever, never poison (D14 supersession). TransientError covers
# deadlocks/leader changes, which the driver considers safe to retry.
RETRYABLE = (ServiceUnavailable, SessionExpired, TransientError, ConnectionError)


class Consumer(StreamConsumer):
    def __init__(self, redis_url: str, session: Session, **kwargs: Any) -> None:
        def apply(msgs: list[UpdateMessage]) -> tuple[int, int]:
            return apply_batch(session, msgs)

        kwargs.setdefault("retryable_exceptions", RETRYABLE)
        super().__init__(redis_url, apply, **kwargs)
