# Sibilla CUDA low-VRAM results — 2026-09-13

Branch: `codex/ds4-sibilla-sm75`  
Target: RTX 2070, CUDA `sm_75`, `ctx=4096`, test port `19195`

## Change

Semantically ported the PR739 selected-expert path onto the modern tree:

- bounded persistent `(layer, expert)` LRU and `persistent_direct` dispatch;
- ready/compute-done events and asynchronous selected-id handoff;
- four-buffer selected-upload stage pool with event reuse;
- route reuse, capacity/eviction handling, deterministic release;
- upload, generic-stage, hit, reset, peak, and cache-summary counters.

The port script checks the input SHA, merge-conflict count, required modern safety
markers, and required cache symbols. The exact generated delta is retained beside
it as `stream-selected-persistent-cache.patch`.

## One-token result

Configuration: 640 MiB stage, 512 MiB reserve. Baseline values are from the
pre-port watchdog cited by `CODEX_GOAL_FINISH_LOWVRAM.md`.

| Metric | Before | After | Change |
|---|---:|---:|---:|
| Generic staged bytes | 33.37 GiB | 8.43 GiB | 3.96x lower, -74.7% |
| Generic upload ranges | 1664 | 1162 | -30.2% |
| Stage epoch resets | 185 | 127 | -31.4% |
| Stage hits | not instrumented | 93 | auditable |
| Selected upload | not instrumented | 4821 ranges / 10.59 GiB | auditable |
| Stage peak / budget | not instrumented | 536.70 / 640.00 MiB | bounded |
| Decode rate | 0.32 tok/s | 0.42 tok/s | +31.3% |
| Request elapsed | 20.006 s | 9.896 s | 2.02x faster |

The four-buffer asynchronous lifecycle removes repeated generic staging and host
barriers. This is visible in lower bytes, ranges, resets, and elapsed time.

The 4x byte target would require at most 8.34 GiB. The best safe result is 8.43
GiB, 0.09 GiB above that threshold. A bounded 704 MiB stage trial regressed to
8.55 GiB and 1172 ranges, so the reproducible default remains 640 MiB. The
remaining path includes the 536.56 MiB output upload and recurring non-routed
decode tensors; increasing the stage budget did not remove them.

The persistent expert cache is capacity-limited by current free VRAM minus a 1
GiB guard and the generic reserve, has a 480-slot minimum working set, LRU
eviction, and deterministic release. Its warm-up route-wrap gate did not open in
the bounded 1-4 token smokes, so persistent hits/misses/evictions are not
applicable to these measurements. Compact selected streaming remained active.

## Validation

- owner-safe `sm_75` build: PASS;
- static host-registration safety: 3 markers, 2 whole-model guards, 1 range guard;
- startup watchdog: PASS; test port stable; required skip marker present;
- one-token watchdog: PASS; completion `OK`;
- two-token watchdog: PASS; 12.54 GiB generic stage, 13.65 GiB selected upload,
  0.42 tok/s;
- four-token watchdog: PASS; 20.81 GiB generic stage, 18.60 GiB selected upload,
  0.42 tok/s;
- EXT4 `mpage_prepare_extent_to_map`: 0 before and after;
- GGUF metadata unchanged:
  `inode=2883591 size=86720111488 mtime=1786204941 ctime=1786204941`;
- Dirty/Writeback stayed below watchdog limits; final Writeback was 0;
- production service remained active on `19194`; no listener remained on `19195`;
- production `ds4_cuda.cu` SHA-256 stayed
  `1605225306abb49f91a5b9843bc5badf60fe2b3aed52852b3b52ddcfbaa2b7a7`.

Primary remote artifacts:

- `upstream-porting/build-instrumented/`
- `upstream-porting/startup-watchdog-instrumented/`
- `upstream-porting/inference-watchdog-persistent-cache/`
- `upstream-porting/inference-watchdog-persistent-cache-2tok/`
- `upstream-porting/inference-watchdog-persistent-cache-4tok-verbose/`
- `upstream-porting/inference-watchdog-persistent-cache-1tok-stage704/`

No production change or cutover was performed.
