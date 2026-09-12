# DwarfStar / ds4 on Sibilla

## Current state: already deployed

Sibilla already runs a CUDA/SSD-streamed DeepSeek V4 Flash backend as the system service `bottazzi-ds4.service`.

Observed runtime:

```text
binary: /home/sibilla-cumana/src/ds4-cuda-stream-pr739/ds4-server
model:  /home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf

--backend cuda
--ssd-streaming
--cuda-low-vram-stream
--ssd-streaming-cold
--ctx 16384
--prefill-chunk 128
--threads 8
--tokens 256
--host 127.0.0.1
--port 19194
```

Systemd drop-in:

```text
DS4_CUDA_LOW_VRAM_STAGE_MB=640
```

Observed startup plan:

```text
KV 0.23 GiB (raw 0.02 + compressed 0.21)
resident model 0.99 GiB
planned total 1.22 GiB
ctx=16384
prefill_cap=128
raw_kv_rows=256
compressed_kv_rows=4098
```

Observed steady GPU residency for ds4 was about 2238 MiB on the RTX 2070 8 GiB.

## Source provenance

The deployed checkout is:

```text
/home/sibilla-cumana/src/ds4-cuda-stream-pr739
branch: experiment/cuda-8gb-stream-all-weights
commit: 05632d2 cuda: retain routed experts in bounded cache
```

Commit `05632d2` is exactly the head of upstream PR `antirez/ds4#739` (`cuda: retain routed experts in bounded cache`). PR #739 is stacked on PR #737 (SSD-streaming host-fallback safety) and PR #738 (async expert upload overlap). As observed on 2026-09-12, all three PRs remain open and unmerged.

Current upstream `main` on 2026-09-12 is `bd66c402070042bf0a79ad6ece8242de4c93680c`. GitHub comparison reports PR739's head as 3 commits ahead but 250 commits behind current `main`; the histories have substantially diverged.

## Critical local delta

The checkout is **dirty** above PR739. Modified files observed include:

```text
Makefile
README.md
ds4.c
ds4.h
ds4_agent.c
ds4_bench.c
ds4_cli.c
ds4_cuda.cu
ds4_eval.c
ds4_gpu.h
ds4_help.c
ds4_metal.m
ds4_server.c
ds4_ssd.c
ds4_ssd.h
rocm/ds4_rocm_current_api_compat.cuh
tests/test_gpu_args_cli.sh
```

Untracked paths observed:

```text
gguf
logs/
tests/test_ssd
tests/test_ssd.c
```

The deployed low-VRAM controls (`--cuda-low-vram-stream`, `--ssd-streaming-cold`, `DS4_CUDA_LOW_VRAM_STAGE_MB`) were not found by code search in current upstream `antirez/ds4` main. Treat these local changes as valuable integration work, not disposable build dirt.

**Do not pull, reset, rebase, checkout another branch, or clean this working tree until its delta has been captured.**

## Hardware

- Linux x86_64
- NVIDIA RTX 2070, 8 GiB VRAM, Turing / `sm_75`
- NVIDIA driver observed: 535.309.01
- CUDA capability reported by `nvidia-smi`: 12.2
- fast internal NVMe

The important constraint is the 8 GiB VRAM ceiling. On discrete CUDA, system RAM is not unified GPU memory.

## Operational VRAM contention

At the observed snapshot:

```text
ds4-server  ~2238 MiB
AgentCPM    ~3718 MiB
Xorg         ~140 MiB
GPU total    8192 MiB
GPU used    ~6103 MiB
```

This explains why Fish Speech could OOM when started concurrently. Runtime coexistence is currently a larger practical constraint than ds4 startup itself.

## Next step

Before any upstream synchronization, capture the complete local patch and identify which changes implement the 8 GiB low-VRAM/cold-stream path. Then benchmark the existing runtime and only afterwards attempt a controlled forward-port onto current upstream in a separate checkout/branch.

The audit helper in `smoke.sh` is read-only and must not rebuild or restart the production service.