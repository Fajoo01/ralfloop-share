role: planner
role: coder
role: judge

# Regressioni note

## Regressione 1: trigger matematici persi dopo rollback
Sintomo:
- richieste tipo "risolvi l'equazione..." non entrano nel loop Ralfloop

Correzione:
- mantenere trigger: calcola, risolvi, equazione, derivata, integrale, sistema, percentuale, media, deviazione standard

## Regressione 2: result.txt mostra codice invece del risultato finale
Sintomo:
- exec_attempt_1.txt ha stdout corretto
- result.txt mostra il candidate invece del valore finale

Correzione:
- per task_type=calculation usare stdout validato come final
- normalizzare float interi tipo 7.0 -> 7

## Regressione 3: ramo host-visible degenera in writer-script
Sintomo:
- il modello genera script che scrivono result.py invece del contenuto finale del file
- patch ripetute possono peggiorare la baseline

Correzione:
- non toccare il ramo host-visible nella baseline corrente
- trattarlo come area sperimentale separata
