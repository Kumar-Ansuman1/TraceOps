"""Strict, immutable contracts for deterministic evidence collection."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.tool_contracts import (
    EvidenceRecordType,
    EvidenceSourceReference,
    GetTraceResult,
    LatencyBreakdownResult,
    ProviderAttemptsResult,
    SearchLogsResult,
)


class EvidenceCollectionContract(BaseModel):
    """Reject unknown fields and prevent collection results from being mutated."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CollectionStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class TelemetryToolName(str, Enum):
    GET_TRACE = "get_trace"
    GET_LATENCY_BREAKDOWN = "get_latency_breakdown"
    GET_PROVIDER_ATTEMPTS = "get_provider_attempts"
    SEARCH_LOGS = "search_logs"


TOOL_EXECUTION_ORDER: tuple[TelemetryToolName, ...] = (
    TelemetryToolName.GET_TRACE,
    TelemetryToolName.GET_LATENCY_BREAKDOWN,
    TelemetryToolName.GET_PROVIDER_ATTEMPTS,
    TelemetryToolName.SEARCH_LOGS,
)


class ToolExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CollectionErrorCode(str, Enum):
    UNSAFE_INPUT = "unsafe_input"
    UNKNOWN_INCIDENT = "unknown_incident"
    TRACE_MISMATCH = "trace_mismatch"
    MALFORMED_FIXTURE = "malformed_fixture"
    SPAN_NOT_FOUND = "span_not_found"
    INVALID_FILTER = "invalid_filter"
    TELEMETRY_TOOL_ERROR = "telemetry_tool_error"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"


FATAL_COLLECTION_ERROR_CODES = frozenset(
    {
        CollectionErrorCode.UNSAFE_INPUT,
        CollectionErrorCode.UNKNOWN_INCIDENT,
        CollectionErrorCode.TRACE_MISMATCH,
        CollectionErrorCode.MALFORMED_FIXTURE,
    }
)


class EvidenceCollectionScope(EvidenceCollectionContract):
    """Exact incident and trace identifiers supplied to every telemetry tool."""

    incident_id: str
    trace_id: str


class ToolExecutionRecord(EvidenceCollectionContract):
    call_id: str = Field(pattern=r"^call-[0-9]{2}-[a-z_]+-[a-f0-9]{16}$")
    tool_name: TelemetryToolName
    execution_order: int = Field(ge=1, le=4)
    scope: EvidenceCollectionScope
    status: ToolExecutionStatus
    error_code: CollectionErrorCode | None = None
    error_message: str | None = Field(default=None, min_length=1, max_length=500)
    returned_source_refs: tuple[str, ...]

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if tuple(sorted(set(self.returned_source_refs))) != self.returned_source_refs:
            raise ValueError("returned source references must be unique and sorted")

        if self.status is ToolExecutionStatus.SUCCEEDED:
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful tool calls cannot contain an error")
            return self

        if self.error_code is None or self.error_message is None:
            raise ValueError("failed tool calls require a controlled error")
        if self.returned_source_refs:
            raise ValueError("failed tool calls cannot claim returned evidence")
        return self


class TimelineEventKind(str, Enum):
    TRACE_STARTED = "trace_started"
    SPAN_STARTED = "span_started"
    LOG_RECORDED = "log_recorded"
    SPAN_ENDED = "span_ended"
    TRACE_ENDED = "trace_ended"


TIMELINE_EVENT_ORDER: dict[TimelineEventKind, int] = {
    TimelineEventKind.TRACE_STARTED: 0,
    TimelineEventKind.SPAN_STARTED: 1,
    TimelineEventKind.LOG_RECORDED: 2,
    TimelineEventKind.SPAN_ENDED: 3,
    TimelineEventKind.TRACE_ENDED: 4,
}


class TimelineObservation(EvidenceCollectionContract):
    timeline_id: str = Field(pattern=r"^timeline-[a-z_]+-[a-f0-9]{16}$")
    occurred_at: AwareDatetime
    event_kind: TimelineEventKind
    record_type: EvidenceRecordType
    record_id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    source_refs: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_sources_and_record_type(self) -> Self:
        if tuple(sorted(set(self.source_refs))) != self.source_refs:
            raise ValueError("timeline source references must be unique and sorted")

        expected_record_type = {
            TimelineEventKind.TRACE_STARTED: EvidenceRecordType.TRACE,
            TimelineEventKind.TRACE_ENDED: EvidenceRecordType.TRACE,
            TimelineEventKind.SPAN_STARTED: EvidenceRecordType.SPAN,
            TimelineEventKind.SPAN_ENDED: EvidenceRecordType.SPAN,
            TimelineEventKind.LOG_RECORDED: EvidenceRecordType.LOG,
        }[self.event_kind]
        if self.record_type is not expected_record_type:
            raise ValueError("timeline event kind does not match its record type")
        return self


def timeline_sort_key(
    observation: TimelineObservation,
) -> tuple[AwareDatetime, int, str, str]:
    """Return the documented deterministic timeline tie-break order."""

    return (
        observation.occurred_at,
        TIMELINE_EVENT_ORDER[observation.event_kind],
        observation.record_id,
        observation.timeline_id,
    )


