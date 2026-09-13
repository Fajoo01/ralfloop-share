# Continue DeepSeek V4.1 CUDA / sm_75 replacement work

Branch: `codex/ds4-v41-sibilla-sm75`

Current remote HEAD before this note: `e39ca928e52297d4a9a7a5c1a568814d4d7d0290`.
Baseline parent: `fa5dc9c9fdfb488c7cfac1d5ec6335a6ad9ccb5f`.

## Goal

Do not stop at "upstream says Metal-only". The user goal remains to replace the current V4 production with DeepSeek V4.1 Flash on Sibilla / RTX 2070 (`sm_75`) if a safe CUDA port can be completed. Treat the current Metal-only state as the starting blocker to remove, not as success by itself.

Only accept blocker closure if the CUDA port proves technically irresponsible after concrete implementation attempts or a hard hardware/model constraint is demonstrated with reproducible evidence.

Production remains read-only during development and validation:

- service: `bottazzi-ds4.service`
- production port: `127.0.0.1:19194`
- test port: `127.0.0.1:19195`
- production source: `/home/sibilla-cumana/src/ds4-cuda-stream-pr739`
- isolated modern source: `/home/sibilla-cumana/src/ds4-main-lowvram-port`

Preserve all low-VRAM / EXT4 safety work inherited from the V4 baseline.

## Verified current state

Upstream commit under study:

`bd66c402070042bf0a79ad6ece8242de4c93680c` — `DeepSeek v4.1 Flash support for Metal`

Upstream documentation explicitly marks DeepSeek V4.1 Flash as Metal-only. The V4.1 GPU API declarations are currently under `#ifdef __APPLE__`; `ds4_cuda.cu` provides none of the required V4.1 implementations. The sm_75 CUDA binary therefore contains no `ds4_gpu_dsv41_*` symbols.

The reproducible audit is:

`experiments/ds4-sibilla/analyze-v41-cuda-readiness.sh`

Observed audit state on Sibilla:

- required V4.1 GPU APIs: 18
- CUDA implementations: 0
- missing CUDA APIs: 18
- runtime contains the `V4.1 requires Metal inference` rejection
- sm_75 cubin exists for the ordinary CUDA build
- file-backed CUDA host-registration safety markers remain present
- production stayed active on 19194
- test 19195 stayed closed
- current-boot EXT4/mpage warnings: 0
- Dirty/Writeback remained bounded

The full owner-safe `CUDA_ARCH=sm_75` baseline build passes. Existing server/API unit tests pass, including V4.1 prompt/rendering-level coverage. This does NOT prove V4.1 CUDA inference.

The inference watchdog now accepts:

- `MODEL_ID=deepseek-v4-flash`
- `MODEL_ID=deepseek-v4.1-flash`

but no V4.1 runtime test may be launched until the CUDA graph/backend is actually implemented and a correct V4.1 GGUF is present.

## Correct V4.1 model artifact

Upstream model target:

`./download_model.sh ds41f-q2`

Expected file:

`gguf/DeepSeek-V4.1-Flash-Q2.gguf`

Upstream documents approximately:

- complete Q2 GGUF: 341 GiB
- main weights: 152 GiB
- Engram tables: 189 GiB

Engram rows are intentionally read from disk and are not a resident table. A fast local SSD is required.

Before downloading, measure available disk capacity and leave a safe margin. Do not start a 341 GiB download blindly.

## CUDA implementation worklist

The current V4.1 interface contains these 18 GPU operations which must acquire CUDA implementations or a semantically equivalent CUDA graph path:

1. `ds4_gpu_dsv41_quantize`
2. `ds4_gpu_dsv41_attention_output_batch`
3. `ds4_gpu_dsv41_attention_output_tp_batch`
4. `ds4_gpu_dsv41_rope`
5. `ds4_gpu_dsv41_rope_stride`
6. `ds4_gpu_dsv41_engram_add`
7. `ds4_gpu_dsv41_pool2`
8. `ds4_gpu_dsv41_candidate_blocks`
9. `ds4_gpu_dsv41_candidate_filter`
10. `ds4_gpu_dsv41_indexer_scores_batch`
11. `ds4_gpu_dsv41_tensor_ops_available`
12. `ds4_gpu_dsv41_indexer_packed_bytes`
13. `ds4_gpu_dsv41_indexer_pack`
14. `ds4_gpu_dsv41_indexer_scores_packed`
15. `ds4_gpu_dsv41_indexer_topk_batch`
16. `ds4_gpu_dsv41_carry_copy`
17. `ds4_gpu_dsv41_projection_rows`
18. `ds4_gpu_dsv41_gather_kv`

