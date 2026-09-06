# Telegram PEC/RUNTS decision regression

## Failure `dc15467594c3`

Path: `Meowgram -> POST /tasks/run route_only -> check_only -> POST /orchestrate -> ModelToolOrchestrator`.

The ready catalog passed to the model contained only:

- `extract_structured_data_v1`
- `semantic_retriever_bge_m3_v1`
- `document_reranker_v1`
- `sandboxed_remote_code`
- `deep_web_research_agentcpm_v1`

No PEC/RUNTS semantic capability was present. The backend retained only `tool_decision_schema_invalid` and SHA-256 prefix `dc15467594c3`; neither journal nor Meowgram durable queue retained the raw decision or its redacted key list. Therefore the historical invalid field cannot be recovered without inventing evidence. New diagnostics persist only field path and validation type, never field values.

Expected model-tool decision schema remains strict: object with only `action`, `response`, `tool_id`, `arguments`; action is `respond|tool`; tool requires a non-empty `tool_id`; extra fields are forbidden. PEC/RUNTS now has a narrower deterministic schema: `action=tool`, `response=null`, `tool_id=pec_find_by_runts_reference`, arguments containing only `runts_reference` and bounded `limit`.

## Fix

Telegram route-only now recognizes an exact `PEC ... RUNTS <native-id>` read intent before generic model-tool orchestration. It extracts only the native reference, validates the strict decision, then uses the existing Capability Registry retrieval (`domain=pec_runts`, `limit=3`) and invokes semantic MCP `pec_find_by_runts_reference`. No endpoint is selected directly by the parser. The MCP response always includes `writes=0`.

Acceptance decision, sanitized:

```json
{"action":"tool","response":null,"tool_id":"pec_find_by_runts_reference","arguments":{"runts_reference":"2603942","limit":100}}
```

The exact Telegram phrase is covered by `tests/test_pec_runts_telegram.py`, including strict rejection of an extra `write` argument.

## 2026-09-06 continuation

The CDP transport previously discarded network events received while waiting for command responses. A queue now preserves these interleaved events; a regression reproduces that loss. Missing PEC page maps to AUTH_REQUIRED, a matching API HTTP 401 to SESSION_EXPIRED, and a genuine timeout remains SOURCE_UNAVAILABLE. No session expiry is inferred merely from timeout.

Live development runtime acceptance returned decisionValid=true, selectedCapability=pec_find_by_runts_reference, mcpInvoked=true, AUTH_REQUIRED, writes=0. The authenticated PEC tab was absent at that check. Production Telegram has not been changed or accepted. Relative targeted baseline ac52bf4: 58 PASS; current plus new Telegram tests: 63 PASS. No global rerun claimed.

Official Aruba documentation supports dedicated mail-client passwords with two-factor authentication: https://guide.pec.it/gestione-account-pec/password/programmi-di-posta.aspx . Account-specific provisioning remains required; no secret was read or generated.
