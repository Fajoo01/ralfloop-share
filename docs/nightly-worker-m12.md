# Nightly Worker / heavy-model routing

Date: 2026-09-03. Production unchanged.

## Runtime

`NightlyEventSink` converts allowlisted Event Router events into persistent, idempotent jobs in the shared Memory Service. It never runs a model inline. The worker processes priority order only during the Europe/Rome 23:00–07:30 window unless explicitly forced by a local operator/test.

Order: deterministic handler, configurable simple/local tier, Qwen35 tier, largest tier. Concrete model IDs are provider configuration, not hardcoded. A job can escalate once on unresolved or low-confidence output. Missing providers fail closed; failed jobs do not enter an automatic retry loop.

Supported task families cover Bandi review/change, ambiguous eligibility, complex documents, unresolved/stale practices, media quality, duplicate/conflict review, research and source inconsistencies.

## Persistence and observability

Jobs, outcomes and audit events use Memory Service. Model audit records provider ID, tier, input/output tokens, duration, tool-call count, outcome and escalation reason. Counters: `nightly_jobs`, `nightly_llm_calls`, `nightly_escalations`, `nightly_failures`.

## Safety

- Recursive secret-key rejection before persistence.
- No generic shell/REST capability.
- No external write or production deployment.
- Event ingestion queues work; it does not wake a model immediately.
- Deterministic resolution consumes zero model calls.
- Provider outage produces `FAILED`, never fabricated success.

Targeted Nightly/Event Router/Memory gate: 13 passed.
