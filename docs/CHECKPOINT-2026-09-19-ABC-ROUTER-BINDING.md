# CHECKPOINT — ABC Relation MCP router binding — 2026-09-19

## Branch

`feat/abc-relation-mcp-v1-20260919`

Repository privato: `Fajoo01/ralfloop-bottazzi`.

## Obiettivo di questo step

Collegare il nuovo ABC Relation MCP al runtime Bot-tazzi senza trattare l'analisi relazionale come un'azione esterna e senza introdurre side effect.

## Modifiche

- `config/capability_routing.json`: aggiunta sezione `read_mcp_keywords` con connector `abc_relation`.
- `src/routing_config.py`: parser separato per MCP read-only.
- `src/router.py`: gli MCP read-only sono risolti per richieste non `external_action`; la confirmation policy degli MCP esterni resta invariata.
- `ralfloop_agent/integration/abc_relation_read.py`: adapter runtime read-only canonico.
- `src/api.py`: `/tasks/run` inserisce stato/analisi ABC quando `abc_relation` è selezionato.
- `ralfloop_agent/nodes/reasoning.py`: anche il reasoning cycle OpenShell usa lo stesso adapter ABC invece di raccogliere un falso `ls -la` per le richieste relazionali.
- `tests/test_abc_relation_router_binding.py`: regressioni router/adapter.
- `tests/test_abc_relation_runtime_binding.py`: regressioni del reasoning cycle ABC.

## Regole di sicurezza

L'adapter ABC non espone né chiama:

- `abc_record_event`
- `abc_create_snapshot`

Il percorso normale usa soltanto letture. Di default legge:

- `abc_get_state`
- `abc_analyze`

Timeline viene caricata soltanto per richieste cronologiche. La biblioteca di psicologia/dialogo viene caricata per richieste che citano manuali/modelli oppure che chiedono come dialogare, comunicare, scrivere o rispondere.

Nessun nome personale è stato aggiunto ai trigger di routing.

## Runtime e fallback

Quando `abc_relation` è disponibile, `abc_memory` e `abc_relcalc` restano compatibilità legacy ma non vengono eseguite in parallelo al nuovo MCP.

Se il broker/socket ABC non è disponibile, l'adapter restituisce uno stato di indisponibilità esplicito con fallback dichiarato:

- `abc_memory`
- `abc_relcalc`

`src/api.py` esegue il fallback legacy soltanto in questo caso. Il reasoning cycle OpenShell non fabbrica una risposta MCP vuota e non sostituisce il fallimento con evidenza shell: restituisce `read_mcp unavailable`, `exit_code=1` e conserva `fallback_skills` nei metadata per l'orchestrazione superiore.

La normale richiesta ABC usa evidenza sintetica `mcp:abc_relation:read_only`. Nessuna scrittura viene tentata.

## Manuali

Resta valida la separazione definita nel MCP v1: Nardone/Salvini, Miller/Rollnick, Investment Model, Interdependence Theory e secure-base sono reference/euristiche. Non sono evidenza autonoma di intenzioni, attrazione o stati mentali nascosti.

## Verifica prevista

Le regressioni coprono:

1. query relazionale -> `abc_relation` read-only senza conferma;
2. nessun routing ABC per una generica relazione annuale di progetto;
3. semantica di conferma MCP esterni invariata;
4. disgiunzione completa tra allowlist read e tool write;
5. contesto minimo di default;
6. timeline/manuali solo su richiesta;
7. fallback legacy su socket non disponibile;
8. reasoning cycle -> adapter ABC canonico invece di shell;
9. broker ABC indisponibile -> errore MCP esplicito, non falsa evidenza `ls -la`.

## Stato della verifica

Le modifiche sono state scritte direttamente sul branch privato via GitHub. In questa sessione Remote Desktop Commander risulta connesso a Sibilla ma le chiamate filesystem/process sono sospese dal limite del connector. Il repository non espone una workflow GitHub Actions utile per dichiarare eseguiti questi nuovi test, quindi non viene attribuito loro un esito finché non esiste una successiva esecuzione reale su Sibilla.

Il checkpoint MCP v1 precedente resta valido per la suite già eseguita prima di questo binding (`15 passed`); quel risultato non viene esteso artificialmente alle modifiche di routing presenti qui.

## Live deployment

Questo step non modifica il sistema live. Restano da fare con accesso operativo alla macchina:

