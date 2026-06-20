# GIARDINO STREAM STABILITY OK - 2026-06-20

## Problema
`frame_grab_failed` frequenti nel detector Bot-tazzi.

## Causa
`go2rtc /api/frame.jpeg` può rispondere oltre i 4 secondi.
Il detector aveva timeout frame a 4s.

Il vecchio stream warmer basato su ffmpeg produceva inoltre rumore:
- DTS non monotoni
- errori H264
- corrupt decoded frame

## Modifiche live

### Detector
- `BOTTAZZI_GARDEN_FRAME_TIMEOUT_SECONDS=10`
- `BOTTAZZI_GARDEN_MAX_GRAB_ATTEMPTS=2`

### Glitch failsoft
- `BOTTAZZI_GARDEN_GLITCH_FAILSOFT=1`
- `BOTTAZZI_GARDEN_GLITCH_FAILSOFT_WINDOW_SECONDS=120`
- `BOTTAZZI_GARDEN_GLITCH_FAILSOFT_RETRY=1`
- `BOTTAZZI_GARDEN_GLITCH_FAILSOFT_REVIEW_COOLDOWN_SECONDS=45`
- `BOTTAZZI_GARDEN_ENABLE_LOWER_MOTION_OVERRIDE=0`

### Warmer
Sostituito il warmer ffmpeg con curl warmer su:

`http://127.0.0.1:1984/api/frame.jpeg?src=camera_giardino_anteriore`

File:
- `/usr/local/sbin/bottazzi-garden-frame-warmer`

Drop-in:
- `/etc/systemd/system/bottazzi-garden-stream-warmer.service.d/30-frame-curl-warmer.conf`

Timeout curl warmer:
- 12s

## Verifica
- detector active
- warmer active
- ExecStart effettivo warmer: `/usr/local/sbin/bottazzi-garden-frame-warmer`
- ffmpeg warmer non più attivo
- frame warmer produce JPEG valido in `/run/bottazzi-garden-warmer-frame.jpg`
- env live detector confermato:
  - `BOTTAZZI_GARDEN_FRAME_TIMEOUT_SECONDS=10`
  - `BOTTAZZI_GARDEN_MAX_GRAB_ATTEMPTS=2`
  - `BOTTAZZI_GARDEN_GLITCH_FAILSOFT=1`
  - `BOTTAZZI_GARDEN_GLITCH_FAILSOFT_WINDOW_SECONDS=120`
  - `BOTTAZZI_GARDEN_ENABLE_LOWER_MOTION_OVERRIDE=0`
