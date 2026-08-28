"""Fixture-backed, read-only telemetry observations for one IntervAI trace."""

from __future__ import annotations

import re
from datetime import datetime
from functools import partial
from typing import Any

from app.fixtures import (
    FixtureInputError,
    FixtureLoadError,
    FixtureNotFoundError,
    MalformedFixtureError,
    UnsafeFixtureError,
    load_telemetry_fixture,
)
from app.telemetry import LogLevel, LogRecord, SpanRecord, TelemetryFixture, TraceRecord
from app.tool_contracts import (
    AttributeObservation,
    EvidenceRecordType,
    EvidenceSourceReference,
    FixtureObservation,
    GetTraceResult,
    LatencyBreakdownResult,
    LogObservation,
    LogSearchFilters,
    LogSearchSummary,
    ProviderAttemptObservation,
    ProviderAttemptsResult,
    SearchLogsResult,
    SpanLatencyObservation,
    SpanObservation,
    ToolScope,
    TraceObservation,
    ValueAvailability,
    ValueOrigin,
    build_source_ref,
)

MAX_LOG_RESULTS = 50
CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")

PROVIDER_ATTRIBUTE_TYPES: dict[str, type[str | int]] = {
    "gen_ai.provider.name": str,
    "gen_ai.request.model": str,
    "gen_ai.usage.input_tokens": int,
    "gen_ai.usage.output_tokens": int,
    "provider.retry_count": int,
    "error.type": str,
    "error.message": str,
}
PROVIDER_DETECTION_ATTRIBUTES = frozenset(
    {
        "gen_ai.provider.name",
        "gen_ai.request.model",
        "gen_ai.usage.input_tokens",
        "gen_ai.usage.output_tokens",
        "provider.retry_count",
    }
)


class TelemetryToolError(ValueError):
    """Base class for stable, caller-safe telemetry tool failures."""

    code = "telemetry_tool_error"


class UnsafeToolInputError(TelemetryToolError):
    code = "unsafe_input"


class UnknownIncidentError(TelemetryToolError):
    code = "unknown_incident"


class TraceMismatchError(TelemetryToolError):
    code = "trace_mismatch"


class SpanNotFoundError(TelemetryToolError):
    code = "span_not_found"


class InvalidFilterError(TelemetryToolError):
    code = "invalid_filter"


class MalformedTelemetryError(TelemetryToolError):
    code = "malformed_fixture"


def _source(
    fixture_id: str,
    record_type: EvidenceRecordType,
    record_id: str,
    field_path: str,
) -> EvidenceSourceReference:
    return EvidenceSourceReference(
        fixture_id=fixture_id,
        record_type=record_type,
        record_id=record_id,
        field_path=field_path,
        source_ref=build_source_ref(
            fixture_id,
            record_type,
            record_id,
            field_path,
        ),
    )


def _recorded(value: Any, *sources: EvidenceSourceReference) -> dict[str, object]:
    return {
        "value": value,
        "availability": ValueAvailability.AVAILABLE,
        "origin": ValueOrigin.RECORDED,
        "sources": sources,
    }


def _calculated(value: Any, *sources: EvidenceSourceReference) -> dict[str, object]:
    return {
        "value": value,
        "availability": ValueAvailability.AVAILABLE,
        "origin": ValueOrigin.CALCULATED,
        "sources": sources,
    }


def _unavailable(source: EvidenceSourceReference) -> dict[str, object]:
    return {
        "value": None,
        "availability": ValueAvailability.UNAVAILABLE,
        "origin": ValueOrigin.UNAVAILABLE,
        "sources": (source,),
    }


def _recorded_or_unavailable(
    value: Any,
    source: EvidenceSourceReference,
) -> dict[str, object]:
    if value is None:
        return _unavailable(source)
    return _recorded(value, source)


def _validate_correlation_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not CORRELATION_ID_PATTERN.fullmatch(value):
        raise UnsafeToolInputError(f"{field_name} has an invalid or unsafe format")


