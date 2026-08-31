from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import telemetry_tools
from app.context_contracts import (
    FactCategory,
    InvestigationContext,
    context_timeline_sort_key,
)
from app.evidence_collector import collect_evidence
from app.evidence_context import (
    MAX_LOG_FACTS,
    MAX_TIMELINE_EVENTS,
    MAX_TOTAL_FACTS,
    MAX_UNAVAILABLE_FIELDS,
    NoUsableEvidenceError,
    build_investigation_context,
)
from app.evidence_contracts import (
    CollectionErrorCode,
    CollectionStatus,
    TelemetryToolName,
    ToolExecutionStatus,
)
from app.fixtures import load_telemetry_fixture
from app.telemetry import TelemetryFixture
from app.tool_contracts import EvidenceRecordType, ValueOrigin

INCIDENT_ID = "INC-SLOW-001"
TRACE_ID = "synthetic-trace-slow-001"
PROVIDER_SPAN_ID = "synthetic-span-provider-001"
ROUTING_SPAN_ID = "synthetic-span-route-001"
ANALYSIS_SPAN_ID = "synthetic-span-analysis-001"


def _build_real_context() -> InvestigationContext:
    return build_investigation_context(collect_evidence(INCIDENT_ID, TRACE_ID))


def _fact(
    context: InvestigationContext,
    metric: str,
    record_id: str,
):
    return next(
        fact
        for fact in context.facts
        if fact.metric == metric and fact.subject_record_id == record_id
    )


def _fixture_with_logs(
    levels: list[str],
    *,
    minimise_attributes: bool = False,
) -> TelemetryFixture:
    fixture = load_telemetry_fixture(INCIDENT_ID)
    payload = fixture.model_dump(mode="json")
    started_at = fixture.telemetry.trace.started_at
    payload["telemetry"]["logs"] = [
        {
            "log_id": f"synthetic-log-context-{index:03d}",
            "observed_at": (
                started_at + timedelta(milliseconds=100 + index)
            ).isoformat(),
            "level": level,
            "message": f"Synthetic context log {index}.",
            "span_id": ANALYSIS_SPAN_ID,
            "attributes": {},
        }
        for index, level in enumerate(levels)
    ]
    if minimise_attributes:
        retained_span_ids = {ROUTING_SPAN_ID, PROVIDER_SPAN_ID}
        for span in payload["telemetry"]["trace"]["spans"]:
            if span["span_id"] not in retained_span_ids:
                span["attributes"] = {}
    return TelemetryFixture.model_validate(payload)


def _oversized_fixture() -> TelemetryFixture:
    fixture = _fixture_with_logs(
        ["debug", "info", "warning", "error"] * 8,
    )
    payload = fixture.model_dump(mode="json")
    started_at = fixture.telemetry.trace.started_at
    for index in range(10):
        span_started = started_at + timedelta(milliseconds=500 + index * 10)
        payload["telemetry"]["trace"]["spans"].append(
            {
                "span_id": f"synthetic-span-provider-extra-{index:03d}",
                "parent_span_id": ANALYSIS_SPAN_ID,
                "name": f"extra provider attempt {index}",
                "kind": "client",
                "started_at": span_started.isoformat(),
                "ended_at": (span_started + timedelta(milliseconds=5)).isoformat(),
                "duration_ms": 5.0,
                "status": "ok",
                "attributes": {
                    "gen_ai.provider.name": "synthetic-provider",
                    "gen_ai.request.model": "synthetic-extra-model",
                    "gen_ai.usage.input_tokens": 10 + index,
                    "gen_ai.usage.output_tokens": 5 + index,
                    "provider.retry_count": 0,
                },
            }
        )
    return TelemetryFixture.model_validate(payload)


def test_builds_context_from_supported_real_collection() -> None:
    context = _build_real_context()

    assert context.context_version == "1.0"
    assert context.scope.incident_id == INCIDENT_ID
    assert context.scope.trace_id == TRACE_ID
    assert context.collection_status is CollectionStatus.COMPLETED
    assert context.tool_execution_summary.successful_count == 4
    assert context.facts
    assert context.timeline
    assert context.citations


def test_repeated_builds_have_equal_objects_and_identical_json() -> None:
    collection = collect_evidence(INCIDENT_ID, TRACE_ID)

    first = build_investigation_context(collection)
    second = build_investigation_context(collection)

    assert first == second
    assert first.model_dump_json() == second.model_dump_json()
    assert InvestigationContext.model_validate_json(first.model_dump_json()) == first


def test_fact_context_and_citation_ids_are_stable_across_runs() -> None:
    first = _build_real_context()
    second = _build_real_context()

    assert first.context_id == second.context_id
    assert [fact.fact_id for fact in first.facts] == [
        fact.fact_id for fact in second.facts
    ]
    assert [entry.citation_id for entry in first.citations] == [
        entry.citation_id for entry in second.citations
    ]
    assert {
        entry.source.source_ref: entry.citation_id for entry in first.citations
    } == {entry.source.source_ref: entry.citation_id for entry in second.citations}


