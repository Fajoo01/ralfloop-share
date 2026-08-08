# Ralfloop Capability Routing - Phase 5 Plan - 2026-07-04

## Stato Attuale

Fasi completate:

- **Fase 1**: scaffold evidence-first capability routing in `src/*`.
- **Fase 2**: integrazione OpenShell con adapter, `ResultEnvelope`, endpoint conferme.
- **Fase 3**: `ShellExecutor` reale, MCP con confirmation action store, audit JSONL.
- **Fase 4A**: Google Email draft-only con feature flag, OAuth via env, fake client per test.
- **Fase 4B**: send reale dopo conferma, sandbox per `task_id`, audit persistente.

Stato dichiarato:

```text
ShellExecutor reale con sandbox per task_id
Google Email draft + send con conferma umana
Audit persistente in logs/audit.jsonl
ResultEnvelope come primary contract
Confirmation store con execute-on-confirm
Test: 35 passed, 1 skipped Google real creds + 7 router = 42 passed, 1 skipped
Core protetto ralfloop_agent/core/* NON TOCCATO
Nessun commit fatto
```

## Problema

Il capability routing ora funziona, ma resta una fase di hardening prima di considerarlo runtime stabile:

- audit e confirmation sono ancora locali e non robusti a restart/process crash;
- `ResultEnvelope` convive con output legacy;
- Gmail reale richiede validazione end-to-end controllata;
- sandbox task-isolated esiste, ma manca lifecycle policy;
- manca una policy esplicita di recovery su conferme fallite o azioni esterne parziali;
- manca un handoff operativo per deploy/config OAuth.

## Obiettivo Fase 5

Portare il capability routing da scaffold integrato a runtime hardenizzato, senza toccare il core protetto.

Contratto finale:

```text
user_goal
-> capability_route
-> evidence / confirmation / patch
-> ResultEnvelope
-> audit persistente
-> risposta legacy compatibile
```

Regola:

```text
ResultEnvelope e' fonte primaria.
Legacy fields restano solo adapter compatibility.
External action non parte mai senza conferma approvata.
```

## Scope Patch

### 1. Persistent Confirmation Store

Creare uno store persistente per confirmation/action state.

Opzione consigliata:

```text
sqlite: data/ralf_confirmations.sqlite
```

Campi minimi:

```text
confirmation_id
action_type
details_json
status
executed
result_json
error
created_at
updated_at
```

Vincoli:

- non salvare secret o token OAuth;
- salvare solo preview/campi necessari;
- mantenere API attuale:
  - `request_confirmation`
  - `confirm_action`
  - `reject_action`
  - `execute_confirmed_action`

### 2. Audit Hardening

Rendere audit append-only e machine-readable.

Path:

```text
logs/audit.jsonl
```

Override:

```text
RALF_AUDIT_PATH=/path/to/audit.jsonl
```

Ogni evento deve avere:

```text
timestamp
operation
task_id
confirmation_id
route
evidence
result_status
error
```

Non loggare:

- OAuth token;
- full email body se contiene dati sensibili;
- password;
- path a secret file se non necessario.

### 3. ResultEnvelope Primary

Aggiornare gli endpoint runtime per rendere `result_envelope` il contratto principale.

Compatibilità:

- mantenere `ok`, `final_answer`, `capability_route`, `audit_summary`;
- ma derivarli dal `ResultEnvelope` dove possibile.

Obiettivo:

```text
response.result_envelope.route.mode
response.result_envelope.evidence.command
response.result_envelope.confirmation.status
response.result_envelope.meta.task_id
```

### 4. Gmail Real E2E Safe Flow

Validare Gmail reale solo dietro flag espliciti:

```text
RALF_MCP_GOOGLE_ENABLED=1
RALF_GOOGLE_CLIENT_SECRETS=/path/to/client_secrets.json
RALF_GOOGLE_TOKEN=/path/to/token.json
RALF_GOOGLE_DRAFT_ONLY=1
```

Fase sicura:

- default `RALF_GOOGLE_DRAFT_ONLY=1`;
- real send solo con:

```text
RALF_GOOGLE_DRAFT_ONLY=0
human confirmation approved
```

Test reali:

```text
RUN_GOOGLE_REAL_TESTS=1
```

Mai eseguire real send nei test automatici standard.

