# Errori ricorrenti

## Shell e quoting
- heredoc chiusi male
- apici sbagliati
- Python incollato direttamente in bash
- escape inutili che rompono la patch

## Modelli
- model_name passato nei settings ma non usato davvero
- un solo planner usato per tutti i ruoli
- used_models giusto ma audit_summary sbagliato

## File
- file scritti in path non montati
- file dentro il container ma non visibili dall'host
- sandbox distrutta prima di esportare i file

## Plugin Cheshire
- uso di message.content invece di message.text
- hook che parte ma non sostituisce davvero la risposta
- settings aggiornati ma non riletti correttamente

## RAG
- collections separate solo di nome ma non interrogate davvero
- stesso contenuto duplicato in tutte le collection

## Errore ricorrente: path temporaneo generico
Il coder tende a usare:
- tempfile.gettempdir()
- os.environ["TEMP"]

Nel setup Ralfloop/Cheshire questo è sbagliato per i file host-visible del plugin.

Path corretto:
- /ralfloop_tmp
