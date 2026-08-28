"""Deterministic fixture-backed evidence collection for one incident trace."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from hashlib import sha256

from pydantic import BaseModel

from app import telemetry_tools
from app.evidence_contracts import (
    FATAL_COLLECTION_ERROR_CODES,
    TOOL_EXECUTION_ORDER,
    CitationCatalog,
    CitationCatalogEntry,
    CollectionErrorCode,
    CollectionStatus,
    EvidenceCollectionResult,
    EvidenceCollectionScope,
    TelemetryToolName,
    TelemetryToolResult,
    TimelineEventKind,
    TimelineObservation,
    ToolExecutionRecord,
    ToolExecutionStatus,
    UnavailableFieldObservation,
    timeline_sort_key,
)
from app.telemetry_tools import TelemetryToolError
from app.tool_contracts import (
    EvidenceRecordType,
    EvidenceSourceReference,
    GetTraceResult,
    ObservedValue,
    SearchLogsResult,
    ValueAvailability,
    ValueOrigin,
)

SAFE_ERROR_MESSAGES: dict[CollectionErrorCode, str] = {
    CollectionErrorCode.UNSAFE_INPUT: (
        "The incident or trace scope has an invalid or unsafe format."
    ),
    CollectionErrorCode.UNKNOWN_INCIDENT: (
        "No telemetry fixture exists for the requested incident."
    ),
    CollectionErrorCode.TRACE_MISMATCH: (
        "The requested trace does not belong to the requested incident."
    ),
    CollectionErrorCode.MALFORMED_FIXTURE: (
        "The incident telemetry fixture is unsafe or malformed."
    ),
    CollectionErrorCode.SPAN_NOT_FOUND: (
        "A requested span was not found in the trace."
    ),
    CollectionErrorCode.INVALID_FILTER: "A telemetry tool filter is invalid.",
    CollectionErrorCode.TELEMETRY_TOOL_ERROR: (
        "A telemetry tool could not complete the request."
    ),
    CollectionErrorCode.TOOL_EXECUTION_FAILED: (
        "A telemetry tool failed unexpectedly."
    ),
}


def _stable_id(prefix: str, payload: str) -> str:
    digest = sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _call_id(
    scope: EvidenceCollectionScope,
    tool_name: TelemetryToolName,
    execution_order: int,
) -> str:
    payload = (
        f"{scope.incident_id}\0{scope.trace_id}\0{execution_order}\0{tool_name.value}"
    )
    return f"call-{execution_order:02d}-{tool_name.value}-{sha256(payload.encode()).hexdigest()[:16]}"


def _call_tool(
    tool_name: TelemetryToolName,
    scope: EvidenceCollectionScope,
) -> TelemetryToolResult:
    if tool_name is TelemetryToolName.GET_TRACE:
        return telemetry_tools.get_trace(scope.incident_id, scope.trace_id)
    if tool_name is TelemetryToolName.GET_LATENCY_BREAKDOWN:
        return telemetry_tools.get_latency_breakdown(
            scope.incident_id,
            scope.trace_id,
        )
    if tool_name is TelemetryToolName.GET_PROVIDER_ATTEMPTS:
        return telemetry_tools.get_provider_attempts(
            scope.incident_id,
            scope.trace_id,
        )
    return telemetry_tools.search_logs(
        scope.incident_id,
        scope.trace_id,
        limit=20,
    )


def _source_references(value: object) -> tuple[EvidenceSourceReference, ...]:
    references: dict[str, EvidenceSourceReference] = {}

    def visit(item: object) -> None:
        if isinstance(item, EvidenceSourceReference):
            references.setdefault(item.source_ref, item)
            return
        if isinstance(item, BaseModel):
            for field_name in type(item).model_fields:
                visit(getattr(item, field_name))
            return
        if isinstance(item, Mapping):
            for child in item.values():
                visit(child)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return tuple(references[source_ref] for source_ref in sorted(references))


def _normalise_tool_error(error: TelemetryToolError) -> CollectionErrorCode:
    try:
        return CollectionErrorCode(error.code)
    except ValueError:
        return CollectionErrorCode.TELEMETRY_TOOL_ERROR


def _build_citation_catalog(
    observations: tuple[TelemetryToolResult, ...],
) -> CitationCatalog:
    entries = tuple(
        CitationCatalogEntry(
            citation_id=_stable_id("citation", source.source_ref),
            source=source,
        )
        for source in _source_references(observations)
    )
    return CitationCatalog(entries=entries)


def _build_unavailable_fields(
    observations: tuple[TelemetryToolResult, ...],
) -> tuple[UnavailableFieldObservation, ...]:
    sources: dict[str, EvidenceSourceReference] = {}
    observed_by: dict[str, set[TelemetryToolName]] = {}

    def visit(item: object, tool_name: TelemetryToolName) -> None:
        if isinstance(item, ObservedValue):
            if item.availability is ValueAvailability.UNAVAILABLE:
                for source in item.sources:
                    sources.setdefault(source.source_ref, source)
                    observed_by.setdefault(source.source_ref, set()).add(tool_name)
            return
        if isinstance(item, BaseModel):
            for field_name in type(item).model_fields:
                visit(getattr(item, field_name), tool_name)
            return
        if isinstance(item, Mapping):
            for child in item.values():
                visit(child, tool_name)
            return
        if isinstance(item, (list, tuple)):
            for child in item:
                visit(child, tool_name)

    for observation in observations:
        tool_name = TelemetryToolName(observation.tool)
        visit(observation, tool_name)

    return tuple(
        UnavailableFieldObservation(
            unavailable_id=_stable_id("unavailable", source_ref),
            source=sources[source_ref],
            observed_by=tuple(
                tool_name
                for tool_name in TOOL_EXECUTION_ORDER
                if tool_name in observed_by[source_ref]
            ),
        )
        for source_ref in sorted(sources)
    )


def _recorded_datetime(observation: ObservedValue[str]) -> datetime | None:
    if (
        observation.availability is not ValueAvailability.AVAILABLE
        or observation.origin is not ValueOrigin.RECORDED
        or observation.value is None
    ):
        return None
    occurred_at = datetime.fromisoformat(observation.value)
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        return None
    return occurred_at


def _sorted_source_refs(*values: object) -> tuple[str, ...]:
    return tuple(source.source_ref for source in _source_references(values))


def _timeline_observation(
    occurred_at: datetime,
    event_kind: TimelineEventKind,
    record_type: EvidenceRecordType,
    record_id: str,
    description: str,
    source_refs: tuple[str, ...],
) -> TimelineObservation:
    identity = f"{event_kind.value}\0{record_type.value}\0{record_id}\0{occurred_at.isoformat()}"
    return TimelineObservation(
        timeline_id=_stable_id(f"timeline-{event_kind.value}", identity),
        occurred_at=occurred_at,
        event_kind=event_kind,
        record_type=record_type,
        record_id=record_id,
        description=description,
        source_refs=source_refs,
    )


def _build_timeline(
    observations: tuple[TelemetryToolResult, ...],
) -> tuple[TimelineObservation, ...]:
    events: list[TimelineObservation] = []
    trace_result = next(
        (
            observation
            for observation in observations
            if isinstance(observation, GetTraceResult)
        ),
        None,
    )
    if trace_result is not None:
        trace = trace_result.trace
        trace_started_at = _recorded_datetime(trace.started_at)
        if trace_started_at is not None:
            events.append(
                _timeline_observation(
                    trace_started_at,
                    TimelineEventKind.TRACE_STARTED,
                    EvidenceRecordType.TRACE,
                    trace.trace_id,
                    f"Trace {trace.trace_id} started.",
                    _sorted_source_refs(trace.source, trace.started_at),
                )
            )

        trace_ended_at = _recorded_datetime(trace.ended_at)
        if trace_ended_at is not None:
            events.append(
                _timeline_observation(
                    trace_ended_at,
                    TimelineEventKind.TRACE_ENDED,
                    EvidenceRecordType.TRACE,
                    trace.trace_id,
                    f"Trace {trace.trace_id} ended.",
                    _sorted_source_refs(trace.source, trace.ended_at),
                )
            )

        for span in trace_result.spans:
            span_name = span.name.value
            started_at = _recorded_datetime(span.started_at)
            if started_at is not None:
                events.append(
                    _timeline_observation(
                        started_at,
                        TimelineEventKind.SPAN_STARTED,
                        EvidenceRecordType.SPAN,
                        span.span_id,
                        f'Span {span.span_id} ("{span_name}") started.',
                        _sorted_source_refs(
                            span.source,
                            span.name,
                            span.started_at,
                        ),
                    )
                )

            ended_at = _recorded_datetime(span.ended_at)
            if ended_at is not None:
                events.append(
                    _timeline_observation(
                        ended_at,
                        TimelineEventKind.SPAN_ENDED,
                        EvidenceRecordType.SPAN,
                        span.span_id,
                        f'Span {span.span_id} ("{span_name}") ended.',
                        _sorted_source_refs(
                            span.source,
                            span.name,
                            span.ended_at,
                        ),
                    )
                )

    logs_result = next(
        (
            observation
            for observation in observations
            if isinstance(observation, SearchLogsResult)
        ),
        None,
    )
    if logs_result is not None:
        for log in logs_result.logs:
            observed_at = _recorded_datetime(log.observed_at)
            if observed_at is None:
                continue
            events.append(
                _timeline_observation(
                    observed_at,
                    TimelineEventKind.LOG_RECORDED,
                    EvidenceRecordType.LOG,
                    log.log_id,
                    (
                        f"Log {log.log_id} recorded at {log.level.value} level: "
                        f"{log.message.value}"
                    ),
                    _sorted_source_refs(
                        log.source,
                        log.observed_at,
                        log.level,
                        log.message,
                    ),
                )
            )

    return tuple(sorted(events, key=timeline_sort_key))


def collect_evidence(incident_id: str, trace_id: str) -> EvidenceCollectionResult:
    """Collect fixture observations once per tool in a fixed deterministic order."""

    scope = EvidenceCollectionScope(incident_id=incident_id, trace_id=trace_id)
    observations: list[TelemetryToolResult] = []
    executions: list[ToolExecutionRecord] = []
    fatal_failure = False

    for execution_order, tool_name in enumerate(TOOL_EXECUTION_ORDER, start=1):
        call_id = _call_id(scope, tool_name, execution_order)
        try:
            observation = _call_tool(tool_name, scope)
        except TelemetryToolError as error:
            error_code = _normalise_tool_error(error)
            fatal_failure = error_code in FATAL_COLLECTION_ERROR_CODES
            executions.append(
                ToolExecutionRecord(
                    call_id=call_id,
                    tool_name=tool_name,
                    execution_order=execution_order,
                    scope=scope,
                    status=ToolExecutionStatus.FAILED,
                    error_code=error_code,
                    error_message=SAFE_ERROR_MESSAGES[error_code],
                    returned_source_refs=(),
                )
            )
            break
        except Exception:  # noqa: BLE001 - convert tool-boundary failures safely
            error_code = CollectionErrorCode.TOOL_EXECUTION_FAILED
            executions.append(
                ToolExecutionRecord(
                    call_id=call_id,
                    tool_name=tool_name,
                    execution_order=execution_order,
                    scope=scope,
                    status=ToolExecutionStatus.FAILED,
                    error_code=error_code,
                    error_message=SAFE_ERROR_MESSAGES[error_code],
                    returned_source_refs=(),
                )
            )
            break

        source_refs = tuple(
            source.source_ref for source in _source_references(observation)
        )
        observations.append(observation)
        executions.append(
            ToolExecutionRecord(
                call_id=call_id,
                tool_name=tool_name,
                execution_order=execution_order,
                scope=scope,
                status=ToolExecutionStatus.SUCCEEDED,
                error_code=None,
                error_message=None,
                returned_source_refs=source_refs,
            )
        )

    immutable_observations = tuple(observations)
    has_failure = any(
        execution.status is ToolExecutionStatus.FAILED for execution in executions
    )
    if not has_failure:
        status = CollectionStatus.COMPLETED
    elif fatal_failure or not immutable_observations:
        status = CollectionStatus.FAILED
    else:
        status = CollectionStatus.PARTIAL

    return EvidenceCollectionResult(
        scope=scope,
        status=status,
        tool_executions=tuple(executions),
        observations=immutable_observations,
        timeline=_build_timeline(immutable_observations),
        unavailable_fields=_build_unavailable_fields(immutable_observations),
        citation_catalog=_build_citation_catalog(immutable_observations),
    )
