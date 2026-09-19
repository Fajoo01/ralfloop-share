# ABC Relation MCP v1

## Scopo

Trasformare il vecchio dominio ABC/relazione da insieme di report, patch e formule sparse in un servizio MCP locale con stato strutturato, provenance, analisi riproducibile e strategia separata dai fatti.

Il servizio non deve decidere cosa pensa Arianna. Deve aiutare a:

- conservare eventi osservabili con fonte e data;
- distinguere fatti, inferenze, contraddizioni, limiti e segnali esterni;
- ricalcolare una lettura prudenziale in modo riproducibile;
- conservare snapshot di stato e strategia;
- usare manuali di psicologia e dialogo come euristiche dichiarate;
- impedire che una tecnica comunicativa venga scambiata per prova psicologica.

## Architettura a strati

### 1. Source artefacts

Chat export, note e JSON legacy rimangono fuori dal database canonico. Il DB salva `source_ref`, eventuale SHA-256 e al massimo un estratto di 600 caratteri.

### 2. Event store

SQLite `abc_events` contiene atomi normalizzati:

- `observed_fact`
- `inference`
- `contradiction`
- `external_signal`
- `operational_constraint`
- `boundary`

Ogni evento ha timestamp, confidence, weight, tags e provenance. Un evento può supersedere un evento precedente senza cancellare la storia.

### 3. Analisi deterministica

La fonte canonica v1 è `abc_relcalc`: riceve gli eventi strutturati e produce score, confidence, bias flags e next safe action.

Il vecchio `abc_formula_loop_v1` resta disponibile solo come diagnostica di compatibilità quando viene fornito esplicitamente un legacy report. Non è il nuovo source of truth.

Regole principali già ereditate:

- le inferenze hanno confidence cappata;
- contraddizioni riducono score/confidence;
- mind-reading e sorveglianza generano warning;
- senza fatti forti non si può produrre certezza alta.

### 4. Snapshot

`abc_snapshots` conserva una fotografia interpretativa:

- state summary;
- hypotheses con probability e confidence separate;
- strategy rules;
- warnings;
- source refs;
- score payload del momento.

Le probabilità legacy vengono importate come `weak` con confidence bassa: sono stime storiche del modello, non frequenze empiriche.

### 5. Reference library

`ralfloop_agent/abc_relation/references.py` contiene una biblioteca versionata dei modelli usati. Ogni riferimento dichiara:

- concetti utilizzabili;
- usi ammessi;
- ciò che NON può dimostrare;
- guardrail.

## Manuali e modelli

### Dialogo strategico — Nardone / Salvini

Uso previsto: progettare il modo di parlare, non leggere la mente dell'altra persona.

Tecniche utilizzabili in forma non coercitiva:

- domande strategiche;
- parafrasi ristrutturanti;
- riassumere per ridefinire;
- scoperta congiunta.

Vincolo ABC: niente false alternative usate per intrappolare, niente pressione, niente persuasione occulta. La risposta reale dell'interlocutore prevale sul modello.

### Motivational Interviewing — Miller / Rollnick

Uso previsto:

- engaging;
- focusing;
- evoking;
- planning;
- ascolto riflessivo;
- rispetto dell'autonomia.

Serve soprattutto come correttivo al rischio di inseguimento, interrogatorio e reflex di convincere.

### Investment model / Interdependence — Rusbult et al.

Uso previsto: leggere serie temporali e investimenti reali senza confondere investimento con scelta dichiarata.

Dimensioni utili:

- satisfaction;
- quality of alternatives;
- investment;
- commitment.

Vincolo ABC: un investimento pratico o domestico non diventa automaticamente prova di commitment romantico.

### Attachment / secure base

Uso previsto solo come euristica descrittiva per prossimità, safe haven e regolazione della distanza.

Vincolo ABC: nessuna diagnosi dello stile di attaccamento di Arianna e nessuna patologizzazione del comportamento.

## Tool MCP v1

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

Non esistono tool per:

- inviare WhatsApp/email/messaggi;
- leggere stato online/accessi;
- tracking o sorveglianza;
- contattare Arianna;
- eseguire automaticamente una strategia sociale.

## Transport

Server stdio:

`python scripts/ralf_abc_relation_mcp_server.py`

Broker AF_UNIX:

`/run/ralf-abc-relation-mcp/mcp.sock`

Il broker usa `SO_PEERCRED` e accetta solo l'UID configurato. Non usa shell per avviare il server.

Client Python:

`ralfloop_agent.abc_relation.client.RelationMCPClient`

## Storage

Default sviluppo:

`~/.local/share/ralfloop/abc_relation.sqlite3`

Deploy systemd:

`/var/lib/ralf-abc-relation-mcp/abc_relation.sqlite3`

Override:

`ABC_RELATION_DB=/path/to/db.sqlite3`

Per disabilitare anche le due scritture locali:

`ABC_MCP_ALLOW_LOCAL_WRITES=0`

## Import legacy

Il comando:

```bash
python scripts/import_abc_relation_legacy.py --db /path/abc_relation.sqlite3 \
  relazione_state_UPDATED.json \
  sunto_strategico_UPDATED.json \
  pattern_osservati_UPDATED.json \
  relazione_probabilistic_model_UPDATED.json \
  timeline_eventi_relazione_UPDATED.json
```

crea uno snapshot strutturato. Non trasforma automaticamente le vecchie percentuali in fatti e non importa il dump WhatsApp completo.

## Privacy

Principi:

1. data minimization;
2. raw chat non canonica;
3. estratti corti e opzionali;
4. provenance obbligatoria;
5. nessun push dei dati relazionali privati su GitHub;
6. GitHub contiene codice, test, schemi e documentazione, non il contenuto privato della relazione.

## Strategia di utilizzo

Per ogni nuovo episodio:

1. registrare prima ciò che è osservabile;
2. registrare separatamente eventuale interpretazione;
3. cercare controevidenze;
4. ricalcolare;
5. leggere lo snapshot corrente;
6. usare i manuali solo per scegliere una comunicazione proporzionata;
7. verificare ex post cosa è successo davvero e aggiornare il modello.

La strategia non deve ottimizzare "come ottenere Arianna". Deve ottimizzare chiarezza, proporzione, autonomia, rispetto dei limiti e riduzione degli errori di lettura.

## Test v1

Suite mirata:

- MCP contract;
- provenance;
- event store;
- mind-reading cap;
- no outbound/surveillance surface;
- reference library;
- snapshot;
- importer legacy;
- regressione `abc_relcalc`.

Smoke end-to-end già eseguito su Sibilla: client Unix -> broker -> server MCP -> SQLite -> analisi.
