from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas import (
    Confidence,
    ConfidenceLabel,
    Environment,
    IncidentIntake,
    InvestigationMetadata,
    InvestigationReport,
    InvestigationStatus,
)


def intake_payload() -> dict[str, object]:
    return {
        "incident_id": "INC-SLOW-001",
        "service": "intervai",
        "environment": Environment.PRODUCTION,
        "symptom": "Answer analysis exceeded its normal latency",
    }


def test_intake_accepts_request_id() -> None:
    payload = intake_payload()
    payload["request_id"] = "request-123"

    intake = IncidentIntake.model_validate(payload)

    assert intake.request_id == "request-123"


def test_intake_accepts_bounded_time_window() -> None:
    payload = intake_payload()
    payload["started_at"] = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
    payload["ended_at"] = datetime(2026, 8, 26, 8, 5, tzinfo=UTC)

    intake = IncidentIntake.model_validate(payload)

    assert intake.ended_at is not None


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"started_at": datetime(2026, 8, 26, 8, 0, tzinfo=UTC)},
        {
            "started_at": datetime(2026, 8, 26, 8, 5, tzinfo=UTC),
            "ended_at": datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
        },
        {
            "started_at": datetime(2026, 8, 26, 8, 0, tzinfo=UTC),
            "ended_at": datetime(2026, 8, 27, 8, 1, tzinfo=UTC),
        },
    ],
)
def test_intake_rejects_invalid_correlation_scope(fields: dict[str, object]) -> None:
    payload = intake_payload()
    payload.update(fields)

    with pytest.raises(ValidationError):
        IncidentIntake.model_validate(payload)


def report_payload() -> dict[str, object]:
    return {
        "incident_id": "INC-SLOW-001",
        "status": InvestigationStatus.DIAGNOSED,
        "executive_summary": "The provider attempt exceeded its timeout.",
        "likely_root_cause": "The primary provider timed out.",
        "confidence": Confidence(score=0.4, label=ConfidenceLabel.LOW),
        "investigation_metadata": InvestigationMetadata(
            duration_ms=10,
            tool_call_count=0,
            workflow_version="deterministic-v0",
        ),
    }


def test_low_confidence_report_cannot_claim_diagnosis() -> None:
    with pytest.raises(ValidationError):
        InvestigationReport.model_validate(report_payload())


def test_insufficient_evidence_report_cannot_claim_root_cause() -> None:
    payload = report_payload()
    payload["status"] = InvestigationStatus.INSUFFICIENT_EVIDENCE

    with pytest.raises(ValidationError):
        InvestigationReport.model_validate(payload)
