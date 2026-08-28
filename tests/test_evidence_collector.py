from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app import telemetry_tools
from app.evidence_collector import collect_evidence
from app.evidence_contracts import (
    TOOL_EXECUTION_ORDER,
    CollectionErrorCode,
    CollectionStatus,
    EvidenceCollectionResult,
    TelemetryToolName,
    TimelineEventKind,
    ToolExecutionStatus,
    timeline_sort_key,
)
from app.fixtures import MalformedFixtureError
from app.telemetry import TelemetryFixture

INCIDENT_ID = "INC-SLOW-001"
TRACE_ID = "synthetic-trace-slow-001"
PROVIDER_SPAN_ID = "synthetic-span-provider-001"
FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "incidents"
    / f"{INCIDENT_ID}.json"
)


def test_valid_evidence_collection_returns_observations_only() -> None:
    result = collect_evidence(INCIDENT_ID, TRACE_ID)

    assert result.status is CollectionStatus.COMPLETED
    assert len(result.tool_executions) == 4
    assert len(result.observations) == 4
    assert all(
        execution.status is ToolExecutionStatus.SUCCEEDED
        for execution in result.tool_executions
    )
    assert [observation.tool for observation in result.observations] == [
        tool.value for tool in TOOL_EXECUTION_ORDER
    ]
    assert result.timeline
    assert result.citation_catalog.entries


def test_collection_contracts_are_strict_and_immutable() -> None:
    result = collect_evidence(INCIDENT_ID, TRACE_ID)
    payload = result.model_dump()
    payload["hypotheses"] = []

    with pytest.raises(ValidationError):
        EvidenceCollectionResult.model_validate(payload)
    with pytest.raises(ValidationError):
        result.status = CollectionStatus.FAILED
    with pytest.raises(ValidationError):
        result.tool_executions[0].execution_order = 4


