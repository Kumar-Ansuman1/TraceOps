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

The repository currently contains the deterministic investigation foundation:

- FastAPI application with a health endpoint
- Pydantic contracts for intake, evidence, hypotheses, and reports
- Strict telemetry-fixture contracts and a read-only fixture loader
- One explicitly synthetic slow `answer_analysis` fixture
- Fixture-backed tools for traces, latency timings, provider spans, and logs
- Stable fixture record references on every returned observation
- A deterministic evidence collector with fixed tool ordering and no retries
- Recorded-time timeline, unavailable-field inventory, and citation catalog
- A bounded evidence-context builder with deterministic fact selection
- Deduplicated facts and citation-closed, LLM-ready context contracts
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
| `app/tool_contracts.py` | Strict tool-result and evidence-reference contracts |
| `app/telemetry_tools.py` | Fixture-backed read-only telemetry tools |
| `app/evidence_contracts.py` | Immutable evidence-collection result contracts |
| `app/evidence_collector.py` | Deterministic evidence-collection coordinator |
| `app/context_contracts.py` | Strict contracts for bounded investigation context |
| `app/evidence_context.py` | Deterministic evidence-context builder |
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

## Fixture-backed telemetry tools

TraceOps currently has four Python telemetry tools. They are not HTTP endpoints:

- `get_trace(incident_id, trace_id)` returns the exact validated trace and spans.
- `get_latency_breakdown(incident_id, trace_id, span_id=None)` returns recorded
  durations plus calculated start offsets, end offsets, and trace-coverage
  percentages. Each value says whether it was recorded or calculated.
- `get_provider_attempts(incident_id, trace_id)` extracts only spans containing
  supported provider attributes. Missing provider, model, token, retry, or error
  attributes are returned as unavailable with a null value.
- `search_logs(incident_id, trace_id, span_id=None, level=None, limit=20)` filters
  only the selected fixture's logs and returns at most 50 records per call.

All four tools use `load_telemetry_fixture()` as their only entrance to incident
evidence. Returned observations carry stable references such as
`fixture://INC-SLOW-001/span/synthetic-span-provider-001#duration_ms`, so later
milestones can cite an exact fixture, record, and field. Tool calls do not write
to or modify fixture evidence.

These tools return observations only. They do not classify the incident, form a
hypothesis, identify a root cause, or recommend an action.

## Deterministic evidence collector

`collect_evidence(incident_id, trace_id)` coordinates the existing telemetry
tools for one exact incident and trace. It does not read fixture files itself;
each tool continues to use `load_telemetry_fixture()` as the only path to
incident evidence.

Every collection uses this fixed order:

1. `get_trace`
2. `get_latency_breakdown`
3. `get_provider_attempts`
4. `search_logs`

Each tool is called at most once and there are no retries. Every attempted call
gets a deterministic call ID, execution order, scope, outcome, controlled error
when applicable, and the exact source references returned by that tool.

The collection status is:

- `completed` when all four tools succeed.
- `partial` when one or more tools returned evidence before a later non-fatal
  tool failure.
- `failed` when no evidence was collected or when unsafe input, an unknown
  incident, a trace mismatch, or a malformed fixture causes a fatal failure.

Collection stops at the first failure. Evidence returned by earlier successful
tools is preserved. The result also contains:

- A timeline made only from recorded trace, span, and log timestamps. Equal
  timestamps use a stable event-type and record-ID tie-break order.
- A deduplicated inventory of fields that the tools explicitly marked
  unavailable. These fields are not labelled relevant or required.
- A deduplicated catalog containing every exact fixture source reference
  returned by successful tools.

The collector returns observations only. It does not classify the incident,
generate hypotheses, identify a root cause, calculate confidence, or recommend
actions. It is a Python workflow, not a new API endpoint.

Example:

```python
from app.evidence_collector import collect_evidence

result = collect_evidence(
    incident_id="INC-SLOW-001",
    trace_id="synthetic-trace-slow-001",
)
print(result.status.value)  # completed
```

## Deterministic evidence-context builder

`build_investigation_context(collection)` converts one
`EvidenceCollectionResult` into compact `InvestigationContext` data for a future
citation-constrained hypothesis generator. It consumes only the supplied
collection result. It does not read fixtures, call telemetry tools, access the
network, call an LLM, form a hypothesis, calculate confidence, or recommend a
remediation.

The context includes:

- The incident/trace scope and collection status.
- Every attempted tool call, its controlled outcome, and tools not executed
  after a failure.
- Structured recorded or calculated facts with stable IDs and exact citation
  IDs.
- A smaller chronological timeline with citation IDs.
- A separate inventory of fields explicitly marked unavailable.
- Counts and truncation flags for facts, timeline events, unavailable fields,
  and the original bounded log search.
- Only the citation entries used by included content; orphan citations are
  rejected.

Fact selection is deterministic. Trace duration/status come first, followed by
the longest non-root span durations, calculated trace coverage, bounded
provider attempts, scalar span attributes (routing first), and logs. Error and
warning logs are selected before informational and debug logs. Stable record,
metric, value, origin, and source identities deduplicate observations repeated
by multiple telemetry tools. Recorded `0` and `false` values remain available
facts; unavailable values never become facts.

The current limits are 24 total facts, four non-root span-duration facts and
their four calculated coverage facts, three provider attempts, six scalar
attribute facts, five log facts, 20 timeline events, and 12 unavailable fields.
Stable timestamps/record IDs break ties, and the context reports whenever any
limit or the original log-search limit reduced the available evidence.

Collection handling is explicit:

- A completed collection produces a completed context.
- A partial collection keeps facts from successful tools, preserves the safe
  failure code/message, and lists tools that were not executed.
- A failed collection with no usable observations raises
  `NoUsableEvidenceError` instead of producing an evidence-empty prompt.

Example:

```python
from app.evidence_collector import collect_evidence
from app.evidence_context import build_investigation_context

collection = collect_evidence(
    incident_id="INC-SLOW-001",
    trace_id="synthetic-trace-slow-001",
)

context = build_investigation_context(collection)
```

### Current limitation

The tools, collector, and context builder currently support only evidence
originating from `fixtures/incidents/INC-SLOW-001.json`: one synthetic,
development-environment IntervAI `answer_analysis` case. They do not connect to
Logfire or any live telemetry source. Investigator code never reads
`fixtures/ground_truth`. No LLM or hypothesis generation is implemented yet.

## Current fixture

`INC-SLOW-001` is synthetic test telemetry, not a recorded production incident.
Its identifiers, timestamps, durations, spans, token counts, and designed root
cause are invented solely to exercise the contract and loader. It must not be
used to make claims about IntervAI's production behavior.

## Safety boundary

The MVP has no production-write tools. Restarting services, changing models or
configuration, deploying code, writing data, and sending notifications are all
outside the current system boundary.
