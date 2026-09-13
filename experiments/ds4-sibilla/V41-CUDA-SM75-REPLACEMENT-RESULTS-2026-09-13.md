# DeepSeek V4.1 CUDA sm_75 replacement — 2026-09-13

## Outcome

**BLOCKED at Gate 1. No cutover.** Current upstream implements DeepSeek V4.1
inference only for Metal. The CUDA backend has none of the required V4.1 GPU
primitives, the complete V4.1 graph is Apple-gated, and runtime explicitly
rejects V4.1 on CUDA. Promoting this build would replace a healthy V4 service
with a server that cannot create a V4.1 engine.

## Source and target

- task baseline: `fa5dc9c9fdfb488c7cfac1d5ec6335a6ad9ccb5f`
- DwarfStar upstream: `antirez/ds4`
- upstream HEAD: `bd66c402070042bf0a79ad6ece8242de4c93680c`
- upstream commit title: `DeepSeek v4.1 Flash support for Metal`
- target: NVIDIA GeForce RTX 2070, compute capability 7.5, `sm_75`, 8192 MiB
- host: 62.5 GiB RAM; data volume free space 1,343,399,309,312 bytes
- isolated tree: `/home/sibilla-cumana/src/ds4-main-lowvram-port`
- production tree: `/home/sibilla-cumana/src/ds4-cuda-stream-pr739` (read-only)

Primary upstream references:

- <https://github.com/antirez/ds4/commit/bd66c402070042bf0a79ad6ece8242de4c93680c>
- <https://github.com/antirez/ds4/blob/bd66c402070042bf0a79ad6ece8242de4c93680c/docs/MODELS.md#deepseek-v41-flash>
- <https://huggingface.co/antirez/deepseek-v4.1-flash-gguf>

## Exact V4.1 artifact

- repository: `antirez/deepseek-v4.1-flash-gguf`
- file: `DeepSeek-V4.1-Flash-Q2.gguf`
- bytes: `365713686528` (340.60 GiB)
- SHA-256: `1ce6a8f8806205c13330d7ca287bd198331dc5ca35ccc5d8a9a92a188a6f6f42`
- main weights: 151.77 GiB
- disk-only Engram tables: 188.83 GiB
- source checkpoint: `deepseek-ai/DeepSeek-V4.1-Flash@df42c109f1defefcbfcedbe7d905718a12266e40`

The data volume has enough capacity. The artifact was not downloaded because
Gate 1 failed before model startup; the mandated gate order forbids proceeding
to Gate 2.

## V4 to V4.1 architecture delta

| Property | V4 Flash | V4.1 Flash |
|---|---:|---:|
| architecture key | `deepseek4` | `deepseek41` |
| layers | 43 | 40 |
| embedding width | 4096 | 5120 |
| routed experts | 256 | 384 |
| experts/token | 6 | 6 |
| expert FF width | 2048 | 2304 |
| Q LoRA rank | 1024 | 1280 |
| index heads | 64 | 32 |
| index top-k | 512 | 512 |
| train context | model-specific | 1,048,576 |
| Engram | absent | layers 1/14; two ~384M-row disk tables |

V4.1 also adds BF16/FP8/FP4 activation rounding, unit-magnitude RoPE,
pair-pooling, candidate-block filtering, sparse KV gather, packed index scoring,
layer-major prefill, and a different attention/output projection path. Its
Q2 GGUF keeps IQ2_XXS gate/up and Q2_K down experts, Q8 attention/shared/output,
plus F16/F32 tensors and native Engram rows. V4 weights and DSpark are not
interchangeable.

Parser, metadata validation, tensor binding, Engram unmapping/readers, tokenizer,
chat rendering, model ID, and server API exist in common source. V4.1 uses
`<｜System｜>`, ordered tool-result rendering, and optional reasoning effort
1-100. `/v1/models` advertises `deepseek-v4.1-flash`; chat requests accept the
known V4/V4.1 aliases. Server frontend unit tests pass.

## CUDA blocker

The upstream V4.1 commit changes:

- `ds4.c`: +3006/-102 lines;
- `ds4_gpu.h`: +101/-2 lines;
- `ds4_metal.m`: +901/-271 lines;
- `ds4_cuda.cu`: **0 lines**.

`ds4_gpu.h` places the V4.1 GPU API under `#ifdef __APPLE__`.
`ds4.c:39046-40886` places the complete inference graph under
`#if defined(__APPLE__) && !defined(DS4_NO_GPU)`. Engine creation requires
`DS4_BACKEND_METAL`. The built CUDA binary retains the rejection text.

Readiness audit on Sibilla:

```text
required_v41_gpu_apis=18
metal_v41_gpu_apis=19
cuda_v41_gpu_apis=0
missing_cuda_apis=18
v41_graph_metal_guard=1
runtime_metal_reject=1
binary_v41_gpu_symbols=0
sm75_cubins=9
host_register_guards=3
V41_CUDA_BLOCKED: upstream graph and GPU primitives are Metal-only
```

Missing CUDA implementations:

