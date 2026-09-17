# Videosorveglianza + domotica — 2026-09-16

## Stato osservato
- Garden detector, frame warmer, face API, camera watcher e timer presence/passages/guest sono attivi.
- `go2rtc` è v1.9.14 (`b5948cf`).
- Il garden frame path mostra errori intermittenti: HTTP 500, timeout, cache stale e `vertical_smear_artifact`.
- Il supervisor temporale blocca correttamente frame duplicati/congelati e non li trasforma in alert.
- Home Assistant e Tuya MCP sono raggiungibili: 96 dispositivi e 151 entità Tuya abilitate.
- Canary reale del nuovo health: 105 entità disponibili, 33 unavailable, 13 unknown, 0 missing.

## Recovery video
Il recovery viene centralizzato nel watchdog. Il detector può delegare usando `BOTTAZZI_GARDEN_STREAM_RECOVERY_OWNER=watchdog`, evitando che detector e timer riavviino go2rtc in concorrenza.

Il watchdog valida sia il frame diretto sia la cache con decode OpenCV, controllo dimensioni e filtro dello smear verticale già usato dal detector. Mantiene un punteggio persistente e usa cooldown; un singolo 500 non causa restart.

Su go2rtc 1.9.14 non usiamo `DELETE /api/streams` come recovery standard. Upstream segnala lifecycle problematico dei producer e race nella API streams su questa versione. Il fallback resta un restart single-flight del solo container go2rtc, senza riavviare anche il detector.

## Health domotica
`tuya_health` continua a restituire `status=completed` quando MCP e Home Assistant funzionano, e aggiunge `availability=healthy|degraded` con conteggi per stato e raggruppamento dei device degradati.

Le entità già offline non devono generare allarmi continui. `ralf_domotics_health_watchdog.py` inizializza una baseline persistente e, ai run successivi, espone soltanto transizioni: `new_unavailable`, `recovered`, `new_missing`, `returned_missing`.

Il watchdog domotico è read-only: non riavvia Home Assistant, non richiama servizi Tuya e non modifica device. Eventuali recovery automatici verranno aggiunti solo per condizioni specifiche e verificabili.

## Vincoli
- Gate citofono invariato: il volto può solo armare il gate; l'apertura fisica richiede il secondo fattore.
- Nessun embedding biometrico, token Home Assistant, credenziale Tuya o URL con segreti viene committato.
- Nessun hardcode di device da considerare guasto: lo stato operativo è basato su baseline/transizioni.

## Site separation: Sede / Camper / Asiago (2026-09-17)

Home Assistant currently assigns none of the 96 Tuya devices to HA areas, so HA `area_id` cannot separate physical locations. Bot-tazzi therefore uses an explicit, versioned device-id map in `config/tuya_sites.json`.

Rules:
- physical sites are `sede`, `camper`, `asiago`;
- mapping is by stable Home Assistant `device_id`, never by runtime name matching;
- only high-confidence devices are assigned automatically;
- ambiguous devices remain `unassigned` rather than being guessed;
- Tuya health exposes per-site counts and degraded devices;
- domotics transition events carry `site`, and the unified activity stream preserves it.

Initial canary with the explicit map: Asiago 4 devices / 1 entity available; Camper 9 devices / 9 entities unavailable; Sede 23 devices / 53 entities (45 available, 6 unavailable, 2 unknown); 60 devices remain unassigned pending classification. The all-unavailable Camper cohort should be treated as a possible site-level offline/power state, not automatically as nine independent device failures.