def test_trace_duration_is_recorded_fact() -> None:
    fact = _fact(_build_real_context(), "duration_ms", TRACE_ID)

    assert fact.category is FactCategory.TRACE
    assert fact.value == 15_420.0
    assert fact.unit == "ms"
    assert fact.origin is ValueOrigin.RECORDED


def test_provider_duration_is_deduplicated_across_tool_outputs() -> None:
    context = _build_real_context()
    facts = [
        fact
        for fact in context.facts
        if fact.subject_record_id == PROVIDER_SPAN_ID and fact.metric == "duration_ms"
    ]

    assert len(facts) == 1
    assert facts[0].value == 14_800.0
    duration_source = (
        "fixture://INC-SLOW-001/span/synthetic-span-provider-001#duration_ms"
    )
    citation_by_source = {
        entry.source.source_ref: entry.citation_id for entry in context.citations
    }
    assert citation_by_source[duration_source] in facts[0].citation_ids


def test_provider_trace_coverage_is_separate_calculated_fact() -> None:
    fact = _fact(
        _build_real_context(),
        "coverage_of_trace_percent",
        PROVIDER_SPAN_ID,
    )

    assert fact.category is FactCategory.CALCULATED_LATENCY
    assert fact.value == 95.979248
    assert fact.unit == "percent"
    assert fact.origin is ValueOrigin.CALCULATED
    assert len(fact.citation_ids) >= 2


def test_zero_retry_count_is_preserved_as_available_recorded_fact() -> None:
    fact = _fact(
        _build_real_context(),
        "attributes.provider.retry_count",
        PROVIDER_SPAN_ID,
    )

    assert fact.value == 0
    assert type(fact.value) is int
    assert fact.origin is ValueOrigin.RECORDED


def test_false_routing_fallback_is_preserved_as_recorded_fact() -> None:
    fact = _fact(
        _build_real_context(),
        "attributes.routing.fallback_used",
        ROUTING_SPAN_ID,
    )

    assert fact.value is False
    assert type(fact.value) is bool
    assert fact.origin is ValueOrigin.RECORDED


def test_unavailable_provider_errors_are_not_available_facts() -> None:
    context = _build_real_context()
    unavailable = {
        (item.record_id, item.field_path): item for item in context.unavailable_fields
    }

    assert (PROVIDER_SPAN_ID, "attributes.error.type") in unavailable
    assert (PROVIDER_SPAN_ID, "attributes.error.message") in unavailable
    assert not any(
        fact.metric in {"attributes.error.type", "attributes.error.message"}
        for fact in context.facts
    )


def test_every_fact_and_timeline_citation_exists_in_output() -> None:
    context = _build_real_context()
    citation_ids = {entry.citation_id for entry in context.citations}

    assert all(set(fact.citation_ids) <= citation_ids for fact in context.facts)
    assert all(set(event.citation_ids) <= citation_ids for event in context.timeline)
    assert all(item.citation_id in citation_ids for item in context.unavailable_fields)


def test_citations_are_unique_ordered_and_have_no_orphans() -> None:
    context = _build_real_context()
    citation_ids = [entry.citation_id for entry in context.citations]
    used = {citation_id for fact in context.facts for citation_id in fact.citation_ids}
    used.update(
        citation_id for event in context.timeline for citation_id in event.citation_ids
    )
    used.update(item.citation_id for item in context.unavailable_fields)
    used.add(context.limitations.log_summary_citation_id)

    assert citation_ids == sorted(set(citation_ids))
    assert set(citation_ids) == used
    assert all(entry.source.fixture_id == INCIDENT_ID for entry in context.citations)
    assert all(
        entry.source.record_id == TRACE_ID
        for entry in context.citations
        if entry.source.record_type is EvidenceRecordType.TRACE
    )


def test_duplicate_observations_do_not_create_duplicate_fact_identities() -> None:
    context = _build_real_context()
    identities = [
        (
            fact.metric,
            fact.subject_record_type,
            fact.subject_record_id,
            fact.value,
            fact.unit,
            fact.origin,
        )
        for fact in context.facts
    ]

    assert len(identities) == len(set(identities))


def test_oversized_input_applies_all_limits_and_reports_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _oversized_fixture()
    monkeypatch.setattr(
        telemetry_tools,
        "load_telemetry_fixture",
        lambda incident_id: fixture,
    )

    context = build_investigation_context(collect_evidence(INCIDENT_ID, TRACE_ID))

    assert len(context.facts) <= MAX_TOTAL_FACTS
    assert context.limitations.facts_available > context.limitations.facts_included
    assert context.limitations.facts_truncated
    assert len(context.timeline) == MAX_TIMELINE_EVENTS
    assert context.limitations.timeline_truncated
    assert len(context.unavailable_fields) == MAX_UNAVAILABLE_FIELDS
    assert context.limitations.unavailable_fields_truncated
    assert context.limitations.logs_matched == 32
    assert context.limitations.logs_returned == 20
    assert context.limitations.original_log_search_truncated


