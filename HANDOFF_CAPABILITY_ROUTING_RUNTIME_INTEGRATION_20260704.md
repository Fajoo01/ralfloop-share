# Capability Routing Runtime Integration - 2026-07-04

## Stato attuale

Lo scaffold evidence-first capability routing e' stato collegato al runtime OpenShell con una patch minima.

Creati:

- `ralfloop_agent/integration/__init__.py`
- `ralfloop_agent/integration/capability_adapter.py`
- `ralfloop_agent/integration/confirmation_store.py`
- `ralfloop_agent/models/__init__.py`
- `ralfloop_agent/models/result_envelope.py`
- `tests/test_capability_runtime_integration.py`

Modificato:

- `openshell_backend/app.py`

Integrazione attuale:

- `/tasks/run` usa `ralfloop_agent.integration.capability_adapter.route_task()`.
- `capability_adapter` usa lo scaffold `src.router.CapabilityRouter`.
- `capability_adapter` arricchisce il reasoning con `reasoning_cycle_node` se disponibile.
- `ResultEnvelope` espone route, evidence, confirmation, answer, meta.
- `external_action` senza conferma viene bloccata prima del runtime agente.
- OpenShell espone endpoint conferme:
  - `POST /confirmations/{id}/approve`
  - `POST /confirmations/{id}/reject`

Compatibilita' preservata:

- `capability_route.task_mode`
- `capability_route.write_policy`
- `capability_route.domain_skills`
- `capability_route.mcp_connectors`
- `capability_route.needs_human_confirmation`
- `capability_route.required_output_fields`
- `capability_route.workflow`
- `capability_route.blocked_actions`

## Verifiche

Comandi eseguiti:

```bash
PYTHONPYCACHEPREFIX="$CACHE" .venv/bin/python -m py_compile \
  ralfloop_agent/integration/__init__.py \
  ralfloop_agent/integration/capability_adapter.py \
  ralfloop_agent/integration/confirmation_store.py \
  ralfloop_agent/models/__init__.py \
  ralfloop_agent/models/result_envelope.py \
  openshell_backend/app.py \
  tests/test_capability_runtime_integration.py \
  tests/test_routing.py
```

Risultato:

```text
py_compile OK
```

Test scaffold + integrazione:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q \
  tests/test_routing.py \
  tests/test_capability_runtime_integration.py
```

Risultato:

```text
10 passed
```

Test storico capability router via utente `sibilla-cumana`:

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m pytest -q tests/test_capability_router.py
```

Risultato:

```text
7 passed
```

Smoke TestClient:

```text
/tasks/run mode=route_only -> stop_reason route_only, route check_only
/tasks/run external_action -> stop_reason human_confirmation_required, pending_confirmation_id presente
```

Diff check:

```text
git diff --check OK
```

Nota warning:

```text
openshell_backend/app.py:552 SyntaxWarning: invalid escape sequence '\/'
```

Warning preesistente/non collegato alla patch.

## Problemi aperti

1. `reasoning_cycle_node` e' usato solo per arricchire il reasoning della route.
   Non guida ancora l'esecuzione del runtime.

2. `ResultEnvelope` e' aggiunto alla risposta, ma non e' ancora l'unico schema del runtime.
   `final_answer`, `capability_route`, `artifacts`, `audit_summary` restano in parallelo per compatibilita'.

3. Executor reale non ancora unificato.
   Lo scaffold ha `src.executor.ShellExecutor`, mentre OpenShell usa ancora i suoi adapter/runtime.

4. MCP reali non collegati.
   `MCPClient` resta mock: email, Telegram, Drive sono bloccati correttamente ma non eseguono connettori reali.

5. Audit log conferme non ancora persistito.
   `confirmation_store` e' in memoria.

6. Persistenza confirmation pending assente.
   Dopo restart del processo, le conferme in memoria si perdono.

7. `ralfloop_agent/core/*` resta protetto da ACL e non e' stato modificato.
   Questo e' voluto per non rompere baseline.

8. La vecchia route `ralfloop_agent.core.capability_router` non e' stata riscritta.
   OpenShell ora usa adapter nuovo, ma i test storici restano compatibili.

## Prossima patch

Obiettivo fase successiva:

```text
Collegare reasoning_cycle_node e result_envelope in modo operativo:
route -> reasoning_cycle_node -> executor/MCP gate -> ResultEnvelope finale unico
```

Patch minima proposta:

1. Creare un piccolo orchestratore separato:

```text
ralfloop_agent/integration/capability_runtime.py
```

Responsabilita':

- ricevere `TaskRequest`;
- calcolare `CapabilityRoute`;
- chiamare `reasoning_cycle_node`;
- bloccare `external_action` senza conferma;
- produrre sempre `ResultEnvelope`;
- non sostituire ancora `RalfloopAgent`.

2. Lasciare `/tasks/run` compatibile:

- campi legacy invariati;
- `result_envelope` sempre presente;
- agent baseline chiamato solo se route non blocca.

3. Aggiungere audit minimo:

```text
route_decision
confirmation_requested
confirmation_approved
confirmation_rejected
runtime_blocked
```

4. Aggiungere test:

- `test_result_envelope_present_for_route_only`
- `test_external_action_never_reaches_agent_without_confirmation`
- `test_human_confirmed_allows_agent_path_but_keeps_policy_metadata`
- `test_reasoning_cycle_receives_capability_route`
- `test_skills_priority_over_mcp_still_preserved`

## Vincoli

- Non toccare `ralfloop_agent/core/*`.
- Non fare refactor totale di `openshell_backend/app.py`.
- Non cambiare comportamento baseline se non necessario.
- Non eseguire azioni esterne senza conferma.
- Non collegare MCP reali senza test e senza confirmation gate.
- Non usare `git add .`.
- Non committare file runtime/cache.
- Ogni patch deve avere:
  - `py_compile`;
  - pytest mirato;
  - `git diff --check`;
  - smoke TestClient su `/tasks/run`.

## Stato finale

La fase attuale e' chiusa come integrazione minima:

```text
evidence-first route presente
result_envelope presente
confirmation gate presente
endpoint conferme presenti
compat legacy preservata
core protetto non toccato
```

La fase successiva deve rendere `ResultEnvelope` il contratto operativo centrale senza eliminare subito i campi legacy.
