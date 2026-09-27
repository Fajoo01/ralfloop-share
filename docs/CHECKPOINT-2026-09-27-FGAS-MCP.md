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

## Real WhatsApp canary

Forwarded evidence cluster identified in chat `Fabio` at `04:27, 27/09/2026`:

- 4 images + 1 PDF purchase order;
- customer: Dimitri Filomena;
- installation: Via Marcello Prestinari 2, Milano (MI);
- purchase: Tecnomat order `3122417408`, 02/08/2026;
- intervention date normalized from the forwarded typo `21/09/206` to `21/09/2026` only because the three-digit year equals the cluster year 2026 with one digit omitted;
- outdoor unit: Bosch `CL5000M 41/2 E`;
- serial recovered by bounded secondary OCR: `86DM-580-000864-7733701932`;
- refrigerant R32, factory charge 1.1 kg, one compressor, non-hermetically-sealed, heat-pump equipment;
- recurring installer profile confirmed from an earlier filled Drive form: Ishak Morgan / MRGSHK78C03Z336A;
- private-use profile default: `E1 Casa`.

Fields intentionally left unresolved because no evidence was found in the forwarded cluster or Drive search: customer tax code, fixed leak-detection system yes/no, gas recovered yes/no, gas added yes/no.

Dependency fix discovered during canary: the WhatsApp Baileys Python client capped RPC responses at 128 KiB, while media are returned base64. Branch `fix/whatsapp-baileys-media-rpc-limit-20260927`, commit `6b47b260`, raises only the bounded transport ceiling to 12 MiB; media acceptance remains capped at 8 MiB. Regression suite: 16 passed. The fix is deployed in the versioned WhatsApp MCP runtime.

F-Gas regression suite after the real-case parsing/OCR changes: 32 passed.

## Tracking

GitHub issue: `Fajoo01/ralfloop-bottazzi#73`.
