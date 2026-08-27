# Incident fixtures

This directory contains redacted JSON records for reproducible TraceOps
investigations. `INC-SLOW-001.json` is currently an explicitly synthetic slow
IntervAI `answer_analysis` request because no suitable production trace was
available. It must be replaced or supplemented with recorded telemetry before
making claims about a real incident.

A fixture must not contain API keys, secrets, resume text, candidate answers,
names, email addresses, audio, or other personal data.

Expected incident classes and known root causes live under `fixtures/ground_truth`.
TraceOps must not read those expected labels during an investigation.

The loader rejects malformed JSON, unknown contract fields, unsafe incident IDs,
oversized files, common secret or personal-data fields, obvious secret or email
values, inconsistent trace relationships, and evaluation-label leakage.
