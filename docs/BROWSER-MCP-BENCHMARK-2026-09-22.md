# Browser MCP live benchmark — 2026-09-22

Release live: `27fba70-browser-mcp-20260922`.

Campioni:
- 20 nuove sessioni MCP per initialize;
- 50 iterazioni in sessione persistente per tools/list, tabs e snapshot;
- 50 route-only HTTP per browser.inspect e browser.interact;
- 20 azioni approval-bound reali sul canary localhost, 5 per click/type/upload/submit;
- 5 azioni per tipo su una singola sessione MCP persistente per isolare il costo del primitivo.

## Risultati principali p50 / p95 / p99

| Misura | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| initialize nuova sessione MCP | 473.79 | 526.97 | 542.84 |
| tools/list persistente | 7.38 | 10.57 | 17.44 |
| browser_tabs persistente | 89.97 | 118.26 | 634.82 |
| browser_snapshot persistente | 52.70 | 66.18 | 99.19 |
| routing browser.inspect | 28.02 | 37.79 | 68.33 |
| routing browser.interact | 26.35 | 41.57 | 66.55 |
| preview approval-bound | 736.08 | 1055.71 | 1109.02 |
| execute approval-bound | 2763.58 | 3300.08 | 3701.63 |
## Primitive action floor with one persistent MCP session

| Primitive | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| click | 613.34 | 636.19 | 639.61 |
| type | 82.50 | 95.16 | 96.98 |
| upload (click chooser + setFiles) | 685.86 | 706.22 | 707.64 |
| submit button click | 618.17 | 624.94 | 626.07 |
| post-action snapshot/readback | 52.26 | 68.25 | 75.08 |

## Diagnosi

Il routing Bot-tazzi non e' il collo di bottiglia: resta sotto ~42 ms al p95. Anche la lettura MCP con sessione gia' inizializzata e' rapida.

Il primo costo strutturale e' l'initialize MCP (~474 ms p50). Il percorso approval-bound apre oggi sessioni separate per snapshot pre-azione, applicazione dell'azione e snapshot post-azione, quindi paga ripetutamente handshake/discovery.

Il secondo costo e' specifico dei click Playwright: anche su sessione persistente click/submit richiedono ~613-618 ms p50, mentre `browser_type` e' ~82 ms. L'upload eredita soprattutto il costo del click che apre il file chooser.

Target successivo: session pooling/resident client nel backend e una singola sessione MCP per la transazione approval `pre-snapshot -> action -> post-snapshot`, mantenendo hash binding e fail-closed. Potenziale teorico: eliminare circa 1-1.5 s dal percorso execute e portare il preview vicino al costo snapshot+routing una volta che il pool e' caldo.

Dati grezzi aggregati: `benchmarks/browser-mcp/2026-09-22-live.json`.
