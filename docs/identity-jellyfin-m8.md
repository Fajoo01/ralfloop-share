# M8 IdentityLink + Jellyfin shadow provisioning

Date: 2026-09-03. Development mode: shadow. Production deployment: unchanged.

## Workflow

`ARCI exact member ID -> deterministic membership verification -> IdentityLink exact lookup -> Jellyfin exact native-ID read -> CREATE/LINK/NOOP/REVIEW/NO_ACTION plan -> Tiremm Admin practice -> Memory timeline`.

- Native ARCI, Jellyfin and RUNTSuite IDs remain separate.
- No fuzzy auto-link. Unlinked Jellyfin usernames are not compared.
- ARCI `SOURCE_UNAVAILABLE` and Jellyfin outage produce `NO_ACTION`.
- Ineligibility never causes automatic disable; existing accounts require review.
- Shadow proposals are deterministic, approval-required and `executable=false`.
- No delete capability or mutation transport exists.

## MCP surface

READ: `jellyfin_list_users`, `jellyfin_get_user`, `jellyfin_get_user_policy`, `jellyfin_get_health`, `jellyfin_list_libraries`, `jellyfin_get_item`, `jellyfin_get_media_streams`.

PROPOSE: `jellyfin_propose_user_create`, `jellyfin_propose_user_link`, `jellyfin_propose_user_enable`, `jellyfin_propose_user_disable`.

Legacy read-only tool names remain exposed for compatibility. No generic REST, password, delete or execute tool.

## Evidence

Live read-only Sibilla smoke: Jellyfin `10.11.6`, health true, 7 users, 3 libraries; exact item read succeeded; playback response contained one media source/two streams. IDs, usernames, titles, paths and token were not printed or stored. Writes: 0.

Targeted identity/Jellyfin/Memory/Admin suite: 32 passed. CSV reconciliation states remain `MATCH`, `ONLY_LEGACY`, `ONLY_ARCI`, `CONFLICT`, `AMBIGUOUS`; CSV retirement is not authorized.
