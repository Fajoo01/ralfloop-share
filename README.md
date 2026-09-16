# Ralfloop + OpenShell Agent

## Cos'è

Repository del core Ralfloop/Bot-tazzi: backend OpenShell, routing per domini,
provider locali, MCP, workflow e test di integrazione.

## Runtime AI locale

Il percorso locale standard usa `llama.cpp` / `llama-server` con API
OpenAI-compatible. I servizi specialistici devono dipendere dal provider
canonico, non da shell agent esterne.

## Componenti principali

- `openshell_backend/`
- `ralfloop_agent/`
- `config/`
- `integrations/`
- `deploy/`
- `tests/`

## Principi

- routing e policy restano in Ralfloop;
- le azioni protette richiedono i gate previsti dal dominio;
- i modelli specialistici sono advisory o estrattori quando dichiarato;
- nessun runtime esterno deve diventare implicitamente source of truth;
- credenziali, token, embedding biometrici e dati locali non vanno nel repository.
