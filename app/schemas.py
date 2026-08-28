"""Validated contracts shared across the TraceOps investigation workflow."""

from __future__ import annotations

from datetime import timedelta
from enum import Enum
from typing import Any, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

MAX_TIME_WINDOW = timedelta(hours=24)


class StrictModel(BaseModel):
    """Reject unknown fields so malformed telemetry is not silently accepted."""

    model_config = ConfigDict(extra="forbid")


class Environment(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class IncidentClass(str, Enum):
    SLOW_LLM_REQUEST = "slow_llm_request"
    MODEL_PROVIDER_FAILURE = "model_provider_failure"
    INCORRECT_FALLBACK_ROUTING = "incorrect_fallback_routing"


class InvestigationStatus(str, Enum):
    DIAGNOSED = "diagnosed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class ConfidenceLabel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceSourceType(str, Enum):
    TRACE = "trace"
    LOG = "log"
    METRIC = "metric"
    ROUTING_RECORD = "routing_record"
    CONFIG_SNAPSHOT = "config_snapshot"
    RUNBOOK = "runbook"


class EvidenceReliability(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class IncidentIntake(StrictModel):
    """Validated user input that identifies one bounded incident."""

    incident_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.-]+$")
    service: str = Field(default="intervai", pattern=r"^intervai$")
    environment: Environment
    symptom: str = Field(min_length=5, max_length=500)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    trace_id: str | None = Field(default=None, min_length=1, max_length=200)
    started_at: AwareDatetime | None = None
    ended_at: AwareDatetime | None = None
    task_type: str | None = Field(default=None, min_length=1, max_length=100)
    expected_model: str | None = Field(default=None, min_length=1, max_length=200)
    observed_model: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_correlation_scope(self) -> Self:
        has_window_start = self.started_at is not None
        has_window_end = self.ended_at is not None

        if has_window_start != has_window_end:
            raise ValueError("started_at and ended_at must be supplied together")

        if not (self.request_id or self.trace_id or has_window_start):
            raise ValueError(
                "provide request_id, trace_id, or a complete bounded time window"
            )

        if self.started_at is not None and self.ended_at is not None:
            window = self.ended_at - self.started_at
            if window <= timedelta(0):
                raise ValueError("ended_at must be later than started_at")
            if window > MAX_TIME_WINDOW:
                raise ValueError("incident time window cannot exceed 24 hours")

        return self


class EvidenceItem(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=100)
    source_type: EvidenceSourceType
    source_ref: str = Field(min_length=1, max_length=500)
    observed_at: AwareDatetime | None = None
    content: str | dict[str, Any]
    relevance: str = Field(min_length=1, max_length=1_000)
    reliability: EvidenceReliability


class Confidence(StrictModel):
    score: float = Field(ge=0.0, le=1.0)
    label: ConfidenceLabel


class Hypothesis(StrictModel):
    hypothesis_id: str = Field(min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=1_000)
    expected_evidence: list[str] = Field(default_factory=list)
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    confidence: Confidence


class TimelineEvent(StrictModel):
    occurred_at: AwareDatetime
    description: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[str] = Field(min_length=1)


class Recommendation(StrictModel):
    priority: int = Field(ge=1, le=5)
    description: str = Field(min_length=1, max_length=1_000)
    approval_required: bool = True


class Citation(StrictModel):
    evidence_id: str = Field(min_length=1, max_length=100)
    source_ref: str = Field(min_length=1, max_length=500)


class InvestigationMetadata(StrictModel):
    duration_ms: int = Field(ge=0)
    tool_call_count: int = Field(ge=0, le=12)
    workflow_version: str = Field(min_length=1, max_length=100)
    model_metadata: dict[str, Any] = Field(default_factory=dict)


class InvestigationReport(StrictModel):
    incident_id: str = Field(min_length=1, max_length=100)
    status: InvestigationStatus
    incident_class: IncidentClass | None = None
    executive_summary: str = Field(min_length=1, max_length=2_000)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=4)
    likely_root_cause: str | None = Field(default=None, max_length=2_000)
    confidence: Confidence
    recommendations: list[Recommendation] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    investigation_metadata: InvestigationMetadata

    @model_validator(mode="after")
    def validate_diagnosis_policy(self) -> Self:
        if self.status is InvestigationStatus.DIAGNOSED:
            if not self.likely_root_cause:
                raise ValueError("a diagnosed report requires a likely root cause")
            if self.confidence.label is ConfidenceLabel.LOW:
                raise ValueError("a low-confidence report cannot claim a diagnosis")

        if (
            self.status is InvestigationStatus.INSUFFICIENT_EVIDENCE
            and self.likely_root_cause is not None
        ):
            raise ValueError(
                "an insufficient-evidence report cannot claim a likely root cause"
            )

        return self


class HealthResponse(StrictModel):
    status: str
    service: str
    version: str
