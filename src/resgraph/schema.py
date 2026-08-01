# Message Schema as defined in D2
#
# TODO — open D2 gaps, undecided (record in SPEC.md before enforcing here):
#   - duplicate relationships: is [(runs_on, host-1)] twice a producer bug
#     (reject), a parse-time dedupe, or the store's problem?
#   - self-edges (target_id == resource_id): reject or tolerate?

from enum import StrEnum
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class Op(StrEnum):
    DELETE = "delete"
    UPSERT = "upsert"


class ResourceType(StrEnum):
    ASG = "asg"
    CONTAINER = "container"
    DB = "db"
    HOST = "host"
    LB = "lb"
    SG = "sg"
    VM = "vm"


_TYPE_PREFIXES = frozenset(t.value for t in ResourceType)


class Relationship(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["runs_on", "attached_to", "routes_to", "member_of"]
    target_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _target_prefix_is_a_known_type(self) -> Self:
        prefix = self.target_id.split("-", 1)[0]
        if prefix not in _TYPE_PREFIXES:
            raise ValueError(f"target_id prefix {prefix!r} is not a known resource type")
        return self


# Node properties the hot store manages. Attrs live in the same property
# namespace, so an attr under one of these keys would be silently
# overwritten by the store and stripped on read — rejected at parse time
# instead (a producer bug, same posture as extra="forbid").
RESERVED_ATTR_KEYS = frozenset({"id", "applied_seq", "deleted", "deleted_seq", "phantom"})


class UpdateMessage(BaseModel):
    # Messages are immutable events; unknown fields are producer bugs (D2:
    # schema grows only via schema_version bumps, so strict parsing is safe).
    model_config = ConfigDict(frozen=True, extra="forbid")

    # Versioning: this model IS schema v1 — never widen this Literal.
    # A new version is a new model (schema_version: Literal[2] = 2), parsed
    # via a discriminated union on this field. Record it in SPEC.md as a
    # superseding decision.
    schema_version: Literal[1] = 1

    # D2: generator world-time. Aware-only — a timestamp without an offset is
    # ambiguous, and the time-travel layer cannot afford ambiguity.
    event_time: AwareDatetime

    sequence: int = Field(ge=0)
    op: Op
    resource_type: ResourceType
    resource_id: str = Field(min_length=1)
    attrs: dict[str, str | int | float | bool] = Field(default_factory=dict)
    relationships: list[Relationship] = Field(default_factory=list)

    @model_validator(mode="after")
    def _delete_carries_no_payload(self) -> Self:
        # D2: delete is a removal statement, not an update — payload is meaningless.
        if self.op is Op.DELETE and (self.attrs or self.relationships):
            raise ValueError("delete must carry empty attrs and relationships (D2)")
        return self

    @model_validator(mode="after")
    def _attrs_avoid_reserved_keys(self) -> Self:
        bad = RESERVED_ATTR_KEYS & self.attrs.keys()
        if bad:
            raise ValueError(f"attrs use store-reserved keys: {sorted(bad)}")
        return self

    @model_validator(mode="after")
    def _id_prefix_matches_type(self) -> Self:
        # loader and ingest each derive labels from one of these two (D2 addendum)
        prefix = self.resource_id.split("-", 1)[0]
        if prefix != self.resource_type.value:
            raise ValueError(
                f"resource_id prefix {prefix!r} does not match "
                f"resource_type {self.resource_type.value!r}"
            )
        return self
