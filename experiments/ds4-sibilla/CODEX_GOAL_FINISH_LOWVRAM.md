# Codex goal: finish Sibilla DS4 CUDA low-VRAM port

Work on branch `codex/ds4-sibilla-sm75` and keep the production DS4 tree/service untouched.

## Objective

Finish the CUDA low-VRAM SSD-streaming port on the modern DwarfStar base so the RTX 2070 / `sm_75` path is both filesystem-safe and materially faster, then leave the branch in a state ready for a separate DeepSeek V4.1 validation branch.

The current functional baseline is already safe enough for controlled testing, but performance is unacceptable because one generated token causes about 33.37 GiB of staged model traffic, many stage-epoch resets, and roughly 0.32 tok/s.

## Trees and runtime boundaries

- Modern isolated port: `/home/sibilla-cumana/src/ds4-main-lowvram-port`
- Production reference: `/home/sibilla-cumana/src/ds4-cuda-stream-pr739`
- Production service: `bottazzi-ds4.service`
- Production port: `19194`
- Test port: `19195`
- Model currently used for parity tests: `/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf`
- GPU target: NVIDIA RTX 2070, CUDA `sm_75`

Treat the production tree and service as read-only references. Do not `pull`, `reset`, `clean`, `switch`, rebase, or modify `/home/sibilla-cumana/src/ds4-cuda-stream-pr739`. Do not alter `bottazzi-ds4.service`.

## Safety work already completed — preserve it

The modern port includes a mitigation for an NVIDIA/Linux file-backed page dirtying failure mode that previously caused an EXT4 warning storm and ~50 GiB of stuck Dirty pages. In `--cuda-low-vram-stream` mode, model-file ranges must not be registered with CUDA via `cudaHostRegister` / `cudaHostUnregister`.

Preserve all `DS4_LOW_VRAM_NO_FILE_HOST_REGISTER` guards and the runtime marker:

`CUDA low-VRAM SSD stream: skipping file-backed model host registration`

Do not reintroduce file-backed CUDA host registration in low-VRAM SSD mode, including indirectly through a copied older production function.

The following already pass and must continue to pass:

- static low-VRAM dirty-page safety verifier
- startup-only watchdog
- single-token watchdog
- no new `mpage_prepare_extent_to_map` warning
- GGUF inode/size/mtime/ctime unchanged after test
- bounded Dirty/Writeback

## Findings already established

1. The initial SSD CUDA model map is correct and is about 0.99 GiB.
2. The modern startup log reports `resident model 2.81 GiB`, but this is working-set accounting, not proof that 2.81 GiB is eagerly uploaded at startup. Modern code takes a max over startup spans / static decode map / active streaming spans / non-routed bytes, whereas production prints `startup_model_span_bytes` directly.
3. Restoring the production-style `if (g_ssd_streaming_mode) return 0;` prefetch suppression did not change the 2.81 GiB accounting number and is not the main performance issue.
4. The real blocker is staging churn during decode.
5. Static diff shows a major semantic gap in the stream-selected CUDA path:
   - modern `stream_selected` references: ~167
   - production `stream_selected` references: ~350
   - production contains substantially richer selected-expert reuse machinery: persistent expert cache, `persistent_direct`, ready/compute events, route tracking, persistent cache release/reuse, and more complete asynchronous stage-buffer lifecycle.
   - modern has a simplified selected upload path that performs chunked reads/copies and ends with `cudaStreamSynchronize`, which is a likely contributor to repeated traffic and host barriers.
6. Production also has a bounded persistent selected-expert cache intended to keep complete `(layer, expert)` records resident so routing hits avoid another SSD upload.

## Required implementation strategy

Do a semantic port, not a blind file transplant.

Compare production and modern `ds4_cuda.cu` around these concepts and port only the missing CUDA SSD-streaming performance semantics that are still valid on the modern upstream base:

