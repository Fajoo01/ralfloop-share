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
- `ralfloop_agent/integration/abc_relation_read.py`: adapter runtime read-only.
- `src/api.py`: `/tasks/run` inserisce stato/analisi ABC quando `abc_relation` è selezionato.
- `tests/test_abc_relation_router_binding.py`: regressioni dedicate.

## Regole di sicurezza

L'adapter ABC non espone né chiama:

- `abc_record_event`
- `abc_create_snapshot`

Il percorso normale usa soltanto letture. Di default legge:

- `abc_get_state`
- `abc_analyze`

Timeline e biblioteca di psicologia/dialogo sono caricate solo quando la richiesta le nomina esplicitamente.

Nessun nome personale è stato aggiunto ai trigger di routing.

## Fallback

Se il broker/socket ABC non è disponibile, il resolver restituisce uno stato di indisponibilità esplicito e richiede il fallback locale:

- `abc_memory`
- `abc_relcalc`

Non viene simulata una risposta MCP vuota e non si tenta alcuna scrittura.

## Manuali

Resta valida la separazione definita nel MCP v1: Nardone/Salvini, Miller/Rollnick, Investment Model, Interdependence Theory e secure-base sono reference/euristiche. Non sono evidenza autonoma di intenzioni, attrazione o stati mentali nascosti.

## Verifica prevista

Il nuovo test copre:

1. query relazionale -> `abc_relation` read-only senza conferma;
2. nessun routing ABC per una generica relazione annuale di progetto;
3. semantica di conferma MCP esterni invariata;
4. disgiunzione completa tra allowlist read e tool write;
5. contesto minimo di default;
6. timeline/manuali solo su richiesta;
7. fallback legacy su socket non disponibile.

## Stato della verifica

Le modifiche sono state scritte direttamente sul branch privato via GitHub. In questa sessione Remote Desktop Commander risulta connesso a Sibilla ma le chiamate filesystem/process sono sospese dal limite del connector, quindi questi nuovi test non vengono dichiarati eseguiti localmente finché non esiste un risultato reale di CI o una successiva esecuzione su Sibilla.

## Live deployment

Questo step non modifica il sistema live. Restano da fare con accesso operativo alla macchina:

1. installare/abilitare il broker systemd ABC;
2. importare i JSON legacy correnti senza dump WhatsApp raw;
3. verificare `/run/ralf-abc-relation-mcp/mcp.sock`;
4. eseguire la suite mirata e smoke end-to-end;
5. solo dopo attivare il branch nel runtime Bot-tazzi.
