# ABC RELCALC OK - 2026-06-27

## Stato
Completata integrazione deterministica calcolatrice relazionale ABC.

## Commit / tag
- e5ecf11 / ralfloop-loop-no-progress-guard-ok-20260627
  - blocco no-progress su sandbox list_dir ripetuto
  - seed task.md nel sandbox
  - final status non finge completed se no_progress/max_iterations

- 2b8ac85 / ralfloop-loop-empty-exec-guard-ok-20260627
  - alias sandbox_exec code -> command heredoc python
  - blocco no-progress su exec vuoti ripetuti

- daf2d30 / ralfloop-abc-relcalc-deterministic-ok-20260627
  - nuovo modulo openshell_backend/skills/abc_relcalc.py
  - test tests/test_abc_relcalc.py
  - scoring prudenziale con bias guards

- 1527157 / ralfloop-abc-relcalc-router-ok-20260627
  - router separa abc_relcalc da abc_memory
  - "calcolatrice relazionale ABC" -> abc_relcalc
  - "rl:abc" -> abc_memory

## Verifiche
- PYTHONPATH=. .venv/bin/python -m pytest tests/test_capability_router.py tests/test_abc_relcalc.py tests/test_reasoning_cycle_node.py -q
- Risultato: 34 passed

## Smoke endpoint
/tasks/run route_only:
- "calcolatrice relazionale ABC" -> domain_skills ["abc_relcalc"]
- "rl:abc aggiorna memoria" -> domain_skills ["abc_memory"]

## Smoke calcolatrice
- osservabile forte: score 76, confidence 0.85, action light_non_pressing_presence
- tono/accesso: score 60, flags mind_reading_risk/surveillance_risk/too_many_inferences/weak_evidence_high_score, action collect_observable_evidence
- contraddizione: score 46, action do_nothing_active

## Nota dirty tree
Rimangono file modificati/untracked preesistenti non toccati da questa chiusura:
- .ralf_run/garden_go2rtc_frame_stability_20260620/*
- openshell_backend/app.py
- vari CODEX/HANDOFF/config/docs/tool/atm non tracciati
