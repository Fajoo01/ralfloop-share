# Garden go2rtc frame stability report

## Problema

Il detector non era bloccato da YOLO/Gemma: il collo di bottiglia era il frame source `/api/frame.jpeg` di go2rtc.

## Causa probabile

- go2rtc espone `camera_giardino_anteriore` solo come sorgente `tuya://...` cloud.
- Non e stata trovata una snapshot HTTP diretta o un substream locale piu leggero nella config attiva.
- `/api/frame.jpeg` richiede decode/fetch lento: p50 circa 4.6s, p95 circa 5s, con errori e timeout.
- Il warmer gia produce `/run/bottazzi-garden-warmer-frame.jpg`: leggerlo da file e istantaneo e riduce gli errori del detector.

## Dati prima

- go2rtc direct: attempts `80`, ok `74`, errors `6`, timeouts `1`, avg `4.6321s`, p95 `4.9632s`, valid JPEG `74`, smear `9`.
- warmer cache file: attempts `80`, ok `80`, errors `0`, avg `0.0001s`, p95 `0.0002s`, valid JPEG `80`, smear `5`, age p95 `5.232s`.

## Modifica applicata

- Patch live su `/opt/bottazzi-garden/garden_person_detector.py`: `grab_frame()` prova prima il cache file del warmer, valida dimensione/JPEG/OpenCV/smear, poi fallback HTTP su go2rtc se cache assente, stale o corrotta.
- Drop-in nuovo: `/etc/systemd/system/bottazzi-garden-detector.service.d/99-frame-cache-source.conf`.
- Env nuovo: `BOTTAZZI_GARDEN_FRAME_CACHE_FILE=/run/bottazzi-garden-warmer-frame.jpg`.
- Env nuovo: `BOTTAZZI_GARDEN_FRAME_CACHE_MAX_AGE_SECONDS=15`.
- YOLO e Gemma non sono stati disattivati.
- Nessuna patch micro-differita.

## Backup

- `.ralf_run/garden_go2rtc_frame_stability_20260620/backups/garden_person_detector.py.before_20260626_130013`
- Nessun backup drop-in precedente: `99-frame-cache-source.conf` non esisteva.

## Servizi

- `systemctl daemon-reload`: eseguito.
- Restart eseguito solo su `bottazzi-garden-detector.service`.
- `bottazzi-garden-stream-warmer.service`: non riavviato.

## Dati dopo

- go2rtc direct: attempts `80`, ok `70`, errors `10`, timeouts `2`, avg `4.7374s`, p95 `5.3209s`, valid JPEG `70`, smear `12`.
- effective detector source cache: attempts `80`, ok `80`, errors `0`, avg `0.0001s`, p95 `0.0002s`, valid JPEG `80`, smear `2`, age p95 `8.234s`.

## Stato finale

- `py_compile` detector: ok con `PYTHONPYCACHEPREFIX` sotto lab.
- `bottazzi-garden-detector.service`: active.
- `bottazzi-garden-stream-warmer.service`: active.
- Env live contiene `BOTTAZZI_GARDEN_FRAME_CACHE_FILE` e `BOTTAZZI_GARDEN_FRAME_CACHE_MAX_AGE_SECONDS`.
- Log post restart: `garden_detector_started` con cache file, `yolo_loaded`, `validate_yolo_with_gemma=true`.
- Nel filtro journal non risultano `Traceback`, `SyntaxError`, `RuntimeError`.

## Limite residuo

- go2rtc/Tuya resta lento e il warmer continua a loggare `frame_warmer_curl_failed`.
- Il detector ora non resta appeso al frame endpoint finche il cache file e valido; se la cache e corrotta/stale, torna al fallback HTTP.

## Prossima patch minima se resta instabile

- Rendere il warmer piu selettivo: validare JPEG e smear prima di sovrascrivere `/run/bottazzi-garden-warmer-frame.jpg`.
- Cercare una sorgente non Tuya cloud: RTSP diretto camera, snapshot HTTP camera, oppure substream reale se scoperto fuori dalla config attuale.