def _load_exact_trace(incident_id: str, trace_id: str) -> TelemetryFixture:
    _validate_correlation_id(trace_id, "trace ID")

    try:
        fixture = load_telemetry_fixture(incident_id)
    except FixtureInputError as exc:
        raise UnsafeToolInputError(
            "incident ID has an invalid or unsafe format"
        ) from exc
    except FixtureNotFoundError as exc:
        raise UnknownIncidentError(f"unknown incident: {incident_id}") from exc
    except (MalformedFixtureError, UnsafeFixtureError) as exc:
        raise MalformedTelemetryError(
            f"incident fixture is unsafe or malformed: {incident_id}"
        ) from exc
    except FixtureLoadError as exc:
        raise MalformedTelemetryError(
            f"incident fixture could not be loaded: {incident_id}"
        ) from exc

    if fixture.telemetry.trace.trace_id != trace_id:
        raise TraceMismatchError(
            f"trace {trace_id} does not belong to incident {incident_id}"
        )
    return fixture


def _find_span(trace: TraceRecord, span_id: str) -> SpanRecord:
    _validate_correlation_id(span_id, "span ID")
    for span in trace.spans:
        if span.span_id == span_id:
            return span
    raise SpanNotFoundError(f"span {span_id} was not found in trace {trace.trace_id}")


def _scope(fixture: TelemetryFixture) -> ToolScope:
    return ToolScope(
        incident_id=fixture.incident.incident_id,
        trace_id=fixture.telemetry.trace.trace_id,
    )


def _isoformat(value: datetime) -> str:
    return value.isoformat()


def _milliseconds_between(started_at: datetime, ended_at: datetime) -> float:
    return round((ended_at - started_at).total_seconds() * 1_000, 3)


def _attribute_observations(
    fixture_id: str,
    record_type: EvidenceRecordType,
    record_id: str,
    attributes: dict[str, Any],
) -> tuple[AttributeObservation, ...]:
    return tuple(
        AttributeObservation(
            name=name,
            observation=_recorded_or_unavailable(
                value,
                _source(
                    fixture_id,
                    record_type,
                    record_id,
                    f"attributes.{name}",
                ),
            ),
        )
        for name, value in sorted(attributes.items())
    )


def _span_observation(fixture_id: str, span: SpanRecord) -> SpanObservation:
    ref = lambda field: _source(
        fixture_id,
        EvidenceRecordType.SPAN,
        span.span_id,
        field,
    )
    return SpanObservation(
        span_id=span.span_id,
        source=ref("record"),
        parent_span_id=_recorded_or_unavailable(
            span.parent_span_id,
            ref("parent_span_id"),
        ),
        name=_recorded(span.name, ref("name")),
        kind=_recorded(span.kind.value, ref("kind")),
        started_at=_recorded(_isoformat(span.started_at), ref("started_at")),
        ended_at=_recorded(_isoformat(span.ended_at), ref("ended_at")),
        duration_ms=_recorded(span.duration_ms, ref("duration_ms")),
        status=_recorded(span.status.value, ref("status")),
        attributes=_attribute_observations(
            fixture_id,
            EvidenceRecordType.SPAN,
            span.span_id,
            span.attributes,
        ),
    )


def get_trace(incident_id: str, trace_id: str) -> GetTraceResult:
    """Return the exact validated trace and its spans without changing evidence."""

    fixture = _load_exact_trace(incident_id, trace_id)
    trace = fixture.telemetry.trace
    fixture_id = fixture.metadata.fixture_id

    fixture_ref = lambda field: _source(
        fixture_id,
        EvidenceRecordType.FIXTURE,
        fixture_id,
        field,
    )
    trace_ref = lambda field: _source(
        fixture_id,
        EvidenceRecordType.TRACE,
        trace.trace_id,
        field,
    )

    return GetTraceResult(
        scope=_scope(fixture),
        fixture=FixtureObservation(
            source=fixture_ref("record"),
            telemetry_origin=_recorded(
                fixture.metadata.telemetry_origin.value,
                fixture_ref("metadata.telemetry_origin"),
            ),
            notice=_recorded(
                fixture.metadata.notice,
                fixture_ref("metadata.notice"),
            ),
        ),
        trace=TraceObservation(
            trace_id=trace.trace_id,
            source=trace_ref("record"),
            request_id=_recorded(trace.request_id, trace_ref("request_id")),
            started_at=_recorded(
                _isoformat(trace.started_at),
                trace_ref("started_at"),
            ),
            ended_at=_recorded(_isoformat(trace.ended_at), trace_ref("ended_at")),
            duration_ms=_recorded(trace.duration_ms, trace_ref("duration_ms")),
            status=_recorded(trace.status.value, trace_ref("status")),
        ),
        spans=tuple(_span_observation(fixture_id, span) for span in trace.spans),
    )


