# Bot-tazzi MD / Goodify MCP — checkpoint 2026-09-20

Branch: `feat/md-goodify-mcp-20260920`

## Obiettivo

Automazione Bot-tazzi per gli scontrini MD/Goodify:

1. decodifica QR da testo o immagine;
2. evita l'app MD quando il QR espone un flusso HTTPS pubblico Goodify/MD;
3. prepara la selezione della non profit `Tiremm Innanz APS`;
4. controlla email MD/Goodify tramite il Google Workspace MCP esistente;
5. inoltra le email al destinatario configurato con deduplica;
6. se una email riporta già una vincita, accoda una notifica Telegram via Meowgram.

Non viene esposto alcun browser generico, CDP, selector, JS evaluator o endpoint arbitrario.

## Tool MCP

- `md_goodify_parse_qr`
- `md_goodify_decode_qr_image`
- `md_goodify_probe_public_flow`
- `md_goodify_prepare_donation`
- `md_goodify_poll_mail`

La lettura immagini usa `zbarimg`, già presente su Sibilla. I file QR sono confinati a
`RALFLOOP_MD_GOODIFY_QR_DIR` (default `/var/lib/ralfloop/md-goodify/qr-inbox`).

Gli URL accettati sono esclusivamente HTTPS sui domini `goodify.com` e `mdspa.it`
(o relativi sottodomini). Anche i redirect vengono rivalidati contro la stessa allowlist.

`md_goodify_prepare_donation` è volutamente side-effect free finché non viene acquisito
un QR MD reale: serve il contratto reale del form pubblico per legare in modo deterministico
la selezione `Tiremm Innanz APS` senza inventare endpoint o campi.

## Gmail e Telegram

Il worker riusa `/run/ralf-google-workspace-mcp/mcp.sock`: nessuna nuova credenziale Google.
L'account collegato verificato live è `fabio@tiremminnanz.com`, con token valido e scope Gmail modify.

Il worker cerca solo mittenti con dominio Goodify/MD. Un forwarding è ammesso soltanto verso
`RALFLOOP_MD_GOODIFY_FORWARD_TO`; se account sorgente e destinatario coincidono, viene saltato.
Gli ID Gmail già processati sono persistiti in uno state file per evitare doppi inoltri/notifiche.

Le vincite vengono riconosciute solo da formulazioni esplicite nell'email. La notifica è accodata
nel normale outbox Telegram già drenato da Meowgram, con request id deterministico per messaggio.

Il worker non clicca né gioca `Tenta la Fortuna`: può classificare e notificare soltanto un esito
già riportato dalla fonte Goodify/MD.

## Validazione 2026-09-20

- `py_compile`: OK;
- `pytest tests/test_md_goodify_mcp.py`: 6 passed;
- MCP stdio live: 5 tool scoperti, `md_goodify_parse_qr` OK;
- probe pubblico `https://www.goodify.com/per-te-privato`: OK, side effects 0;
- Gmail live su `fabio@tiremminnanz.com`: connessione OK, 0 messaggi MD/Goodify negli ultimi 30 giorni;
- nessun inoltro e nessuna notifica eseguiti durante la validazione live.

## Deployment preparato

- `deploy/systemd/ralf-md-goodify-mcp-broker.service`
- `deploy/systemd/ralf-md-goodify-worker.service`
- `config/md-goodify.env.example`
- `scripts/ralf_md_goodify_mcp_server.sh`

Il runtime Python previsto è quello canonico di Bot-tazzi:
`/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python`.

Prima dell'attivazione live va creato un release isolato e registrato il provider
`md_goodify -> /run/ralf-md-goodify-mcp/mcp.sock` nel catalogo MCP live senza
rimpiazzare gli altri provider correnti.

## Prossimo test necessario

Serve un QR MD reale non ancora usato. Con quello si acquisiscono URL finale e form pubblico,
si aggiunge soltanto il binding specifico necessario alla scelta di Tiremm e si verifica il
post-condition della donazione. Nessun reverse engineering di autenticazioni o protezioni.
