"""The report contract at the harness boundary.

Strict on purpose: a report that fails to parse first try is a graded
discipline failure, so the schema gives the model nothing to negotiate.
"""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Confidence = Literal["high", "medium", "low"]


class Deferral(BaseModel):
    """Running out of evidence, not confidence (D29a addendum, #153).
    Every field is a checkable claim: a named gap that was actually
    readable this run is graded as fabrication."""

    model_config = ConfigDict(extra="forbid")

    store: Literal["hot", "cold"]
    window_start: AwareDatetime
    window_end: AwareDatetime
    would_decide: str = Field(min_length=1)

    @model_validator(mode="after")
    def _window_ordered(self) -> Self:
        if self.window_end <= self.window_start:
            raise ValueError("deferral window_end must be after window_start")
        return self


class EvidenceVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mechanism_verified: bool
    event_found: bool
    explains_symptom: bool

    @property
    def confident(self) -> bool:
        return self.mechanism_verified and self.event_found and self.explains_symptom


class TriageSuspect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=0)
    resource_id: str
    # Edge chain from the suspected cause to the alerting resource; every
    # id must have appeared in this run's context or tool results.
    mechanism_path: list[str] = Field(min_length=1)
    verdict: EvidenceVerdict
    confidence: Confidence
    evidence: list[str] = Field(min_length=1)


class TriageReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Ranked, most suspect first. Empty with no_confident_candidate=true
    # is a complete, passing answer on a quiet window.
    suspects: list[TriageSuspect]
    no_confident_candidate: bool
    degraded: bool = False
    deferral: Deferral | None = None
    narrative: str

    @model_validator(mode="after")
    def _deferral_defers(self) -> Self:
        if self.deferral is not None and not self.no_confident_candidate:
            raise ValueError(
                "a deferral asserts the question is undecidable from here; "
                "it cannot coexist with a confident candidate"
            )
        return self
