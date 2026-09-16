# Ralf / Ralfloop / Skill System - Context for DeepSeek

Data: 2026-07-03
Repo: `/home/sibilla-cumana/ralfloop_agent_scaffold`

## 1. Scopo del documento

Questo documento serve per dare a DeepSeek un quadro tecnico compatto del sistema Ralf/Ralfloop:

- architettura locale;
- loop operativo;
- backend OpenShell;
- skill specialistiche;
- motori LLM locali;
- problemi aperti;
- richieste di consulenza tecnica.

Non contiene credenziali, token o dati segreti.

## 2. Definizione rapida

Ralfloop e' un orchestratore locale per trasformare richieste tecniche verificabili in output validati.

Principio base:

```text
utente
-> routing
-> planner
-> coder / tool action
-> executor
-> judge / evaluator
-> output finale + artifact + audit
```

Regola centrale:

```text
il modello propone
l'esecutore valida
il judge decide
la policy blocca operazioni rischiose
```

Il sistema non dovrebbe affidare a un LLM il risultato finale quando esiste una verifica deterministica. Per calcoli, scoring e task tecnici, lo stdout o la formula versionata devono prevalere sulla prosa del modello.

## 3. Componenti principali

### 3.1 OpenShell backend

File principale:

```text
openshell_backend/app.py
```

Ruolo:

- FastAPI backend locale;
- crea sandbox;
- legge/scrive/lista file dentro sandbox;
- esegue comandi controllati;
- fa stream probe;
- espone endpoint `/tasks/run`;
- collega agent loop, planner e adapter reali.

Endpoint/azioni ricorrenti:

```text
POST /sandboxes
GET  /sandboxes/{sid}/list
GET  /sandboxes/{sid}/read
POST /sandboxes/{sid}/write
POST /sandboxes/{sid}/exec
POST /sandboxes/{sid}/probe_stream
POST /tasks/run
```

Vincoli:

- policy default-deny dove possibile;
- niente scritture arbitrarie fuori sandbox senza esplicita autorizzazione;
- niente effetti esterni senza conferma;
- audit degli eventi.

### 3.2 Ralfloop agent core

Cartella:

```text
ralfloop_agent/
```

Sottosistemi principali:

```text
ralfloop_agent/core/
ralfloop_agent/adapters/
ralfloop_agent/providers/
ralfloop_agent/tools/
ralfloop_agent/contracts/
ralfloop_agent/logging/
ralfloop_agent/experimental/
```

Nota operativa: nella sessione corrente alcuni file sotto `ralfloop_agent/core/` risultano non leggibili dall'utente `bandi` per permessi filesystem. La struttura pero' e' inferibile dai test e dagli import:

```text
core/loop.py
core/state.py
core/policy.py
core/capability_router.py
```

Ruoli probabili:

- `loop.py`: ciclo observe/plan/act/evaluate;
- `state.py`: stato agente;
- `policy.py`: controlli su path, comandi, URL;
- `capability_router.py`: instradamento task verso capability/skill.

### 3.3 Adapter OpenShell

File:

```text
ralfloop_agent/adapters/openshell_adapter.py
ralfloop_agent/adapters/openshell_real_adapter.py
```

Ruolo:

- astrae accesso a sandbox locale o backend reale;
- produce `ToolResult`;
- applica policy prima di read/write/exec/http/probe;
- normalizza errori come `policy_denied`, timeout, runtime error.

Tool/action principali:

```text
sandbox_read_file
sandbox_write_file
sandbox_list_dir
sandbox_exec
sandbox_http_fetch
sandbox_probe_stream
```

## 4. Loop operativo Ralfloop v1

Spec riferimento:

```text
```

Classi task:

```text
shell_commands
python_code
calculation
structured_text
generic_text
```

Contratti:

- `shell_commands`: il coder deve produrre comandi shell, senza prosa interna.
- `python_code`: il coder deve produrre Python eseguibile.
- `calculation`: usare Python/stdout come fonte di verita'.
- `structured_text`: aderire al formato richiesto.
- `generic_text`: non forzare il loop se non verificabile.

Artifact attesi per task nel loop:

```text
planner_rag.txt
coder_rag.txt
judge_rag.txt
planner.txt
coder.txt
result.txt
meta.txt
exec_attempt_1.txt
exec_attempt_2.txt
candidate_attempt_1.py/.sh
candidate_attempt_2.py/.sh
```

