# Ralfloop / DeepSeek Update - Phase 3 - 2026-07-04

## Stato nuovo

Fase 3 completata: executor reale, MCP confirmation flow, audit JSONL e node wrapper operativo.

La patch resta conservativa:

- nessuna modifica a `ralfloop_agent/core/*`;
- nessun invio esterno senza conferma;
- MCP Google Workspace predisposto ma non forzato se non configurato;
- compatibilita' routing/endpoint preservata.

## Componenti implementati

### ShellExecutor reale

File:

```text
src/executor.py
```

Funzionalita':

- sandbox default: `/tmp/ralf_sandbox`;
- `subprocess.run`;
- `shell=False`;
- timeout default 30s;
- path guard contro directory traversal;
- cattura sempre:
  - `command`;
  - `path`;
  - `exit_code`;
  - `stdout`;
  - `stderr`;
- nuovo metodo:

```python
run_in_sandbox(command: Sequence[str], cwd: str | None = None)
```

### MCP client con conferma

File:

```text
src/mcp_client.py
```

Azioni scriventi:

```text
send_email
send_telegram
drive_upload
```

Tutte bloccano prima dell'esecuzione e producono `NeedsConfirmationError`.

Dopo approve:

- `execute_confirmed()` esegue l'action registrata;
- se `arclio_mcp_gsuite` non e' installato/configurato, ritorna `not_configured`;
- non invia nulla senza confirmation gate.

### Confirmation action store

File:

```text
ralfloop_agent/integration/confirmation_store.py
```

Aggiunto:

```text
pending_actions
execute_confirmed_action()
```

Le conferme ora possono contenere:

- action type;
- details;
- callable reale;
- args;
- status;
- executed;
- result.

### Audit logging

File:

```text
src/audit.py
```

Scrive JSONL:

```text
logs/audit.jsonl
```

Formato:

```json
{
  "timestamp": "...",
  "operation": "...",
  "route": {},
  "evidence": {},
  "confirmation": {}
}
```

Path override:

```text
RALF_AUDIT_PATH
```

### Node wrapper operativo

File:

```text
ralfloop_agent/nodes/reasoning.py
```

Flusso:

```text
user_goal
-> route_task
-> ShellExecutor oppure MCPClient
-> ResultEnvelope
-> audit.log_operation
```

Mode:

- `check_only`: esegue `ls -la` in sandbox;
- `patch_allowed`: raccoglie `git diff --no-ext-diff` in sandbox e crea `PatchEvidence`;
- `external_action`: richiede conferma e ritorna envelope con confirmation pending.

## Verifiche

Compile:

```text
py_compile OK
```

Test:

```text
tests/test_routing.py
tests/test_capability_runtime_integration.py
tests/test_shell_executor.py
tests/test_mcp_client.py
tests/test_audit.py
```

Risultato:

```text
17 passed
```

Test storico capability router via `sibilla-cumana`:

```text
tests/test_capability_router.py -> 7 passed
```

Totale mirato:

```text
24 passed
```

Smoke node:

```text
check_only -> esegue ls -la
external_action -> human_confirmation_required
approve -> executed=True, result.status=not_configured se MCP reale assente
```

Diff check:

```text
git diff --check OK
```

## Problemi aperti

1. MCP reale Google non e' ancora configurato con credenziali/OAuth.
2. `arclio_mcp_gsuite`/`gworkspace-mcp` sono opzionali e non installati automaticamente.
3. Audit e confirmation store restano locali/in-process.
4. Sandbox e' `/tmp/ralf_sandbox`, non ancora persistente per sessione/progetto.
5. `ResultEnvelope` convive ancora con campi legacy.
6. `reasoning_cycle_node` storico resta in `ralfloop_agent/experimental`; il nuovo wrapper e' in `ralfloop_agent/nodes/reasoning.py`.

## Fase 4 proposta

Obiettivo:

```text
hardening e wiring end-to-end senza rompere baseline
```

Patch minima:

1. Configurare provider MCP reale solo dietro feature flag.
2. Aggiungere `RALF_MCP_GOOGLE_ENABLED=1`.
3. Persistenza confirmation/audit su file JSONL o sqlite locale.
4. Sandbox per task id:

```text
/tmp/ralf_sandbox/<task_id>/
```

5. Aggiungere ResultEnvelope come contratto primario mantenendo legacy fields.
6. Collegare il wrapper `ralfloop_agent/nodes/reasoning.py` al vero runtime solo per mode opt-in.

Test fase 4:

- no-send senza confirmation;
- approve con MCP fake configurato;
- audit persistente;
- sandbox task-isolated;
- route/evidence/envelope sempre presenti;
- rollback su MCP non configurato.

## Vincoli

- Non toccare `ralfloop_agent/core/*`.
- Non fare refactor totale.
- Non inviare email/Telegram/Drive senza conferma.
- Non rendere MCP reale default.
- Non installare pacchetti o credenziali automaticamente.
- Non usare `git add .`.
- Test obbligatori prima del commit.

## Sintesi

Ralf ora ha:

```text
real shell executor
confirmation-backed MCP action store
audit JSONL
capability reasoning wrapper
ResultEnvelope path
external action gate
24 test mirati verdi
```

La fase successiva deve trasformare questi pezzi in un flusso end-to-end persistente e configurabile, senza bypassare conferme umane.
