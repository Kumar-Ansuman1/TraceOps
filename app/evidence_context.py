"""Build bounded, deterministic, citation-preserving investigation context."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace

from app.context_contracts import (
    CONTEXT_VERSION,
    ContextCitationEntry,
    ContextLimitations,
    ContextTimelineEvent,
    ContextToolExecution,
    ContextUnavailableField,
    EvidenceFact,
    FactCategory,
    FactValue,
    InvestigationContext,
    InvestigationContextScope,
    ToolExecutionSummary,
    stable_context_id,
)
from app.evidence_contracts import (
    TOOL_EXECUTION_ORDER,
    EvidenceCollectionResult,
    ToolExecutionStatus,
)
from app.tool_contracts import (
    AttributeObservation,
    EvidenceRecordType,
    EvidenceSourceReference,
    GetTraceResult,
    LatencyBreakdownResult,
    LogObservation,
    ObservedValue,
    ProviderAttemptsResult,
    SearchLogsResult,
    ValueAvailability,
    ValueOrigin,
)

# Limits are deliberately small enough for a future prompt. Per-category limits
# apply before the total fact limit, using the documented priority order below.
MAX_TOTAL_FACTS = 24
MAX_LATENCY_SPAN_FACTS = 4
MAX_PROVIDER_ATTEMPTS = 3
MAX_ATTRIBUTE_FACTS = 6
MAX_LOG_FACTS = 5
MAX_TIMELINE_EVENTS = 20
MAX_UNAVAILABLE_FIELDS = 12

_LOG_LEVEL_PRIORITY = {"error": 0, "warning": 1, "info": 2, "debug": 3}


class EvidenceContextBuildError(ValueError):
    """Base class for caller-safe context-building failures."""

    code = "context_build_failed"


class NoUsableEvidenceError(EvidenceContextBuildError):
    """Raised when a collection cannot support a future evidence-only prompt."""

    code = "no_usable_evidence"

    def __init__(self) -> None:
        super().__init__("The evidence collection contains no usable observations.")


class InvalidEvidenceCollectionError(EvidenceContextBuildError):
    """Raised when evidence cannot be safely closed over its citations or scope."""

    code = "invalid_evidence_collection"

    def __init__(self) -> None:
        super().__init__("The evidence collection cannot produce a safe context.")


@dataclass(frozen=True)
class _FactCandidate:
    category: FactCategory
    metric: str
    record_type: EvidenceRecordType
    record_id: str
    description: str
    value: FactValue
    unit: str | None
    origin: ValueOrigin
    source_refs: tuple[str, ...]
    priority: tuple[int, int, float, str, str]
    selection_group: str
    selection_entity: str

    @property
    def evidence_identity(self) -> tuple[str, str, str, str, str, str]:
        """Identity used for deduplication across overlapping tool outputs."""

        return (
            self.metric,
            self.record_type.value,
            self.record_id,
            json.dumps(self.value, ensure_ascii=True, separators=(",", ":")),
            self.unit or "",
            self.origin.value,
        )


def _available_scalar(observation: ObservedValue[object]) -> FactValue | None:
    if observation.availability is not ValueAvailability.AVAILABLE:
        return None
    value = observation.value
    if value is None or type(value) not in (str, int, float, bool):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _source_refs(
    *items: EvidenceSourceReference | ObservedValue[object],
) -> tuple[str, ...]:
    refs: set[str] = set()
    for item in items:
        if isinstance(item, EvidenceSourceReference):
            refs.add(item.source_ref)
        else:
            refs.update(source.source_ref for source in item.sources)
    return tuple(sorted(refs))


def _format_value(value: FactValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=True)
    return str(value)


def _trace_candidates(
    collection: EvidenceCollectionResult,
) -> list[_FactCandidate]:
    candidates: list[_FactCandidate] = []
    trace_result = next(
        (item for item in collection.observations if isinstance(item, GetTraceResult)),
        None,
    )
    if trace_result is not None:
        trace = trace_result.trace
        duration = _available_scalar(trace.duration_ms)
        if duration is not None:
            candidates.append(
                _FactCandidate(
                    category=FactCategory.TRACE,
                    metric="duration_ms",
                    record_type=EvidenceRecordType.TRACE,
                    record_id=trace.trace_id,
                    description=(
                        f"The complete trace duration was {_format_value(duration)} ms."
                    ),
                    value=duration,
                    unit="ms",
                    origin=trace.duration_ms.origin,
                    source_refs=_source_refs(trace.source, trace.duration_ms),
                    priority=(0, 0, 0.0, trace.trace_id, "duration_ms"),
                    selection_group="trace",
                    selection_entity=trace.trace_id,
                )
            )

        status = _available_scalar(trace.status)
        if status is not None:
            candidates.append(
                _FactCandidate(
                    category=FactCategory.TRACE,
                    metric="status",
                    record_type=EvidenceRecordType.TRACE,
                    record_id=trace.trace_id,
                    description=(
                        f"The trace status was recorded as {_format_value(status)}."
                    ),
                    value=status,
                    unit=None,
                    origin=trace.status.origin,
                    source_refs=_source_refs(trace.source, trace.status),
                    priority=(0, 1, 0.0, trace.trace_id, "status"),
                    selection_group="trace",
                    selection_entity=trace.trace_id,
                )
            )

    latency_result = next(
        (
            item
            for item in collection.observations
            if isinstance(item, LatencyBreakdownResult)
        ),
        None,
    )
    if latency_result is not None:
        duration = _available_scalar(latency_result.trace_duration_ms)
        if duration is not None:
            candidates.append(
                _FactCandidate(
                    category=FactCategory.TRACE,
                    metric="duration_ms",
                    record_type=EvidenceRecordType.TRACE,
                    record_id=collection.scope.trace_id,
                    description=(
                        f"The complete trace duration was {_format_value(duration)} ms."
                    ),
                    value=duration,
                    unit="ms",
                    origin=latency_result.trace_duration_ms.origin,
                    source_refs=_source_refs(latency_result.trace_duration_ms),
                    priority=(0, 0, 0.0, collection.scope.trace_id, "duration_ms"),
                    selection_group="trace",
                    selection_entity=collection.scope.trace_id,
                )
            )
    return candidates


def _latency_candidates(
    collection: EvidenceCollectionResult,
) -> list[_FactCandidate]:
    result = next(
        (
            item
            for item in collection.observations
            if isinstance(item, LatencyBreakdownResult)
        ),
        None,
    )
    if result is None:
        return []

    non_root_spans = [
        span
        for span in result.spans
        if span.parent_span_id.availability is ValueAvailability.AVAILABLE
    ]
    non_root_spans.sort(
        key=lambda span: (
            -float(_available_scalar(span.recorded_duration_ms) or -1.0),
            span.span_id,
        )
    )

    candidates: list[_FactCandidate] = []
    for rank, span in enumerate(non_root_spans):
        name_value = _available_scalar(span.name)
        name = str(name_value) if name_value is not None else span.span_id
        name_sources: tuple[ObservedValue[object], ...] = (
            (span.name,) if name_value is not None else ()
        )

        duration = _available_scalar(span.recorded_duration_ms)
        if duration is not None:
            candidates.append(
                _FactCandidate(
                    category=FactCategory.SPAN_LATENCY,
                    metric="duration_ms",
                    record_type=EvidenceRecordType.SPAN,
                    record_id=span.span_id,
                    description=(
                        f"Span {span.span_id} ({json.dumps(name)}) duration was "
                        f"{_format_value(duration)} ms."
                    ),
                    value=duration,
                    unit="ms",
                    origin=span.recorded_duration_ms.origin,
                    source_refs=_source_refs(
                        span.source,
                        span.recorded_duration_ms,
                        *name_sources,
                    ),
                    priority=(1, rank, -float(duration), span.span_id, "duration_ms"),
                    selection_group="span_latency",
                    selection_entity=span.span_id,
                )
            )

        coverage = _available_scalar(span.coverage_of_trace_percent)
        if coverage is not None:
            candidates.append(
                _FactCandidate(
                    category=FactCategory.CALCULATED_LATENCY,
                    metric="coverage_of_trace_percent",
                    record_type=EvidenceRecordType.SPAN,
                    record_id=span.span_id,
                    description=(
                        f"Span {span.span_id} ({json.dumps(name)}) covered "
                        f"{_format_value(coverage)}% of the trace."
                    ),
                    value=coverage,
                    unit="percent",
                    origin=span.coverage_of_trace_percent.origin,
                    source_refs=_source_refs(
                        span.source,
                        span.coverage_of_trace_percent,
                        *name_sources,
                    ),
                    priority=(2, rank, -float(coverage), span.span_id, "coverage"),
                    selection_group="calculated_latency",
                    selection_entity=span.span_id,
                )
            )
    return candidates


def _provider_candidates(
    collection: EvidenceCollectionResult,
) -> list[_FactCandidate]:
    result = next(
        (
            item
            for item in collection.observations
            if isinstance(item, ProviderAttemptsResult)
        ),
        None,
    )
    if result is None:
        return []

    attempts = sorted(
        result.attempts,
        key=lambda attempt: (
            str(_available_scalar(attempt.started_at) or ""),
            attempt.span_id,
        ),
    )
    field_specs = (
        ("duration_ms", "duration_ms", "duration", "ms"),
        ("status", "status", "status", None),
        (
            "provider_name",
            "attributes.gen_ai.provider.name",
            "provider name",
            None,
        ),
        ("model", "attributes.gen_ai.request.model", "model", None),
        (
            "input_tokens",
            "attributes.gen_ai.usage.input_tokens",
            "input token count",
            "tokens",
        ),
        (
            "output_tokens",
            "attributes.gen_ai.usage.output_tokens",
            "output token count",
            "tokens",
        ),
        (
            "retry_count",
            "attributes.provider.retry_count",
            "retry count",
            "count",
        ),
        ("error_type", "attributes.error.type", "error type", None),
        ("error_message", "attributes.error.message", "error message", None),
    )

    candidates: list[_FactCandidate] = []
    for attempt_rank, attempt in enumerate(attempts):
        for field_rank, (field_name, metric, label, unit) in enumerate(field_specs):
            observation = getattr(attempt, field_name)
            value = _available_scalar(observation)
            if value is None:
                continue
            if field_name == "duration_ms":
                description = (
                    f"Provider attempt span {attempt.span_id} duration was "
                    f"{_format_value(value)} ms."
                )
            else:
                description = (
                    f"Provider attempt span {attempt.span_id} {label} was recorded "
                    f"as {_format_value(value)}."
                )
            candidates.append(
                _FactCandidate(
                    category=FactCategory.PROVIDER_ATTEMPT,
                    metric=metric,
                    record_type=EvidenceRecordType.SPAN,
                    record_id=attempt.span_id,
                    description=description,
                    value=value,
                    unit=unit,
                    origin=observation.origin,
                    source_refs=_source_refs(attempt.source, observation),
                    priority=(
                        3,
                        attempt_rank,
                        float(field_rank),
                        attempt.span_id,
                        metric,
                    ),
                    selection_group="provider_attempt",
                    selection_entity=attempt.span_id,
                )
            )
    return candidates


def _attribute_priority(attribute: AttributeObservation) -> tuple[int, str]:
    name = attribute.name
    if name.startswith("routing."):
        return (0, name)
    if name.startswith("error."):
        return (1, name)
    if name.startswith(("gen_ai.", "provider.")):
        return (2, name)
    return (3, name)


def _attribute_unit(name: str) -> str | None:
    if name in {"gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"}:
        return "tokens"
    if name == "provider.retry_count":
        return "count"
    return None


def _attribute_candidates(
    collection: EvidenceCollectionResult,
) -> list[_FactCandidate]:
    result = next(
        (item for item in collection.observations if isinstance(item, GetTraceResult)),
        None,
    )
    if result is None:
        return []

    ordered: list[
        tuple[int, str, str, AttributeObservation, EvidenceSourceReference]
    ] = []
    for span in result.spans:
        for attribute in span.attributes:
            attribute_rank, attribute_name = _attribute_priority(attribute)
            ordered.append(
                (attribute_rank, attribute_name, span.span_id, attribute, span.source)
            )
    ordered.sort(key=lambda item: (item[0], item[1], item[2]))

    candidates: list[_FactCandidate] = []
    for rank, (_, _, span_id, attribute, span_source) in enumerate(ordered):
        value = _available_scalar(attribute.observation)
        if value is None:
            continue
        metric = f"attributes.{attribute.name}"
        candidates.append(
            _FactCandidate(
                category=FactCategory.SPAN_ATTRIBUTE,
                metric=metric,
                record_type=EvidenceRecordType.SPAN,
                record_id=span_id,
                description=(
                    f"Span {span_id} attribute {attribute.name} was recorded as "
                    f"{_format_value(value)}."
                ),
                value=value,
                unit=_attribute_unit(attribute.name),
                origin=attribute.observation.origin,
                source_refs=_source_refs(span_source, attribute.observation),
                priority=(4, rank, 0.0, span_id, metric),
                selection_group="attribute",
                selection_entity=f"{span_id}\0{attribute.name}",
            )
        )
    return candidates


def _log_candidates(collection: EvidenceCollectionResult) -> list[_FactCandidate]:
    result = next(
        (
            item
            for item in collection.observations
            if isinstance(item, SearchLogsResult)
        ),
        None,
    )
    if result is None:
        return []

    ordered_logs: list[tuple[int, str, str, LogObservation]] = []
    for log in result.logs:
        level = _available_scalar(log.level)
        observed_at = _available_scalar(log.observed_at)
        level_text = str(level) if level is not None else ""
        ordered_logs.append(
            (
                _LOG_LEVEL_PRIORITY.get(level_text, 4),
                str(observed_at or ""),
                log.log_id,
                log,
            )
        )
    ordered_logs.sort(key=lambda item: (item[0], item[1], item[2]))

    candidates: list[_FactCandidate] = []
    for rank, (level_rank, _, _, log) in enumerate(ordered_logs):
        level = _available_scalar(log.level)
        message = _available_scalar(log.message)
        if level is None or message is None:
            continue
        article = "an" if str(level)[:1].lower() in "aeiou" else "a"
        candidates.append(
            _FactCandidate(
                category=FactCategory.LOG,
                metric="message",
                record_type=EvidenceRecordType.LOG,
                record_id=log.log_id,
                description=(
                    f"Log {log.log_id} recorded {article} {level} message: "
                    f"{_format_value(message)}"
                ),
                value=message,
                unit=None,
                origin=log.message.origin,
                source_refs=_source_refs(log.source, log.level, log.message),
                priority=(5, rank, float(level_rank), log.log_id, "message"),
                selection_group="log",
                selection_entity=log.log_id,
            )
        )
    return candidates


def _deduplicate_candidates(
    candidates: list[_FactCandidate],
) -> list[_FactCandidate]:
    deduplicated: dict[tuple[str, str, str, str, str, str], _FactCandidate] = {}
    for candidate in sorted(candidates, key=lambda item: item.priority):
        existing = deduplicated.get(candidate.evidence_identity)
        if existing is None:
            deduplicated[candidate.evidence_identity] = candidate
            continue
        merged_refs = tuple(
            sorted(set(existing.source_refs) | set(candidate.source_refs))
        )
        deduplicated[candidate.evidence_identity] = replace(
            existing,
            source_refs=merged_refs,
        )
    return sorted(deduplicated.values(), key=lambda item: item.priority)


def _apply_fact_limits(candidates: list[_FactCandidate]) -> list[_FactCandidate]:
    selected: list[_FactCandidate] = []
    group_counts = {
        "span_latency": 0,
        "calculated_latency": 0,
        "attribute": 0,
        "log": 0,
    }
    provider_entities: list[str] = []

    for candidate in candidates:
        group = candidate.selection_group
        if group in {"span_latency", "calculated_latency"}:
            if group_counts[group] >= MAX_LATENCY_SPAN_FACTS:
                continue
            group_counts[group] += 1
        elif group == "provider_attempt":
            if candidate.selection_entity not in provider_entities:
                provider_entities.append(candidate.selection_entity)
            if (
                provider_entities.index(candidate.selection_entity)
                >= MAX_PROVIDER_ATTEMPTS
            ):
                continue
        elif group == "attribute":
            if group_counts[group] >= MAX_ATTRIBUTE_FACTS:
                continue
            group_counts[group] += 1
        elif group == "log":
            if group_counts[group] >= MAX_LOG_FACTS:
                continue
            group_counts[group] += 1
        selected.append(candidate)

    return selected[:MAX_TOTAL_FACTS]


def _record_ids_by_type(
    collection: EvidenceCollectionResult,
) -> dict[EvidenceRecordType, set[str]]:
    record_ids = {
        EvidenceRecordType.FIXTURE: {collection.scope.incident_id},
        EvidenceRecordType.TRACE: {collection.scope.trace_id},
        EvidenceRecordType.SPAN: set(),
        EvidenceRecordType.LOG: set(),
    }
    for observation in collection.observations:
        if isinstance(observation, (GetTraceResult, LatencyBreakdownResult)):
            record_ids[EvidenceRecordType.SPAN].update(
                span.span_id for span in observation.spans
            )
        elif isinstance(observation, ProviderAttemptsResult):
            record_ids[EvidenceRecordType.SPAN].update(
                attempt.span_id for attempt in observation.attempts
            )
        elif isinstance(observation, SearchLogsResult):
            record_ids[EvidenceRecordType.LOG].update(
                log.log_id for log in observation.logs
            )
    return record_ids


def _citation_mapping(
    collection: EvidenceCollectionResult,
) -> dict[str, ContextCitationEntry]:
    record_ids = _record_ids_by_type(collection)
    mapping: dict[str, ContextCitationEntry] = {}
    for entry in collection.citation_catalog.entries:
        source = entry.source
        if source.fixture_id != collection.scope.incident_id:
            raise InvalidEvidenceCollectionError()
        if source.record_id not in record_ids[source.record_type]:
            raise InvalidEvidenceCollectionError()
        mapping[source.source_ref] = ContextCitationEntry(
            citation_id=entry.citation_id,
            source=source,
        )
    return mapping


def _citation_ids(
    source_refs: tuple[str, ...],
    citation_by_ref: dict[str, ContextCitationEntry],
) -> tuple[str, ...]:
    try:
        return tuple(
            sorted(
                {citation_by_ref[source_ref].citation_id for source_ref in source_refs}
            )
        )
    except KeyError as error:
        raise InvalidEvidenceCollectionError() from error


def _facts(
    collection: EvidenceCollectionResult,
    citation_by_ref: dict[str, ContextCitationEntry],
) -> tuple[tuple[EvidenceFact, ...], int]:
    candidates = _deduplicate_candidates(
        [
            *_trace_candidates(collection),
            *_latency_candidates(collection),
            *_provider_candidates(collection),
            *_attribute_candidates(collection),
            *_log_candidates(collection),
        ]
    )
    selected = _apply_fact_limits(candidates)
    facts = tuple(
        EvidenceFact(
            fact_id=stable_context_id(
                "fact",
                candidate.category.value,
                candidate.metric,
                candidate.record_type.value,
                candidate.record_id,
                candidate.value,
                candidate.unit,
                candidate.origin.value,
            ),
            category=candidate.category,
            metric=candidate.metric,
            subject_record_type=candidate.record_type,
            subject_record_id=candidate.record_id,
            description=candidate.description,
            value=candidate.value,
            unit=candidate.unit,
            origin=candidate.origin,
            citation_ids=_citation_ids(candidate.source_refs, citation_by_ref),
        )
        for candidate in selected
    )
    return facts, len(candidates)


def _timeline(
    collection: EvidenceCollectionResult,
    citation_by_ref: dict[str, ContextCitationEntry],
) -> tuple[ContextTimelineEvent, ...]:
    return tuple(
        ContextTimelineEvent(
            event_id=event.timeline_id,
            occurred_at=event.occurred_at,
            event_kind=event.event_kind,
            record_type=event.record_type,
            record_id=event.record_id,
            description=event.description,
            citation_ids=_citation_ids(event.source_refs, citation_by_ref),
        )
        for event in collection.timeline[:MAX_TIMELINE_EVENTS]
    )


def _unavailable_fields(
    collection: EvidenceCollectionResult,
    citation_by_ref: dict[str, ContextCitationEntry],
) -> tuple[ContextUnavailableField, ...]:
    selected = sorted(
        collection.unavailable_fields,
        key=lambda item: (
            item.source.record_type.value,
            item.source.record_id,
            item.source.field_path,
            item.unavailable_id,
        ),
    )[:MAX_UNAVAILABLE_FIELDS]
    return tuple(
        ContextUnavailableField(
            unavailable_id=item.unavailable_id,
            record_type=item.source.record_type,
            record_id=item.source.record_id,
            field_path=item.source.field_path,
            observed_by=item.observed_by,
            citation_id=_citation_ids((item.source.source_ref,), citation_by_ref)[0],
        )
        for item in selected
    )


def _tool_summary(collection: EvidenceCollectionResult) -> ToolExecutionSummary:
    attempted = tuple(
        ContextToolExecution(
            call_id=item.call_id,
            tool_name=item.tool_name,
            execution_order=item.execution_order,
            status=item.status,
            error_code=item.error_code,
            error_message=item.error_message,
            returned_source_count=len(item.returned_source_refs),
        )
        for item in collection.tool_executions
    )
    return ToolExecutionSummary(
        attempted=attempted,
        not_executed=TOOL_EXECUTION_ORDER[len(attempted) :],
        successful_count=sum(
            item.status is ToolExecutionStatus.SUCCEEDED for item in attempted
        ),
        failed_count=sum(
            item.status is ToolExecutionStatus.FAILED for item in attempted
        ),
    )


def _log_limitations(
    collection: EvidenceCollectionResult,
    citation_by_ref: dict[str, ContextCitationEntry],
) -> tuple[int | None, int | None, bool | None, str | None]:
    result = next(
        (
            item
            for item in collection.observations
            if isinstance(item, SearchLogsResult)
        ),
        None,
    )
    if result is None:
        return (None, None, None, None)
    citation_id = _citation_ids((result.summary.source.source_ref,), citation_by_ref)[0]
    return (
        result.summary.matched_count,
        result.summary.returned_count,
        result.summary.truncated,
        citation_id,
    )


def build_investigation_context(
    collection: EvidenceCollectionResult,
) -> InvestigationContext:
    """Convert one collection result into bounded, observation-only context.

    Selection priority is trace duration/status; longest non-root span durations;
    their calculated trace coverage; bounded provider attempts; routing and other
    scalar span attributes; then error/warning logs before info/debug logs.
    Stable evidence identity deduplicates overlaps before limits are applied.
    """

    if not collection.observations:
        raise NoUsableEvidenceError()

    citation_by_ref = _citation_mapping(collection)
    facts, facts_available = _facts(collection, citation_by_ref)
    if not facts:
        raise NoUsableEvidenceError()

    timeline = _timeline(collection, citation_by_ref)
    unavailable = _unavailable_fields(collection, citation_by_ref)
    logs_matched, logs_returned, logs_truncated, log_summary_citation = (
        _log_limitations(collection, citation_by_ref)
    )
    limitations = ContextLimitations(
        fact_limit=MAX_TOTAL_FACTS,
        facts_available=facts_available,
        facts_included=len(facts),
        facts_truncated=len(facts) < facts_available,
        timeline_event_limit=MAX_TIMELINE_EVENTS,
        timeline_events_available=len(collection.timeline),
        timeline_events_included=len(timeline),
        timeline_truncated=len(timeline) < len(collection.timeline),
        unavailable_field_limit=MAX_UNAVAILABLE_FIELDS,
        unavailable_fields_available=len(collection.unavailable_fields),
        unavailable_fields_included=len(unavailable),
        unavailable_fields_truncated=(
            len(unavailable) < len(collection.unavailable_fields)
        ),
        logs_matched=logs_matched,
        logs_returned=logs_returned,
        original_log_search_truncated=logs_truncated,
        log_summary_citation_id=log_summary_citation,
    )

    used_citation_ids = {
        citation_id for fact in facts for citation_id in fact.citation_ids
    }
    used_citation_ids.update(
        citation_id for event in timeline for citation_id in event.citation_ids
    )
    used_citation_ids.update(item.citation_id for item in unavailable)
    if log_summary_citation is not None:
        used_citation_ids.add(log_summary_citation)
    citations = tuple(
        sorted(
            (
                entry
                for entry in citation_by_ref.values()
                if entry.citation_id in used_citation_ids
            ),
            key=lambda entry: entry.citation_id,
        )
    )

    citation_ids = tuple(entry.citation_id for entry in citations)
    context_id = stable_context_id(
        "context",
        CONTEXT_VERSION,
        collection.scope.incident_id,
        collection.scope.trace_id,
        collection.status.value,
        tuple(fact.fact_id for fact in facts),
        tuple(event.event_id for event in timeline),
        tuple(item.unavailable_id for item in unavailable),
        citation_ids,
    )
    return InvestigationContext(
        context_version=CONTEXT_VERSION,
        context_id=context_id,
        scope=InvestigationContextScope(
            incident_id=collection.scope.incident_id,
            trace_id=collection.scope.trace_id,
        ),
        collection_status=collection.status,
        tool_execution_summary=_tool_summary(collection),
        facts=facts,
        timeline=timeline,
        unavailable_fields=unavailable,
        limitations=limitations,
        citations=citations,
    )