1. installare/abilitare il broker systemd ABC;
2. importare i JSON legacy correnti senza dump WhatsApp raw;
3. verificare `/run/ralf-abc-relation-mcp/mcp.sock`;
4. eseguire suite mirata e smoke end-to-end sia su `/tasks/run` sia sul reasoning cycle;
5. solo dopo attivare il branch nel runtime Bot-tazzi.


---

## Aggiornamento live — 2026-09-20

Questa sezione sostituisce lo stato `non live` descritto sopra.

### Bug `/tasks/run` confermato

Il primo smoke HTTP reale su `POST http://127.0.0.1:19090/tasks/run` mostrava che il route ABC finiva ancora nel vecchio `RalfloopAgent -> planner/coder/judge`, con tentativi `sandbox_read_file` su path inventati.

È stato aggiunto in `openshell_backend/app.py::run_task()` uno shortcut read-only prima del GPU handoff / planner loop. Quando `abc_relation` è selezionato e la route non è `external_action`, l'entrypoint delega a `run_capability_reasoning_cycle()` e restituisce:

- `capability_route`;
- `result_envelope`;
- evidence `mcp:abc_relation:read_only`;
- `approval_required=false`;
- nessuna chiamata al planner sandbox.

La regressione `test_openshell_tasks_run_shortcuts_abc_before_legacy_agent` sostituisce `_run_task_impl` con una funzione che fallisce se viene invocata, così un ritorno futuro al vecchio loop viene rilevato direttamente dal test.

### Causa reale del routing live mancante

Durante il rollout iniziale il codice e il JSON del release risultavano corretti fuori dal worker, ma nel processo uvicorn `read_mcp_keywords` risultava vuoto.

La causa è stata isolata nel drop-in systemd:

`/etc/systemd/system/ralfloop-backend.service.d/99-memory-rag-overlay.conf`

che monta read-only l'intera directory:

`/home/sibilla-cumana/ralf-memory-rag/current/config`

sopra:

`/home/sibilla-cumana/ralfloop-production/current/config`

Il vecchio overlay Memory RAG conteneva un `capability_routing.json` precedente all'introduzione di `read_mcp_keywords`, quindi mascherava il file corretto del release produzione.

Non sono stati aggiunti trigger hardcoded nell'entrypoint. È stato invece creato un nuovo release atomico dell'overlay Memory RAG, identico al precedente salvo la sezione `read_mcp_keywords`, copiata dal routing canonico del release produzione:

`/home/sibilla-cumana/ralf-memory-rag/releases/32d0bc9ed2b1f102015f4750cdf955fb93c7417c-abc-routing-5182440`

Rollback overlay immediato:

`/home/sibilla-cumana/ralf-memory-rag/releases/32d0bc9ed2b1f102015f4750cdf955fb93c7417c-mailchimp-preview`

Le modifiche diagnostiche temporanee usate per isolare il problema sono state rimosse dalla versione finale.

### Payload ABC read-only

Il payload live dello stato ABC supera 12 KiB. Il limite di rendering dell'adapter è stato portato da `12000` a `32768` caratteri, sufficiente allo stato live corrente e alle sezioni timeline/reference richieste.

È stata aggiunta una regressione che verifica che un contesto equivalente contenga integralmente:

- 12 entry timeline;
- 5 riferimenti;
- status `weak_historical_note`;
- confidence `0.35`.

### Test finali eseguiti realmente su Sibilla

Interprete:

`/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python`

Suite ABC/router binding:

`pytest -q tests/test_abc_relation_router_binding.py tests/test_abc_relation_runtime_binding.py`

Risultato: `14 passed, 2 warnings in 1.08s`.

Regressioni router/runtime/chat/reasoning:

`pytest -q tests/test_capability_router.py tests/test_capability_runtime_integration.py tests/test_chat_api.py tests/test_reasoning_cycle_node.py`

Risultato: `63 passed, 2 warnings in 1.65s`.

`git diff --check` -> OK.

Durante l'indagine sono state eseguite anche suite più larghe. Hanno evidenziato failure preesistenti/non pertinenti al diff ABC: una in `tests/test_routing.py::test_patch_allowed_mode` (`evidence.tests` vuoto) e dieci nel legacy `tests/test_abc_formula_loop.py`. Le suite finali sopra, che coprono il percorso modificato e le regressioni richieste, sono verdi.

### Smoke HTTP reali finali

Eseguite realmente contro `http://127.0.0.1:19090/tasks/run` le quattro query previste:

1. `analizza la strategia relazionale`
2. `come è messa la curva relazionale?`
3. `mostrami gli ultimi eventi`
4. `analizza la situazione usando anche dialogo strategico e manuali di psicologia`

Tutte e quattro restituiscono:

