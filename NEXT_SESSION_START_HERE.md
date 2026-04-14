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
- v0.5.13-grammar-diagnostics-upstream-ready
- v0.5.14-grammar-upstream-diagnoser

Stato verificato:
- grammar diagnostics espongono raw_items/raw_issues e final_items/final_issues
- raw_issues grammar vengono instradati in autofix_candidate
- il diagnoser riconosce grammar upstream issues
- per suspicious_apostrophe_token non produce più solo contract_mismatch generico
- tag locale creato:
  - v0.5.14-grammar-upstream-diagnoser

Commit chiave recenti:
- 980242c feat: expose grammar diagnostics for validator issues
- dad0ba6 feat: expose raw and final grammar diagnostics
- f49d68a feat: route grammar raw issues into autofix candidate
- db6f173 feat: diagnose grammar upstream issues for autofix

Stato reale:
- il sistema ora vede l'errore a monte e lo traduce in diagnosi strutturata
- non genera ancora una patch concreta in automatico
- prossimo step non è più capire il problema, ma insegnare all'autofix a proporre la patch

Prossimo target minimo:
- agganciare diagnosis.kind=grammar_upstream_issue alla generazione di una patch candidate
- caso iniziale da coprire: suspicious_apostrophe_token
- target file atteso:
  - openshell_backend/skill_grammar_rag.py
- target symbol atteso:
  - _simple_local_grammar_fallback
- evitare refactor largo fuori dal flusso autofix
