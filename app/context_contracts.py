"""Strict, immutable contracts for deterministic LLM-ready evidence context."""

from __future__ import annotations

import json
from enum import Enum
from hashlib import sha256
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.evidence_contracts import (
    TIMELINE_EVENT_ORDER,
    TOOL_EXECUTION_ORDER,
    CollectionErrorCode,
    CollectionStatus,
    TelemetryToolName,
    TimelineEventKind,
    ToolExecutionStatus,
)
from app.tool_contracts import EvidenceRecordType, EvidenceSourceReference, ValueOrigin

CONTEXT_VERSION = "1.0"


def stable_context_id(prefix: str, *parts: object) -> str:
    """Build a stable identifier from canonical, evidence-derived identity parts."""

    payload = json.dumps(
        parts,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{prefix}-{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def stable_evidence_id(prefix: str, payload: str) -> str:
    """Mirror the collector's stable IDs for preserved evidence identities."""

    return f"{prefix}-{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


class ContextContract(BaseModel):
    """Reject unknown fields, require strict inputs, and prevent mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FactCategory(str, Enum):
    TRACE = "trace"
    SPAN_LATENCY = "span_latency"
    CALCULATED_LATENCY = "calculated_latency"
    PROVIDER_ATTEMPT = "provider_attempt"
    SPAN_ATTRIBUTE = "span_attribute"
    LOG = "log"


FactValue = str | int | float | bool


class InvestigationContextScope(ContextContract):
    incident_id: str = Field(pattern=r"^INC-[A-Z0-9-]+$")
    trace_id: str = Field(min_length=1, max_length=200)


class ContextToolExecution(ContextContract):
    call_id: str = Field(pattern=r"^call-[0-9]{2}-[a-z_]+-[a-f0-9]{16}$")
    tool_name: TelemetryToolName
    execution_order: int = Field(ge=1, le=4)
    status: ToolExecutionStatus
    error_code: CollectionErrorCode | None = None
    error_message: str | None = Field(default=None, min_length=1, max_length=500)
    returned_source_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        if self.status is ToolExecutionStatus.SUCCEEDED:
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful tool executions cannot contain an error")
            return self
        if self.error_code is None or self.error_message is None:
            raise ValueError("failed tool executions require a controlled error")
        if self.returned_source_count:
            raise ValueError("failed tool executions cannot claim returned evidence")
        return self


class ToolExecutionSummary(ContextContract):
    attempted: tuple[ContextToolExecution, ...] = Field(min_length=1, max_length=4)
    not_executed: tuple[TelemetryToolName, ...]
    successful_count: int = Field(ge=0, le=4)
    failed_count: int = Field(ge=0, le=1)

    @model_validator(mode="after")
    def validate_execution_order(self) -> Self:
        orders = tuple(item.execution_order for item in self.attempted)
        if orders != tuple(range(1, len(self.attempted) + 1)):
            raise ValueError("attempted tool executions must be consecutive")

        attempted_tools = tuple(item.tool_name for item in self.attempted)
        if attempted_tools != TOOL_EXECUTION_ORDER[: len(attempted_tools)]:
            raise ValueError("attempted tools must follow the fixed collection order")
        if self.not_executed != TOOL_EXECUTION_ORDER[len(attempted_tools) :]:
            raise ValueError("not_executed must be the unattempted tool-order suffix")

        successful = sum(
            item.status is ToolExecutionStatus.SUCCEEDED for item in self.attempted
        )
        failed = sum(
            item.status is ToolExecutionStatus.FAILED for item in self.attempted
        )
        if self.successful_count != successful or self.failed_count != failed:
            raise ValueError("tool execution counts do not match attempted executions")
        return self


class EvidenceFact(ContextContract):
    fact_id: str = Field(pattern=r"^fact-[a-f0-9]{16}$")
    category: FactCategory
    metric: str = Field(min_length=1, max_length=200)
    subject_record_type: EvidenceRecordType
    subject_record_id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    value: FactValue
    unit: str | None = Field(default=None, min_length=1, max_length=50)
    origin: ValueOrigin
    citation_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity_and_citations(self) -> Self:
        if self.origin is ValueOrigin.UNAVAILABLE:
            raise ValueError("available facts cannot use the unavailable origin")
        if self.citation_ids != tuple(sorted(set(self.citation_ids))):
            raise ValueError("fact citation IDs must be unique and sorted")
        expected_id = stable_context_id(
            "fact",
            self.category.value,
            self.metric,
            self.subject_record_type.value,
            self.subject_record_id,
            self.value,
            self.unit,
            self.origin.value,
        )
        if self.fact_id != expected_id:
            raise ValueError("fact_id must match the fact's stable evidence identity")
        return self


class ContextTimelineEvent(ContextContract):
    event_id: str = Field(pattern=r"^timeline-[a-z_]+-[a-f0-9]{16}$")
    occurred_at: AwareDatetime
    event_kind: TimelineEventKind
    record_type: EvidenceRecordType
    record_id: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1_000)
    citation_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_event(self) -> Self:
        if self.citation_ids != tuple(sorted(set(self.citation_ids))):
            raise ValueError("timeline citation IDs must be unique and sorted")
        identity = (
            f"{self.event_kind.value}\0{self.record_type.value}\0"
            f"{self.record_id}\0{self.occurred_at.isoformat()}"
        )
        expected_id = stable_evidence_id(f"timeline-{self.event_kind.value}", identity)
        if self.event_id != expected_id:
            raise ValueError("event_id must match the stable timeline identity")
        return self


def context_timeline_sort_key(
    event: ContextTimelineEvent,
) -> tuple[AwareDatetime, int, str, str]:
    return (
        event.occurred_at,
        TIMELINE_EVENT_ORDER[event.event_kind],
        event.record_id,
        event.event_id,
    )


class ContextUnavailableField(ContextContract):
    unavailable_id: str = Field(pattern=r"^unavailable-[a-f0-9]{16}$")
    record_type: EvidenceRecordType
    record_id: str = Field(min_length=1, max_length=200)
    field_path: str = Field(min_length=1, max_length=500)
    observed_by: tuple[TelemetryToolName, ...] = Field(min_length=1)
    citation_id: str = Field(pattern=r"^citation-[a-f0-9]{16}$")

    @model_validator(mode="after")
    def validate_identity_and_tool_order(self) -> Self:
        expected_tools = tuple(
            tool for tool in TOOL_EXECUTION_ORDER if tool in set(self.observed_by)
        )
        if self.observed_by != expected_tools:
            raise ValueError("observed_by must be unique and follow tool order")
        return self


class ContextLimitations(ContextContract):
    fact_limit: int = Field(ge=1)
    facts_available: int = Field(ge=0)
    facts_included: int = Field(ge=0)
    facts_truncated: bool
    timeline_event_limit: int = Field(ge=1)
    timeline_events_available: int = Field(ge=0)
    timeline_events_included: int = Field(ge=0)
    timeline_truncated: bool
    unavailable_field_limit: int = Field(ge=1)
    unavailable_fields_available: int = Field(ge=0)
    unavailable_fields_included: int = Field(ge=0)
    unavailable_fields_truncated: bool
    logs_matched: int | None = Field(default=None, ge=0)
    logs_returned: int | None = Field(default=None, ge=0)
    original_log_search_truncated: bool | None = None
    log_summary_citation_id: str | None = Field(
        default=None,
        pattern=r"^citation-[a-f0-9]{16}$",
    )

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        count_groups = (
            (
                self.facts_available,
                self.facts_included,
                self.facts_truncated,
            ),
            (
                self.timeline_events_available,
                self.timeline_events_included,
                self.timeline_truncated,
            ),
            (
                self.unavailable_fields_available,
                self.unavailable_fields_included,
                self.unavailable_fields_truncated,
            ),
        )
        for available, included, truncated in count_groups:
            if included > available:
                raise ValueError("included counts cannot exceed available counts")
            if truncated != (included < available):
                raise ValueError(
                    "truncation flags must match available/included counts"
                )

        log_values = (
            self.logs_matched,
            self.logs_returned,
            self.original_log_search_truncated,
            self.log_summary_citation_id,
        )
        if any(value is None for value in log_values) and not all(
            value is None for value in log_values
        ):
            raise ValueError(
                "log limitation metadata must be all present or all absent"
            )
        if self.logs_matched is not None and self.logs_returned is not None:
            if self.logs_returned > self.logs_matched:
                raise ValueError("logs_returned cannot exceed logs_matched")
            expected = self.logs_returned < self.logs_matched
            if self.original_log_search_truncated is not expected:
                raise ValueError("log truncation must match matched/returned counts")
        return self


class ContextCitationEntry(ContextContract):
    citation_id: str = Field(pattern=r"^citation-[a-f0-9]{16}$")
    source: EvidenceSourceReference

    @model_validator(mode="after")
    def validate_stable_id(self) -> Self:
        expected = stable_evidence_id("citation", self.source.source_ref)
        if self.citation_id != expected:
            raise ValueError("citation_id must match its stable source reference")
        return self


class InvestigationContext(ContextContract):
    """Bounded, citation-closed evidence for a future hypothesis generator."""

    context_version: Literal["1.0"]
    context_id: str = Field(pattern=r"^context-[a-f0-9]{16}$")
    scope: InvestigationContextScope
    collection_status: CollectionStatus
    tool_execution_summary: ToolExecutionSummary
    facts: tuple[EvidenceFact, ...] = Field(min_length=1)
    timeline: tuple[ContextTimelineEvent, ...]
    unavailable_fields: tuple[ContextUnavailableField, ...]
    limitations: ContextLimitations
    citations: tuple[ContextCitationEntry, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_context_closure(self) -> Self:
        if self.timeline != tuple(sorted(self.timeline, key=context_timeline_sort_key)):
            raise ValueError("context timeline must be deterministically ordered")

        unavailable_order = tuple(
            sorted(
                self.unavailable_fields,
                key=lambda item: (
                    item.record_type.value,
                    item.record_id,
                    item.field_path,
                    item.unavailable_id,
                ),
            )
        )
        if self.unavailable_fields != unavailable_order:
            raise ValueError("unavailable fields must be deterministically ordered")

        citation_ids = tuple(entry.citation_id for entry in self.citations)
        if citation_ids != tuple(sorted(set(citation_ids))):
            raise ValueError("citations must be unique and ordered by citation ID")

        used_citations = {
            citation_id for fact in self.facts for citation_id in fact.citation_ids
        }
        used_citations.update(
            citation_id for event in self.timeline for citation_id in event.citation_ids
        )
        used_citations.update(item.citation_id for item in self.unavailable_fields)
        if self.limitations.log_summary_citation_id is not None:
            used_citations.add(self.limitations.log_summary_citation_id)
        if used_citations != set(citation_ids):
            raise ValueError("citations must be complete and contain no orphan entries")

        citation_by_id = {entry.citation_id: entry for entry in self.citations}
        for item in self.unavailable_fields:
            source_ref = citation_by_id[item.citation_id].source.source_ref
            expected_id = stable_evidence_id("unavailable", source_ref)
            if item.unavailable_id != expected_id:
                raise ValueError(
                    "unavailable_id must match its stable source reference"
                )

        for entry in self.citations:
            if entry.source.fixture_id != self.scope.incident_id:
                raise ValueError("citations cannot cross the incident scope")
            if (
                entry.source.record_type is EvidenceRecordType.TRACE
                and entry.source.record_id != self.scope.trace_id
            ):
                raise ValueError("trace citations cannot cross the trace scope")

        limitations = self.limitations
        if limitations.facts_included != len(self.facts):
            raise ValueError("facts_included must match the context facts")
        if limitations.timeline_events_included != len(self.timeline):
            raise ValueError("timeline_events_included must match the context timeline")
        if limitations.unavailable_fields_included != len(self.unavailable_fields):
            raise ValueError(
                "unavailable_fields_included must match the context inventory"
            )

        summary = self.tool_execution_summary
        if self.collection_status is CollectionStatus.COMPLETED:
            if summary.successful_count != 4 or summary.failed_count:
                raise ValueError("completed context requires four successful tools")
        elif self.collection_status is CollectionStatus.PARTIAL:
            if not summary.successful_count or summary.failed_count != 1:
                raise ValueError("partial context requires evidence and one failure")
        elif summary.failed_count != 1:
            raise ValueError("failed context must preserve its collection failure")

        expected_context_id = stable_context_id(
            "context",
            self.context_version,
            self.scope.incident_id,
            self.scope.trace_id,
            self.collection_status.value,
            tuple(fact.fact_id for fact in self.facts),
            tuple(event.event_id for event in self.timeline),
            tuple(item.unavailable_id for item in self.unavailable_fields),
            citation_ids,
        )
        if self.context_id != expected_context_id:
            raise ValueError("context_id must match the included evidence identity")
        return self