- `ok=true`;
- `capability=abc_relation`;
- `approval_required=false`;
- `stop_reason=abc_relation_read_completed`;
- `mcp_connectors=["abc_relation"]`;
- evidence `mcp:abc_relation:read_only`;
- `exit_code=0`.

Verifica timeline live:

- timeline canonica MCP: `0` eventi atomici;
- fallback `legacy_timeline`: `12` entry;
- status univoco: `weak_historical_note`;
- confidence univoca: `0.35`.

Verifica reference library live: `5` riferimenti:

- `dialogo_strategico`;
- `motivational_interviewing`;
- `investment_model_interdependence`;
- `interdependence_theory`;
- `attachment_secure_base`.

Sul PID finale del worker backend, dopo i quattro smoke:

- `[LOOP]`: `0`;
- `sandbox_read_file`: `0`;
- `ls -la`: `0`;
- vecchi log `[TASKS_RUN]`: `0`;
- `POST /tasks/run`: `4`.

### Write policy

Il normale percorso ABC resta strettamente read-only.

`abc_record_event` e `abc_create_snapshot` compaiono nei file live soltanto nella dichiarazione `WRITE_TOOLS`; non esistono call-site in:

- `openshell_backend/app.py`;
- `ralfloop_agent/nodes/reasoning.py`;
- `ralfloop_agent/integration/abc_relation_read.py`.

La regressione mantiene `READ_ONLY_TOOLS.isdisjoint(WRITE_TOOLS)`.

### Broker e import live

`ralf-abc-relation-mcp-broker.service` -> `active`.

Il broker resta sul release già validato:

`/home/sibilla-cumana/ralf-abc-relation-mcp/releases/d4d81810995eaa35911d642a681fc719efbb5bb8`

Socket canonico:

`/run/ralf-abc-relation-mcp/mcp.sock`

Il client live come utente `sibilla-cumana` ha verificato:

- timeline canonica `0`;
- legacy timeline `12`;
- reference library `5`.

Non è stato rifatto l'import: resta valido l'import già verificato di esattamente 5 JSON strutturati, senza dump WhatsApp RAW. Lo score canonico resta neutro in assenza di eventi atomici: score `50`, confidence `0`, evidence count `0`.

### Release produzione e rollback

Release codice live finale:

`/home/sibilla-cumana/ralfloop-production/releases/310a9180635414e51f37af643464fb0a1574f24e`

Commit codice:

`310a9180635414e51f37af643464fb0a1574f24e` — `abc: finalize live read-only task binding`

`ralfloop-backend.service` -> `active`.

Rollback immediato produzione:

`/home/sibilla-cumana/ralfloop-production/releases/5182440b30d61f91b5de84a94ec9f04b16769065`

Restano inoltre conservati i release precedenti `d4d81810995eaa35911d642a681fc719efbb5bb8` e `b5005a2c05e25699d44394463e8988cce0f9ddb4`.

### Esito

Il binding live `/tasks/run -> abc_relation -> reasoning cycle read-only -> broker MCP` è ora validato end-to-end. Il vecchio planner sandbox non viene più avviato per le quattro query ABC previste.

## Hardening overlay e startup canary — 2026-09-20

Il rischio residuo del rollout ABC è stato eliminato alla radice. Il vecchio
`99-memory-rag-overlay.conf` montava l'intera directory `ralf-memory-rag/current/config`
sopra `ralfloop-production/current/config`, permettendo a un overlay più vecchio di
nascondere `capability_routing.json` del release canonico.

Correzione definitiva:

- i config live necessari di Memory RAG sono stati integrati nel release Bot-tazzi;
- il drop-in Memory RAG ora monta soltanto i moduli Python necessari;
- non esiste più un bind Memory RAG dell'intera directory `production/current/config`;
- `mcp_catalog_v1.json` resta sovrapposto singolarmente dal drop-in Meta Social;
- aggiunto `scripts/check_abc_live_routing.py`;
- aggiunto `98-abc-routing-canary.conf` con `ExecStartPre` fail-closed;
- il canary stabile è installato in `/usr/local/libexec/ralfloop-check-abc-routing.py`;
- `RALFLOOP_RELEASE_ROOT` è fissato esplicitamente a `production/current`.

Il primo tentativo del canary stabile ha rilevato un falso project root `/usr/local`
per la presenza di `/usr/local/src`; il backend non è partito, come previsto dal
fail-closed. È stato corretto passando esplicitamente `RALFLOOP_RELEASE_ROOT`, quindi
`ExecStartPre` è passato con `status=0` e il backend è tornato `active`.
Verifica finale:

