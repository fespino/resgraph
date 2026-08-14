"""The serving gateway: meter the token path and route by precedence. This
package adds the serving plumbing the in-process client factory deliberately
excluded — queues, streaming accounting, caches, and failover with telemetry."""

from .router import (
    CLASSIFICATION,
    DEFAULT_REGISTRY,
    GLOBAL_DEFAULT,
    GLOBAL_DEFAULT_WORKER,
    JUDGMENT,
    OVERRIDE,
    PIN,
    TASK_CLASS_DEFAULT,
    WORKHORSE,
    ClassRoute,
    RouteDecision,
    resolve,
)

__all__ = [
    "CLASSIFICATION",
    "DEFAULT_REGISTRY",
    "GLOBAL_DEFAULT",
    "GLOBAL_DEFAULT_WORKER",
    "JUDGMENT",
    "OVERRIDE",
    "PIN",
    "TASK_CLASS_DEFAULT",
    "WORKHORSE",
    "ClassRoute",
    "RouteDecision",
    "resolve",
]
