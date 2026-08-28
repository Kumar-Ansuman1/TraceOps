from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app import telemetry_tools
from app.fixtures import MalformedFixtureError, load_telemetry_fixture
from app.telemetry import LogLevel, TelemetryFixture
from app.telemetry_tools import (
    InvalidFilterError,
    MalformedTelemetryError,
    SpanNotFoundError,
    TraceMismatchError,
    UnknownIncidentError,
    UnsafeToolInputError,
    get_latency_breakdown,
    get_provider_attempts,
    get_trace,
    search_logs,
)
from app.tool_contracts import (
    GetTraceResult,
    ValueAvailability,
    ValueOrigin,
)

INCIDENT_ID = "INC-SLOW-001"
TRACE_ID = "synthetic-trace-slow-001"
ANALYSIS_SPAN_ID = "synthetic-span-analysis-001"
PROVIDER_SPAN_ID = "synthetic-span-provider-001"
FIXTURE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "incidents"
    / f"{INCIDENT_ID}.json"
)


def _walk_models(value: Any) -> list[BaseModel]:
    models: list[BaseModel] = []
    if isinstance(value, BaseModel):
        models.append(value)
        for field_value in value.__dict__.values():
            models.extend(_walk_models(field_value))
    elif isinstance(value, (list, tuple)):
        for item in value:
            models.extend(_walk_models(item))
    return models


def _fixture_with_many_logs(count: int) -> TelemetryFixture:
    fixture = load_telemetry_fixture(INCIDENT_ID)
    payload = fixture.model_dump(mode="json")
    started_at = fixture.telemetry.trace.started_at
    payload["telemetry"]["logs"] = [
        {
            "log_id": f"synthetic-log-bounded-{index:03d}",
            "observed_at": (started_at + timedelta(milliseconds=index)).isoformat(),
            "level": "info",
            "message": f"Synthetic bounded log {index}.",
            "span_id": ANALYSIS_SPAN_ID,
            "attributes": {},
        }
        for index in range(count)
    ]
    return TelemetryFixture.model_validate(payload)


def test_get_trace_extracts_valid_fixture_with_stable_references() -> None:
    result = get_trace(INCIDENT_ID, TRACE_ID)

    assert result.scope.incident_id == INCIDENT_ID
    assert result.trace.duration_ms.value == 15_420.0
    assert result.trace.duration_ms.origin is ValueOrigin.RECORDED
    assert len(result.spans) == 6
    assert result.trace.duration_ms.sources[0].source_ref == (
        "fixture://INC-SLOW-001/trace/synthetic-trace-slow-001#duration_ms"
    )

    for model in _walk_models(result):
        if hasattr(model, "sources"):
            sources = model.sources
            assert sources
            assert all(source.source_ref.startswith("fixture://") for source in sources)


def test_tool_output_contract_is_strict_and_immutable() -> None:
    result = get_trace(INCIDENT_ID, TRACE_ID)
    payload = result.model_dump()
    payload["unexpected"] = "not allowed"

    with pytest.raises(ValidationError):
        GetTraceResult.model_validate(payload)
    with pytest.raises(ValidationError):
        result.scope.incident_id = "INC-CHANGED"


def test_latency_breakdown_uses_exact_recorded_timings() -> None:
    result = get_latency_breakdown(
        INCIDENT_ID,
        TRACE_ID,
        span_id=PROVIDER_SPAN_ID,
    )
    provider = result.spans[0]

    assert provider.recorded_duration_ms.value == 14_800.0
    assert provider.recorded_duration_ms.origin is ValueOrigin.RECORDED
    assert provider.start_offset_ms.value == 310.0
    assert provider.end_offset_ms.value == 15_110.0
    assert provider.coverage_of_trace_percent.value == round(
        (14_800.0 / 15_420.0) * 100,
        6,
    )
    assert provider.start_offset_ms.origin is ValueOrigin.CALCULATED
    assert len(provider.start_offset_ms.sources) == 2


def test_provider_attempts_use_only_recorded_provider_span_attributes() -> None:
    result = get_provider_attempts(INCIDENT_ID, TRACE_ID)

    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.span_id == PROVIDER_SPAN_ID
    assert attempt.provider_name.value == "synthetic-provider"
    assert attempt.model.value == "synthetic-answer-model"
    assert attempt.input_tokens.value == 640
    assert attempt.output_tokens.value == 180
    assert attempt.retry_count.value == 0
    assert attempt.retry_count.origin is ValueOrigin.RECORDED


