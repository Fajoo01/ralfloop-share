# CHECKPOINT 2026-09-27 — F-Gas installation MCP

## Goal

Automate completion of the air-conditioner F-Gas installation form from evidence forwarded through WhatsApp.

## Architecture

- `scripts/ralf_fgas_installation_mcp_server.py`: semantic MCP surface.
- `ralfloop_agent/fgas_installation.py`: deterministic extraction, validation and DOCX/PDF rendering.
- Google Workspace MCP is read-only source of truth for the current Drive template.
- WhatsApp MCP is read-only evidence intake.
- No Drive upload, WhatsApp send, email send or other external mutation is exposed by this MCP.

## MCP tools

- `fgas_drive_status`
- `fgas_extract_from_text`
- `fgas_validate_installation`
- `fgas_render_installation`
- `fgas_prepare_from_whatsapp`

## Drive evidence checked live

Current template discovered through `manage_drive`:

- name: `MODULO INSTALLAZIONE 2.docx`
- Drive file id: `1Oed_oGhMNeR4p2RS5-87uXacB5zSWoZ5`

The Drive search also found prior filled examples. They were inspected only to verify the layout; copies containing real customer data were removed from the worktree and are not committed.

## Safety / correctness rules

- Missing mandatory workflow fields remain missing; the MCP does not invent values.
- Generic browser, shell and Drive mutation tools are not exposed.
- Known false barcode pattern `CS...` is not promoted to a serial number.
- Historical Hisense rule retained for `2AMW42U4RGC`: R32, 0.95 kg; Hisense serial validation expects `1K` + 23 characters.
- Rendering uses the current Drive DOCX on every render attempt; Drive failure is fail-closed.
- Output is local DOCX/PDF only. External writes/sends remain separate approval-bound workflows.

## Verification completed before integration

- F-Gas unit tests: `tests/test_fgas_installation_mcp.py` — 9 passed.
- F-Gas + live MCP catalog tests: 14 passed.
- Post-rebase regression on production baseline, including capability RAG router: 29 passed.
- Live Drive discovery: READY, exact DOCX selected.
- Live rendering from Drive template: DOCX and PDF produced with no missing fields in the synthetic canary.
- PDF text inspection confirmed client, serial, model, refrigerant, charge, location, intervention and installer fields.

## Tracking

GitHub issue: `Fajoo01/ralfloop-bottazzi#73`.