Retry policy:

- max baseline: 2 tentativi;
- retry su errori concreti;
- hint strutturati: `syntax_error`, `permission_error`, `timeout`, `runtime_error`, `missing_path`, ecc.

## 5. Legacy external-agent bridge

Removed from the active architecture. Routing, policy and provider ownership now stay inside Ralfloop.

## 6. Planner e provider LLM

File:

```text
ralfloop_agent/providers/ollama.py
ralfloop_agent/providers/inference_runtime.py
```

Planner presenti:

```text
DeterministicPlanner
OllamaPlanner
```

`DeterministicPlanner`:

- fallback rule-based;
- utile quando LLM fallisce;
- copre lettura file, list directory, exec base, Ollama tags, stream probe.

`OllamaPlanner`:

- planner LLM locale;
- produce JSON con:

```json
{
  "tool_name": "sandbox_exec | sandbox_read_file | sandbox_list_dir | sandbox_http_fetch",
  "tool_input": {},
  "why": "..."
}
```

### 6.1 Runtime abstraction aggiunta

Modulo:

```text
ralfloop_agent/providers/inference_runtime.py
```

Oggetti:

```text
RuntimeCapabilities
GenerateRequest
GenerateResult
InferenceRuntime
OllamaRuntime
OpenAICompatRuntime
LMStudioRuntime
```

Runtime attivi/logici:

```text
ollama          -> http://127.0.0.1:11434/api/generate
lmstudio        -> http://127.0.0.1:1234/v1/chat/completions
openai_compat   -> http://127.0.0.1:8000/v1/chat/completions
```

Selezione runtime:

```bash
RALF_INFERENCE_RUNTIME=ollama
RALF_INFERENCE_RUNTIME=lmstudio
RALF_INFERENCE_ACCEL_DISABLE=1
```

Default stabile:

```text
Ollama
```

LM Studio:

- adapter presente;
- server non risultava acceso nel test corrente;
- se LM Studio espone OpenAI-compatible API su `127.0.0.1:1234`, Ralf puo' usarlo via `RALF_INFERENCE_RUNTIME=lmstudio`.

### 6.2 Benchmark runtime

Tool:

```text
tools/inference_runtime_benchmark.py
```

Metriche:

- latency;
- prompt tokens;
- output tokens;
- tokens/sec;
- JSON parse rate;
- errori/timeout.

Comandi esempio:

```bash
PYTHONPATH=. .venv/bin/python tools/inference_runtime_benchmark.py --dry-run
PYTHONPATH=. .venv/bin/python tools/inference_runtime_benchmark.py --runtime lmstudio --dry-run
PYTHONPATH=. .venv/bin/python tools/inference_runtime_benchmark.py --runtime ollama --timeout 60
```

Risultato osservato su Ollama/qwen2.5:7b nella sessione corrente:

```text
attempts: 3
ok: 2
failed: 1
avg_latency_sec: 25.2026
avg_tokens_per_second: 5.0811
json_parse_rate: 0.0
```

Interpretazione:

- Ollama locale funziona ma e' lento;
- JSON strict planner non e' affidabile su quel micro-benchmark;
- serve test serio su LM Studio / llama.cpp / speculative decoding prima di cambiare default.

## 7. Reasoning cycle node

File:

```text
ralfloop_agent/experimental/reasoning_cycle_node.py
tests/test_reasoning_cycle_node.py
```

Nome parlato storico:

```text
"telepatia"
```

Nome tecnico corretto:

```text
reasoning_cycle_node
state_handoff
internal_state_packet
```

Scopo:

- non e' un frontier model;
- e' un nodo deterministico/strutturato per migliorare ragionamento operativo;
- produce stato esplicito e serializzabile;
- aiuta policy gate e next_action minima.

Input logico:

```text
user_goal
observations
constraints
memory
last_result
```

Cicli:

```text
1. genera ipotesi operative
2. critica ipotesi e cerca contraddizioni
3. propone verifica concreta o next_action minima
```

Output include:

```text
hypotheses
objections
evidence_needed
selected_next_action
confidence
stop_reason
contradictions
decision
```

Fix gia' fatti:

- blocco hard policy violation;
- no network + goal fetch => blocked;
- read-only + goal patch/write => blocked;
- runtime/baseline intoccabile + goal patch runtime => blocked;
- failed tool traceback => inspect failure prima di write action;
- empty goal => status stop, action none;
- lessico policy IT/EN;
- output JSON-safe.

