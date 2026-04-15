# Next session start here

Branch attivo:
- feat/sandbox-inspect-api

Working tree:
- pulito tranne eventuali aggiornamenti volontari di questo handoff

Checkpoint chiusi:
- v0.5.13-grammar-diagnostics-upstream-ready
- v0.5.14-grammar-upstream-diagnoser
- v0.5.15-coder-handoff-runtime

Stato verificato:
- grammar raw_issues/final_issues esposti
- grammar raw_issues instradati in autofix_candidate
- diagnoser riconosce grammar_upstream_issue
- coder_patch_candidate costruito
- coder_handoff_prompt costruito
- fastpath skill insufficient invoca davvero il coder model
- coder output viene allegato in autofix_candidate.coder_text
- test mirati verdi

Commit chiave recenti:
- f49d68a feat: route grammar raw issues into autofix candidate
- db6f173 feat: diagnose grammar upstream issues for autofix
- 19d2b31 feat: provide coder patch candidate for grammar upstream issues
- a29849b feat: add coder handoff prompt for grammar upstream issues
- c5eaf59 feat: run coder handoff prompt for skill autofix proposals

Stato reale:
- il coder è ora nel loop runtime per gli skill insufficient
- non c'è ancora apply automatico della patch proposta
- manca ancora parsing/apply/validate del coder_text

Prossimo target minimo:
- estrarre dal coder_text una patch candidate applicabile
- applicarla solo al target_file previsto
- eseguire i test rilevanti
- accettare o rollback
