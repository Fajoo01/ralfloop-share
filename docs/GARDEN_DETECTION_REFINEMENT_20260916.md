# Garden detection refinement — 2026-09-16

## Problema osservato
La pipeline YOLO/Gemma del giardino produce falsi positivi e mancate rilevazioni.
Dai log di produzione emerge un difetto strutturale: i frame duplicati/congelati venivano comunque associati al tracker e incrementavano `track_hits`, quindi un falso positivo statico poteva maturare artificialmente.

Un replay shadow sugli ultimi 16 snapshot ha trovato 13/16 frame duplicati o congelati. Questo è un problema distinto dalla qualità del detector e va trattato come salute dello stream.

## Modifiche implementate
- I frame duplicati o congelati non accumulano più evidenza nel tracker.
- La confidenza del track conserva una storia breve e usa una mediana temporale.
- Un singolo frame forte non genera più alert diretto: viene rinviato al VLM/Gemma.
- YOLO resta sensibile (`conf` bassa come soglia di candidatura), per non perdere passaggi rapidi.
- I filtri basati sul movimento ora ricevono `FrameHealth` dopo che è stato realmente calcolato.
- Il detector di origine viene propagato al supervisor.
- Aggiunto `local_motion_score` sul box: il movimento globale non basta più a validare un falso positivo statico in una zona diversa.
- Per Gemma viene preparata evidenza composta full-frame + crop del candidato.

## Ispirazioni tecniche
- Frigate: soglie per oggetto, zone/mask, stationary tracking e score aggregato nel tempo.
- Ultralytics: ByteTrack/BoT-SORT con track buffer, soglie di associazione e score fusion.
- Norfair: `initialization_delay`/hit counter prima di considerare un track stabile.

## Test
`tests/garden_detection/test_temporal_consensus.py`: 5 test verdi.
Coprono: inizializzazione track, singolo frame forte -> review VLM, consenso su due frame sani, duplicato che non matura il track, mediana bassa che blocca l'alert.

## Stato deploy
Le modifiche sono ancora nel worktree Git dedicato e non sono state copiate in `/opt/bottazzi-garden` al momento di questa nota.
Prima del deploy: completare shadow Gemma, correggere il path temporaneo dell'immagine evidence, backup dei file live, deploy atomico, restart e canary.
