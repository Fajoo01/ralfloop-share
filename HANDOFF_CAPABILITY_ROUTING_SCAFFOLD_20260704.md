# Capability Routing Scaffold - 2026-07-04

## Problema
Aggiungere scaffold evidence-first capability routing senza toccare il core Ralfloop protetto.

## Modifica
Creati:
- main.py
- src/models.py
- src/executor.py
- src/router.py
- src/skills.py
- src/mcp_client.py
- src/confirmation.py
- src/api.py
- tests/test_routing.py

Endpoint:
- POST /tasks/run
- POST /confirmations/{id}/approve
- POST /confirmations/{id}/reject

## Verifica
- python3 -m py_compile OK
- .venv/bin/python -m pytest -q tests/test_routing.py -> 5 passed
- TestClient /tasks/run OK:
  - route_only
  - check_only
  - patch_allowed
  - external_action con pending_confirmation_id

## Nota
Scaffold funzionante ma ancora parallelo.
Prossimo passo: integrare nel result_envelope unico Ralfloop/OpenShell.