Uso desiderato:

```text
node prima del LLM o sandwich node -> LLM -> node
```

Non deve:

- attivare da solo runtime behavior;
- bypassare policy;
- promettere ragionamento frontier;
- generare numeri decisionali non verificati.

## 8. Skill ABC/RSC

Cartella:

```text
openshell_backend/skills/
```

File principali:

```text
abc_formula_loop.py
abc_memory.py
abc_memory_curator.py
abc_relcalc.py
abc_rulebooks/
```

Rulebook:

```text
openshell_backend/skills/abc_rulebooks/relation_manual_v1.md
openshell_backend/skills/abc_rulebooks/anti_bias_manual_v1.md
openshell_backend/skills/abc_rulebooks/evidence_weights_v1.json
openshell_backend/skills/abc_rulebooks/calibration_cases_v1.jsonl
```

### 8.1 ABC Formula Loop

Versione:

```text
abc_formula_loop_v1
```

Scopo:

- scoring relazionale deterministico;
- niente numeri "a sentimento" dal LLM;
- LLM eventualmente solo per estrarre evidenze o review manuali, non per calcolo runtime;
- formula e pesi versionati sono fonte primaria.

Formula concettuale:

```text
raw_score = 50 + sum(weights)
confidence = max(0.0, 1.0 - penalties)
rlfull_current = clamp(raw_score * confidence, 0, 100)
prudential_score = clamp(raw_score * min(confidence, 0.8), 0, 100)
```

Output:

```text
rlfull_current
prudential_score
relcalc_score
confidence
operative_range
action
bias_flags
formula_version
trace
```

Regola:

```text
Formula Loop = fonte primaria per score/range/action.
Legacy memory/LLM diagnostics = solo diagnostica.
```

### 8.2 ABC Memory

File:

```text
abc_memory.py
abc_memory_curator.py
abc_memory/
```

Obiettivo:

- memoria conservativa;
- event sourcing;
- raw history append-only;
- fatti non cancellati;
- interpretazioni ristrutturabili;
- correzioni applicate allo stato corrente senza cancellare storico.

Tipi evento:

```text
full_patch
micro_delta
correction_patch
reinterpretation_patch
supersession_patch
```

Regola stato corrente:

```text
last full_patch valida
+ micro_delta successivi
+ correction_patch applicate
+ reinterpretation_patch
= merged_current_context
```

Curator:

- schema-driven;
- corregge errori di classificazione;
- esempio: "amico cinese = Matthew" deve diventare `correction_patch`;
- `facts_corrected` deve contenere `old_value`, `new_value`, `mode=superseded_not_deleted`;
- non deve aumentare automaticamente pressione terzo/competitor.

Bug gia' corretti:

- micro-delta non sostituisce full patch;
- correction_patch applica correzione senza cancellare raw;
- `Formula Loop` resta fonte primaria;
- sintesi incorpora reinterpretation_patch recenti;
- sottosistema palestra/Gabriele/pre-Asia resta qualitativo, non "terzo attivo" automatico.

Problema ancora da monitorare:

- nuovi eventi factual con molte osservazioni devono entrare come factual delta operativo, non solo reinterpretation audit;
- la formula deve pesare solo evidence_id validati, non frasi grezze.

Schema corretto:

```text
frase osservata
-> interpretazione vincolata
-> evidence_id
-> peso deterministico
```

Non corretto:

```text
frase osservata
-> peso diretto
```

## 9. Skill / domini esterni noti

### 9.1 Jellyfin / video verify

File:

```text
tools/jellyfin_gemma_video_verify.py
config/ralf/jellyfin_gemma_video_verify.json
```

Scopo:

- verificare media Jellyfin;
- usare Gemma/Ollama per controllo video quando serve;
- evitare falsi positivi e download inutili/doppioni;
- distinguere film nuovi da sostituzioni non necessarie.

Problemi emersi:

- download doppioni/audio;
- generi non devono essere necessariamente entrambi presenti, puo' bastare uno o l'altro;
- serve policy conservativa: non sostituire se il film c'e' gia' in forma accettabile.

### 9.2 Garden detector

Live fuori repo:

```text
/opt/bottazzi-garden/garden_person_detector.py
bottazzi-garden-detector.service
bottazzi-garden-stream-warmer.service
```

