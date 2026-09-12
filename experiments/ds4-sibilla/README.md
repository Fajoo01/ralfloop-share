# DwarfStar / ds4 on Sibilla

Target: test `antirez/ds4` on Sibilla with CUDA SSD streaming and an NVIDIA RTX 2070 (`sm_75`).

## Goal

Determine whether DeepSeek V4 Flash Q2 can run usefully on Sibilla by keeping only a small expert cache on the discrete GPU and streaming cold experts from the internal NVMe SSD.

This is an experiment, not yet a production runtime replacement.

## Hardware assumptions

Expected Sibilla profile:

- Linux x86_64
- NVIDIA RTX 2070, 8 GiB VRAM, Turing / `sm_75`
- about 62 GiB system RAM
- fast internal NVMe (Samsung 990 PRO class)

The important constraint is the 8 GiB **VRAM** ceiling. On discrete CUDA, do not treat system RAM as if it were Apple unified memory.

## Upstream facts to respect

Current ds4 uses:

```text
--ssd-streaming
--ssd-streaming-cache-experts <budget>
```

For CUDA/Turing, build explicitly for `sm_75`:

```bash
make cuda CUDA_ARCH=sm_75
```

Do not combine a low explicit `--gpu-vram` placement budget with `--ssd-streaming`; current upstream has reported tiered-placement conflicts in that configuration.

Low-VRAM CUDA SSD streaming is still young upstream. Expect tuning and possible CUDA-specific bugs. Keep the first run tiny and record the complete startup memory plan.

## First-pass plan

1. Collect hardware, driver, CUDA and NVMe facts.
2. Clone/update upstream `antirez/ds4` outside this repository.
3. Build for `sm_75`.
4. Run `ds4 --help` and verify the streaming flags exist in the checked-out commit.
5. Start with a very small context and token count.
6. Test auto-budget first if supported by the checked-out version.
7. Sweep explicit expert-cache budgets conservatively. On an 8 GiB card, start around 3–5 GB, not 8 GB.
8. Record wall time, prefill t/s, generation t/s, RSS, GPU memory, and failures.

## Safety margin

Do **not** assume `--ssd-streaming-cache-experts 5GB` means total GPU use is 5 GiB. ds4 also needs resident tensors, buffers, prefill workspace and KV/cache memory. A budget that looks safe on paper can still OOM an 8 GiB card.

Start with:

```text
ctx=2048 or 4096
tokens=1..20
expert cache=3GB
```

Then move upward only if the startup memory plan and `nvidia-smi` leave adequate headroom.

## Runner

Use:

```bash
bash experiments/ds4-sibilla/smoke.sh
```

By default the script performs diagnostics and builds ds4, but does not download an ~80 GiB model automatically. Set a model path explicitly for an inference smoke test:

```bash
DS4_MODEL=/path/to/ds4flash.gguf \
DS4_CACHE=3GB \
DS4_CTX=2048 \
DS4_TOKENS=8 \
bash experiments/ds4-sibilla/smoke.sh
```

## Results to save

For each run, keep:

- ds4 commit SHA
- NVIDIA driver + CUDA toolkit version
- `nvidia-smi` before/after
- RAM + swap status
- NVMe model path/filesystem
- exact command
- startup memory plan
- prefill t/s
- generation t/s
- peak GPU memory if available
- peak RSS
- crash/OOM/error text

Put results in this directory as dated Markdown files.