def test_tools_run_once_in_fixed_order_with_at_most_four_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[TelemetryToolName] = []
    originals: dict[TelemetryToolName, Callable[..., object]] = {
        TelemetryToolName.GET_TRACE: telemetry_tools.get_trace,
        TelemetryToolName.GET_LATENCY_BREAKDOWN: (
            telemetry_tools.get_latency_breakdown
        ),
        TelemetryToolName.GET_PROVIDER_ATTEMPTS: (
            telemetry_tools.get_provider_attempts
        ),
        TelemetryToolName.SEARCH_LOGS: telemetry_tools.search_logs,
    }

    for tool_name, original in originals.items():

        def wrapper(
            *args: object,
            _tool_name: TelemetryToolName = tool_name,
            _original: Callable[..., object] = original,
            **kwargs: object,
        ) -> object:
            calls.append(_tool_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(telemetry_tools, tool_name.value, wrapper)

    result = collect_evidence(INCIDENT_ID, TRACE_ID)

    assert result.status is CollectionStatus.COMPLETED
    assert tuple(calls) == TOOL_EXECUTION_ORDER
    assert len(calls) == 4
    assert Counter(calls) == Counter({tool_name: 1 for tool_name in calls})


def test_repeated_collection_output_is_deterministic() -> None:
    first = collect_evidence(INCIDENT_ID, TRACE_ID)
    second = collect_evidence(INCIDENT_ID, TRACE_ID)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_timeline_is_chronological_with_deterministic_ties() -> None:
    result = collect_evidence(INCIDENT_ID, TRACE_ID)

    assert result.timeline == tuple(sorted(result.timeline, key=timeline_sort_key))

    at_trace_start = [
        event
        for event in result.timeline
        if event.occurred_at.isoformat() == "2026-08-27T04:30:00+00:00"
    ]
    assert [event.event_kind for event in at_trace_start] == [
        TimelineEventKind.TRACE_STARTED,
        TimelineEventKind.SPAN_STARTED,
    ]

    at_analysis_start = [
        event
        for event in result.timeline
        if event.occurred_at.isoformat() == "2026-08-27T04:30:00.100000+00:00"
    ]
    assert [event.event_kind for event in at_analysis_start] == [
        TimelineEventKind.SPAN_STARTED,
        TimelineEventKind.LOG_RECORDED,
    ]

    at_provider_end = [
        event
        for event in result.timeline
        if event.occurred_at.isoformat() == "2026-08-27T04:30:15.110000+00:00"
    ]
    assert [event.event_kind for event in at_provider_end] == [
        TimelineEventKind.SPAN_STARTED,
        TimelineEventKind.SPAN_ENDED,
    ]


def test_timeline_uses_recorded_timestamp_sources_and_stable_citations() -> None:
    first = collect_evidence(INCIDENT_ID, TRACE_ID)
    second = collect_evidence(INCIDENT_ID, TRACE_ID)
    first_citations = {event.timeline_id: event.source_refs for event in first.timeline}
    second_citations = {
        event.timeline_id: event.source_refs for event in second.timeline
    }

    assert first_citations == second_citations
    catalog_refs = {entry.source.source_ref for entry in first.citation_catalog.entries}
    assert all(set(event.source_refs) <= catalog_refs for event in first.timeline)
    assert all(
        any(
            source_ref.endswith(("#started_at", "#ended_at", "#observed_at"))
            for source_ref in event.source_refs
        )
        for event in first.timeline
    )
    assert all(
        "offset" not in source_ref
        for event in first.timeline
        for source_ref in event.source_refs
    )

    provider_start = next(
        event
        for event in first.timeline
        if event.event_kind is TimelineEventKind.SPAN_STARTED
        and event.record_id == PROVIDER_SPAN_ID
    )
    assert set(provider_start.source_refs) == {
        ("fixture://INC-SLOW-001/span/synthetic-span-provider-001#record"),
        ("fixture://INC-SLOW-001/span/synthetic-span-provider-001#name"),
        ("fixture://INC-SLOW-001/span/synthetic-span-provider-001#started_at"),
    }


def test_citation_catalog_deduplicates_repeated_tool_sources() -> None:
    result = collect_evidence(INCIDENT_ID, TRACE_ID)
    source_refs = [entry.source.source_ref for entry in result.citation_catalog.entries]
    repeated_tool_ref = (
        "fixture://INC-SLOW-001/span/synthetic-span-provider-001#duration_ms"
    )

    assert source_refs == sorted(set(source_refs))
    assert source_refs.count(repeated_tool_ref) == 1
    assert (
        sum(
            repeated_tool_ref in execution.returned_source_refs
            for execution in result.tool_executions
        )
        == 3
    )


def test_unavailable_fields_are_deduplicated_without_relevance_labels() -> None:
    result = collect_evidence(INCIDENT_ID, TRACE_ID)
    by_field = {
        observation.source.field_path: observation
        for observation in result.unavailable_fields
    }

    assert set(by_field) == {
        "parent_span_id",
        "attributes.error.type",
        "attributes.error.message",
    }
    assert by_field["parent_span_id"].observed_by == (
        TelemetryToolName.GET_TRACE,
        TelemetryToolName.GET_LATENCY_BREAKDOWN,
    )
    assert by_field["attributes.error.type"].observed_by == (
        TelemetryToolName.GET_PROVIDER_ATTEMPTS,
    )
    unavailable_payload = [
        item.model_dump(mode="json") for item in result.unavailable_fields
    ]
    assert all("relevance" not in item for item in unavailable_payload)
    assert all("required" not in item for item in unavailable_payload)


def test_partial_result_preserves_evidence_after_non_fatal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_called = False

    def fail_provider_attempts(*args: object, **kwargs: object) -> object:
        raise RuntimeError("internal details must not escape")

    monkeypatch.setattr(
        telemetry_tools,
        "get_provider_attempts",
        fail_provider_attempts,
    )
    original_search_logs = telemetry_tools.search_logs

    def guarded_search(*args: object, **kwargs: object) -> object:
        nonlocal search_called
        search_called = True
        return original_search_logs(*args, **kwargs)

    monkeypatch.setattr(telemetry_tools, "search_logs", guarded_search)

    result = collect_evidence(INCIDENT_ID, TRACE_ID)

    assert result.status is CollectionStatus.PARTIAL
    assert [observation.tool for observation in result.observations] == [
        "get_trace",
        "get_latency_breakdown",
    ]
    assert len(result.tool_executions) == 3
    failure = result.tool_executions[-1]
    assert failure.tool_name is TelemetryToolName.GET_PROVIDER_ATTEMPTS
    assert failure.status is ToolExecutionStatus.FAILED
    assert failure.error_code is CollectionErrorCode.TOOL_EXECUTION_FAILED
    assert failure.error_message == "A telemetry tool failed unexpectedly."
    assert "internal details" not in failure.error_message
    assert not search_called
    assert result.timeline
    assert result.citation_catalog.entries


@pytest.mark.parametrize(
    ("incident_id", "trace_id", "error_code"),
    [
        (
            "../INC-SLOW-001",
            TRACE_ID,
            CollectionErrorCode.UNSAFE_INPUT,
        ),
        (
            INCIDENT_ID,
            "../../unsafe-trace",
            CollectionErrorCode.UNSAFE_INPUT,
        ),
        (
            "INC-UNKNOWN-999",
            TRACE_ID,
            CollectionErrorCode.UNKNOWN_INCIDENT,
        ),
        (
            INCIDENT_ID,
            "synthetic-trace-other-001",
            CollectionErrorCode.TRACE_MISMATCH,
        ),
    ],
)
def test_scope_errors_are_controlled_fatal_failures(
    incident_id: str,
    trace_id: str,
    error_code: CollectionErrorCode,
) -> None:
    result = collect_evidence(incident_id, trace_id)

    assert result.status is CollectionStatus.FAILED
    assert result.observations == ()
    assert result.timeline == ()
    assert result.unavailable_fields == ()
    assert result.citation_catalog.entries == ()
    assert len(result.tool_executions) == 1
    failure = result.tool_executions[0]
    assert failure.tool_name is TelemetryToolName.GET_TRACE
    assert failure.error_code is error_code
    assert failure.error_message


def test_malformed_fixture_is_a_controlled_fatal_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_malformed(incident_id: str) -> TelemetryFixture:
        raise MalformedFixtureError(f"malformed {incident_id}")

    monkeypatch.setattr(
        telemetry_tools,
        "load_telemetry_fixture",
        raise_malformed,
    )

    result = collect_evidence(INCIDENT_ID, TRACE_ID)

    assert result.status is CollectionStatus.FAILED
    assert result.observations == ()
    assert result.tool_executions[0].error_code is (
        CollectionErrorCode.MALFORMED_FIXTURE
    )
    assert result.tool_executions[0].error_message == (
        "The incident telemetry fixture is unsafe or malformed."
    )


def test_collector_never_reads_evaluation_ground_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if "ground_truth" in path.parts:
            raise AssertionError("collector accessed evaluation ground truth")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    assert collect_evidence(INCIDENT_ID, TRACE_ID).status is (
        CollectionStatus.COMPLETED
    )


def test_collection_does_not_modify_fixture_evidence() -> None:
    before = FIXTURE_PATH.read_bytes()

    collect_evidence(INCIDENT_ID, TRACE_ID)

    assert FIXTURE_PATH.read_bytes() == before


def test_result_has_no_diagnostic_or_recommendation_fields() -> None:
    result = collect_evidence(INCIDENT_ID, TRACE_ID)
    forbidden_fields = {
        "diagnosis",
        "diagnostic",
        "incident_class",
        "root_cause",
        "likely_root_cause",
        "hypothesis",
        "hypotheses",
        "confidence",
        "recommendation",
        "recommendations",
    }

    def collect_keys(value: object) -> set[str]:
        keys: set[str] = set()
        if isinstance(value, dict):
            keys.update(str(key) for key in value)
            for child in value.values():
                keys.update(collect_keys(child))
        elif isinstance(value, list):
            for child in value:
                keys.update(collect_keys(child))
        return keys

    payload = result.model_dump(mode="json")
    assert not (collect_keys(payload) & forbidden_fields)
    assert all(
        not any(
            word in event.description.lower()
            for word in ("cause", "hypothesis", "recommend")
        )
        for event in result.timeline
    )
