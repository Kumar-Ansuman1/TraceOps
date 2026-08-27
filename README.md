# TraceOps

TraceOps is an AI production incident investigator. It is being built first for
IntervAI and will diagnose incidents using bounded, read-only evidence from
traces, logs, provider attempts, routing records, configuration snapshots, and
runbooks.

TraceOps is not a chatbot and does not modify production systems. Its output is
an evidence-backed root-cause analysis for human review.

## Initial scope

The MVP supports three incident categories:

- Slow LLM requests
- Model-provider or API failures
- Incorrect fallback routing

The repository currently contains the M1 foundation:

- FastAPI application with a health endpoint
- Pydantic contracts for intake, evidence, hypotheses, and reports
- Strict telemetry-fixture contracts and a read-only fixture loader
- One explicitly synthetic slow `answer_analysis` fixture
- Evaluation ground truth stored separately from investigator-visible evidence
- Contract tests for correlation rules and diagnosis safety
- Architecture and requirements specification

LangGraph, LLM calls, Qdrant, live telemetry access, remediation actions, the
investigation endpoint, dashboard, Docker, and AWS are intentionally not part of
this milestone.

## Project structure

| Path | Purpose |
| --- | --- |
| `app/main.py` | FastAPI application boundary |
| `app/schemas.py` | Validated investigation data contracts |
| `app/telemetry.py` | Validated trace and log fixture contracts |
| `app/fixtures.py` | Safe, read-only incident fixture loader |
| `fixtures/incidents/` | Redacted, reproducible incident telemetry |
| `fixtures/ground_truth/` | Evaluation-only labels hidden from investigators |
| `tests/` | API and contract tests |
| `ARCHITECTURE_AND_REQUIREMENTS.md` | MVP scope, workflow, safety, and evaluation design |

## Local setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
```

Activate the environment on Windows:

```powershell
.venv\Scripts\Activate.ps1
```

Activate it on macOS or Linux:

```bash
source .venv/bin/activate
```

Install the project and development dependencies:

```bash
python -m pip install -e ".[dev]"
```

Run the API:

```bash
uvicorn app.main:app --reload
```

Check the health endpoint:

```bash
curl http://127.0.0.1:8000/health
```

Run the tests:

```bash
pytest
```

## Current API

### `GET /health`

Returns the service name, version, and health status. The investigation endpoint
has not been implemented yet.

## Current fixture

`INC-SLOW-001` is synthetic test telemetry, not a recorded production incident.
Its identifiers, timestamps, durations, spans, token counts, and designed root
cause are invented solely to exercise the contract and loader. It must not be
used to make claims about IntervAI's production behavior.

## Safety boundary

The MVP has no production-write tools. Restarting services, changing models or
configuration, deploying code, writing data, and sending notifications are all
outside the current system boundary.