```text
attention_output_batch, attention_output_tp_batch,
candidate_blocks, candidate_filter, carry_copy, engram_add, gather_kv,
indexer_pack, indexer_packed_bytes, indexer_scores_batch,
indexer_scores_packed, indexer_topk_batch, pool2, projection_rows,
quantize, rope, rope_stride, tensor_ops_available
```

This is not a parser fix or a backend flag. A responsible port needs CUDA
kernels with numerical oracles for all 18 operations, a backend-neutral version
of the 1,841-line graph, V4.1 tensor residency/SSD integration, and new bounded
cache sizing for 384 experts on 8 GiB VRAM. Upstream supplies Metal-only V4.1
kernel/graph tests; there is no CUDA V4.1 correctness oracle. Removing the
runtime check or adding success stubs would compile but produce invalid output.

Reproduce with:

```sh
vpnpc copy-to sibilla experiments/ds4-sibilla/analyze-v41-cuda-readiness.sh \
  /home/sibilla-cumana/ds4-v41-readiness.sh
vpnpc run sibilla -- bash /home/sibilla-cumana/ds4-v41-readiness.sh
```

Expected exit code: `2`.

## Gates

| Gate | Result | Evidence |
|---|---|---|
| 1 static | **BLOCKED** | 18/18 CUDA APIs missing; graph/runtime Metal-only |
| baseline `sm_75` build | PASS | `CUDA_ARCH=sm_75`; `ds4` and `ds4-server`; `build.rc=0` |
| baseline safety | PASS | diff clean; 0 merge markers; 3 host-register guards |
| frontend/API unit tests | PASS | `ds4_test --server`: `server: OK` |
| filesystem precheck | PASS | mpage=0; EXT4/NVMe warnings=0; Writeback=0 |
| 2 startup V4.1 | NOT RUN | Gate 1 failed |
| 3 one token | NOT RUN | Gate 1 failed |
| 4 two/four tokens | NOT RUN | Gate 1 failed |
| 5 ctx 4096/8192/16384 | NOT RUN | Gate 1 failed |

Build command:

```sh
make -B -j2 ds4 ds4-server CUDA_ARCH=sm_75
```

Build warning: one pre-existing signedness warning at `ds4.c:67029`. No new
warning was introduced. Artifact log:
`upstream-porting/build-v41-static-blocker/`.

V4 production baseline remains 8.43 GiB generic staging, 1162 ranges, 127
epoch resets, 0.42 tok/s, 640 MiB stage. V4.1 staging, selected traffic,
persistent-cache statistics, VRAM/RAM runtime, TTFT, and tok/s are unavailable
because no CUDA inference path exists. No cosmetic comparison is claimed.

## Candidate command and systemd state

The command that would be required after a real CUDA port is:

```sh
DS4_CUDA_LOW_VRAM_STAGE_MB=640 \
/home/sibilla-cumana/src/ds4-main-lowvram-port/ds4-server \
  -m /home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4.1-flash/DeepSeek-V4.1-Flash-Q2.gguf \
  --backend cuda --ssd-streaming --cuda-low-vram-stream --ssd-streaming-cold \
  --ctx 16384 --prefill-chunk 128 --threads 8 --tokens 256 \
  --host 127.0.0.1 --port 19194
```

It must not be installed while the static blocker exists. No candidate unit or
drop-in was installed. Final production files remain:

- unit: `/etc/systemd/system/bottazzi-ds4.service`, SHA-256
  `c3b5e1ae5d8d434519442f567f8a2c33db25f7dfbb38d020f8071eab6ee088ce`;
- drop-in: `low-vram-stage.conf`, SHA-256
  `4e69aa513edaef2a36d6582d612c78bb68b0c014c4e59b374c6cb8a1970a42e1`;
- wrapper: `/home/sibilla-cumana/.local/bin/bottazzi-ds4-server`, SHA-256
  `3b1410ccc27e95b38352d7d77aa14c37a39fae2d9b1cbc818c6f5a4957af5b71`.

## Rollback and production integrity

No rollback action was needed because no cutover occurred. The procedural
rollback anchor is the unchanged wrapper/unit/drop-in above, pointing to:

```text
/home/sibilla-cumana/src/ds4-cuda-stream-pr739/ds4-server
/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf
```

Production binary SHA-256:
`694a0a6c4581367c8788f914f2ec3bfb651d986fce874ee2f071ad6018acf19c`.
Production CUDA source SHA-256 remains
`1605225306abb49f91a5b9843bc5badf60fe2b3aed52852b3b52ddcfbaa2b7a7`.
The service is active on `127.0.0.1:19194`; `19195` is free. The V4 GGUF
metadata remains `inode=2883591 size=86720111488 mtime=1786204941
ctime=1786204941`.

## Branch changes

- V4.1 CUDA readiness audit with explicit failure status;
- watchdog model alias parameter for future V4.1 gates;
- this replacement/blocker report.

Cutover result: **not performed; replacement is technically irresponsible until
the CUDA V4.1 graph and kernels exist and pass the mandated gates.**
