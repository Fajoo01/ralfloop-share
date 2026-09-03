# ARCI MCP M7-A audit

Date: 2026-09-03. Scope: repository and Git history. No live ARCI call; writes: 0.

## Finding

Current server exposes only `arci_read_organization_profile`. `src/arci.py` expects six tools, but five additional tools exist only as consumer validators/test doubles: current server/provider does not implement them. Individual identity, complete member/card enumeration, membership verification and DataTables pagination are absent.

| Capability | Known route | Adapter/provider | MCP server | Tested | Paginated | Provenance | Status |
|---|---|---:|---:|---:|---:|---:|---|
| `arci_get_current_user` | `/user` | NO | NO | NO | n/a | NO | GAP |
| `arci_list_members` | `/users`; `/list_users/datatables` | NO | NO | mock only | NO | NO | GAP/INCOMPLETE_SOURCE until proof |
| `arci_get_member` | `/users/{id}` | NO | NO | NO | n/a | NO | GAP |
| `arci_list_cards` | `/cards`; `/cards/datatables` | NO | NO | mock aggregate only | NO | partial mock | GAP/INCOMPLETE_SOURCE until proof |
| `arci_get_card` | `/cards/{id}` | NO | NO | NO | n/a | NO | GAP |
| `arci_list_member_cards` | relation `cards.user_id` | NO | NO | NO | depends on complete cards | NO | GAP |
| `arci_verify_membership` | composed exact member + cards | unavailable stub | eligibility-only MCP returns unavailable | outage tests | n/a | synthetic unavailable evidence | GAP |
| `arci_list_pending_card_requests` | cards status `10` | NO | NO | NO | depends on complete cards | NO | GAP |
| `arci_get_club` | `/clubs/{id}` | consumer validator only | NO | mock only | n/a | mock only | GAP |
| `arci_list_clubs` | `/clubs` | NO | NO | NO | UNKNOWN | NO | GAP |
| `arci_get_committee` | `/committees/{id}` | consumer validator only | NO | mock only | n/a | mock only | GAP |
| `arci_list_committees` | `/committees` | NO | NO | NO | UNKNOWN | NO | GAP |
| `arci_get_regional` | `/regionals/{id}` | consumer validator only | NO | mock only | n/a | mock only | GAP |
| `arci_list_regionals` | `/regionals` | NO | NO | NO | UNKNOWN | NO | GAP |
| `arci_list_alerts` | `/dashboard_alerts` | consumer validator only | NO | mock only | UNKNOWN | mock only | GAP |
| organization aggregate | fixed portal `/admin/office/circolosoci/` | YES, CDP bounded | YES | YES | explicit completeness check for governance subset | YES | IMPLEMENTED |

Routes above are handoff-known, not treated as verified response contracts until backed by captured sanitized fixtures or authoritative code/docs.

## Real contracts currently verified

- `ArciOrganizationProfile`: strict Pydantic, PII-minimized, `FOUND/PARTIAL/AUTH_REQUIRED/UNAVAILABLE`.
- Current card mock fields: `status`, `validity`, `expired`, enable/disable timestamps, preregistration, consumer movement status. No native card/member IDs: insufficient for identity.
- Identity policy already requires exact stable IDs; no fuzzy linking.
- Status handoff mapping: `10 pending`, `20 approved`, `25 expelled`, `30 rejected`; raw status must remain present; unknown maps to `UNKNOWN(raw)`.

## Pagination gate

`GET /users` and `GET /cards` cannot claim completeness. Required proof for each collection:

1. sanitized first/middle/last DataTables fixtures;
2. request fields and cursor/offset semantics from `/list_users/datatables` and `/cards/datatables`;
3. reported total equals unique native IDs accumulated;
4. stable ordering or overlap-safe dedupe;
5. termination proof and maximum-page guard;
6. malformed/repeated/empty premature page => `INCOMPLETE_SOURCE`;
7. only then allow `complete=true`; otherwise retain CSV reconciliation.

## Identity semantics

- Primary person identity: `users.id`.
- Card identity: `cards.id`; relation: `cards.user_id -> users.id`.
- Card number, campaign/validity and club ID are distinct identifiers.
- Ambiguous/missing relation: no write, no automatic IdentityLink.

## Error/provenance target

Normalize to platform errors: `NOT_FOUND`, `AMBIGUOUS`, `UNAUTHORIZED`, `FORBIDDEN`, `SOURCE_UNAVAILABLE`, `RATE_LIMITED`, `MALFORMED_RESPONSE`, `INCOMPLETE_SOURCE`, `CONFLICT`, `STALE_DATA`. Each success must include route, native ID/query fingerprint, observation time and content hash. Secrets/headers/raw sensitive records never enter result/audit.

## Write inventory

Current repository contains no verified complete ARCI write-route catalog. Known categories from portal/domain behavior: create/update member, create/request/update card, approve/reject/expel card, CSV import/export-related operations. All remain `ADMIN`/`DENY`; no MCP exposure, execution, live probing or payload invention. Endpoint/precondition/rollback details remain `UNKNOWN` until authoritative evidence exists.

## M7-B implementation gate

- Obtain sanitized real fixtures or authoritative API implementation for every route.
- Implement typed transport adapter; never `raw_request`/generic proxy.
- Implement exact-ID reads first; DataTables paginator second; composed membership verification third.
- Server `tools/list` must equal implemented provider methods, not consumer aspirations.
- Contract, pagination, malformed response, outage, provenance, no-PII/no-write safety tests required.

## M7-B1 implementation status

Semantic point-read layer now exists in `arci_point_reads.py`. MCP advertises its five tools only when an authenticated fixed-method transport is injected; the current production entrypoint injects none and therefore preserves the previous surface.

| Capability | REST route | Semantic service | MCP conditional | Fixture | Pagination | Provenance | Test class |
|---|---|---:|---:|---:|---:|---:|---|
| `arci_get_member` | `/users/{id}` | YES | YES | YES | n/a | YES | SANITIZED_FIXTURE |
| `arci_get_card` | `/cards/{id}` | YES | YES | YES | n/a | YES | SANITIZED_FIXTURE |
| `arci_list_member_cards` | exact member relation | YES | YES | YES | n/a | YES | SANITIZED_FIXTURE |
| `arci_get_club` | `/clubs/{id}` | YES | YES | YES | n/a | YES | SANITIZED_FIXTURE |
| `arci_verify_membership` | composed point reads | YES | YES | YES | n/a | YES | SANITIZED_FIXTURE |
| `arci_list_members` | `/list_users/datatables` | NO | NO | NO | NO | NO | NONE |
| `arci_list_cards` | `/cards/datatables` | NO | NO | NO | NO | NO | NONE |
| `arci_list_pending_cards` | `/cards/datatables` | NO | NO | NO | NO | NO | NONE |

`REAL_CONTRACT` remains false until an authenticated transport and sanitized captures are available. No mock result counts as real coverage. Complete-list tools remain absent (`INCOMPLETE_SOURCE` by capability absence). Writes remain zero.
