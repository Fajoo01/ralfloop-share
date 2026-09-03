# Bottazzi platform inventory

Inventory date: 2026-09-03. Scope: repository/worktree only. Production not contacted.

| SYSTEM | AUTHORITATIVE SOURCE | CURRENT ADAPTER | API | MCP | READ | WRITE | EVENTS | IDENTITY | STORAGE | AUDIT | PRODUCTION STATUS | GAPS |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Unified assistant | Source registries | `unified_assistant/registry.py` | chat API | broker-selected tools | registry facade | policy-gated | none | domain IDs | JSON/YAML | routing traces | feature-flagged | registries fragmented; no single normalized contract before platform v1 |
| Capability/domain registry | `config/domain_capability_mapping.yaml`, `domains/registry.json`, local/model tool registries | canonical + unified facades | domain API | indirect | yes | canonical registry rejects side effects | none | capability/domain IDs | YAML/JSON | `logs/domain_capability_mapping.jsonl` | active local code | old schemas lack permission/promotion/health uniformity |
| Capability routing | trigger config + deterministic code | `core/capability_router.py`, local router, model routing | chat/runtime | selects connectors | yes | classifies only | none | task/domain | config | runtime traces | active | no shared BM25 eval baseline; entire catalog may still be assembled elsewhere |
| Tiremm Admin v1 | verified source records | `unified_assistant/tiremm_admin.py` | unified assistant vertical slice | no dedicated server | practices, deadlines, blockers, sources | proposal only | practice artifacts | practice ID | in-memory projection; SQLite snapshot helpers | source/evidence hashes | shadow, flag default off | persistence and semantic MCP missing |
| IdentityLink | exact native IDs | `unified_assistant/service_identity.py` | none | service-identity MCP consumers | exact lookup | local link store only | none | local/ARCI/RUNTSuite/Jellyfin native IDs | in-memory | redacted plan audit | shadow | persistent store; no fuzzy link by design |
| ARCI | authenticated ARCI portal/API | `src/arci.py`, `unified_assistant/arci_portal.py` | known portal routes | org aggregate + exact eligibility facade | org aggregate; current exact member provider unavailable | none | none | member/card/club IDs distinguished in design | remote source; legacy CSV outside service | broker/server response metadata | read-only | complete typed reads, pagination proof, write inventory |
| RUNTSuite | RUNTSuite API/domain DB | `runtsuite_adapter.py` | observed `/api/v1` reads | eight semantic reads | member/projects/calls/meetings/attendance/cards/links/review | preview only in domain model; absent MCP | no webhook framework found | external member ID | remote API + audited local DB | source refs | shadow read-only | remaining reads, writes inventory, events |
| Jellyfin | Jellyfin server API | `service_identity_mcp.py` | `GET /Users` | list/get user state | minimized users/state | executor hard-disabled | none | native user ID/exact username | remote | policy fingerprint + plan audit | shadow read-only | health/libraries/user policy capabilities; proposals integration |
| Gmail/Calendar/Drive | Google Workspace | `src/google_workspace.py`, fake client | Google APIs | workspace broker | Gmail search/read/thread/attachment; Drive/Calendar support | approval-bound Gmail operations | no shared event router | message/thread/file/event IDs | remote | approval/audit paths | brokered; feature flags | normalize into memory/event store |
| Mailchimp | Mailchimp Marketing API | `src/mailchimp.py`, MCP broker/server | vendor API | semantic audience/campaign tools | audiences/campaigns/content/members/segments/tags | approval-bound draft/send | none | list/campaign/member IDs | remote + SQLite approval | execution claims/audit | brokered | event ingestion; promotion metadata normalization |
| WhatsApp | authenticated work profile | `src/whatsapp.py`, MCP broker/server | connector-specific | semantic read tools | work-profile scoped | policy-gated | none | message/chat IDs | remote | broker audit | constrained | event ingestion and durable provenance |
| Bandi | official/source pages and normalized domain artifacts | `domains/active/bandi`, bandi adapters/research | multiple source adapters | domain semantic port, not unified MCP server | search/research/evidence | promotion approval framework | weekly timer | source/bando IDs | JSON artifacts + SQLite queues/caches | extensive domain approval/review logs | active domain, scheduled research | normalized central service/MCP/event changes incomplete |
| RAG/embeddings | source artifacts | semantic retrieval modules; `qwen3-embedding:4b` registry entry | none | model tool | lexical/semantic paths | none | none | source refs | files/caches | provenance envelopes | optional | not administrative source of truth; hybrid service absent |
| Memory | source stores + verified events | `unified_assistant/memory.py` | none | none | scoped exact lexical in-memory retrieval | write-policy gate only | ABC JSONL adapter | memory item/source IDs | source-specific files; no central service | retrieval trace | scaffold | persistent structured/event/document memory, FTS/BM25, dedupe, MCP |
| Approval engine | SQLite approval state + HMAC-bound scope | `domains/domain_approval*.py` | `/domain-approvals/...` | consumed by protected MCP tools | status/read | one-shot claim/execution gates | outbox | request/execution IDs | SQLite + JSONL | append audit | lab/selected integrations | common ActionProposal/Approval/ExecutionResult adapter |
| Audit | subsystem records | `logging/audit.py`, domain storage helpers | none | none | inspect files | append-only intended | audit events | audit/proposal IDs | JSONL | redaction varies by subsystem | active | one normalized schema and secret regression across all tools |
| Event/scheduler | systemd timers/source pollers | bandi weekly, GLM/evolver/media timers | none | none | scheduled reads | bounded workers | timer events only | job IDs | files/SQLite | worker logs | deployed candidates | common Event contract/router/deduplication absent |
| Deployment/systemd | checked-in units/config examples | `deploy/`, broker scripts | loopback services | Unix sockets/stdio | health where defined | deployment prohibited this run | timers | service names | `/run`, `/var/lib` documented | journald/files | candidates + some known live architecture | no platform-wide manifest/promotion gate |
| Secrets | environment/key files | per-client loaders | n/a | brokers isolate secrets | tokens not returned | gated | none | n/a | env/config/key files | redaction helpers | mixed | consolidate policy; detect accidental logs/config secrets |
| Surveillance | hardware/live services external to repo | garden detector/media artifacts only | unknown | none | limited existing garden pipeline | prohibited | detector events | camera/entity IDs unknown | artifacts | run reports | not inventoried live | ONVIF/RTSP/NVR/Frigate/Home Assistant inventory deferred by priority |

## Reuse decisions

- Keep domain logic in existing services; API/MCP remain adapters.
- Extend `UnifiedRegistryFacade`; do not replace canonical/domain/model registries.
- Keep Tiremm Admin v1 compatible. Add persistence behind its contracts later.
- Keep approval engine. Normalize contracts via adapter, not rewrite.
- Preserve ARCI CSV until complete API pagination/reconciliation proved.
- No generic REST MCP, fuzzy identity linking, automatic Jellyfin deletion, or source-less administrative facts.

## Verified paths

- MCP servers: `scripts/ralf_*_mcp_server.py`, `ralfloop_agent/unified_assistant/*_mcp.py`.
- REST/domain clients: `src/`, `ralfloop_agent/unified_assistant/*_adapter.py`, domain packages.
- Tests/eval: `tests/`, `tests_scaffold/`, `scripts/tiremm_admin_eval.py`, `benchmarks/`.
- Deployment: `deploy/systemd/`, `deploy/systemd/user/`.
