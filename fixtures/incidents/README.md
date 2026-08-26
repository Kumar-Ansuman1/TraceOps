# Incident fixtures

This directory will contain redacted JSON records for reproducible TraceOps
investigations. The first fixture will represent one historical slow IntervAI
`answer_analysis` request.

A fixture must not contain API keys, secrets, resume text, candidate answers,
names, email addresses, audio, or other personal data.

Every evaluation fixture must include a separately maintained expected incident
class and known root cause. TraceOps must not read those expected labels during
an investigation.

