# Browser MCP live rollout — 2026-09-22

Release overlay live:
`/home/sibilla-cumana/ralf-memory-rag/releases/27fba70-browser-mcp-20260922`

Rollback overlay:
`/home/sibilla-cumana/ralf-memory-rag/releases/32d0bc9ed2b1f102015f4750cdf955fb93c7417c-abc-routing-5182440`

Integrazione costruita dalla base live `32d0bc9` con i commit Browser MCP e preservando le patch Tuya/Bandi già presenti nell'overlay.

Playwright MCP resta condiviso su `localhost:19321`, CDP `127.0.0.1:9236`, bridge Unix `/run/ralf-browser-playwright-mcp/mcp.sock`, profilo `--shared-browser-context --no-webmcp`.

`browser.inspect` è READ e auto-route; usa solo `browser_tabs(action=list)` e `browser_snapshot`. `browser.interact` è `CONFIRM_WRITE` e non è auto-route.

Sicurezza write:
- payload/target e pre-snapshot hash-bound all'approvazione;
- CAS one-shot, niente retry ciechi dopo esito incerto;
- post-action snapshot obbligatorio;
- upload con allowlist sorgente, SHA-256/size binding, staging 0700/0600 e cleanup;
- `browser_run_code_unsafe` non è mappato a skill logiche.

Hotfix version-guarded Playwright MCP 0.0.82:
- blank page title hang;
- locator `aria-ref` normalizzato per le azioni.

Gate candidato: 77 test verdi. Gate integrato Browser/Bandi/Tuya: 76 test verdi.
Canary live socket: tools/list 25 tool, tabs PASS, snapshot PASS.
Canary live approval-bound: click/type/upload/submit 4/4 `EXECUTED_VERIFIED` con readback semantico.