class UnavailableFieldObservation(EvidenceCollectionContract):
    unavailable_id: str = Field(pattern=r"^unavailable-[a-f0-9]{16}$")
    source: EvidenceSourceReference
    observed_by: tuple[TelemetryToolName, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tool_order(self) -> Self:
        expected = tuple(
            sorted(
                set(self.observed_by),
                key=TOOL_EXECUTION_ORDER.index,
            )
        )
        if self.observed_by != expected:
            raise ValueError("observed_by must be unique and follow tool order")
        return self


class CitationCatalogEntry(EvidenceCollectionContract):
    citation_id: str = Field(pattern=r"^citation-[a-f0-9]{16}$")
    source: EvidenceSourceReference


class CitationCatalog(EvidenceCollectionContract):
    entries: tuple[CitationCatalogEntry, ...]

    @model_validator(mode="after")
    def validate_deduplication(self) -> Self:
        source_refs = tuple(entry.source.source_ref for entry in self.entries)
        citation_ids = tuple(entry.citation_id for entry in self.entries)
        if source_refs != tuple(sorted(set(source_refs))):
            raise ValueError("citation source references must be unique and sorted")
        if len(citation_ids) != len(set(citation_ids)):
            raise ValueError("citation IDs must be unique")
        return self


TelemetryToolResult = Annotated[
    GetTraceResult | LatencyBreakdownResult | ProviderAttemptsResult | SearchLogsResult,
    Field(discriminator="tool"),
]


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


class EvidenceCollectionResult(EvidenceCollectionContract):
    """Complete observation-only output from one bounded collection attempt."""

    scope: EvidenceCollectionScope
    status: CollectionStatus
    tool_executions: tuple[ToolExecutionRecord, ...] = Field(
        min_length=1,
        max_length=4,
    )
    observations: tuple[TelemetryToolResult, ...] = Field(max_length=4)
    timeline: tuple[TimelineObservation, ...]
    unavailable_fields: tuple[UnavailableFieldObservation, ...]
    citation_catalog: CitationCatalog

    @model_validator(mode="after")
    def validate_complete_result(self) -> Self:
        execution_orders = tuple(
            execution.execution_order for execution in self.tool_executions
        )
        expected_orders = tuple(range(1, len(self.tool_executions) + 1))
        if execution_orders != expected_orders:
            raise ValueError("tool execution orders must be consecutive")

        executed_tools = tuple(
            execution.tool_name for execution in self.tool_executions
        )
        if executed_tools != TOOL_EXECUTION_ORDER[: len(executed_tools)]:
            raise ValueError("telemetry tools must follow the fixed execution order")
        if len({execution.call_id for execution in self.tool_executions}) != len(
            self.tool_executions
        ):
            raise ValueError("tool call IDs must be unique")
        if any(execution.scope != self.scope for execution in self.tool_executions):
            raise ValueError("every tool execution must use the collection scope")

        successful_executions = tuple(
            execution
            for execution in self.tool_executions
            if execution.status is ToolExecutionStatus.SUCCEEDED
        )
        observation_tools = tuple(
            TelemetryToolName(observation.tool) for observation in self.observations
        )
        if observation_tools != tuple(
            execution.tool_name for execution in successful_executions
        ):
            raise ValueError("observations must match successful tool executions")

        observation_by_tool = {
            TelemetryToolName(observation.tool): observation
            for observation in self.observations
        }
        for execution in successful_executions:
            observation = observation_by_tool[execution.tool_name]
            if (
                observation.scope.incident_id != self.scope.incident_id
                or observation.scope.trace_id != self.scope.trace_id
            ):
                raise ValueError(
                    "successful tool output must match the collection scope"
                )
            expected_refs = tuple(
                source.source_ref for source in _source_references(observation)
            )
            if execution.returned_source_refs != expected_refs:
                raise ValueError(
                    "tool execution references must match returned observations"
                )

        failed_executions = tuple(
            execution
            for execution in self.tool_executions
            if execution.status is ToolExecutionStatus.FAILED
        )
        fatal_failure = any(
            execution.error_code in FATAL_COLLECTION_ERROR_CODES
            for execution in failed_executions
        )
        if self.status is CollectionStatus.COMPLETED:
            if len(successful_executions) != 4 or failed_executions:
                raise ValueError("completed collections require four successful tools")
        elif self.status is CollectionStatus.PARTIAL:
            if not successful_executions or not failed_executions or fatal_failure:
                raise ValueError(
                    "partial collections require evidence and a non-fatal failure"
                )
        elif not failed_executions or (successful_executions and not fatal_failure):
            raise ValueError(
                "failed collections require no evidence or a fatal collection error"
            )

        if self.timeline != tuple(sorted(self.timeline, key=timeline_sort_key)):
            raise ValueError("timeline observations must be deterministically ordered")

        unavailable_refs = tuple(
            observation.source.source_ref for observation in self.unavailable_fields
        )
        if unavailable_refs != tuple(sorted(set(unavailable_refs))):
            raise ValueError("unavailable fields must be unique and sorted")

        observation_sources = _source_references(self.observations)
        observation_refs = tuple(source.source_ref for source in observation_sources)
        catalog_refs = tuple(
            entry.source.source_ref for entry in self.citation_catalog.entries
        )
        if catalog_refs != observation_refs:
            raise ValueError("citation catalog must contain every returned source once")

        catalog_ref_set = set(catalog_refs)
        if any(
            source_ref not in catalog_ref_set
            for event in self.timeline
            for source_ref in event.source_refs
        ):
            raise ValueError("timeline citations must exist in the citation catalog")
        if any(source_ref not in catalog_ref_set for source_ref in unavailable_refs):
            raise ValueError(
                "unavailable field citations must exist in the citation catalog"
            )
        return self
