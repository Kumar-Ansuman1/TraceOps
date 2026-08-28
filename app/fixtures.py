"""Safe, deterministic loading for investigator-visible telemetry fixtures."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.telemetry import TelemetryFixture

DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "incidents"
MAX_FIXTURE_BYTES = 1_000_000
INCIDENT_ID_PATTERN = re.compile(r"^INC-[A-Z0-9-]+$")

FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "api_key",
        "access_token",
        "audio",
        "authorization",
        "candidate_answer",
        "candidate_email",
        "candidate_name",
        "candidate_phone",
        "cookie",
        "email",
        "expected_incident_class",
        "full_name",
        "known_root_cause",
        "password",
        "phone",
        "refresh_token",
        "request_body",
        "response_body",
        "resume_text",
        "secret",
        "token",
    }
)

FORBIDDEN_VALUE_PATTERNS = (
    re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.IGNORECASE),
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


class FixtureLoadError(ValueError):
    """Raised when a fixture is missing, malformed, unsafe, or schema-invalid."""


class FixtureInputError(FixtureLoadError):
    """Raised when a caller supplies an unsafe fixture identifier or path."""


class FixtureNotFoundError(FixtureLoadError):
    """Raised when no fixture exists for a valid incident identifier."""


class MalformedFixtureError(FixtureLoadError):
    """Raised when fixture bytes or relationships do not satisfy the contract."""


class UnsafeFixtureError(FixtureLoadError):
    """Raised when fixture content contains a forbidden field or value."""


def _normalise_field_name(field_name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", field_name.strip().lower()).strip("_")


def _is_forbidden_field(field_name: str) -> bool:
    normalised_field = _normalise_field_name(field_name)
    return any(
        normalised_field == forbidden or normalised_field.endswith(f"_{forbidden}")
        for forbidden in FORBIDDEN_FIELD_NAMES
    )


def _reject_unsafe_content(value: Any, location: str = "fixture") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_forbidden_field(str(key)):
                raise UnsafeFixtureError(f"unsafe field '{key}' found at {location}")
            _reject_unsafe_content(child, f"{location}.{key}")
        return

    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_unsafe_content(child, f"{location}[{index}]")
        return

    if isinstance(value, str):
        for pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(value):
                raise UnsafeFixtureError(f"sensitive value found at {location}")


def load_telemetry_fixture(
    incident_id: str,
    fixtures_dir: Path = DEFAULT_FIXTURE_DIR,
) -> TelemetryFixture:
    """Load one bounded fixture after path, safety, and schema validation."""

    if not INCIDENT_ID_PATTERN.fullmatch(incident_id):
        raise FixtureInputError("incident ID has an invalid or unsafe format")

    fixture_root = fixtures_dir.resolve()
    fixture_path = (fixture_root / f"{incident_id}.json").resolve()
    if not fixture_path.is_relative_to(fixture_root):
        raise FixtureInputError("fixture path escapes the configured directory")

    try:
        file_size = fixture_path.stat().st_size
    except FileNotFoundError as exc:
        raise FixtureNotFoundError(
            f"fixture not found for incident {incident_id}"
        ) from exc

    if file_size > MAX_FIXTURE_BYTES:
        raise MalformedFixtureError("fixture exceeds the maximum allowed size")

    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MalformedFixtureError("fixture is not valid UTF-8 JSON") from exc

    _reject_unsafe_content(payload)

    try:
        fixture = TelemetryFixture.model_validate(payload)
    except ValidationError as exc:
        raise MalformedFixtureError(
            "fixture does not match the telemetry contract"
        ) from exc

    if fixture.metadata.fixture_id != incident_id:
        raise MalformedFixtureError(
            "requested incident ID does not match fixture metadata"
        )

    return fixture
