"""Gateway response cache: byte-identical full requests, nothing looser.

This is the second of the two cache layers and answers a different question
than the provider's prefix cache: the provider caches a shared PREFIX across
different requests; this layer returns a completed response for an IDENTICAL
full request. Conflating them is the classic cache error, so they are named
and measured separately.

Identity is the full request semantics — alias + every request field — and a
one-token difference is a different resource. Only deterministic requests are
cacheable (a temperature-0 setup, non-streamed): caching a sampled response
would replay one draw as if it were the answer. The clock is injected, so
TTL behavior tests offline."""

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

DEFAULT_TTL_S = 900.0
DEFAULT_MAX_ENTRIES = 256


def cache_key(alias: str, kwargs: dict[str, Any]) -> str:
    canonical = json.dumps({"alias": alias, "kwargs": kwargs}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ResponseCache:
    def __init__(
        self,
        ttl_s: float = DEFAULT_TTL_S,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self.clock = clock
        self.hits = 0
        self.misses = 0
        self.tokens_saved = 0
        self._entries: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            self.misses += 1
            return None
        expires_at, value, tokens = entry
        if self.clock() >= expires_at:
            del self._entries[key]
            self.misses += 1
            return None
        self._entries.move_to_end(key)
        self.hits += 1
        self.tokens_saved += tokens
        return value

    def put(self, key: str, value: Any, tokens: int) -> None:
        self._entries[key] = (self.clock() + self.ttl_s, value, tokens)
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
