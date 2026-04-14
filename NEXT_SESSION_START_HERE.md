# Next session start here

Branch attivo:
- feat/sandbox-inspect-api

Working tree:
- pulito tranne eventuali aggiornamenti volontari di questo handoff

Checkpoint chiusi:
- v0.5.2-grammar-autofix-state-aligned
- v0.5.3-loop-http-completion-consistent
- v0.5.4-file-inspect-grounded
- v0.5.5-decision-model-diagnoser
- v0.5.6-loop-policy-renderer-extracted
- v0.5.7-tool-dispatch-extracted
- v0.5.8-read-file-postprocess-extracted
- v0.5.9-role-helpers-extracted
- v0.5.10-state-transitions-extracted
- v0.5.11-seeded-context-bootstrap-extracted
- v0.5.12-seed-writer-extracted

Stato verificato:
- loop alleggerito in moduli contracts/policy dedicati
- grammar apostrophe autofix introdotto
- self-heal suspicious apostrophe tokens introdotto
- ricorsione self-heal corretta
- grammar diagnostics API esposta
- raw/final grammar diagnostics esposti
- fallback grammaticale locale disattivabile via flag
- test mirati verdi:
  - grammar/diagnostics verdi
  - con DISABLE_LOCAL_GRAMMAR_FALLBACK=1 il flusso continua a funzionare

Commit chiave recenti:
- 6880a4a fix: autofix apostrophe tokens in grammar fallback
- c6a0f29 feat: self-heal suspicious apostrophe grammar tokens
- 061d5fc fix: remove recursive self-heal loop in grammar fallback
- 980242c feat: expose grammar diagnostics for validator issues
- 9d0242c feat: expose raw and final grammar diagnostics

Stato reale del fallback:
- non è più critico per il flusso provato
- conviene tenerlo dietro flag per ora
- non conviene rimuoverlo di colpo finché non gira un po'

Prossimo target minimo:
- portare i diagnostics grammar nel layer a monte
- trasformare raw_issues in diagnostic strutturato per diagnoser/autofix_candidate
- non patchare più a mano skill_grammar_rag.py per ogni caso
