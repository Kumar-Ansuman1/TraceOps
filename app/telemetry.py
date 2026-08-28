"""Strict contracts for recorded or explicitly synthetic telemetry fixtures."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Self

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from app.schemas import Environment, StrictModel


class TelemetryOrigin(str, Enum):
    RECORDED = "recorded"
    SYNTHETIC = "synthetic"


class TelemetryStatus(str, Enum):
    OK = "ok"
    ERROR = "error"


class SpanKind(str, Enum):
    SERVER = "server"
    INTERNAL = "internal"
    CLIENT = "client"


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FixtureMetadata(StrictModel):
    schema_version: Literal["1.0"]
    fixture_id: str = Field(pattern=r"^INC-[A-Z0-9-]+$")
    telemetry_origin: TelemetryOrigin
    redacted: bool
    contains_sensitive_data: bool
    notice: str = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def validate_safety_markers(self) -> Self:
        if not self.redacted:
            raise ValueError("telemetry fixtures must be marked as redacted")
        if self.contains_sensitive_data:
            raise ValueError("telemetry fixtures cannot contain sensitive data")
        if (
            self.telemetry_origin is TelemetryOrigin.SYNTHETIC
            and "synthetic" not in self.notice.lower()
        ):
            raise ValueError("synthetic fixtures require an explicit synthetic notice")
        return self


class IncidentTelemetryContext(StrictModel):
    incident_id: str = Field(pattern=r"^INC-[A-Z0-9-]+$")
    service: Literal["intervai"]
    environment: Environment
    task_type: Literal["answer_analysis"]
    symptom: str = Field(min_length=5, max_length=500)
    request_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    started_at: AwareDatetime
    ended_at: AwareDatetime

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.ended_at <= self.started_at:
            raise ValueError("incident ended_at must be later than started_at")
        return self


class SpanRecord(StrictModel):
    span_id: str = Field(min_length=1, max_length=200)
    parent_span_id: str | None = Field(default=None, min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=300)
    kind: SpanKind
    started_at: AwareDatetime
    ended_at: AwareDatetime
    duration_ms: float = Field(gt=0)
    status: TelemetryStatus
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_timing(self) -> Self:
        if self.ended_at <= self.started_at:
            raise ValueError("span ended_at must be later than started_at")
        actual_duration_ms = (self.ended_at - self.started_at).total_seconds() * 1000
        if abs(actual_duration_ms - self.duration_ms) > 1.0:
            raise ValueError("span duration_ms does not match its timestamps")
        return self


class TraceRecord(StrictModel):
    trace_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    started_at: AwareDatetime
    ended_at: AwareDatetime
    duration_ms: float = Field(gt=0)
    status: TelemetryStatus
    spans: list[SpanRecord] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_trace_structure(self) -> Self:
        if self.ended_at <= self.started_at:
            raise ValueError("trace ended_at must be later than started_at")

        actual_duration_ms = (self.ended_at - self.started_at).total_seconds() * 1000
        if abs(actual_duration_ms - self.duration_ms) > 1.0:
            raise ValueError("trace duration_ms does not match its timestamps")

        span_ids = [span.span_id for span in self.spans]
        if len(span_ids) != len(set(span_ids)):
            raise ValueError("trace span IDs must be unique")

        known_span_ids = set(span_ids)
        root_span_ids = {
            span.span_id for span in self.spans if span.parent_span_id is None
        }
        if len(root_span_ids) != 1:
            raise ValueError("trace must contain exactly one root span")

        parents = {span.span_id: span.parent_span_id for span in self.spans}
        for span in self.spans:
            if (
                span.parent_span_id is not None
                and span.parent_span_id not in known_span_ids
            ):
                raise ValueError(f"span {span.span_id} references an unknown parent")
            if span.started_at < self.started_at or span.ended_at > self.ended_at:
                raise ValueError(f"span {span.span_id} falls outside trace bounds")

            visited: set[str] = set()
            current_span_id: str | None = span.span_id
            while current_span_id is not None:
                if current_span_id in visited:
                    raise ValueError("trace span hierarchy contains a cycle")
                visited.add(current_span_id)
                current_span_id = parents[current_span_id]

            if not (visited & root_span_ids):
                raise ValueError("every span must descend from the root span")

        return self


class LogRecord(StrictModel):
    log_id: str = Field(min_length=1, max_length=200)
    observed_at: AwareDatetime
    level: LogLevel
    message: str = Field(min_length=1, max_length=1_000)
    span_id: str | None = Field(default=None, min_length=1, max_length=200)
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


class TelemetryBundle(StrictModel):
    trace: TraceRecord
    logs: list[LogRecord] = Field(default_factory=list)


class TelemetryFixture(StrictModel):
    """Investigator-visible telemetry with no evaluation labels or root cause."""

    metadata: FixtureMetadata
    incident: IncidentTelemetryContext
    telemetry: TelemetryBundle

    @model_validator(mode="after")
    def validate_cross_references(self) -> Self:
        trace = self.telemetry.trace
        if self.metadata.fixture_id != self.incident.incident_id:
            raise ValueError("fixture ID must match incident ID")
        if self.incident.trace_id != trace.trace_id:
            raise ValueError("incident trace ID must match telemetry trace ID")
        if self.incident.request_id != trace.request_id:
            raise ValueError("incident request ID must match telemetry request ID")
        if (
            self.incident.started_at != trace.started_at
            or self.incident.ended_at != trace.ended_at
        ):
            raise ValueError("incident time bounds must match telemetry trace bounds")

        span_ids = {span.span_id for span in trace.spans}
        for log in self.telemetry.logs:
            if log.span_id is not None and log.span_id not in span_ids:
                raise ValueError(f"log {log.log_id} references an unknown span")
            if not trace.started_at <= log.observed_at <= trace.ended_at:
                raise ValueError(f"log {log.log_id} falls outside trace bounds")

        if self.metadata.telemetry_origin is TelemetryOrigin.SYNTHETIC:
            identifiers = [
                self.incident.request_id,
                self.incident.trace_id,
                *(span.span_id for span in trace.spans),
                *(log.log_id for log in self.telemetry.logs),
            ]
            if any(
                not identifier.startswith("synthetic-") for identifier in identifiers
            ):
                raise ValueError(
                    "synthetic telemetry identifiers require synthetic- prefixes"
                )

        return self
