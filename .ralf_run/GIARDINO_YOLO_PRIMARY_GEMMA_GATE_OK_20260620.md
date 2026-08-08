# GIARDINO YOLO PRIMARY GEMMA GATE OK - 2026-06-20

## Problema
Il detector live non usava YOLO: usava MobileNetSSD + motion fallback + Gemma.

YOLO era presente solo in:
- `/opt/bottazzi-garden/garden_yolo_fallback.py`

ma non era nella pipeline principale.

I frame “corrotti” arrivavano dallo stream/go2rtc come JPEG validi ma con smear/glitch verticale.

## Causa
Il servizio partiva con:

`/usr/bin/python3`

dove `ultralytics` non era disponibile.

## Patch live
Backup:

`/opt/bottazzi-garden/garden_person_detector.py.bak_yolo_primary_gemma_gate_20260620_100251`

File modificato:

`/opt/bottazzi-garden/garden_person_detector.py`

Drop-in:

`/etc/systemd/system/bottazzi-garden-detector.service.d/98-yolo-primary-gemma-gate.conf`

Il servizio ora usa:

`/home/bandi/bottazzi-yolo/.venv/bin/python3`

## Env live confermato
- `BOTTAZZI_GARDEN_USE_YOLO=1`
- `BOTTAZZI_GARDEN_YOLO_MODEL=/home/bandi/bottazzi-access-ui/yolov8n.pt`
- `BOTTAZZI_GARDEN_YOLO_CONFIDENCE=0.22`
- `BOTTAZZI_GARDEN_YOLO_IMGSZ=640`
- `BOTTAZZI_GARDEN_VALIDATE_YOLO=1`
- `BOTTAZZI_GARDEN_FRAME_TIMEOUT_SECONDS=8`
- `BOTTAZZI_GARDEN_GLITCH_FAILSOFT=1`
- `BOTTAZZI_GARDEN_GLITCH_FAILSOFT_WINDOW_SECONDS=120`
- `BOTTAZZI_GARDEN_GLITCH_FAILSOFT_REVIEW_COOLDOWN_SECONDS=240`
- `BOTTAZZI_GARDEN_ENABLE_LOWER_MOTION_OVERRIDE=0`

## Pipeline nuova
- frame valido
- YOLO rileva `person`
- Gemma conferma `human=true`
- invio Telegram

## Sicurezze
- YOLO fail-closed se Gemma fallisce
- frame vertical-smear rigettati già in `grab_frame`
- review glitch resa più stretta
- cooldown review glitch: 240s

## Verifica
- servizio active
- ExecStart effettivo:
  `/home/bandi/bottazzi-yolo/.venv/bin/python3 /opt/bottazzi-garden/garden_person_detector.py`
- `ultralytics` OK nel venv YOLO
- `ultralytics` version: 8.4.51
- `py_compile` OK con venv YOLO
- log live:
  - `garden_detector_started`
  - `yolo_enabled=true`
  - `validate_yolo_with_gemma=true`
  - `yolo_loaded`
  - `grab_rejected_corrupt_frame`
- nessun `Traceback`
- nessun `SyntaxError`
- nessun `RuntimeError`

## Nota
Se la camera manda solo frame corrotti durante un passaggio, nessun modello può vedere la persona.
Ora però quei frame non vengono più scambiati per passaggi validi e il detector riprova.
