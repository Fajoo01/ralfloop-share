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

Stato verificato:
- grammar fastpath: PASS
- file_inspect seeded context: PASS
- diagnoser integrato in autofix_candidate
- skill insufficiency response deduplicata
- completion policy estratta da loop.py
- final answer renderer estratto da loop.py
- tool dispatch estratto da loop.py
- read-file postprocess estratto dal blocco speciale del run()
- role helpers estratti da loop.py
- test mirati verdi:
  - 17 passed, 8 deselected

Commit chiave recenti:
- dd01d8e refactor: extract loop completion and final answer policies
- bb6b769 refactor: extract tool dispatch from loop
- 993da12 refactor: wire read-file postprocess into loop
- f336234 refactor: extract role helpers from loop

Loop.py ora contiene ancora soprattutto:
- orchestrazione run()
- seeding iniziale user_goal.txt / skill_context.txt / extra_context.json
- finalizzazione stato completed/failed

Prossimo target minimo:
- lasciare stare per ora il seeding iniziale se non blocca altro
- valutare una piccola state-transition helper per il ciclo run()
- evitare refactor largo