- `cuda_stream_selected_cache`
- persistent selected-expert cache / `(layer, expert)` records
- `persistent_direct`
- route-hit reuse
- ready / compute-done events
- selected upload stream lifecycle
- four-buffer stage-pool reuse
- stage-event reuse
- persistent cache capacity / eviction behavior
- `g_model_range_bytes` interaction with bounded cache limits
- low-VRAM stage epoch reset behavior
- any production logic that prevents redundant gate/up/down uploads for the same routed experts

Preserve modern-only functionality, especially:

- DeepSeek V4.1 source support already present in the modern upstream base
- Engram handling / unmapping
- modern static-decode-map accounting
- modern multi-GPU / placement code
- modern CUDA/ROCm API changes
- current low-VRAM cache-plan logic
- current host-register safety mitigation

Do not replace modern `ds4_cuda.cu` wholesale with production.

## Performance goal

Reduce single-token low-VRAM SSD staging traffic from the current ~33.37 GiB by at least 4x if technically possible on this model, while keeping VRAM bounded and without regressing filesystem safety.

The primary acceptance signal is reduced redundant model upload traffic and route/cache misses, not merely a prettier startup memory report.

Instrument enough counters/logs to make the result auditable. At minimum report for the one-token smoke:

- staged/upload bytes
- upload range count
- stage hits/reuses
- persistent expert-cache hits/misses/evictions if applicable
- number of stage epoch resets
- elapsed inference time / tok/s
- peak/bounded cache use

If the 4x target cannot be reached without violating the VRAM budget, document the measured reason and leave the best safe bounded result.

## Required validation order

Do not jump directly to long inference.

1. Static checks / `git diff --check`.
2. Owner-safe `sm_75` build.
3. Static low-VRAM dirty safety verifier.
4. Startup-only watchdog on `19195` with production still on `19194`.
5. Single-token inference watchdog only.
6. Compare counters against the existing ~33.37 GiB / ~0.32 tok/s baseline.
7. Only if single-token remains clean and staging is materially reduced, run a short multi-token smoke (2-4 tokens), still with Dirty/Writeback/dmesg watchdogs.

Do not test `ctx=16384` yet. Use `ctx=4096` for parity/performance work until the single-token path is fixed.

AgentCPM may be temporarily stopped only by the existing watchdog helpers when VRAM headroom is required, and must always be restored on cleanup.

## Files/helpers already in the GitHub branch

Use and update the existing helpers under `experiments/ds4-sibilla/`, including:

- `verify-lowvram-dirty-safety.sh`
- `build-test-low-vram-port.sh`
- `runtime-startup-watchdog.sh`
- `runtime-inference-watchdog.sh`
- `analyze-lowvram-stage-churn.sh`
- existing incident / patch documentation

Improve these helpers if necessary so the final performance result is reproducible.

## Git discipline

Make small, reviewable commits on `codex/ds4-sibilla-sm75`.

Before each risky runtime change, commit the source/helper changes first. Do not merge the draft PR or cut production over.

All operations on `/home/sibilla-cumana/src/ds4-main-lowvram-port` must be owner-safe (`sibilla-cumana`). Do not use global `safe.directory '*'`.

Keep historical `.rej` files; do not delete them as cleanup.

## Definition of done

This task is done when:

- modern `sm_75` build passes;
- low-VRAM file-backed host registration remains disabled;
- startup watchdog passes;
- one-token watchdog passes with zero new EXT4 `mpage_prepare_extent_to_map` warnings;
- Dirty/Writeback stay bounded;
- GGUF metadata stays unchanged;
- redundant staging is materially reduced from the ~33.37 GiB baseline and the reason for the improvement is visible in counters/logs;
- any persistent cache is strictly VRAM-bounded and has deterministic cleanup;
- production service/tree were untouched;
- GitHub branch contains the source changes, updated watchdog/instrumentation helpers, and a short results note with before/after measurements.

After this is complete, stop. Do not switch production. The next task will create a separate V4.1 validation branch from this stable CUDA low-VRAM base.
