# Reasoning cycle failure priority - 2026-06-27

Branch:
ralfloop-reasoning-cycle-node-20260627-092916

Commit:
75649b0bde65967bb3c29161c05db45cdbf7233a

Tag:
ralfloop-reasoning-cycle-failure-priority-ok-20260627

Problema:
last_result fallito veniva superato dal template write_experimental_module.

Causa:
Priorità decisionale: patch_candidate + no_runtime arrivava prima del failure handling.

Modifica:
Failure con traceback/runtimeerror/syntaxerror/timeout/failed/exit_code != 0 ora produce:
- decision.status=continue
- selected_next_action.action_type=inspect_failure
- writes_allowed=false
- commands read-only rg

Verifica manuale:
- goal: Fix the failing parser with the smallest safe change.
- last_result: ok=false, exit_code=1, stderr Traceback RuntimeError
- risultato: inspect_failure, writes_allowed=false

File modificati:
- ralfloop_agent/experimental/reasoning_cycle_node.py
- tests/test_reasoning_cycle_node.py

Verifica:
- py_compile OK
- pytest -q tests/test_reasoning_cycle_node.py: 18 passed
- git diff --check OK

Non toccati:
- ralfloop_agent/core/*
- runtime esterno legacy
- openshell_backend/app.py
- host-visible
- stable_snapshots

Note:
- openshell_backend/app.py resta M preesistente.
- warning permessi core/* preesistente.
