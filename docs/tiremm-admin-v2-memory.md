# Tiremm Admin v2 + Memory Service

M6 adds `TiremmAdminV2` as orchestration over unchanged v1 validation/projection logic.

```text
SourceRecord -> TiremmAdminStore v1 validation -> MemoryService source persistence
Practice     -> v1 conflict/provenance checks -> SQLite practice + PRACTICE_UPDATED event
restart      -> sources + practices restore   -> same v1 projection/query behavior
MCP          -> semantic read/propose tools   -> no SQL/HTTP/generic execute surface
```

Capabilities: `tiremm_list_open_practices`, `tiremm_get_practice`, `tiremm_get_deadlines`, `tiremm_get_blocked`, `tiremm_get_waiting`, `tiremm_get_next_actions`, `tiremm_get_conflicts`, `tiremm_get_sources`, `tiremm_get_timeline`, `tiremm_prepare_action`.

`tiremm_prepare_action` validates source-bound evidence and returns an approval-required proposal. It never executes external actions.

Compatibility: existing `TiremmAdminStore`, `TiremmAdminSQLite`, query adapter, tests and feature flag remain unchanged. Runtime activation remains explicit; no production wiring or service deployment occurred.
