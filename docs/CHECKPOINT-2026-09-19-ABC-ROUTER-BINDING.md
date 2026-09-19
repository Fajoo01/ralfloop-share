# CHECKPOINT — ABC Relation MCP router binding — 2026-09-19

## Branch

`feat/abc-relation-mcp-v1-20260919`

Repository privato: `Fajoo01/ralfloop-bottazzi`.

## Obiettivo di questo step

Collegare il nuovo ABC Relation MCP al runtime Bot-tazzi senza trattare l'analisi relazionale come un'azione esterna e senza introdurre side effect.

## Modifiche

- `config/capability_routing.json`: aggiunta sezione `read_mcp_keywords` con connector `abc_relation`.
- `src/routing_config.py`: parser separato per MCP read-only.
- `src/router.py`: gli MCP read-only sono risolti per richieste non `external_action`; la confirmation policy degli MCP esterni resta invariata.
- `ralfloop_agent/integration/abc_relation_read.py`: adapter runtime read-only canonico.
- `src/api.py`: `/tasks/run` inserisce stato/analisi ABC quando `abc_relation` è selezionato.
- `ralfloop_agent/nodes/reasoning.py`: anche il reasoning cycle OpenShell usa lo stesso adapter ABC invece di raccogliere un falso `ls -la` per le richieste relazionali.
- `tests/test_abc_relation_router_binding.py`: regressioni router/adapter.
- `tests/test_abc_relation_runtime_binding.py`: regressioni del reasoning cycle ABC.

## Regole di sicurezza

L'adapter ABC non espone né chiama:

- `abc_record_event`
- `abc_create_snapshot`

Il percorso normale usa soltanto letture. Di default legge:

- `abc_get_state`
- `abc_analyze`

Timeline viene caricata soltanto per richieste cronologiche. La biblioteca di psicologia/dialogo viene caricata per richieste che citano manuali/modelli oppure che chiedono come dialogare, comunicare, scrivere o rispondere.

Nessun nome personale è stato aggiunto ai trigger di routing.

## Runtime e fallback

Quando `abc_relation` è disponibile, `abc_memory` e `abc_relcalc` restano compatibilità legacy ma non vengono eseguite in parallelo al nuovo MCP.

Se il broker/socket ABC non è disponibile, l'adapter restituisce uno stato di indisponibilità esplicito con fallback dichiarato:

- `abc_memory`
- `abc_relcalc`

`src/api.py` esegue il fallback legacy soltanto in questo caso. Il reasoning cycle OpenShell non fabbrica una risposta MCP vuota e non sostituisce il fallimento con evidenza shell: restituisce `read_mcp unavailable`, `exit_code=1` e conserva `fallback_skills` nei metadata per l'orchestrazione superiore.

La normale richiesta ABC usa evidenza sintetica `mcp:abc_relation:read_only`. Nessuna scrittura viene tentata.

## Manuali

Resta valida la separazione definita nel MCP v1: Nardone/Salvini, Miller/Rollnick, Investment Model, Interdependence Theory e secure-base sono reference/euristiche. Non sono evidenza autonoma di intenzioni, attrazione o stati mentali nascosti.

## Verifica prevista

Le regressioni coprono:

1. query relazionale -> `abc_relation` read-only senza conferma;
2. nessun routing ABC per una generica relazione annuale di progetto;
3. semantica di conferma MCP esterni invariata;
4. disgiunzione completa tra allowlist read e tool write;
5. contesto minimo di default;
6. timeline/manuali solo su richiesta;
7. fallback legacy su socket non disponibile;
8. reasoning cycle -> adapter ABC canonico invece di shell;
9. broker ABC indisponibile -> errore MCP esplicito, non falsa evidenza `ls -la`.

## Stato della verifica

Le modifiche sono state scritte direttamente sul branch privato via GitHub. In questa sessione Remote Desktop Commander risulta connesso a Sibilla ma le chiamate filesystem/process sono sospese dal limite del connector. Il repository non espone una workflow GitHub Actions utile per dichiarare eseguiti questi nuovi test, quindi non viene attribuito loro un esito finché non esiste una successiva esecuzione reale su Sibilla.

Il checkpoint MCP v1 precedente resta valido per la suite già eseguita prima di questo binding (`15 passed`); quel risultato non viene esteso artificialmente alle modifiche di routing presenti qui.

## Live deployment

Questo step non modifica il sistema live. Restano da fare con accesso operativo alla macchina:

1. installare/abilitare il broker systemd ABC;
2. importare i JSON legacy correnti senza dump WhatsApp raw;
3. verificare `/run/ralf-abc-relation-mcp/mcp.sock`;
4. eseguire suite mirata e smoke end-to-end sia su `/tasks/run` sia sul reasoning cycle;
5. solo dopo attivare il branch nel runtime Bot-tazzi.
