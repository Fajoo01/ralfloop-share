# Ralfloop + OpenShell Agent

## Cos'è
Progetto per un agente con orchestrazione esterna in Ralfloop ed esecuzione confinata in OpenShell.

## Contenuto
- `integrations/cheshire_cat/ralfloop_bridge/`: plugin Cheshire Cat
- `handoff/`: note di stato e continuità
- `docs/`, `scripts/`, `tests/`: spazio per sviluppo ordinato

## Stato attuale
- baseline verificata
- shell ok
- calculation ok
- host-visible non attivo
- non toccare il runtime baseline senza ramo separato

## Note importanti
- non reinserire host-visible nel runtime attivo
- non rimettere stable_snapshots dentro il package del plugin