- regressione overlay/canary/config: `72 passed, 2 warnings`;
- suite ABC mirata dopo il canary stabile: `16 passed, 2 warnings`;
- `git diff --check`: OK;
- un test Unified Assistant (`bandi -> email draft`) resta rosso anche col vecchio config:
  baseline preesistente, non introdotto da questo hardening;
- quattro smoke HTTP ABC reali: tutti `ok=true`, `approval_required=false`,
  `mcp=["abc_relation"]`, evidence `mcp:abc_relation:read_only`, `exit=0`;
- `mostrami gli ultimi eventi`: 12 note legacy;
- query manuali/dialogo: 5 riferimenti;
- `/health`: `ok`;
- `ExecStartPre`: `status=0`;
- nessun bind Memory RAG di `production/current/config` nel namespace finale.

Commit principali hardening:

- `0f0d35c` — integra config runtime e aggiunge overlay ristretto + canary;
- `813d772` — rende il canary indipendente dal singolo release;
- `fd30fd3` — fissa esplicitamente la root live del canary.

Release live finale:
`/home/sibilla-cumana/ralfloop-production/releases/fd30fd3b905a1d543958b56d307c3b4ca535353c`

Rollback immediato:
`/home/sibilla-cumana/ralfloop-production/releases/813d772be2574481f953a406f4c76109224d48f7`

Backup del vecchio drop-in full-config, non caricato da systemd:
`/etc/systemd/system/ralfloop-backend.service.d/99-memory-rag-overlay.conf.pre-20260920.bak`.

## Revalidation operativa — 2026-09-20 23:44 CEST

Ripresa dal prompt che indicava ancora `d4d8181`: il branch privato era già avanzato fino all'hardening finale e il worktree risultava pulito.

Verifica rieseguita sul runtime corrente:

- branch/upstream privato: `feat/abc-relation-live-integration-20260920` a `95de3fe` prima di questa nota;
- release produzione live: `fd30fd3b905a1d543958b56d307c3b4ca535353c`;
- broker ABC live: release `d4d81810995eaa35911d642a681fc719efbb5bb8`;
- `ralfloop-backend.service`: active;
- `ralf-abc-relation-mcp-broker.service`: active;
- suite router/runtime/chat/reasoning: `79 passed, 2 warnings in 2.59s`;
- `git diff --check`: OK;
- quattro smoke HTTP reali: tutti `ok=true`, capability `abc_relation`, `approval_required=false`, evidence `mcp:abc_relation:read_only`, `exit_code=0`;
- log worker dopo gli smoke: quattro `POST /tasks/run 200`, nessun `[LOOP]`, `sandbox_read_file` o `ls -la`;
- timeline live: 12 note legacy, tutte `weak_historical_note`, confidence `0.35`;
- reference library live: 5 framework;
- `abc_relcalc`: score `50`, confidence `0`, evidence count `0`;
- nessun call-site di `abc_record_event` / `abc_create_snapshot` nei percorsi normali; compaiono solo nella dichiarazione `WRITE_TOOLS` dell'adapter.

Non è stato necessario modificare o riavviare il runtime: il binding live risulta già valido e resta sul release atomico `fd30fd3`.


## Revalidation operativa — 2026-09-21

Il prompt di ripresa indicava ancora `d4d8181`, ma il branch privato conteneva già il binding `/tasks/run` e l'hardening overlay/canary. Non è stata applicata una patch duplicata.

Verifica rieseguita sul runtime corrente:

- branch privato pulito a `878282fe4d1f6e90c307192af8f3e637f355dab4` prima di questa nota;
- suite ABC + importer + relcalc + router/runtime/chat/reasoning: `94 passed, 2 warnings in 3.02s`;
- `git diff --check`: OK prima della nota;
- `ralfloop-backend.service`: `active`;
- `ralf-abc-relation-mcp-broker.service`: `active`;
- produzione live: `/home/sibilla-cumana/ralfloop-production/releases/fd30fd3b905a1d543958b56d307c3b4ca535353c`;
- broker ABC live: `/home/sibilla-cumana/ralf-abc-relation-mcp/releases/d4d81810995eaa35911d642a681fc719efbb5bb8`;
- canary `ExecStartPre`: presente, ultimo `status=0`;
- quattro smoke HTTP reali richiesti: tutti `ok=true`, capability `abc_relation`, `approval_required=false`, evidence `mcp:abc_relation:read_only`, `exit_code=0`;
- verifica aggiuntiva timeline: `12` note legacy, tutte `weak_historical_note`, confidence `0.35`;
- verifica aggiuntiva reference library: `5` riferimenti;
- log backend negli smoke: `0` `[LOOP]`, `0` `sandbox_read_file`, `0` `ls -la`;
- `abc_record_event` e `abc_create_snapshot` non hanno call-site nei percorsi normali; compaiono soltanto nella dichiarazione `WRITE_TOOLS` dell'adapter.