def test_missing_provider_attributes_are_explicitly_unavailable() -> None:
    attempt = get_provider_attempts(INCIDENT_ID, TRACE_ID).attempts[0]

    assert attempt.error_type.value is None
    assert attempt.error_type.availability is ValueAvailability.UNAVAILABLE
    assert attempt.error_type.origin is ValueOrigin.UNAVAILABLE
    assert attempt.error_type.sources[0].source_ref.endswith("#attributes.error.type")
    assert attempt.error_message.value is None


def test_search_logs_filters_by_incident_trace_span_and_level() -> None:
    result = search_logs(
        INCIDENT_ID,
        TRACE_ID,
        span_id=ANALYSIS_SPAN_ID,
        level=LogLevel.INFO,
        limit=10,
    )

    assert result.summary.matched_count == 2
    assert result.summary.returned_count == 2
    assert not result.summary.truncated
    assert {log.level.value for log in result.logs} == {"info"}
    assert {log.span_id.value for log in result.logs} == {ANALYSIS_SPAN_ID}
    assert all(
        log.message.sources[0].source_ref.startswith(
            "fixture://INC-SLOW-001/log/synthetic-log-"
        )
        for log in result.logs
    )


def test_log_search_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    expanded_fixture = _fixture_with_many_logs(60)
    monkeypatch.setattr(
        telemetry_tools,
        "load_telemetry_fixture",
        lambda incident_id: expanded_fixture,
    )

    result = search_logs(INCIDENT_ID, TRACE_ID, level="info", limit=3)

    assert result.summary.matched_count == 60
    assert result.summary.returned_count == 3
    assert result.summary.truncated
    assert len(result.logs) == 3

    with pytest.raises(InvalidFilterError, match="between 1 and 50"):
        search_logs(INCIDENT_ID, TRACE_ID, limit=51)


@pytest.mark.parametrize("level", ["INFO", "critical", 10])
def test_log_search_rejects_invalid_level_filters(level: object) -> None:
    with pytest.raises(InvalidFilterError):
        search_logs(INCIDENT_ID, TRACE_ID, level=level)  # type: ignore[arg-type]


def test_tools_return_controlled_lookup_errors() -> None:
    with pytest.raises(UnknownIncidentError, match="unknown incident"):
        get_trace("INC-UNKNOWN-999", TRACE_ID)
    with pytest.raises(TraceMismatchError, match="does not belong"):
        get_trace(INCIDENT_ID, "synthetic-trace-other-001")
    with pytest.raises(SpanNotFoundError, match="was not found"):
        get_latency_breakdown(
            INCIDENT_ID,
            TRACE_ID,
            span_id="synthetic-span-missing-001",
        )
    with pytest.raises(SpanNotFoundError, match="was not found"):
        search_logs(
            INCIDENT_ID,
            TRACE_ID,
            span_id="synthetic-span-missing-001",
        )


@pytest.mark.parametrize(
    ("incident_id", "trace_id"),
    [
        ("../INC-SLOW-001", TRACE_ID),
        (INCIDENT_ID, "../../unsafe-trace"),
    ],
)
def test_tools_reject_unsafe_identifiers(incident_id: str, trace_id: str) -> None:
    with pytest.raises(UnsafeToolInputError, match="invalid or unsafe format"):
        get_trace(incident_id, trace_id)


def test_malformed_fixture_failure_is_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_malformed(incident_id: str) -> TelemetryFixture:
        raise MalformedFixtureError(f"malformed {incident_id}")

    monkeypatch.setattr(
        telemetry_tools,
        "load_telemetry_fixture",
        raise_malformed,
    )

    with pytest.raises(MalformedTelemetryError, match="unsafe or malformed"):
        get_trace(INCIDENT_ID, TRACE_ID)


def test_tools_never_read_evaluation_ground_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_read_text = Path.read_text

    def guarded_read_text(path: Path, *args: Any, **kwargs: Any) -> str:
        if "ground_truth" in path.parts:
            raise AssertionError("telemetry tools accessed evaluation ground truth")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read_text)

    get_trace(INCIDENT_ID, TRACE_ID)
    get_latency_breakdown(INCIDENT_ID, TRACE_ID)
    get_provider_attempts(INCIDENT_ID, TRACE_ID)
    search_logs(INCIDENT_ID, TRACE_ID)


def test_tool_calls_do_not_modify_fixture_evidence() -> None:
    before = FIXTURE_PATH.read_bytes()

    get_trace(INCIDENT_ID, TRACE_ID)
    get_latency_breakdown(INCIDENT_ID, TRACE_ID)
    get_provider_attempts(INCIDENT_ID, TRACE_ID)
    search_logs(INCIDENT_ID, TRACE_ID)

    assert FIXTURE_PATH.read_bytes() == before
