# Handoff skill self-learning OK

## Stato
Dimostrato funzionamento end-to-end del self-learning di skill non hardcoded.

## Casi verificati

### 1. Somma numeri da file
Richiesta:
- somma i numeri nel file /tmp/numeri_test.txt

Esito:
- llm_judge_reusability: OK
- propose_skill_candidate: OK
- validate_skill_candidate: TRUE
- store_candidate_skill: OK
- cache hit: OK
- promotion to stable: OK

Candidate:
- openshell_backend/cache_skills_candidates/python/auto_llm_somma_i_numeri_nel_file_tmp_numeri_test_txt.json

Stable:
- openshell_backend/cache_skills/python/auto_llm_somma_i_numeri_nel_file_tmp_numeri_test_txt.json

### 2. Chiavi top-level JSON
Richiesta:
- stampa le chiavi top-level del file /tmp/test.json

Esito:
- llm_judge_reusability: OK
- propose_skill_candidate: OK
- validate_skill_candidate: TRUE
- store_candidate_skill: OK
- cache hit: OK
- promotion to stable: OK

Candidate:
- openshell_backend/cache_skills_candidates/python/auto_llm_stampa_le_chiavi_top_level_del_file_tmp_test_json.json

Stable:
- openshell_backend/cache_skills/python/auto_llm_stampa_le_chiavi_top_level_del_file_tmp_test_json.json

## Fix fatti
- parser JSON LLM reso tollerante agli escape regex non validi in JSON
- restore di _dedupe_skill_path()
- run_python_inline() corretto per output multilinea
- promotion candidate -> stable verificata
- proposer hardcoded rimosso: discovery skill ora passa da giudizio/proposta LLM

## Significato
Il sistema non è più limitato a skill matematiche hardcoded.
Ora sa apprendere skill file-based deterministiche e promuoverle automaticamente dopo riuso.

## Prossimi passi
1. rimuovere i debug temporanei [LLM_JUDGE]
2. migliorare categorizzazione stable path per skill non-math
3. aggiungere altri casi:
   - csv
   - yaml/json transform
   - grep/estrazioni testo
   - piccoli shell task deterministici
4. valutare soglia promotion > 2 per evitare spam di skill mediocri