def get_latency_breakdown(
    incident_id: str,
    trace_id: str,
    span_id: str | None = None,
) -> LatencyBreakdownResult:
    """Return recorded span timings plus explicitly calculated trace-relative values."""

    fixture = _load_exact_trace(incident_id, trace_id)
    trace = fixture.telemetry.trace
    fixture_id = fixture.metadata.fixture_id
    spans = trace.spans if span_id is None else [_find_span(trace, span_id)]

    trace_started_ref = _source(
        fixture_id,
        EvidenceRecordType.TRACE,
        trace.trace_id,
        "started_at",
    )
    trace_duration_ref = _source(
        fixture_id,
        EvidenceRecordType.TRACE,
        trace.trace_id,
        "duration_ms",
    )

    observations: list[SpanLatencyObservation] = []
    for span in sorted(spans, key=lambda item: (item.started_at, item.span_id)):
        span_ref = partial(
            _source,
            fixture_id,
            EvidenceRecordType.SPAN,
            span.span_id,
        )
        started_ref = span_ref("started_at")
        ended_ref = span_ref("ended_at")
        duration_ref = span_ref("duration_ms")
        observations.append(
            SpanLatencyObservation(
                span_id=span.span_id,
                source=span_ref("record"),
                parent_span_id=_recorded_or_unavailable(
                    span.parent_span_id,
                    span_ref("parent_span_id"),
                ),
                name=_recorded(span.name, span_ref("name")),
                recorded_duration_ms=_recorded(span.duration_ms, duration_ref),
                start_offset_ms=_calculated(
                    _milliseconds_between(trace.started_at, span.started_at),
                    trace_started_ref,
                    started_ref,
                ),
                end_offset_ms=_calculated(
                    _milliseconds_between(trace.started_at, span.ended_at),
                    trace_started_ref,
                    ended_ref,
                ),
                coverage_of_trace_percent=_calculated(
                    round((span.duration_ms / trace.duration_ms) * 100, 6),
                    duration_ref,
                    trace_duration_ref,
                ),
            )
        )

    return LatencyBreakdownResult(
        scope=_scope(fixture),
        trace_duration_ms=_recorded(trace.duration_ms, trace_duration_ref),
        spans=tuple(observations),
    )


def _provider_attribute(
    fixture_id: str,
    span: SpanRecord,
    attribute_name: str,
) -> dict[str, object]:
    source = _source(
        fixture_id,
        EvidenceRecordType.SPAN,
        span.span_id,
        f"attributes.{attribute_name}",
    )
    if attribute_name not in span.attributes or span.attributes[attribute_name] is None:
        return _unavailable(source)

    value = span.attributes[attribute_name]
    expected_type = PROVIDER_ATTRIBUTE_TYPES[attribute_name]
    if type(value) is not expected_type:
        raise MalformedTelemetryError(
            f"provider attribute {attribute_name} has an invalid recorded type"
        )
    return _recorded(value, source)