Elementi:

```text
YOLO primary
Gemma gated validation
go2rtc frame source
warmer cache
frame cache fallback HTTP
```

Stato storico:

- problema non era YOLO/Gemma ma `/api/frame.jpeg` go2rtc lento/instabile;
- micro-differita 0/2/4/6s non migliorava;
- soluzione: cache warmer frame come source prioritaria;
- detector usa cache e fallback HTTP.

### 9.3 ATM Telegram

File noti:

```text
openshell_backend/atm_telegram.py
openshell_backend/skill_atm_telegram.py
tests/test_atm_telegram_address_resolution.py
```

Scopo:

- risposte Telegram su bus/ATM;
- risoluzione indirizzi;
- browser/cookie/profile dedicato;
- vincolo: non rompere `rlcalc` e altre skill.

### 9.4 Meowgram / Telegram

Repo esterno:

```text
/srv/projects/Meowgram
```

Ruolo:

- bot Telegram come interfaccia;
- non deve contenere logica di calcolo;
- deve chiamare Ralfloop/tool e inviare risultati.

Caso `rlfull`:

- comando `rlfull` o `/rlfull`;
- genera/invia `RSC_ABC_RAPPORTO_FULL_CURRENT.txt`;
- deve includere blocco deterministic summary ABC/Relcalc nel TXT;
- mantiene summary separato per compatibilita'.

### 9.5 Trade Republic / browser skill

File presenti:

```text
openshell_backend/skills/trade_republic_browser_assistant.py
openshell_backend/skills/trade_republic_browser_cdp_observer.py
openshell_backend/skills/trade_republic_browser_detail_observer.py
openshell_backend/skills/trade_republic_browser_detail_parser.py
openshell_backend/skills/trade_republic_market_enricher.py
openshell_backend/skills/trade_republic_pdf_parser.py
openshell_backend/skills/trade_republic_portfolio.py
openshell_backend/skills/trade_republic_rebound_model.py
```

Nota:

- skill browser/portfolio finanziario;
- da trattare come high-risk: no invii/azioni esterne senza conferma.

### 9.6 Camper / POI / gas installazioni

File presenti:

```text
openshell_backend/skills/camper_poi_clean.py
openshell_backend/skills/camper_poi_import.py
openshell_backend/skills/campingcar_infos_research.py
```

Problemi recenti utente:

- compilatore moduli installazioni Morgan/GAS mette dati nel rettangolo sbagliato;
- `Rif. scontrino/ordine` e `Data scontrino/acquisto cliente` devono andare vicino a:

```text
DATA VOSTRA FATTURA O SCONTRINO CLIENTE
```

Non e' ancora chiaro da questo documento quale modulo implementi quel compilatore.

## 10. Policy operative generali

Regole consolidate:

```text
default-deny
no network se vincolo no-network
no write se read-only/no-write
external action richiede conferma
email/invio remoto richiede conferma umana
runtime/baseline intoccabile se dichiarato
no git add .
no commit salvo richiesta
no file runtime/cache in commit
```

Per task email/documenti:

- preparare bozza;
- mostrare testo e allegati;
- inviare solo dopo conferma;
- se utente chiede conferma Telegram, non inviare prima della conferma.

Per scoring ABC:

- Formula Loop primaria;
- LLM non genera score runtime;
- memory/curator puo' classificare fatti, ma non assegnare numeri manuali;
- evidence_id validati -> pesi deterministic.

## 11. Stato motori inferenza locali

### Attivo

```text
Ollama HTTP
http://127.0.0.1:11434
```

Modelli visti via `/api/tags`:

```text
qwen2.5:7b
qwen2.5-coder:7b
gemma4:12b-it-qat
gemma4-lowctx:latest
llama3.2-vision:latest
glm-ocr:latest
llava:latest
```

### Integrato come adapter, non attivo al test

```text
LM Studio
http://127.0.0.1:1234/v1/chat/completions
```

Al test:

```text
connection refused
```

### Previsto/lab, non attivo

```text
OpenAI-compatible local server
llama.cpp server
DeepSpec / DSpark speculative decoding
dynamic quantization
SSD streaming / mmap
```

## 12. Tema DeepSeek / speculative decoding

Documento correlato:

```text
docs/ralf_local_inference_acceleration_todo.md
```

Obiettivo:

