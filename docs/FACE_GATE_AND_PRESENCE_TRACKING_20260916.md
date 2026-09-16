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

## Diagnostica dataset e replay
- DB dlib live: Fabio 7 embedding, Maria 2.
- DB InsightFace live: Arianna 34 embedding: i due motori non coprono lo stesso insieme di identità.
- Builder candidato InsightFace da sole cartelle note: Fabio 7, Maria 2, Arianna 18; 22 immagini scartate per qualità.
- Il DB candidato non risolve da solo il problema: su alcuni burst storici InsightFace non trova un volto utile.
- I backup dlib più vecchi sono più sensibili a Fabio, ma quelli senza Arianna aumentano il rischio di falsi Fabio: non vanno ripristinati alla cieca.

## Diagnostica cattura citofono
I frame storici mostrano spesso tre problemi: cattura prima che il volto sia visibile, duplicati quasi identici e vertical smear nella fascia bassa.
Il watcher attuale termina appena ottiene un qualsiasi gruppo di frame, quindi può elaborare sei immagini inutili invece di aspettare un volto buono.
La prossima revisione seleziona tentativi per qualità entro una finestra temporale limitata e conserva il secondo consenso vocale obbligatorio.

## Ispirazioni applicate
Frigate separa detection, unknown/recognition threshold, area minima, numero minimo di facce e filtro blur; la stessa separazione viene adottata qui senza copiarne le soglie numeriche.
Il consenso ora conta frame unici, non il numero grezzo di hit prodotto da motori/varianti diverse.
InsightFace espone anche il margine rispetto al secondo candidato: per l'accesso non basta essere il migliore, bisogna esserlo con separazione sufficiente.

## Stato test branch
Suite combinata al 2026-09-16: 17 test verdi.
Copre consenso multi-frame, storico falso Fabio a distanza 0.4065, allowlist accesso, margine InsightFace, matching fisico one-to-one, garden-event non riutilizzabile, pending/grace e correlazione identità-track.
Nessuna modifica di questo blocco è ancora stata applicata all'apertura fisica del cancello.

## Replay storico e decisione di sicurezza
- Suite locale aggiornata: 19 test verdi; `py_compile` pulito.
- Replay stratificato su 10 burst reali salvati (vecchi Fabio/Arianna/Maria + recenti incerti): nessun caso ha ottenuto autorizzazione all'apertura con il nuovo consenso.
- I due vecchi burst etichettati `fabio` non contengono oggi evidenza sufficiente per il nuovo criterio multi-frame: non si abbassano le soglie per farli passare.
- Strategia: migliorare la cattura futura (finestra più densa, 2 fps / 10 frame) invece di rendere permissivo il matcher.
- Il DB InsightFace candidato unifica le identità note e viene generato solo da cartelle note; non viene versionato perché contiene embedding biometrici.
- L'auto-training storico basato su giudizio Gemma resta materiale di review e non viene assunto come ground truth.
- I crop di debug InsightFace sono disabilitati di default; attivabili solo esplicitamente via env.
- Per armare il cancello resta necessaria un'identità in allowlist (`fabio` di default) e il consenso multi-frame; la voce resta il secondo consenso per l'apertura fisica.

## Canary presenza: migrazione v2
Il primo deploy del correlatore v2 ha evidenziato un problema di migrazione: i track storici venivano considerati nuovi e 13 eventi `presence_correlator_v2` sono stati generati retroattivamente.
I timer sono stati fermati, i dati post-canary salvati in backup e sono stati rimossi esclusivamente i 13 record v2 appena creati. Gli stati derivati sono stati ricostruiti dall'ultimo evento valido precedente.
Stato ripristinato: Fabio `inside`, Arianna `outside`, Maria `inside`, nessun `pending_exit` spurio.
Correzione: `v2_cutover_ts` viene inizializzato alla prima esecuzione; senza `BOTTAZZI_PRESENCE_BACKFILL=1` vengono elaborati solo passage con timestamp di elaborazione uguale o successivo al cutover.
Canary dopo fix: prima/mid/dopo = 53/53/53 righe in `presence_events.jsonl`; nessun backfill e nessuna mutazione di stato.
