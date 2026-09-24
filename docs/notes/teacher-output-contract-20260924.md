# Teacher deterministic output contract — 2026-09-24

## Scopo

Impedire che una bozza LLM venga mostrata allo studente prima dei controlli deterministici del Teacher.

## Implementazione

- hard enforcement di `max_response_chars` con taglio su confini di frase/parola;
- preservazione della micro-verifica finale quando il testo viene accorciato;
- aggiunta deterministica di una micro-verifica quando `micro_check=true` e il modello la omette;
- per `hint` con `allow_final_solution=false`, rimozione di frasi che dichiarano esplicitamente risposta/risultato/soluzione finale;
- rimozione anche di uguaglianze che risolvono direttamente l'espressione originale dell'esercizio (es. `7 x 8 = 56`), pur lasciando possibili passi intermedi;
- quando è disponibile `concept_evidence` curata, il corpo fattuale mostrato allo studente viene ricostruito dall'evidence e non dalla prosa del modello; l'LLM non può aggiungere nuovi fatti a quella risposta;
- nello streaming, i delta LLM vengono bufferizzati: nessun token grezzo viene inviato prima della validazione; dopo il guard viene emesso il solo testo validato;
- gli interventi vengono registrati in `output_guard` con le ragioni applicate.

## Interazione con gli altri guard

Il controllo anti-ripetizione confronta ora versioni passate attraverso lo stesso contratto d'uscita, per evitare che una micro-domanda aggiunta faccia sembrare diverse due spiegazioni sostanzialmente identiche.

Il grounding da materiale fornito resta gestito dalle policy deterministiche già presenti prima dell'inferenza. Il nuovo guard non sostituisce quel percorso.

## Grammar evidence

Il Grammar MCP interno dichiara il proprio output come `morphology_and_valency_evidence_not_contextual_truth`. Per questo il contratto non trasforma automaticamente analisi morfologiche/valenziali in una correzione contestuale unica. Il caso L2 fuori corpus (`a il` -> `al`) resta un lavoro distinto: serve una regola/correzione deterministica contestuale oppure evidence esplicitamente approvata come forma preferita, non una scelta arbitraria tra analisi del Grammar MCP.

## Test

- test mirati output contract: 5 passed;
- perimetro completo `tests/test_teacher*.py`: 236 passed, 3 skipped;
- `git diff --check`: da eseguire prima del commit.

## Deploy

Nessun deploy produzione in questo commit. La produzione va riconverta sulla release corrente prima di pubblicare il guard, senza retrocedere modifiche successive di altri verticali.