- migliorare velocita' LLM locale senza perdere qualita';
- non cambiare default senza benchmark;
- introdurre backend abstraction e benchmark;
- valutare speculative decoding target/draft;
- valutare DSpark/DeepSpec solo come laboratorio separato.

Richieste tecniche per DeepSeek:

1. Proporre architettura runtime locale per Ralf con:
   - Ollama default stabile;
   - LM Studio adapter;
   - llama.cpp/OpenAI-compatible adapter;
   - DeepSpec/DSpark lab isolato.

2. Proporre benchmark minimo per:
   - JSON strict planner;
   - code generation;
   - judge/evaluator;
   - ABC summary;
   - tool routing;
   - email draft con vincoli.

3. Proporre soglie rollout:
   - tokens/sec;
   - TTFT;
   - JSON parse rate;
   - task success rate;
   - equivalenza decisione tool;
   - regressioni allucinazione.

4. Valutare se speculative decoding e' adatto a:
   - planner JSON strict;
   - coder patch minimale;
   - judge;
   - reasoning_cycle_node.

5. Suggerire come integrare draft/target model senza rompere:
   - deterministic fallback;
   - policy gate;
   - audit;
   - result envelope.

## 13. Problemi aperti da far valutare

### 13.1 Local LLM performance

Ollama/qwen2.5:7b nel benchmark reale e' risultato lento e non affidabile su JSON strict.

Serve capire:

- prompt schema migliore;
- model selection;
- LM Studio performance;
- llama.cpp server con grammar/JSON schema;
- speculative decoding effettivamente lossless;
- fallback automatico se output invalido.

### 13.2 Skill routing

Ci sono molte skill/domains. Serve evitare che task simili vadano alla skill sbagliata.

Esempi:

- `rl:abc` deve aggiornare memoria, non calcolare soltanto.
- `rlcalc` deve usare formula/calcolatrice, non memoria.
- Jellyfin deve evitare doppioni, non sostituire tutto.
- email/banca deve preparare bozza, non inviare senza conferma.

### 13.3 Memory curator ABC

Il sistema ha iniziato a correggere bene eventi RL:ABC, ma serve rendere robusta la pipeline:

```text
raw event append-only
-> curator schema-driven
-> validator deterministic
-> merge deterministic
-> formula loop primary score
```

Rischio:

- regex troppo deboli;
- LLM curator che inventa;
- factual delta classificati solo come reinterpretation;
- frasi grezze pesate direttamente.

### 13.4 Result envelope unico

Serve schema unico per ogni tool/skill:

```json
{
  "ok": true,
  "status": "continue | blocked | stop | needs_confirmation",
  "source": "deterministic | llm | hybrid",
  "action": {},
  "artifacts": [],
  "warnings": [],
  "audit": {}
}
```

Obiettivo:

- evitare doppia verita';
- separare diagnostica legacy da output operativo;
- rendere machine-readable ogni passaggio.

## 14. Cosa non fare

Non proporre:

- riscrittura totale;
- cloud-only;
- score relazionali generati dal LLM;
- eliminazione fallback deterministico;
- bypass policy per velocita';
- host-visible dentro baseline senza test;
- training DeepSpec pesante senza proof-of-value;
- automatic email/send/external action senza conferma.

## 15. Output desiderato da DeepSeek

DeepSeek dovrebbe rispondere con:

1. audit architettura;
2. rischi principali;
3. proposta modulare;
4. patch plan minimo;
5. benchmark plan;
6. schema runtime provider;
7. schema result envelope;
8. policy gate migliorata;
9. piano per speculative decoding/DSpark;
10. cosa tenere deterministico.

Formato preferito:

```text
Problema
Causa
Soluzione proposta
Patch minima
Test
Rischi
Rollback
```

## 16. Riassunto finale

Ralfloop oggi e':

- orchestratore locale con backend OpenShell;
- policy e sandbox;
- skill domain-specific;
- ABC Formula Loop deterministico;
- ABC memory append-only/event-sourced;
- reasoning_cycle_node sperimentale per deliberazione/policy;
- runtime LLM basato su Ollama, con adapter LM Studio/OpenAI-compatible appena aggiunto;
- benchmark locale appena introdotto.

Linea guida:

```text
LLM = planner/extractor/proposer
deterministic code = score/verify/merge/policy
executor = fonte verita' per task verificabili
judge = selezione finale solo dopo evidenza
```
