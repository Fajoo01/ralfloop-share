# PEC / RUNTS operational vertical

## Flow

`Bottazzi -> capability retrieval -> semantic PEC/RUNTS MCP -> Event Router -> Memory -> Nightly Worker`

PEC discovery uses the authenticated Aruba browser session through a bounded CDP adapter. It observes only the known inbox read response, never opens a message, and therefore does not alter unread state. Native IDs remain opaque; persisted facts include source reference, observation time and content hash. Pagination enforces stable totals, unique IDs, repeated-page protection and a maximum-page bound; incomplete enumeration fails closed.

Eight semantic tools cover PEC discovery/exact lookup/attachments/RUNTS notification correlation, RUNTS synchronization/authority lookup/RUNTSuite exact correlation and non-executable action preparation. Capability retrieval injects at most three tools from domain `pec_runts`. There is no generic HTTP tool and write execution is zero.

PEC is notification evidence only. `runts_get_authoritative_for_pec` requires an exact RUNTS native reference and retrieves the administrative content from RUNTS. Missing SPID/CIE authentication returns `RUNTS_AUTH_REQUIRED`; it never reconstructs authority from PEC.

## Live evidence

On 2026-09-03, `scripts/pec_runts_shadow_smoke.py` invoked Bottazzi against the authenticated PEC tab on Sibilla: capability `pec_discover_messages`, 10 reads, 10 Memory entities, all provenance hashes present, zero writes. Output was aggregate-only; no subject, sender, body, token or native ID entered logs.

RUNTS remains at the Ministry landing route. Manual boundary: in the existing Sibilla tab click `Accedi/Registrati`, choose SPID/CIE, complete the phone challenge, then leave the authenticated RUNTS application tab open. Contract discovery and promotion remain blocked until that route is visible.

## Safety

- READ only for browser discovery.
- PREPARE persists an approval-required, non-executable proposal.
- WRITE absent and denied by tool inventory.
- Exact native-ID correlation; no fuzzy link.
- Shared Memory/Event Store; no parallel database.
- Production services unchanged.
