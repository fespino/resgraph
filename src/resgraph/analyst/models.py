"""The report contract at the harness boundary.

Strict on purpose: a report that fails to parse first try is a graded
discipline failure, so the schema gives the model nothing to negotiate.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]


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
    narrative: str