Nessuna modifica runtime e nessun restart sono necessari: il binding live resta valido sul release atomico `fd30fd3`; rollback immediato resta `813d772be2574481f953a406f4c76109224d48f7`.

## Canonical event-store bootstrap — 2026-09-21

È stato avviato il passaggio dalle sole note legacy a eventi atomici canonici.

Prima della scrittura è stato creato un backup SQLite consistente fuori dal repository. Sono stati poi registrati tramite il broker MCP live 8 eventi normalizzati user-reported, senza raw chat e con provenance `chatgpt_snapshot`.

Risultato canonico dopo il primo batch:

- active events: `8`;
- observed facts: `5`;
- inferences: `0`;
- score: `69`;
- confidence: `0.95`;
- bias flags: `[]`;
- next safe action: `light_non_pressing_presence`.

Lo snapshot legacy resta disponibile e conserva 12 note storiche; `mostrami gli ultimi eventi` usa ora gli 8 eventi canonici come timeline primaria.
È stato inoltre aggiunto `tools/abc_relation_record_events.py` per rendere l'ingestione ripetibile senza inserire dati privati nel repository:

- input: JSON già normalizzato;
- default: dry-run/validazione;
- scrittura soltanto con `--commit`;
- timestamp timezone-aware obbligatorio;
- enum ABC validati;
- limiti confidence/weight e raw excerpt applicati;
- nessuna lettura automatica di dump WhatsApp.

Verifica dopo l'aggiunta del tool:

- `py_compile`: OK;
- suite mirata + regressioni router/runtime/chat/reasoning: `84 passed, 2 warnings in 2.51s`;
- `git diff --check`: OK;
- smoke HTTP `/tasks/run` sulla curva: score `69`, confidence `0.95`, evidence `8`;
- smoke `mostrami gli ultimi eventi`: timeline canonica `8`, legacy timeline nello snapshot `12`.

Nessun restart del backend è richiesto per questo helper offline; il runtime live continua a usare il release già validato `fd30fd3`.

## Human-readable ABC answers — 2026-09-21

Il percorso live ABC non restituisce più il payload JSON come `final_answer`. Il JSON read-only resta integralmente in `result_envelope.evidence.stdout`, mentre `final_answer` viene renderizzato in forma leggibile e prudenziale.

Guardrail espliciti nel renderer:

- lo score ABC è un indicatore tecnico rispetto al neutro 50, non una probabilità di successo;
- la confidence misura qualità/coerenza dell'evidenza registrata, non certezza sulle intenzioni dell'altra persona;
- i manuali restano euristiche e non prove di stati mentali nascosti;
- la strategia viene resa solo tramite le safe action del calcolatore.

Test prima del rollout:

- `py_compile`: OK;
- suite ABC/router/runtime/chat/reasoning: `84 passed, 2 warnings in 2.07s`;
- `git diff --check`: OK.
Commit codice rollout:

`b39b6f882c9627bd8a2bdae84a9fbab0fedeb042` — `abc: render human-readable live relation answers`

Release live:

`/home/sibilla-cumana/ralfloop-production/releases/b39b6f882c9627bd8a2bdae84a9fbab0fedeb042`

Rollback immediato:

`/home/sibilla-cumana/ralfloop-production/releases/fd30fd3b905a1d543958b56d307c3b4ca535353c`

`ExecStartPre` ABC -> status `0`; backend e broker restano active; `/health` -> `ok`.

Quattro smoke HTTP reali hanno restituito `ok=true`, `approval_required=false`, evidence `mcp:abc_relation:read_only`, `exit_code=0`. Score live: `69`, evidence count `8`, safe action `light_non_pressing_presence`; timeline `8`; reference library `5`. Nessun `[LOOP]`, `sandbox_read_file` o `ls -la` nei log del nuovo worker.

Gli 8 summary iniziali in inglese sono stati superseduti nel DB da equivalenti italiani tramite la semantica nativa `supersedes_event_id`: restano 8 eventi attivi, score e confidence invariati, provenance conservata. Backup SQLite creato prima della sostituzione; nessun dato relazionale privato è stato committato su GitHub.
