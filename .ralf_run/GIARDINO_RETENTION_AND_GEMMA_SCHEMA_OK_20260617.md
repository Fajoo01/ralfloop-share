# GIARDINO RETENTION + GEMMA SCHEMA OK - 2026-06-17

## Retention
Aggiunto doppio limite:
- elimina snapshot più vecchi di 7 giorni
- mantiene massimo 30000 snapshot recenti
- elimina eventi più vecchi di 7 giorni
- timer systemd giornaliero attivo

Verifica:
- snapshot finali: 30000
- spazio snapshot ridotto da circa 15G a circa 1.3G
- cleanup service: success
- timer: active

## Gemma vision
Uso corretto verificato:
- immagine base64 in `images`
- `stream=false`
- `keep_alive=0s`
- schema JSON strict
- timeout Gemma portato a 70s
- risposta JSON valida senza campi extra

## Patch detector
Marker:
- `GARDEN_GEMMA_SCHEMA_STRICT_PATCH_20260617`

Drop-in:
- `/etc/systemd/system/bottazzi-garden-detector.service.d/95-gemma-schema-strict.conf`

Env live verificato:
- `BOTTAZZI_GARDEN_VISION_JSON_SCHEMA=1`
- `BOTTAZZI_GARDEN_VISION_TIMEOUT_SECONDS=70`
- `BOTTAZZI_GARDEN_VISION_KEEP_ALIVE=0s`
- `BOTTAZZI_GARDEN_VISION_MODEL=gemma4:12b-it-qat`

## Meowgram
Endpoint review Telegram ripristinato:
- `127.0.0.1:18127`
- `Bot-tazzi alert HTTP endpoint listening`
- review motion candidate inviato con status 200
