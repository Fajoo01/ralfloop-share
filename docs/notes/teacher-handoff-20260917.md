# Teacher Bot-tazzi — handoff 2026-09-17

## Scopo
Riprendere il tutor universale Bot-tazzi: qualità pedagogica, UI/avatar/voce, MCP interni deterministici e fallback Gemma 3 4B in pool k3s.

## Repository e branch
- repo privato: `Fajoo01/ralfloop-bottazzi`
- worktree: `/home/bandi/ralfloop-teacher-integration`
- branch: `teacher/production-integration-20260915`
- HEAD GitHub: `34f964e49af4e33087dac7802758224e401f087c`

## Produzione reale verificata
- backend current: `/home/sibilla-cumana/ralfloop-production/releases/3023e9309484aa5b16ea901c07aaf2c709017f16`
- il current backend contiene `3c6e80c` come antenato, quindi include il tutor con obiezioni/controesempi via Core MCP C.
- `34f964e` NON è ancora nel backend current.
- web current: `/home/bandi/.local/share/ralf-teacher-web/releases/3c6e80cc7505d903427099d1fcbf9f868432b792`

## Stato tutor già live
- 13 tool studente invariati.
- Core MCP C e Grammar MCP restano interni.
- `core.classify_turn` riconosce controesempi/obiezioni; caso fazzoletto -> `counterexample`.
- domanda studente e contesto esercizio sono separati nel prompt.
- UI attività: avatar/personaggio vicino a feedback e textarea, non più solo bollino globale.
- Fish TTS e jaw animation sono già integrati nel web corrente.
## Commit `34f964e` pronto ma non deployato
- aggiunge `core.concept_evidence` nel Core MCP C;
- dataset curato `ralfloop_agent/teacher/data/concept_evidence.tsv`;
- shutdown graceful del Core MCP;
- fallback CPU grounded con Gemma 3 4B, solo fast path e solo con evidenza deterministica/grammaticale;
- regressione completa del blocco integrazione: `295 passed, 4 skipped`.

## Caso didattico di riferimento
Domanda: `ma un fazzoletto prende la forma del contenitore ma non è liquido cosa c'entra il ghiaccio`.
Il tutor deve chiarire che un solido può essere flessibile/deformabile; adattarsi al contenitore non basta a definire un liquido; un liquido fluisce spontaneamente; il ghiaccio serve solo come esempio dello stato solido dell'acqua.

## Pool Gemma 3 4B — WIP locale, NON deployato
File locali non ancora committati:
- `deploy/k8s/teacher-gemma-pool.yaml`
- `docs/TEACHER_GEMMA_POOL.md`
- `tools/prepare_teacher_gemma_worker.py`
- `tools/teacher_model_pool_mcp/`

Hardware noto: 9 PC totali equivalenti a Temistocle (i5-6400T, 4 core, circa 8 GB RAM). Temistocle ha già `gemma3:4b` e passa il preflight worker. Benchmark warm: Sibilla ~20,1 s / 180 token; Temistocle ~39,0 s / 180 token. Strategia: repliche indipendenti, non sharding.

Il Model Pool MCP C compila con `-Werror`; design: `pool.acquire`, `pool.release`, `pool.health`, lease bounded e selezione load-aware. Attenzione: il binario WIP usa `--config FILE`; l'ultimo smoke fallito usava erroneamente `--node`, quindi va rifatto con config reale prima di commit/deploy.
## Ordine consigliato per la nuova chat
1. Leggere questo file e verificare stato reale di Git, release e servizi; non fidarsi ciecamente dell'handoff.
2. Non sovrascrivere il current `3023e930`: integrare il Teacher sopra il current più recente.
3. Sistemare e testare il Model Pool MCP WIP con `--config FILE`; aggiungere test automatici reali MCP.
4. Completare packaging release del nuovo binario MCP e manifest k3s.
5. Provare il pool su Sibilla + Temistocle senza attivarlo nel Teacher live.
6. Solo dopo benchmark/gate verdi, collegare il fallback Teacher al Model Pool MCP invece che direttamente a Ollama.
7. Conservare i 13 tool studente e non esporre infrastruttura/amministrazione nel Teacher MCP.
8. Per i nuovi PC: join k3s -> preflight -> `gemma3:4b` -> label `ralf-teacher-gemma=ready` -> pod Ready -> benchmark -> ingresso pool.

## Vincoli
- software di supporto all'LLM preferibilmente MCP interni; C per fast path deterministici quando utile;
- modello pesante il meno possibile;
- nessun tool amministrativo/infrastrutturale sul MCP studente;
- usare GitHub privato anche come diario tecnico;
- non terminare benchmark/processi DS4 di altri lavori;
- testare comportamento reale prima di dichiarare deploy riuscito.
