# M9 Bandi Platform

Date: 2026-09-03. Production unchanged.

Existing `BandoRegistry`, domain builder, semantic retrieval and weekly research remain intact. New operational layer adds:

- strict `NormalizedBando` contract with source, grants, requirements, attachments, timestamps and hash;
- reusable `SourceAdapter` protocol and explicit source-order catalog;
- Memory Service-backed current projection plus append-only event timeline;
- `BANDO_DISCOVERED`, `BANDO_UPDATED`, `BANDO_DEADLINE_CHANGED`, `BANDO_DOCUMENT_CHANGED`, `BANDO_FAQ_CHANGED`, `BANDO_CLOSED`;
- deterministic APS/RUNTS/territory/deadline/budget/partnership/cofinancing eligibility;
- ambiguity-only LLM escalation flag;
- eight semantic, source-backed, zero-write MCP capabilities.

Priority inventory covers Comune di Milano, Regione Lombardia, Fondazione Cariplo, CSV Lombardia, ministerial/RUNTS/youth sources, banking foundations and Intesa philanthropy. Unverified source adapters stay disabled. Framework does not depend on this catalog and existing weekly discovery remains available as fallback research, not authoritative memory.

No LLM crawler, generic web/REST tool or separate Bandi database introduced.
