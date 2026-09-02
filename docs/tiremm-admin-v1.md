# Tiremm Admin v1

Status: vertical slice, read-only, disabled in production. Base: `37b1ec9`.
Experimental gate: `RALFLOOP_TIREMM_ADMIN=1`; default is `0`. No production runtime
currently instantiates the store even when the data-layer flag is parsed.

## Existing-system inventory

| Source/system | Existing adapter/broker | Current format | Permission | Freshness/stable ID | Tiremm ingestion |
|---|---|---|---|---|---|
| Gmail | `src/google_workspace.py`, `scripts/ralf_google_workspace_mcp_broker.py` | validated message/thread dicts | read allowlist; writes approval-bound | live MCP; Gmail message/thread ID | map read result to `SourceRecord(gmail)` |
| PEC | none found | none | unavailable | provider ID required | future read-only adapter; no Gmail aliasing |
| Drive/documents | legacy `src/mcp_client.py::drive_upload`; visual/document and bando readers | file/evidence envelopes | upload is write/confirmation; no general Drive read broker | file/document ID + content hash | local/document snapshots now; Drive read later |
| Calendar | no dedicated Google Calendar runtime found | dates occur inside validated email/domain data | unavailable | event ID required | future read-only event snapshot |
| ARCI/Hydra | `src/arci.py`, `scripts/ralf_arci_mcp_{server,broker}.py` | authenticated organization tool result | strict read-only | organization/tool identity + content hash | map to `SourceRecord(arci)` |
| Mailchimp | `src/mailchimp.py`, MCP broker/server, campaign approval workflow | strict tool envelopes | reads available; create/send approval-bound | campaign/list IDs | map read result to `SourceRecord(mailchimp)` |
| Contacts | recipient resolver and Google Workspace message addresses | resolved recipient/evidence | lookup only in this slice | normalized address/source message | counterparty only when source-bound |
| Projects/bandi | domain registry, weekly research, semantic retrieval | evidence envelopes with chunk/source/document IDs | read/research; promotion controlled | canonical URL + text hash + chunk ID | reuse provenance, do not duplicate RAG |
| RAG/semantic | `bandi_semantic_retrieval.py`, model-tool semantic ranker | BM25/semantic evidence | read-only | evidence chunk ID | query after Practice filtering only |
| Database | SQLite approval store, OTP binding, local evolver DB | purpose-specific SQLite | transactional, scoped | table primary keys | new isolated SQLite prototype; existing DB schemas are not reusable state |
| Scheduler | bando jury scheduler, GPU scheduler | bounded jobs/audit artifacts | explicit invocation | job/batch IDs | not activated; deadline queries remain pure |
| Audit/approval | domain approval SQLite+JSONL, email OTP, unified audit | immutable events/approval state | protected | request/event IDs | future actions reuse this; no new executor |
| OAuth/secrets | environment/broker boundaries and Google client | external credentials, never Practice data | broker-only | account/provider identity | adapters emit sanitized records only |
| MCP transport | AF_UNIX brokers with schema validation | MCP envelopes | tool-specific | socket/tool/request IDs | connector boundary; not storage |

| Problem | Cause | Change | Verification |
|---|---|---|---|
| Administrative facts are spread across systems | No shared practice identity/lifecycle | Source-bound `Practice`, `Deadline`, `NextAction` models | Deterministic unit eval |
| A model could invent or merge facts | Free-form extraction has no evidence contract | Every deadline/action references ingested evidence; missing evidence fails closed | Missing/undeclared evidence tests |
| Sources can disagree | Email/calendar/document dates may differ | Conflicts are retained and surfaced; no automatic winner | Conflicting-deadline test |
| External actions are risky | Read and write paths were mixed conceptually | Next action carries existing `PolicyClass`; ingestion performs no writes | `CONFIRM_WRITE` assertion |
| Memory can grow indefinitely | Source/practice projections need limits | Hard source/practice quotas and content bounds | Quota and dedup tests |

## Architecture

```text
Gmail/PEC/Drive/Calendar/ARCI/Mailchimp read adapters
                         |
                    SourceRecord
                         |
               exact hash + dedup + quota
                         |
                source-bound Practice
                         |
          conflicts / deadlines / next action
                         |
             Tiremm namespace + retrieval
                         |
                   model + policy
```

The projection does not scrape live accounts and does not mutate source systems.
Existing adapters remain authoritative. A connector must first produce an immutable
`SourceRecord`; a separate deterministic projector links known evidence to a practice.
No LLM output becomes verified evidence.

## Data contract

- `SourceRecord`: kind, external ID, observed timestamp, title, bounded content,
  location, metadata, deterministic evidence hash.
- `Practice(v1)`: kind, status, priority, opened/updated timestamps, responsible party,
  counterparties, evidence, deadlines, blockers, communications, documents, events,
  sourced facts and structured next action.
- `NextAction`: type, description, due date, approval requirement, blockers, evidence.
- `Deadline`, artifacts, blockers and facts: at least one evidence ID.
- `PracticeConflict`: all incompatible values and all supporting evidence IDs.
- `RetrievalHit`: practice, provenance envelopes, unresolved conflicts.

Supported source kinds: `gmail`, `pec`, `document`, `calendar`, `arci`, `mailchimp`.

## Ingestion phases

1. Snapshot adapters, read-only: normalize existing tool output to `SourceRecord`.
2. Deduplicate by source kind, external ID, and exact content SHA-256.
3. Project only with an explicit stable `practice_id` supplied by trusted metadata or
   reviewed mapping. Semantic similarity may propose links later but cannot persist
   them as facts.
4. Reconcile source changes by adding new evidence; never rewrite old evidence.
5. Promote actions only through existing policy/approval workflows.

No live ingestion is enabled in this commit. Gmail/PEC/Drive/Calendar/ARCI/Mailchimp
credentials, sends, edits, campaign actions, and calendar mutations are out of scope.

## Query/deadline engine

Pure methods expose open/get/due/blocked/waiting/next-action/source/conflict queries,
plus `overdue`, `due_within`, `no_next_action`, `stale_since` and latest verified
source. They require no LLM. `TiremmAdminQueryAdapter` classifies a narrow request,
retrieves only matching Practice rows, attaches exact SourceRefs and fails closed when
no source-backed state exists.

## Storage

`TiremmAdminSQLite` is an opt-in experimental store using SQLite WAL, transactions,
schema metadata, JSON payloads and stable primary keys. It is deliberately separate
from approval/OTP databases: those schemas represent authorization state, not
administrative records. No environment flag or runtime default instantiates it.

## Eval gate

The initial deterministic suite checks:

- source deduplication and quota failure;
- missing and stale evidence failure;
- conflicting deadline preservation;
- closed-practice lifecycle;
- source-bound retrieval and Tiremm memory projection;
- action policy preservation;
- unknown-practice empty retrieval;
- undeclared evidence rejection.

Fixture v0 contains six sanitized multisource practices: project/bando, TARI, grant
reporting, family communication, event and ARCI membership. It includes overdue,
imminent, blocked, waiting, completed and conflicting states. Deterministic eval v0:
25/25. Integrated targeted tests: 72/72.

`ActionProposal` permits only local draft/proposal types, always requires approval,
and `reject_external_execution()` unconditionally denies execution.
