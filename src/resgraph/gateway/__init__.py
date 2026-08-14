"""The serving gateway: meter the token path and route by precedence. This
package adds the serving plumbing the in-process client factory deliberately
excluded — queues, streaming accounting, caches, and failover with telemetry."""

from .router import (
    ANTHROPIC,
    CLASSIFICATION,
    DEFAULT_REGISTRY,
    GLOBAL_DEFAULT,
    GLOBAL_DEFAULT_MODEL,
    JUDGMENT,
    LOCAL,
    OVERRIDE,
    PIN,
    TASK_CLASS_DEFAULT,
    WORKHORSE,
    ClassRoute,
    RouteDecision,
    backend_of,
    resolve,
)

__all__ = [
    "ANTHROPIC",
    "CLASSIFICATION",
    "DEFAULT_REGISTRY",
    "GLOBAL_DEFAULT",
    "GLOBAL_DEFAULT_MODEL",
    "JUDGMENT",
    "LOCAL",
    "OVERRIDE",
    "PIN",
    "TASK_CLASS_DEFAULT",
    "WORKHORSE",
    "ClassRoute",
    "RouteDecision",
    "backend_of",
    "resolve",
]
