# Ralfloop + OpenShell Agent

## Cosè

Snapshot condivisibile del lavoro su Ralfloop + OpenShell Agent
e dellintegrazione con Cheshire Cat tramite il plugin ralfloop_bridge.

## Plugin

Il plugin si trova in:
`integrations/cheshire_cat/ralfloop_bridge/`

File principali:
- `main_plugin.py`
- `plugin.json`
- `settings.py`
- `settings.example.json`
- `tools.py`
- `requirements.txt`
- `rag/`
- `rag_addons/`
- `specs/`

## Handoff

- `handoff/NEXT_SESSION_START_HERE.md`
- `handoff/HANDOFF_BASELINE_OK_20260407.json`

## Stato attuale

- baseline verificata
- shell ok
- calculation ok
- host-visible non attivo nel runtime corrente

## Cosa non contiene

- runtime completo gia installato
- settings.json reale
- credenziali o token
- log, backup sporchi, snapshot intermedi

## Cosa non toccare

- non reinserire host-visible nel runtime attivo
- non rimettere stable_snapshots nel package del plugin
- non mischiare baseline stabile ed esperimenti

## Installazione minima

1. Copiare il plugin nella cartella plugin di Cheshire Cat.
2. Creare settings.json locale partendo da settings.example.json.
3. Verificare dipendenze e percorsi locali.
4. Riavviare Cheshire Cat.
5. Testare in ambiente controllato.


## Codice core incluso

Oltre al plugin Cheshire Cat, questa repo include anche il codice principale dello scaffold locale:

- `openshell_backend/app.py`
- `ralfloop_agent/`
- `config/project.json`
- `pyproject.toml`
- `tests_scaffold/`

Quindi la repo non contiene solo il bridge/plugin, ma anche il core agente e il backend emersi nello scaffold locale.
