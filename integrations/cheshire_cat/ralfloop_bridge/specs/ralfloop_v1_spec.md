# Ralfloop v1 Spec

## 1. Obiettivo
Ralfloop è un orchestratore locale che trasforma richieste tecniche verificabili in output validati, con artifact persistiti, retry controllato e routing per tipo task.

## 2. Principi
- Il modello propone.
- L'esecutore valida.
- Il judge decide il risultato finale.
- I task verificabili usano il loop.
- I task non verificabili non devono forzare il loop.
- La baseline stabile non va sporcata con esperimenti non isolati.

## 3. Classi di task
- shell_commands
- python_code
- calculation
- structured_text
- generic_text

## 4. Routing
### 4.1 shell_commands
Usare per:
- richieste di comandi bash/shell
- task CLI
- file temporanei shell
Vincoli:
- preferire mktemp o /tmp
- non usare /ralfloop_tmp per file temporanei shell generici

### 4.2 python_code
Usare per:
- script Python piccoli o medi
- parsing
- trasformazioni file
- task tecnici verificabili via esecuzione Python

### 4.3 calculation
Usare per:
- percentuali
- equazioni
- derivate
- integrali
- sistemi
- media
- deviazione standard
- calcoli verificabili via Python
Vincoli:
- output finale deve preferire stdout validato
- float interi tipo 7.0 devono diventare 7

### 4.4 structured_text
Usare per:
- JSON
- markdown strutturato
- testo con formato rigido
Solo se non serve esecuzione reale.

### 4.5 generic_text
Usare per:
- chat generica
- spiegazioni
- scrittura libera non verificabile
Non forzare il loop.

## 5. Trigger
Trigger minimi baseline:
- calcola
- risolvi
- equazione
- derivata
- integrale
- sistema
- percentuale
- media
- deviazione standard
- scrivi uno script
- scrivi un programma
- scrivi un endpoint
- scrivi un plugin
- genera codice
- fammi uno script
- crea uno script
- dammi i comandi
- dammi i comandi da shell
- dammi comandi shell
- comandi shell
- comandi bash
- dammi i comandi bash

## 6. Contratti di output per classe

### 6.1 shell_commands
- output del coder: solo comandi shell
- niente prose
- niente markdown fences nel candidate interno
- result.txt può essere fence shell user-facing

### 6.2 python_code
- output del coder: solo Python eseguibile
- niente prose
- niente markdown fences nel candidate interno

### 6.3 calculation
- output del coder: Python breve eseguibile
- output finale user-facing: solo risultato finale validato
- usare stdout come fonte di verità quando disponibile

### 6.4 structured_text
- output aderente al formato richiesto
- no esecuzione salvo esplicita necessità

### 6.5 generic_text
- nessuna validazione esecutiva obbligatoria

## 7. Artifact obbligatori
Per ogni task entrato nel loop:
- planner_rag.txt
- coder_rag.txt
- judge_rag.txt
- planner.txt
- coder.txt
- result.txt
- meta.txt

Se c'è esecuzione:
- exec_attempt_1.txt
- exec_attempt_2.txt se esiste
- candidate_attempt_1.py o .sh se esiste
- candidate_attempt_2.py o .sh se esiste

## 8. Planner
Responsabilità:
- classificare il task
- scegliere task_type
- scegliere output_format
- non scrivere codice
- produrre JSON valido
- preferire calculation per task matematici verificabili

## 9. Coder
Responsabilità:
- produrre l'output grezzo richiesto dal planner
- non aggiungere prose
- rispettare i contratti di output
- nei retry correggere il fallimento concreto prima di ridisegnare tutto

## 10. Judge
Responsabilità:
- usare execution summary quando presente
- scegliere il risultato migliore
- per calculation preferire stdout validato
- non degradare il risultato finale con prose inutile

## 11. Retry policy
- retry su errore di esecuzione
- retry su successo mediocre solo se il gate lo richiede
- max baseline: 2 tentativi
- retry hint strutturato:
  - missing_dependency
  - missing_path
  - syntax_error
  - permission_error
  - timeout
  - name_error
  - type_error
  - value_error
  - runtime_error
  - ok_empty_stdout
  - no_execution

## 12. RAG per ruolo
### planner
- routing rules
- regressioni note
- policy task_type/output_format

### coder
- playbook task riusciti
- anti-pattern
- regole path
- regole output

### judge
- checklist di validazione
- regressioni note
- regole per scegliere stdout vs candidate

## 13. Policy path
- shell temporanei generici: /tmp o mktemp
- path host-visible plugin: /ralfloop_tmp
- non inventare subfolder non richiesti
- non usare path host-only arbitrari

## 14. Ramo host-visible
### Stato attuale
- NON baseline
- ramo noto come instabile

### Regola v1
- non patchare nella baseline
- trattare come area sperimentale separata

### Obiettivo futuro
- il coder deve generare il contenuto finale del file
- il plugin deve scrivere il file host-visible
- evitare writer-script autoreferenziali

## 15. Smoke test baseline
### shell
Prompt:
- dammi i comandi bash e validali davvero: usa mktemp per creare un file temporaneo, scrivici CIAO e leggilo
Atteso:
- exec_attempt_1 ok
- stdout CIAO
- result.txt shell coerente

### equazione
Prompt:
- risolvi l'equazione 2*x + 5 = 19
Atteso:
- exec_attempt_1 ok
- stdout 7.0
- result.txt 7

### derivata
Prompt:
- calcola la derivata di x^3 + 2*x
Atteso:
- result.txt 3*x**2 + 2

### integrale
Prompt:
- calcola l'integrale di x^2
Atteso:
- result.txt x**3/3

### sistema
Prompt:
- risolvi il sistema: x + y = 5, x - y = 1
Atteso:
- result.txt con x = 3, y = 2 oppure equivalente

## 16. Regressioni note
- trigger matematici persi dopo rollback
- result.txt che mostra candidate invece del valore finale
- host-visible che degenera in writer-script
- patch host-visible che peggiorano la baseline

## 17. Regole di change management
- ogni patch non banale richiede snapshot
- ogni patch richiede smoke test minimi
- host-visible va sviluppato su file separato o branch logico separato
- se shell o calculation si rompono: rollback immediato all'ultima baseline sana

## 18. Baseline attuale
- shell ok
- calculation ok
- symbolic math base ok
- retry strutturato presente
- host-visible fuori scope operativo della baseline
