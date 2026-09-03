# PEC / RUNTS live read-only audit

Date: 2026-09-03. Host: Sibilla via `vpnpc`. Writes: 0. No cookie, session ID, address, subject, body or real native ID persisted.

PEC authenticated page verified at `https://webmail.pec.it/new/messages/INBOX`. The SPA reads `POST /newuismart/cgi-bin/ajaxmail`; observed list request/response schema is stored in `tests/fixtures/pec/ajaxmail_list.real_contract.sanitized.json`. Pagination exposes `page`, `pageCount`, `itemsPerPage`, offsets and `nextPage`; one observed page contained 50 rows. Public frontend code confirms `Act_Msgs=1` and `Tpl=mail_list` for message listing.

RUNTS tab is currently the Ministry services landing page, not the authenticated RUNTS application. Exact manual boundary: click `Accedi/Registrati`, choose the configured SPID/CIE identity, and approve on the phone if the identity provider requests it. Until the browser reaches an authenticated RUNTS application route, adapters must return `RUNTS_AUTH_REQUIRED`; PEC notification text is not authoritative RUNTS content.
