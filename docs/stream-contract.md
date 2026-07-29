# The stream contract: why consumers must not assume referential integrity

The generator guarantees (and its property suite asserts) that every
relationship target is **alive at emit time, in world order**. It is
tempting to conclude consumers may rely on that. They must not — the
two ends of the stream hold different contracts:

- **The generator's guarantee** holds in *world order*: at the moment a
  message is emitted, its relationship targets exist.
- **The consumer's view** is the stream *after transport* — batching,
  consumer groups, retries — where the only ordering guarantee is
  **per-resource** (D2). Global order does not survive.

Concretely: a `vm → runs_on → host-000009` edge can reach the consumer
*before* `host-000009`'s create (cross-resource reordering) or *after*
its delete (the vm's message was emitted while the host lived; the
delete overtook it in transport).

This is why the hot-store ingest must tolerate dangling edges (D3)
rather than enforce referential integrity at apply time. The
generator's guarantee and the consumer's assumption are different
contracts; conflating them — "the producer validates, so the consumer
can trust" — is the classic distributed-systems bug this platform
exists to exercise. Producer-side validation constrains what is *said*;
transport decides what is *heard*, and in which order.
