# Face gate + registro ingressi/uscite — stato 2026-09-16

## Componenti live recuperati
- `bottazzi-face-api.service` → `/home/sibilla-cumana/bottazzi-face/face_api.py`, porta 18124.
- `bottazzi-camera-wake-watcher.service` → riconoscimento da stream citofono e armamento cancello.
- `bottazzi-passage-tracker.service` → `/opt/bottazzi-presence/passage_tracker.py`.
- `bottazzi-guest-tracker.service` → `/opt/bottazzi-presence/guest_tracker.py`.
- `bottazzi-presence-correlator.service` → `/opt/bottazzi-presence/presence_correlator.py`.

## Apertura cancello già provata
La documentazione storica conferma apertura end-to-end: volto riconosciuto → `gate_armed_until` → consenso voce → `switch.cancello_switch_1`.
È presente anche un precedente falso riconoscimento Fabio/Arianna, mitigato allora con `FACE_MAX_BEST_DISTANCE=0.39`.

## Registro presenze già presente
`passage_tracker.py` correla citofono lato strada e camera giardino lato interno entro 180 s:
- citofono prima, giardino dopo → entrata probabile;
- giardino prima, citofono dopo → uscita probabile.

`presence_correlator.py` lega invece un volto noto del citofono a un passaggio giardino e mantiene stato `inside/outside`, con grace di 10 minuti per uscita/rientro breve.
`guest_tracker.py` usa InsightFace CPU e crea identità temporanee `guest_###` per sconosciuti, poi prova a seguirne lo stato.

## Problemi strutturali da correggere
1. Face API usa due motori diversi (dlib/face_recognition + InsightFace) senza calibrazione comune del punteggio.
2. Decisione cancello storicamente basata anche su un singolo snapshot: insufficiente per access control.
3. Nessun controllo di liveness/anti-spoof esplicito nel codice recuperato.
4. Passage tracker usa nearest-neighbour temporale entro 180 s: con più persone può associare il giardino al citofono sbagliato.
5. Il registro usa più file/stati separati (`registry.json`, `passage_state.json`, `guest_registry.json`) e può divergere.
6. Il garden detector attuale è rumoroso: il registro dipende dalla sua affidabilità.

## Direzione di perfezionamento
- Face recognition a consenso multi-frame con score aggregato e margine tra primo e secondo candidato.
- Separare chiaramente detection, recognition e autorizzazione apertura.
- Liveness/passive anti-spoof o secondo fattore locale prima dell'apertura automatica.
- Correlazione ingressi/uscite per track/eventi, non solo nearest timestamp.
- Registro eventi append-only unico + stato derivato, idempotente e ricalcolabile.
- Guest tracking separato dall'autorizzazione: uno sconosciuto può essere tracciato ma non autorizza mai il cancello.
