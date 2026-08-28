import json
from pathlib import Path

import pytest

from app.fixtures import FixtureLoadError, load_telemetry_fixture
from app.telemetry import TelemetryOrigin

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"
INCIDENTS_DIR = FIXTURES_DIR / "incidents"
GROUND_TRUTH_DIR = FIXTURES_DIR / "ground_truth"


def fixture_payload() -> dict[str, object]:
    return json.loads((INCIDENTS_DIR / "INC-SLOW-001.json").read_text())


def write_fixture(directory: Path, payload: object) -> None:
    directory.mkdir()
    (directory / "INC-SLOW-001.json").write_text(json.dumps(payload))


def test_valid_synthetic_fixture_loads() -> None:
    fixture = load_telemetry_fixture("INC-SLOW-001")

    assert fixture.metadata.telemetry_origin is TelemetryOrigin.SYNTHETIC
    assert fixture.incident.service == "intervai"
    assert fixture.incident.task_type == "answer_analysis"
    assert fixture.telemetry.trace.duration_ms == 15_420.0
    assert len(fixture.telemetry.trace.spans) == 6


def test_evaluation_labels_are_separate_from_investigator_evidence() -> None:
    investigator_payload = fixture_payload()
    ground_truth = json.loads((GROUND_TRUTH_DIR / "INC-SLOW-001.json").read_text())

    serialized_evidence = json.dumps(investigator_payload)
    assert "expected_incident_class" not in serialized_evidence
    assert "known_root_cause" not in serialized_evidence
    assert ground_truth["expected_incident_class"] == "slow_llm_request"
    assert ground_truth["telemetry_origin"] == "synthetic"


def test_loader_rejects_malformed_json(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "incidents"
    fixture_dir.mkdir()
    (fixture_dir / "INC-SLOW-001.json").write_text("{not-json")

    with pytest.raises(FixtureLoadError, match="valid UTF-8 JSON"):
        load_telemetry_fixture("INC-SLOW-001", fixture_dir)


def test_loader_rejects_contract_violations(tmp_path: Path) -> None:
    payload = fixture_payload()
    payload["unexpected_field"] = "not allowed"
    fixture_dir = tmp_path / "incidents"
    write_fixture(fixture_dir, payload)

    with pytest.raises(FixtureLoadError, match="telemetry contract"):
        load_telemetry_fixture("INC-SLOW-001", fixture_dir)


def test_loader_rejects_sensitive_fields(tmp_path: Path) -> None:
    payload = fixture_payload()
    payload["telemetry"]["trace"]["spans"][0]["attributes"]["api_key"] = (
        "not-a-real-key"
    )
    fixture_dir = tmp_path / "incidents"
    write_fixture(fixture_dir, payload)

    with pytest.raises(FixtureLoadError, match="unsafe field 'api_key'"):
        load_telemetry_fixture("INC-SLOW-001", fixture_dir)


def test_loader_rejects_sensitive_values(tmp_path: Path) -> None:
    payload = fixture_payload()
    payload["telemetry"]["logs"][0]["message"] = "Contact person@example.com"
    fixture_dir = tmp_path / "incidents"
    write_fixture(fixture_dir, payload)

    with pytest.raises(FixtureLoadError, match="sensitive value"):
        load_telemetry_fixture("INC-SLOW-001", fixture_dir)


def test_loader_rejects_evaluation_label_leakage(tmp_path: Path) -> None:
    payload = fixture_payload()
    payload["telemetry"]["trace"]["spans"][0]["attributes"]["known_root_cause"] = (
        "hidden answer"
    )
    fixture_dir = tmp_path / "incidents"
    write_fixture(fixture_dir, payload)

    with pytest.raises(FixtureLoadError, match="unsafe field 'known_root_cause'"):
        load_telemetry_fixture("INC-SLOW-001", fixture_dir)


@pytest.mark.parametrize("incident_id", ["../INC-SLOW-001", "inc-slow-001", "INC_1"])
def test_loader_rejects_unsafe_incident_ids(incident_id: str) -> None:
    with pytest.raises(FixtureLoadError, match="invalid or unsafe format"):
        load_telemetry_fixture(incident_id)


def test_loader_rejects_unlabelled_synthetic_identifiers(tmp_path: Path) -> None:
    payload = fixture_payload()
    payload["incident"]["request_id"] = "request-that-looks-recorded"
    payload["telemetry"]["trace"]["request_id"] = "request-that-looks-recorded"
    fixture_dir = tmp_path / "incidents"
    write_fixture(fixture_dir, payload)

    with pytest.raises(FixtureLoadError, match="telemetry contract"):
        load_telemetry_fixture("INC-SLOW-001", fixture_dir)
