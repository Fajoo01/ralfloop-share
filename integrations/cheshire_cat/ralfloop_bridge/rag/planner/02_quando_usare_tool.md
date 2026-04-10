# Quando usare tool

## Usa LLM puro quando
- la richiesta è solo scrittura o riscrittura
- basta generare un file testuale
- non serve verificare sul filesystem
- non serve esecuzione reale

## Usa backend OpenShell quando
- bisogna eseguire comandi
- bisogna leggere file sandbox
- bisogna scrivere file in sandbox
- bisogna fare probe stream
- bisogna verificare output reale

## Usa cartella temp host-visible quando
- l'output finale deve essere recuperabile dall'utente
- il risultato è un file da salvare
- il task produce artefatti che devono vivere oltre la sandbox

## Non usare tool quando
- il task è banale e basta produrre testo
- non c'è nessun valore aggiunto nella sandbox
- il risultato può essere scritto direttamente dal plugin

## Regola pratica
Preferire il percorso più corto che mantiene verificabilità.