def test_timeline_truncation_is_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _oversized_fixture()
    monkeypatch.setattr(
        telemetry_tools,
        "load_telemetry_fixture",
        lambda incident_id: fixture,
    )
    collection = collect_evidence(INCIDENT_ID, TRACE_ID)

    first = build_investigation_context(collection)
    second = build_investigation_context(collection)

    assert [event.event_id for event in first.timeline] == [
        event.event_id for event in second.timeline
    ]
    assert first.timeline == tuple(
        sorted(first.timeline, key=context_timeline_sort_key)
    )


def test_log_priority_and_bounds_are_deterministic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    levels = [
        "debug",
        "info",
        "warning",
        "error",
        "error",
        "warning",
        "info",
        "debug",
        "error",
        "warning",
        "info",
        "debug",
    ]
    fixture = _fixture_with_logs(levels, minimise_attributes=True)
    monkeypatch.setattr(
        telemetry_tools,
        "load_telemetry_fixture",
        lambda incident_id: fixture,
    )

    context = build_investigation_context(collect_evidence(INCIDENT_ID, TRACE_ID))
    log_facts = [fact for fact in context.facts if fact.category is FactCategory.LOG]

    assert len(log_facts) == MAX_LOG_FACTS
    assert [fact.subject_record_id for fact in log_facts] == [
        "synthetic-log-context-003",
        "synthetic-log-context-004",
        "synthetic-log-context-008",
        "synthetic-log-context-002",
        "synthetic-log-context-005",
    ]


def test_partial_collection_preserves_evidence_and_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_provider_attempts(*args: object, **kwargs: object) -> object:
        raise RuntimeError("private failure details")

    monkeypatch.setattr(
        telemetry_tools,
        "get_provider_attempts",
        fail_provider_attempts,
    )
    collection = collect_evidence(INCIDENT_ID, TRACE_ID)

    context = build_investigation_context(collection)

    assert context.collection_status is CollectionStatus.PARTIAL
    assert context.facts
    failure = context.tool_execution_summary.attempted[-1]
    assert failure.tool_name is TelemetryToolName.GET_PROVIDER_ATTEMPTS
    assert failure.status is ToolExecutionStatus.FAILED
    assert failure.error_code is CollectionErrorCode.TOOL_EXECUTION_FAILED
    assert failure.error_message == "A telemetry tool failed unexpectedly."
    assert "private failure" not in failure.error_message
    assert context.tool_execution_summary.not_executed == (
        TelemetryToolName.SEARCH_LOGS,
    )
    assert context.limitations.logs_matched is None


def test_failed_collection_without_observations_raises_safe_error() -> None:
    collection = collect_evidence("INC-UNKNOWN-999", TRACE_ID)

    with pytest.raises(
        NoUsableEvidenceError,
        match="contains no usable observations",
    ) as error:
        build_investigation_context(collection)

    assert error.value.code == "no_usable_evidence"


def test_context_contracts_reject_unknown_fields_and_mutation() -> None:
    context = _build_real_context()
    payload = context.model_dump()
    payload["diagnosis"] = "not allowed"

    with pytest.raises(ValidationError):
        InvestigationContext.model_validate(payload)
    with pytest.raises(ValidationError):
        context.context_id = "context-0000000000000000"
    with pytest.raises(ValidationError):
        context.facts[0].value = 1


def test_builder_consumes_only_collection_and_never_calls_tools_or_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collection = collect_evidence(INCIDENT_ID, TRACE_ID)

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("builder crossed its collection-only boundary")

    for tool_name in (
        "get_trace",
        "get_latency_breakdown",
        "get_provider_attempts",
        "search_logs",
    ):
        monkeypatch.setattr(telemetry_tools, tool_name, forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)

    assert build_investigation_context(collection).facts


def test_investigator_path_never_reads_ground_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if "ground_truth" in path.parts:
            raise AssertionError("investigator code accessed evaluation ground truth")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    context = build_investigation_context(collect_evidence(INCIDENT_ID, TRACE_ID))
    assert context.facts
    assert "ground_truth" not in context.model_dump_json()


def test_context_contains_no_diagnosis_or_recommendation_language() -> None:
    context = _build_real_context()
    forbidden = (
        "root cause",
        "bottleneck",
        "hypothesis",
        "confidence",
        "recommendation",
        "remediation",
    )

    descriptions = [fact.description for fact in context.facts]
    descriptions.extend(event.description for event in context.timeline)
    assert all(
        not any(word in description.lower() for word in forbidden)
        for description in descriptions
    )
