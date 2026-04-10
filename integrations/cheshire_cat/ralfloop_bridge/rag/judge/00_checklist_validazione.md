# Checklist validazione

## Paths
- il codice usa il path fornito dal runtime context o dalla configurazione?
- evita path inventati?
- evita sottocartelle non richieste?
- evita tempfile.gettempdir() e TEMP?

## Output
- se l'utente chiede codice, l'output finale è solo codice?
- se si salva result.py, il codice scrive davvero il file?
- il contenuto non è vuoto o inutile senza motivo?