### 5. Sandbox Lifecycle

Sandbox corrente:

```text
/tmp/ralf_sandbox/<task_id>/
```

Da aggiungere:

- helper `cleanup_old_sandboxes(max_age_hours)`;
- test che non tocchi directory fuori sandbox;
- audit del sandbox path per ogni task.

### 6. Failure Recovery

Definire stati:

```text
pending
approved
rejected
executed
failed
```

Regole:

- `failed` non deve riprovare automaticamente;
- retry solo con nuova conferma o retry esplicito;
- errore MCP deve finire in `ResultEnvelope.meta.error`;
- audit deve contenere `confirmation_failed`.

## File Ammessi

```text
src/audit.py
src/executor.py
src/mcp_client.py
src/google_client.py
src/google_client_fake.py
src/models.py
ralfloop_agent/integration/confirmation_store.py
ralfloop_agent/nodes/reasoning.py
ralfloop_agent/models/result_envelope.py
tests/test_audit.py
tests/test_shell_executor.py
tests/test_mcp_client.py
tests/test_google_client.py
tests/test_mcp_client_google.py
tests/test_capability_runtime_integration.py
docs/RALFLOOP_PHASE5_PLAN_20260704.md
```

File protetti:

```text
ralfloop_agent/core/*
openshell_backend/core/*
abc_memory/*
.ralf_run/*
```

## Test Obbligatori

Compile:

```bash
PYTHONPYCACHEPREFIX="$(mktemp -d /tmp/ralf_phase5_pycache_${USER}.XXXXXX)" \
  .venv/bin/python -m py_compile \
  src/audit.py \
  src/executor.py \
  src/mcp_client.py \
  src/google_client.py \
  src/google_client_fake.py \
  ralfloop_agent/integration/confirmation_store.py \
  ralfloop_agent/nodes/reasoning.py \
  ralfloop_agent/models/result_envelope.py
```

Pytest:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q \
  tests/test_routing.py \
  tests/test_capability_runtime_integration.py \
  tests/test_shell_executor.py \
  tests/test_mcp_client.py \
  tests/test_mcp_client_google.py \
  tests/test_google_client.py \
  tests/test_audit.py
```

Router storico:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q \
  tests/test_capability_router.py
```

Diff:

```bash
git diff --check
```

## Criteri Di Successo

- `ResultEnvelope` presente in ogni risposta `/tasks/run`.
- Ogni response evidence-first contiene `command`, `path`, `exit_code` quando esegue shell.
- External actions producono `pending_confirmation_id`.
- Approve esegue una sola volta l'action registrata.
- Reject non esegue nulla.
- Gmail draft/send non parte senza conferma.
- Audit JSONL contiene route, task, confirmation, result.
- Sandbox per `task_id` isolata.
- Core protetto non modificato.
- Nessun secret nel repo.

## Prompt Fase 5 Per Codex

```text
Obiettivo:
Harden capability routing Ralfloop rendendo persistenti confirmation/audit,
stabilizzando ResultEnvelope come contratto primario,
validando Gmail safe E2E e aggiungendo lifecycle sandbox.

Vincoli:
- NON toccare ralfloop_agent/core/*
- NON toccare openshell_backend/core/*
- NON toccare abc_memory/*
- NON toccare .ralf_run/*
- NON fare git add .
- NON salvare credenziali Google nel repo
- NON inviare email reali nei test standard
- Patch minima, testata, reversibile

Implementa:
1. Confirmation store persistente sqlite o JSONL atomico.
2. Audit JSONL hardenizzato con task_id/confirmation_id/result/error.
3. ResultEnvelope come fonte primaria negli endpoint.
4. Gmail send reale solo dopo confirmation approved e draft_only=0.
5. Sandbox lifecycle per task_id.
6. Test per persistence, failure recovery, no double execution, reject no-op, audit append-only.

Esegui:
- py_compile sui file modificati
- pytest capability routing mirato
- pytest router storico
- git diff --check

Report finale:
- file modificati
- test passati
- conferma core protetto non toccato
- conferma nessun secret committato
- stato commit: non fare commit se non richiesto
```

## Nota Operativa

La fase 5 non deve aumentare autonomia esterna.
Deve ridurre rischio operativo:

```text
more durable state
more explicit envelope
more auditable actions
less implicit behavior
```
