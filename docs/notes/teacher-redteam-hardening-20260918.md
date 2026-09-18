# Teacher universale — hardening pedagogico e red-team 2026-09-18

## Stato finale del ciclo

- Branch: `teacher/production-integration-20260915`.
- Nessun deploy produzione eseguito.
- DS4 `127.0.0.1:19194` e `127.0.0.1:19196` lasciati attivi e non sospesi.
- WIP Gemma (`deploy/k8s/teacher-gemma-pool.yaml`, `docs/TEACHER_GEMMA_POOL.md`, `tools/prepare_teacher_gemma_worker.py`, `tools/teacher_model_pool_mcp/`) lasciati fuori da questo ciclo.
- JEV-Qwen 3B resta escluso dal path live: nessuna integrazione come giudice.

## Modifiche principali

1. Supervisor deterministico esteso: claim verificabili, domande supportate da evidence, confusion e richieste di esempio vengono servite dal Core/evidence quando la copertura è sufficiente.
2. Evidence curata ampliata/rifinita per scienze, matematica, fisica, statistica, storia, italiano L2, informatica e linguistica.
3. Ranking dell'evidence reso sensibile al turno corrente, ai termini specifici e alla novità rispetto alla risposta precedente.
4. Anti-ripetizione deterministico: i follow-up preferiscono una frase nuova e pertinente; hint percentuali/frazioni, grounded abstention e frazioni equivalenti cambiano strategia senza cambiare verità.
5. `core.fraction_relation` aggiunto per confrontare esattamente due frazioni con prodotti incrociati interi.
6. `core.math_check`/`core.math_hint` estesi agli sconti percentuali; il check delle equazioni lineari e il divieto di solution leakage restano hard.
7. Ambiguità referenziale: richieste come “questo/quello” senza espressione vengono fermate deterministicamente invece di inventare il riferimento.
8. Grounding persistente: attribuzioni ad autore/filosofo/teoria non presenti nel materiale vengono rifiutate senza completamento del modello.
9. Classifier C: `non capisco ...` ha precedenza sulle euristiche di controesempio/rationale; mantenuti i casi contrastivi reali.
10. Adattamento pedagogico preservato: L2 resta `oral_rehearsal`, livello scholar resta `argument_critique`; il path deterministico non cancella la modalità di accesso.

## Validazione

- Unit/regression mirati finali: **38 passed**.
- Perimetro `tests/test_teacher*.py`: **220 passed, 3 skipped**.
- Core C ricompilato con `-Wall -Wextra -Werror -pedantic`.
- Corpus red-team: **25 casi / 77 turni**.
- Failure classificate nel run hardened: **0**.
- `conceptual_correctness`: **77/77**.
- `multi_turn_continuity`: **77/77**.
- `no_unnecessary_repetition`: **77/77**.
- `no_early_solution`: **77/77**.
- `misconception_recognition`: **77/77**.
- `pedagogical_policy`: **77/77**.
- Turni gestiti deterministicamente: **77/77**.
- Chiamate CPU al modello nel corpus coperto: **0**.
- Source mode: 72 `deterministic_core`, 3 `provided_material`, 2 `deterministic_policy`.

Artefatti: `tests/data/teacher_redteam_hardened_20260918.jsonl` e `docs/notes/teacher-redteam-hardened-20260918.md`.

## Decisioni

Il risultato non significa che il Teacher universale possa fare a meno del modello: il determinismo copre il corpus curato e deve restare un guardrail/verifier per i concetti noti. Per domande aperte non coperte dall'evidence il modello rimane necessario. Non ampliare il Core tramite hardcode per inseguire qualunque domanda: aggiungere evidence o tool deterministici solo quando esiste una regola verificabile e generalizzabile.

Il prossimo ciclo, prima di un deploy, deve testare materiale e topic fuori dal corpus attuale e la UI reale. Il run hardened è una regression suite, non una stima della qualità media su tutto lo spazio delle domande.
