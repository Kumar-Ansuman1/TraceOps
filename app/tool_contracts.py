"""Strict, immutable output contracts for fixture-backed telemetry tools."""

from __future__ import annotations

from enum import Enum
from typing import Generic, Literal, TypeVar
from urllib.parse import quote

from pydantic import ConfigDict, Field, JsonValue, model_validator

from app.schemas import StrictModel
from app.telemetry import LogLevel

T = TypeVar("T")


class ToolContract(StrictModel):
    """Reject unknown output fields and prevent result mutation after creation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class EvidenceRecordType(str, Enum):
    FIXTURE = "fixture"
    TRACE = "trace"
    SPAN = "span"
    LOG = "log"


class ValueAvailability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class ValueOrigin(str, Enum):
    RECORDED = "recorded"
    CALCULATED = "calculated"
    UNAVAILABLE = "unavailable"


def build_source_ref(
    fixture_id: str,
    record_type: EvidenceRecordType,
    record_id: str,
    field_path: str,
) -> str:
    """Build a stable reference without relying on a fixture's list ordering."""

    encoded_record_id = quote(record_id, safe="")
    encoded_field_path = quote(field_path, safe="._-")
    return (
        f"fixture://{fixture_id}/{record_type.value}/{encoded_record_id}"
        f"#{encoded_field_path}"
    )


class EvidenceSourceReference(ToolContract):
    fixture_id: str = Field(pattern=r"^INC-[A-Z0-9-]+$")
    record_type: EvidenceRecordType
    record_id: str = Field(min_length=1, max_length=200)
    field_path: str = Field(min_length=1, max_length=500)
    source_ref: str = Field(min_length=1, max_length=1_000)

    @model_validator(mode="after")
    def validate_canonical_reference(self) -> EvidenceSourceReference:
        expected = build_source_ref(
            self.fixture_id,
            self.record_type,
            self.record_id,
            self.field_path,
        )
        if self.source_ref != expected:
            raise ValueError("source_ref is not the canonical fixture record reference")
        return self


class ObservedValue(ToolContract, Generic[T]):
    value: T | None
    availability: ValueAvailability
    origin: ValueOrigin
    sources: tuple[EvidenceSourceReference, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_availability(self) -> ObservedValue[T]:
        if self.availability is ValueAvailability.AVAILABLE:
            if self.value is None:
                raise ValueError("an available observation requires a value")
            if self.origin is ValueOrigin.UNAVAILABLE:
                raise ValueError(
                    "an available observation needs a recorded or calculated origin"
                )
            return self

        if self.value is not None or self.origin is not ValueOrigin.UNAVAILABLE:
            raise ValueError(
                "an unavailable observation must contain null and use unavailable origin"
            )
        return self


class ToolScope(ToolContract):
    incident_id: str = Field(pattern=r"^INC-[A-Z0-9-]+$")
    trace_id: str = Field(min_length=1, max_length=200)


class FixtureObservation(ToolContract):
    source: EvidenceSourceReference
    telemetry_origin: ObservedValue[str]
    notice: ObservedValue[str]


class AttributeObservation(ToolContract):
    name: str = Field(min_length=1, max_length=500)
    observation: ObservedValue[JsonValue]


class TraceObservation(ToolContract):
    trace_id: str = Field(min_length=1, max_length=200)
    source: EvidenceSourceReference
    request_id: ObservedValue[str]
    started_at: ObservedValue[str]
    ended_at: ObservedValue[str]
    duration_ms: ObservedValue[float]
    status: ObservedValue[str]


class SpanObservation(ToolContract):
    span_id: str = Field(min_length=1, max_length=200)
    source: EvidenceSourceReference
    parent_span_id: ObservedValue[str]
    name: ObservedValue[str]
    kind: ObservedValue[str]
    started_at: ObservedValue[str]
    ended_at: ObservedValue[str]
    duration_ms: ObservedValue[float]
    status: ObservedValue[str]
    attributes: tuple[AttributeObservation, ...]


class GetTraceResult(ToolContract):
    tool: Literal["get_trace"] = "get_trace"
    scope: ToolScope
    fixture: FixtureObservation
    trace: TraceObservation
    spans: tuple[SpanObservation, ...] = Field(min_length=1)


class SpanLatencyObservation(ToolContract):
    span_id: str = Field(min_length=1, max_length=200)
    source: EvidenceSourceReference
    parent_span_id: ObservedValue[str]
    name: ObservedValue[str]
    recorded_duration_ms: ObservedValue[float]
    start_offset_ms: ObservedValue[float]
    end_offset_ms: ObservedValue[float]
    coverage_of_trace_percent: ObservedValue[float]


class LatencyBreakdownResult(ToolContract):
    tool: Literal["get_latency_breakdown"] = "get_latency_breakdown"
    scope: ToolScope
    trace_duration_ms: ObservedValue[float]
    spans: tuple[SpanLatencyObservation, ...] = Field(min_length=1)


class ProviderAttemptObservation(ToolContract):
    span_id: str = Field(min_length=1, max_length=200)
    source: EvidenceSourceReference
    started_at: ObservedValue[str]
    ended_at: ObservedValue[str]
    duration_ms: ObservedValue[float]
    status: ObservedValue[str]
    provider_name: ObservedValue[str]
    model: ObservedValue[str]
    input_tokens: ObservedValue[int]
    output_tokens: ObservedValue[int]
    retry_count: ObservedValue[int]
    error_type: ObservedValue[str]
    error_message: ObservedValue[str]


class ProviderAttemptsResult(ToolContract):
    tool: Literal["get_provider_attempts"] = "get_provider_attempts"
    scope: ToolScope
    attempts: tuple[ProviderAttemptObservation, ...]


class LogObservation(ToolContract):
    log_id: str = Field(min_length=1, max_length=200)
    source: EvidenceSourceReference
    observed_at: ObservedValue[str]
    level: ObservedValue[str]
    message: ObservedValue[str]
    span_id: ObservedValue[str]
    attributes: tuple[AttributeObservation, ...]


class LogSearchFilters(ToolContract):
    span_id: str | None = Field(default=None, min_length=1, max_length=200)
    level: LogLevel | None = None
    limit: int = Field(ge=1)


class LogSearchSummary(ToolContract):
    source: EvidenceSourceReference
    matched_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_counts(self) -> LogSearchSummary:
        if self.returned_count > self.matched_count:
            raise ValueError("returned_count cannot exceed matched_count")
        if self.truncated != (self.returned_count < self.matched_count):
            raise ValueError("truncated must match the returned and matched counts")
        return self


class SearchLogsResult(ToolContract):
    tool: Literal["search_logs"] = "search_logs"
    scope: ToolScope
    filters: LogSearchFilters
    summary: LogSearchSummary
    logs: tuple[LogObservation, ...]
