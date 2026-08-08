# Ralfloop / DeepSeek Update - 2026-07-04

## Stato nuovo

Abbiamo chiuso il primo scaffold evidence-first capability routing.

Commit:

```text
0819e2f Add handoff for capability routing scaffold
e7ca31b Add evidence-first capability routing scaffold
```

## Cosa e' stato implementato

Nuovo scaffold parallelo, non integrato nel runtime principale:

```text
main.py
src/__init__.py
src/api.py
src/confirmation.py
src/executor.py
src/mcp_client.py
src/models.py
src/router.py
src/skills.py
tests/test_routing.py
HANDOFF_CAPABILITY_ROUTING_SCAFFOLD_20260704.md
```

Il core protetto non e' stato toccato:

```text
ralfloop_agent/core/*
openshell_backend/*
.ralf_run/*
abc_memory/*
```

## Funzionalita'

### Endpoint

```text
POST /tasks/run
POST /confirmations/{id}/approve
POST /confirmations/{id}/reject
```

### Modes

```text
check_only
patch_allowed
external_action
route_only
```

### Evidence contract

Ogni esecuzione operativa restituisce:

```text
command
path
exit_code
stdout
stderr
```

Per patch:

```text
diff
tests
```

### Skills locali mockate

```text
bandi
abc_memory
jellyfin
garden_detector
trade_republic
```

Regola architetturale confermata:

```text
Skills prima di MCP.
MCP e' connettore, non sostituisce skill domain-specific.
```

### MCP mock

```text
send_email
send_telegram
drive_upload
browser_inspect
```

Azioni esterne scriventi:

```text
send_email
send_telegram
drive_upload
```

richiedono conferma umana e producono `pending_confirmation_id`.

## Verifiche gia' passate

```text
python3 -m py_compile OK
.venv/bin/python -m pytest -q tests/test_routing.py -> 5 passed
TestClient /tasks/run OK
```

Casi coperti:

```text
route_only
check_only
patch_allowed
external_action con pending_confirmation_id
approval confirmation endpoint
```

## Stato architetturale

Lo scaffold e' funzionante ma parallelo.

Non e' ancora collegato a:

```text
openshell_backend/app.py
ralfloop_agent/core/capability_router.py
result_envelope unico
Cheshire Cat bridge runtime
```

Motivo: i file `ralfloop_agent/core/*` sono protetti da permessi/ACL e non sono stati modificati.

## Problema successivo da far valutare a DeepSeek

Serve integrare lo scaffold nel runtime Ralfloop senza rompere baseline.

Obiettivo prossimo:

```text
route_task / CapabilityRoute
-> result_envelope unico
-> /tasks/run OpenShell
-> evidence-first output
-> human confirmation middleware
```

## Domande per DeepSeek

1. Come fondere lo scaffold `src/*` con `ralfloop_agent/core/capability_router.py` minimizzando il rischio?
2. Quale schema `result_envelope` usare per unificare:
   - route;
   - evidence;
   - patch diff/tests;
   - pending confirmation;
   - final answer?
3. Dove deve vivere il middleware conferme:
   - in OpenShell backend;
   - nel core Ralfloop;
   - nel bridge Cheshire;
   - o in uno strato condiviso?
4. Come mantenere priorita' Skills > MCP in modo testabile?
5. Come garantire che `external_action` non esegua mai send/upload senza conferma?
6. Come collegare questo routing al reasoning_cycle_node senza creare doppia policy?

## Vincoli da rispettare nella prossima patch

```text
no refactor totale
no cambio comportamento baseline
no bypass policy
no external send senza conferma
no score/manual decision dal LLM
no git add .
patch minima e testata
```

## Sintesi operativa

Ralf ora ha un primo scaffold evidence-first isolato e testato.

La patch utile successiva non e' aggiungere nuove euristiche, ma collegare questo scaffold al result envelope e al runtime `/tasks/run` esistente, conservando:

```text
evidence-first
human-confirmation-first
skills-before-MCP
deterministic fallback
auditability
```
