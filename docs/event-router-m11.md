# M11 Event Router

Date: 2026-09-03. Production unchanged.

## Contract

Accepted origins: poller, webhook, scheduler, API watcher, service and MCP subscription. Every accepted event has deterministic ID, strict type, timestamps, entity references and mandatory provenance. Events enter the shared append-only Memory Service before routing.

Decisions: `IGNORE`, `STORE_ONLY`, `UPDATE_PRACTICE`, `RUN_WORKFLOW`, `WAKE_AGENT`, `NOTIFY`.

## Safety semantics

- Duplicate event: `IGNORE`; workflow never reruns.
- Unknown event: `STORE_ONLY`; no model wake.
- Disabled/missing workflow handler: `STORE_ONLY`.
- Missing wake handler: `STORE_ONLY`.
- `NOTIFY`: downgraded to `STORE_ONLY`; notification requires separate policy/approval path.
- Conflicting rules rejected at initialization.
- No generic shell/REST tool, send or external mutation.

Default deterministic rules route Bandi changes, media tickets/detections, media fix proposals, stale practices and source inconsistencies. Workflow execution requires both explicit allowlist and installed handler.

Metrics: `event_received`, `event_deduplicated`, `event_workflow_run`, `event_wake_agent`. `OperationalMetrics` is process-local and exporter-neutral.

Targeted Event Router + Memory suite: 8 passed.
