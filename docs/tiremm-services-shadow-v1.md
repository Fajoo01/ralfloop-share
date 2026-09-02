# Tiremm services shadow v1

Base `66e57cf`. Production unchanged. `RALFLOOP_JELLYFIN_PROVISIONING=off` by
default; invalid values fail to `off`.

| Problem | Cause | Change | Verification |
|---|---|---|---|
| Jellyfin users created manually | No member-authoritative provisioning path | IdentityLink, deterministic policy and shadow plans | ARCI/Jellyfin eval |
| ARCI identity unavailable to automation | Deployed MCP exposes aggregates only | Exact-ID MCP contract; current provider returns SOURCE_UNAVAILABLE | Live tools/list audit |
| Jellyfin MCP is library-only | Existing tools manage movie metadata | Separate GET-only user MCP | Strict schema/no write tools |
| RUNTSuite could be duplicated | Existing suite already owns members/accounting/projects | Explicit read adapter/MCP over observed endpoints | Capability-source audit |
| CSV contains excess personal data | Full ARCI export used operationally | Reconciliation reads only stable ID/status; no fuzzy matching | Five-state fixture |

## RUNTSuite inventory

Observed application: FastAPI `Tiremm RUNTS Suite` v0.1.0; local/private-network
middleware, SQLite/SQLAlchemy plus several direct SQLite modules. No general RBAC was
found. Public surface is limited to `/health` and selected `/portal/*`; portal minutes
uses TOTP sessions. No webhook framework or autonomous scheduler was found.

| Area | Read capability | Existing writes | Native identity/data |
|---|---|---|---|
| Members | `GET /members/` | `POST /members/` | `member_id`, `external_member_id`; also stores local profile fields |
| Member cards | `GET /member-cards/list`, resolve currently POST-shaped | register, create accounting account, CSV upload | card links, ARCI external ID |
| Member/account link | `GET /member-account-links/` | explicit POST | native member/account IDs |
| Attendance | `GET /attendance/` | POST | attendance rows |
| Projects/funding | GET projects/funding calls | POST/create, budget/document writes | project/funding IDs |
| Accounting | movements, accounts, ledger, categories, reviews | create/patch/delete/import/classify | 2,407 movements in audited local DB |
| Documents | project/supporting/deposit GET/download | upload/OCR/link/confirm/delete | document IDs and paths |
| Meetings/votes | list meetings, transcripts/documents, vote sessions | create/draft/minutes/resolution/vote | meeting/member/vote IDs |
| Reporting | Mod D draft/PDF, analytics, prima-nota export | review/deposit confirmation | RUNTS mappings and snapshots |
| Suppliers/payroll/withholdings | dedicated endpoints | dedicated writes | separate native rows |
| Chat | DB-query and skill-search; write-preview GET | preview/apply exists | transient preview ID |

Observed local DB: 43 members, 41 card links, 1 member/account link, 5 projects,
1 funding call, 1 meeting, 1 supporting document. No course/activity/enrollment model
or communication subsystem was found; attendance exists but must not be relabelled as
course enrollment. ARCI relation today is CSV import, not MCP.

Implemented MCP tools are individually named: get member by exact external ID, list
projects, funding calls, meetings, attendance, member cards, member/account links and
review queue. No `runtsuite_execute(anything)` exists.

## Jellyfin legacy inventory

- Live Docker image: `jellyfin/jellyfin`; public server reports 10.11.6.
- Endpoint: port 8096 on Sibilla; authentication already uses an API token loaded from
  external config. Token is not copied, logged or committed.
- State: 7 users; 3 collection folders; Playback Reporting 17.0.0.0 plugin.
- Existing MCP exposes only movie identity, library refresh and deduplication tools.
  It exposes no user lifecycle capability.
- Legacy ARCI exports found: four CSV snapshots, 41/42 rows, all with stable `ID`;
  status counts in newest snapshot: 33 `R`, 9 `N`.
- RUNTSuite `/member-cards/upload-csv` imports the full export and may update local
  member fields. No file-to-Jellyfin user creation script was found locally or on
  Sibilla. The effective Jellyfin account step is manual.

Legacy flow evidenced by code:

```text
ARCI portal export CSV -> local files -> RUNTSuite member/card import
                                     -> human Jellyfin account management
```

The legacy path is retained.

## ARCI MCP

Live `tools/list` exposes exactly: organization profile, club, current-card aggregates,
committee, regional data and dashboard alerts. Current-card rows expose card status,
validity/expiry and lifecycle timestamps but no member stable ID. No individual lookup
input exists. Therefore current ARCI MCP cannot establish person-level Jellyfin
eligibility. `arci_verify_member_eligibility` is implemented as a strict exact-ID MCP
contract, but its deployed-capability adapter returns `SOURCE_UNAVAILABLE` until an
authoritative individual query is added. It never falls back to names.

## Identity model

`IdentityLink` stores only local ID, ARCI stable ID, optional native RUNTSuite/Jellyfin
IDs, timestamps, provisioning state and source references. Names, addresses, tax IDs,
documents, card history and passwords are excluded. Native systems remain authoritative.

## Shadow provisioning

Deterministic flow:

```text
exact ARCI verification -> eligibility policy v1 -> exact IdentityLink
-> Jellyfin GET-only state -> CREATE/LINK/NOOP/REVIEW/NO_ACTION plan
-> redacted audit -> source-backed Tiremm Admin Practice
```

Expiry produces review, never automatic deletion/suspension. ARCI outage produces no
action. Existing exact legacy account produces LINK, not CREATE. Username uses a
normalized display name and stable-ID hash only on collision; card number is never the
public username. Execution always raises `PermissionError` in this slice.

Implemented MCPs:

- `ralf_arci_eligibility_mcp_server.py`: exact stable-ID verification contract;
- `ralf_jellyfin_user_mcp_server.py`: list/get user state, GET-only;
- `ralf_runtsuite_mcp_server.py`: explicit observed RUNTSuite reads.

They are not installed or enabled in production.

## Eval v0

The service-identity/RUNTSuite subset passes 24/24 tests. The integrated Tiremm,
identity, MCP, memory, registry and runtime selection passes 96/96 tests. Coverage
includes malformed MCP/source responses, retries, exact legacy linking, username
collisions, outages, expiry, audit redaction and execution rejection. No test contacts
or mutates an external service. Machine-readable results are in
`benchmarks/tiremm-services/eval-v0.json`.

An allowlisted pure execution guard recognizes only `CREATE_USER`, `ENABLE_USER`,
`DISABLE_USER` and `UPDATE_POLICY`, requires mode `approved` plus an approval ID, and
has no mutation transport. `DELETE_USER` is absent. Production remains `off`.

## Legacy reconciliation decision

The reconciler supports `MATCH`, `ONLY_LEGACY`, `ONLY_ARCI`, `CONFLICT`, `AMBIGUOUS`
using stable IDs only. Rows without stable IDs are ambiguous; no fuzzy person matching.
Real reconciliation cannot run because live ARCI MCP cannot return individual stable
IDs. **The CSV cannot yet be eliminated from the workflow.** It can be removed only
after ARCI MCP adds exact individual lookup and achieves complete reconciliation.

## Security

- no password/token fields in models or audit;
- no Jellyfin DELETE tool or executor;
- no create on ambiguous/not-found/unavailable;
- no mass suspension on outage;
- no automatic suspension on expiry;
- no RUNTSuite generic executor;
- all MCP schemas reject additional properties.