Port in small semantic groups rather than copying the entire Metal backend.

Recommended order:

### Phase A — API exposure and CPU/reference contracts

- Map every V4.1 call site in `ds4.c` to its Metal implementation.
- Record shapes, strides, formats, causal bounds and rounding requirements.
- Identify which operations can reuse existing V4/CUDA kernels without semantic change.
- Add compile-time declarations for CUDA only when an implementation exists.
- Keep the runtime Metal rejection until the graph is complete enough to execute safely.

### Phase B — simple tensor primitives

Start with the most isolated operations:

- quantize / activation-format rounding
- RoPE / RoPE stride
- carry copy
- projection rows
- gather KV

For each primitive, add a deterministic small-input test comparing CUDA with the CPU/reference or Metal-defined semantics. Do not rely on end-to-end text output as the first correctness check.

### Phase C — V4.1 sparse/indexer path

Implement and test:

- pool2
- candidate blocks
- candidate filter
- indexer scores
- packed indexer representation
- packed scores
- exact top-k ordering

Preserve causal widths, source-row strides and the minimum-visible-key behavior required by the graph.

### Phase D — attention output and Engram

Implement and validate:

- Engram add
- full-head attention output batch
- TP attention output path only if needed for this single-GPU Sibilla goal; do not block single-GPU progress on unused TP functionality if the core graph can cleanly gate TP off.

The Sibilla target is a single RTX 2070. Avoid implementing unrelated distributed functionality before the single-GPU path works.

### Phase E — graph enablement

Only after required single-GPU primitives are present:

- make the V4.1 graph available on CUDA;
- change `ds4_gpu_dsv41_tensor_ops_available()` appropriately;
- remove/relax the `V4.1 requires Metal inference` rejection for CUDA only when the runtime path is complete;
- retain a hard reject for unsupported combinations instead of silently falling back to wrong semantics.

## Turing / sm_75 constraints

Do not introduce instructions that require Ampere, Ada or Blackwell. The target is RTX 2070 / compute capability 7.5.

Where upstream Metal uses BF16 / FP8 / FP4 semantic rounding, correctness is the requirement; native hardware BF16/FP8/FP4 instructions are not assumed on Turing. Software conversion or existing CUDA scalar/vector kernels are acceptable if they preserve released V4.1 semantics and fit the low-VRAM budget.

Performance optimization comes after correctness and filesystem safety.

## Model acquisition gate

Before downloading `ds41f-q2`:

1. measure free bytes on the filesystem that will hold the model;
2. confirm at least the full 341 GiB plus operational safety margin is available;
3. record the destination path;
4. prefer the upstream tested artifact exactly;
5. preserve the existing V4 GGUF and rollback path.

If disk capacity is insufficient, record that separately from the CUDA implementation blocker. Do not confuse storage capacity with backend support.

## Runtime gates after CUDA implementation

Follow the existing order strictly:

1. static audit PASS (`missing_cuda_apis=0`, no runtime Metal-only rejection for supported CUDA path, V4.1 symbols in sm_75 binary);
2. owner-safe `sm_75` build PASS;
3. startup-only watchdog on 19195;
4. 1-token watchdog;
5. 2-token and 4-token watchdogs;
6. contexts 4096 -> 8192 -> 16384;
7. Bottazzi API compatibility;
8. reversible production cutover only after all acceptance criteria pass.

At every runtime gate retain:

- `DS4_LOW_VRAM_NO_FILE_HOST_REGISTER` protection;
- EXT4/mpage monitoring;
- Dirty/Writeback bounds;
- GGUF inode/size/mtime/ctime verification;
- production 19194 availability during isolated tests;
- AgentCPM stop/restore trap;
- separate generic-stage and selected-expert traffic accounting.

## Do not do

- do not cut over now;
- do not remove the V4 production model;
- do not treat a successful ordinary sm_75 build as V4.1 CUDA support;
- do not treat passing server template tests as inference support;
- do not download the 341 GiB model before checking disk capacity;
- do not port the whole Metal file blindly;
- do not remove safety guards to make the graph compile;
- do not silently emulate unsupported operations with numerically different behavior.

## Final Definition of Done

Success remains one of two outcomes:

A. DeepSeek V4.1 Flash is validated on CUDA sm_75, passes all low-VRAM/filesystem gates, integrates with Bottazzi, and is promoted to 19194 with a tested rollback; or

B. after concrete CUDA implementation work, a hard blocker is demonstrated with reproducible evidence and documented precisely enough that the replacement decision is justified.

The current fact that upstream ships V4.1 as Metal-only is not by itself enough to choose B.