def get_provider_attempts(incident_id: str, trace_id: str) -> ProviderAttemptsResult:
    """Extract provider spans without inferring unrecorded attempts or attributes."""

    fixture = _load_exact_trace(incident_id, trace_id)
    trace = fixture.telemetry.trace
    fixture_id = fixture.metadata.fixture_id
    provider_spans = [
        span
        for span in trace.spans
        if any(name in span.attributes for name in PROVIDER_DETECTION_ATTRIBUTES)
    ]

    attempts: list[ProviderAttemptObservation] = []
    for span in sorted(
        provider_spans, key=lambda item: (item.started_at, item.span_id)
    ):
        span_ref = partial(
            _source,
            fixture_id,
            EvidenceRecordType.SPAN,
            span.span_id,
        )
        attempts.append(
            ProviderAttemptObservation(
                span_id=span.span_id,
                source=span_ref("record"),
                started_at=_recorded(
                    _isoformat(span.started_at),
                    span_ref("started_at"),
                ),
                ended_at=_recorded(
                    _isoformat(span.ended_at),
                    span_ref("ended_at"),
                ),
                duration_ms=_recorded(span.duration_ms, span_ref("duration_ms")),
                status=_recorded(span.status.value, span_ref("status")),
                provider_name=_provider_attribute(
                    fixture_id,
                    span,
                    "gen_ai.provider.name",
                ),
                model=_provider_attribute(
                    fixture_id,
                    span,
                    "gen_ai.request.model",
                ),
                input_tokens=_provider_attribute(
                    fixture_id,
                    span,
                    "gen_ai.usage.input_tokens",
                ),
                output_tokens=_provider_attribute(
                    fixture_id,
                    span,
                    "gen_ai.usage.output_tokens",
                ),
                retry_count=_provider_attribute(
                    fixture_id,
                    span,
                    "provider.retry_count",
                ),
                error_type=_provider_attribute(fixture_id, span, "error.type"),
                error_message=_provider_attribute(fixture_id, span, "error.message"),
            )
        )

    return ProviderAttemptsResult(scope=_scope(fixture), attempts=tuple(attempts))


def _parse_log_level(level: LogLevel | str | None) -> LogLevel | None:
    if level is None or isinstance(level, LogLevel):
        return level
    if not isinstance(level, str):
        raise InvalidFilterError("log level must be a supported string value")
    try:
        return LogLevel(level)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in LogLevel)
        raise InvalidFilterError(f"log level must be one of: {allowed}") from exc


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= MAX_LOG_RESULTS:
        raise InvalidFilterError(
            f"limit must be an integer between 1 and {MAX_LOG_RESULTS}"
        )


def _log_observation(fixture_id: str, log: LogRecord) -> LogObservation:
    log_ref = lambda field: _source(
        fixture_id,
        EvidenceRecordType.LOG,
        log.log_id,
        field,
    )
    return LogObservation(
        log_id=log.log_id,
        source=log_ref("record"),
        observed_at=_recorded(_isoformat(log.observed_at), log_ref("observed_at")),
        level=_recorded(log.level.value, log_ref("level")),
        message=_recorded(log.message, log_ref("message")),
        span_id=_recorded_or_unavailable(log.span_id, log_ref("span_id")),
        attributes=_attribute_observations(
            fixture_id,
            EvidenceRecordType.LOG,
            log.log_id,
            log.attributes,
        ),
    )


def search_logs(
    incident_id: str,
    trace_id: str,
    *,
    span_id: str | None = None,
    level: LogLevel | str | None = None,
    limit: int = 20,
) -> SearchLogsResult:
    """Return a bounded set of fixture logs for exact correlation filters."""

    _validate_limit(limit)
    parsed_level = _parse_log_level(level)
    fixture = _load_exact_trace(incident_id, trace_id)
    trace = fixture.telemetry.trace
    fixture_id = fixture.metadata.fixture_id

    if span_id is not None:
        _find_span(trace, span_id)

    matches = [
        log
        for log in fixture.telemetry.logs
        if (span_id is None or log.span_id == span_id)
        and (parsed_level is None or log.level is parsed_level)
    ]
    matches.sort(key=lambda item: (item.observed_at, item.log_id))
    returned_logs = matches[:limit]

    return SearchLogsResult(
        scope=_scope(fixture),
        filters=LogSearchFilters(
            span_id=span_id,
            level=parsed_level,
            limit=limit,
        ),
        summary=LogSearchSummary(
            source=_source(
                fixture_id,
                EvidenceRecordType.TRACE,
                trace.trace_id,
                "logs",
            ),
            matched_count=len(matches),
            returned_count=len(returned_logs),
            truncated=len(returned_logs) < len(matches),
        ),
        logs=tuple(_log_observation(fixture_id, log) for log in returned_logs),
    )
