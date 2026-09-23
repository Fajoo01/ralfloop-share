# BaffoFlix support MCP — checkpoint 2026-09-23

## Goal
Let Bot-tazzi handle the repetitive BaffoFlix support cases: where to connect, forgotten-password recovery, and Quick Connect for a verified existing user.

## Public access observed and verified
- Public BaffoFlix server: `http://93.49.206.156:8096`
- Public landing page: `https://www.tiremminnanz.com/b/`
- `/System/Info/Public` returned HTTP 200 and identified the server as BaffoFlix / Jellyfin 10.11.6.
- The address was also present in Sibilla Chrome history on 2026-09-22.

These values are runtime configuration, not hardcoded in the MCP implementation.

## MCP tools
- `baffoflix_get_access_info`: read-only public entry points plus bounded public server health. It intentionally drops Jellyfin `LocalAddress` and never returns the API token.
- `baffoflix_start_password_recovery`: starts Jellyfin's native `POST /Users/ForgotPassword` flow for one exact username. Explicit confirmation is required. `PinFile` is never returned.
- `baffoflix_authorize_quick_connect`: authorizes one pending Quick Connect code for one exact enabled non-admin Jellyfin user. Explicit confirmation is required. It never returns an access token.

## Security boundary
A requester must not be allowed to choose an arbitrary Jellyfin `user_id` merely by saying a username. The `user_id` supplied to Quick Connect is expected to come from a trusted identity link (member/SSO -> Jellyfin account). Admin and disabled accounts are rejected.

No raw new password is accepted by this MCP. No server-side password-reset file or PIN file is exposed. Automatic PIN redemption is intentionally not implemented in v1.

## Known Jellyfin 10.11.6 concern
Password-reset PIN handling has a reported 10.11.6 regression (Jellyfin issue #16579). For already-linked members, Quick Connect is the preferred low-friction recovery path while the PIN reset path remains bounded to initiation only.

## Remaining integration
The repository already models `IdentityLink` between an ARCI/member identity and a Jellyfin native user id, but the currently installed individual ARCI-member lookup is not exposed. Until that identity lookup is live, Bot-tazzi must not auto-authorize Quick Connect from a free-form name alone.

## Branch/worktree
- branch: `feat/baffoflix-support-mcp-20260923`
- worktree: `/home/bandi/ralfloop-baffoflix-support-mcp-20260923`
- base: `b868788`
