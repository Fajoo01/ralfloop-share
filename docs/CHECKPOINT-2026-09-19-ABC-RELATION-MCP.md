# CHECKPOINT — ABC Relation MCP v1 — 2026-09-19

## Branch

`feat/abc-relation-mcp-v1-20260919`

Repository privato: `Fajoo01/ralfloop-bottazzi`.

## Obiettivo raggiunto

Il dominio relazione/ABC non dipende più concettualmente da un unico report testuale mutabile. Esiste un MCP v1 con:

- event store SQLite;
- provenance;
- separazione fact / inference / contradiction / boundary;
- `abc_relcalc` deterministico come scoring canonico;
- Formula Loop legacy solo diagnostico;
- snapshot di stato e strategia;
- importer dei JSON legacy;
- reference library di psicologia e dialogo;
- stdio MCP server;
- broker AF_UNIX same-UID;
- client Python;
- systemd unit hardenizzata.

## File principali

- `ralfloop_agent/abc_relation/models.py`
- `ralfloop_agent/abc_relation/store.py`
- `ralfloop_agent/abc_relation/service.py`
- `ralfloop_agent/abc_relation/mcp.py`
- `ralfloop_agent/abc_relation/client.py`
- `ralfloop_agent/abc_relation/references.py`
- `ralfloop_agent/abc_relation/importer.py`
- `scripts/ralf_abc_relation_mcp_server.py`
- `scripts/ralf_abc_relation_mcp_broker.py`
- `scripts/import_abc_relation_legacy.py`
- `deploy/systemd/ralf-abc-relation-mcp-broker.service`
- `docs/ABC_RELATION_MCP_V1.md`

## Tool MCP

Read:

- `abc_get_state`
- `abc_get_timeline`
- `abc_search_events`
- `abc_analyze`
- `abc_explain_event`
- `abc_list_snapshots`
- `abc_get_reference_library`
- `abc_policy_status`

Local write only:

- `abc_record_event`
- `abc_create_snapshot`

Nessun tool outbound, nessun send, nessun tracking, nessuna lettura online status.

## Manuali/modelli inclusi

- Nardone / Salvini — Dialogo strategico;
- Miller / Rollnick — Motivational Interviewing 4e;
- Rusbult / Agnew / Arriaga — Investment Model;
- Rusbult / Van Lange — Interdependence Theory;
- attachment / secure-base come euristica non diagnostica.

Regola fondamentale: queste fonti guidano interpretazione prudenziale e forma della comunicazione; non costituiscono evidenza delle intenzioni nascoste di Arianna.

## Privacy

- dump WhatsApp completo fuori dal DB canonico;
- raw excerpt opzionale max 600 caratteri;
- source ref + hash supportati;
- dati relazionali privati non committati su GitHub;
- GitHub contiene solo codice, test, schemi e documentazione.

## Legacy

L'importer dei vecchi JSON:

- crea snapshot;
- importa le vecchie probability come hypotheses `weak`;
- forza confidence bassa per i numeri legacy;
- non trasforma le percentuali in fatti;
- non popola artificialmente gli observed facts.

## Verifica eseguita su Sibilla

Suite mirata finale:

`15 passed in 0.85s`

Include:

- nuovo MCP;
- importer;
- regressione `abc_relcalc`.

Inoltre:

- `py_compile` sui moduli/scripts ABC: OK;
- `git diff --check`: OK;
- worktree finale pulito.

Smoke precedente end-to-end riuscito:

`RelationMCPClient -> AF_UNIX broker -> MCP server -> SQLite -> abc_relcalc`

Durante lo smoke:

- policy letta correttamente;
- reference library letta correttamente;
- evento locale scritto;
- analisi ricalcolata.

## Non ancora dichiarato live

La unità `deploy/systemd/ralf-abc-relation-mcp-broker.service` è pronta ma non è stata installata/abilitata sul sistema con privilegi root in questa sessione.

## Prossimi passi

1. creare release/worktree read-only sotto `/home/sibilla-cumana/ralf-abc-relation-mcp/current`;
2. installare la unità systemd e attivare `/run/ralf-abc-relation-mcp/mcp.sock`;
3. importare i JSON legacy correnti nel DB live, senza importare il dump WhatsApp raw;
4. aggiungere il binding del nuovo MCP al router Bot-tazzi per le richieste relazionali;
5. iniziare la migrazione degli eventi recenti in atomi osservabili con provenance;
6. in seguito fare backtesting delle regole rispetto agli esiti successivi per calibrare i pesi invece di correggerli a intuito.
