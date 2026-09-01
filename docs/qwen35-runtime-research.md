# Bottazzi: Qwen3.5 MoE runtime research

## Safety boundary

Research branch: `codex/qwen35-runtime-research`. Production remains on `legacy`.
No running service, model route, policy, or default was changed. Experimental launch is
dry-run unless `--execute` is explicit; a 1024 MiB VRAM fit margin is mandatory.

## Initial state

- Host: 6C/12T Xeon W-2133, 62 GiB RAM, RTX 2070 8 GB (CC 7.5), one NUMA node.
- Models volume: ext4 on Samsung 990 PRO 2 TB NVMe; 1.3 TB free.
- CPU governor: `powersave`; THP: `madvise`; swap: 8 GiB.
- llama.cpp: build 9542, commit `6b80c74f2`, CUDA and CPU builds.
- Production: AgentCPM llama.cpp, DeepSeek ds4 SSD-streaming, Ollama Qwen3.5-9B.
- GPU inventory was already about 7.5 GiB occupied. GPU experiments therefore require
  the existing lifecycle broker or a maintenance window; this branch does not preempt it.

## Verified architecture and runtime surface

The official Qwen config declares 40 layers, 256 routed experts, top-8 routing,
one shared expert, 2048 hidden width, 262,144 positions, alternating three Gated
DeltaNet layers and one full-attention layer, and one MTP layer. Total weights remain
about 35B even though active weights are about 3B.

The installed llama.cpp build exposes `--cpu-moe`, automatic GPU fitting, mmap,
direct I/O, Flash Attention, quantized K/V, prompt cache, cache reuse, slot save,
continuous batching, metrics, and these speculation modes: `draft-mtp`,
`ngram-simple`, `ngram-map-k`, `ngram-map-k4v`, `ngram-mod`, `ngram-cache`.

## DS4 applicability decision

DS4's SSD tier, selected-expert cache, async loading, hot-list seeding, and cache
instrumentation are useful phase-2 references. They are not phase-1 defaults:
Q4_K_M is only 20.50 GiB and fits resident RAM with operating margin. The local DS4
tree is heavily customized and dirty; copying it would add production risk. Upstream
issues also show that CUDA cache effectiveness and cache-size monotonicity cannot be
assumed. Router-aware speculative prefetch and eviction protection remain measurable,
bounded experiments, not accepted llama.cpp features.

## First-pass plan

| Order | Configuration | Purpose |
|---:|---|---|
| 1 | Q4_K_M, CPU, mmap, no speculation | Resident-RAM truth baseline |
| 2 | Q4_K_M, CPU MoE + fitted GPU dense layers | Safe RAM→VRAM tier |
| 3 | Same + `ngram-mod` | Repetitive Bottazzi workload |
| 4 | Same + `draft-mtp` | MTP, only if GGUF contains MTP tensors |
| 5 | Q3_K_M | Only if Q4 latency or memory is unacceptable |

Combined MTP/ngram is rejected until separately validated. SSD streaming is rejected
for the 35B unless resident RAM loses in measured wall time or capacity.

## Baselines captured before Qwen35

| Model/path | Samples | Prefill tok/s | Decode tok/s | Deterministic success | Note |
|---|---:|---:|---:|---:|---|
| Qwen3.5-9B historical production logs | 27 | 550.75 median | 12.13 median | n/a | ranges 239.64–588.07 / 2.04–12.80 |
| Qwen3.5-9B Ollama CPU fallback | 5 | 25.27 median | 4.91 median | 4/5 | GPU already occupied; repeated reload/swap |
| DeepSeek V4 Flash ds4 | 2/5 | unavailable | unavailable | 1/2 | 162–218 s/request; run stopped |

The CPU fallback is a contention diagnostic, not a replacement for the historical
Qwen9 baseline. The DeepSeek partial run demonstrates current escalation latency but
is too incomplete for a quality-rate claim.

## Qwen35 measured results

| Test | Result | RAM / cache | Decision |
|---|---:|---:|---|
| pp128 CPU | 42.66 tok/s; 3.00 s | mmap | usable |
| pp2048 CPU | 23.91 tok/s; 85.64 s | mmap | prefix reuse required |
| pp8192 CPU | 21.38 tok/s; 383.12 s | mmap | non-interactive cold prefill |
| tg32 CPU | 7.71 tok/s | mmap | slower than healthy Qwen9 GPU |
| Quality suite | 4/5; median TTFT 1.33 s | peak run RSS 31.32 GiB | no quality promotion yet |
| Exact prompt repeat | 33/37 cached; TTFT 0.22–0.41 s | checkpoint restore | useful |
| `ngram-mod`, cold | 7.84 tok/s | 16 MiB hash pool | negligible regression |
| `ngram-mod`, repeated | 19.73–20.25 tok/s; 128/128 accepted | same output | 2.55× decode |
| `draft-mtp` | startup failure: no MTP layers in GGUF | n/a | unavailable for pinned artifact |

The full llama-bench reached 32,842,272 KiB RSS, performed no process swaps, and read
about 21.2 GiB through the filesystem. System swap occupancy rose to 6.8 GiB under the
combined production workload, although AgentCPM and DeepSeek health endpoints remained
healthy and no OOM occurred. This is not enough margin to run CPU Qwen35 concurrently
with every production model indefinitely.

The runtime also reported that `cache_reuse` is unsupported by the hybrid/recurrent
context. Exact repeated prompts can restore a 33-token checkpoint, reducing TTFT from
1.33 s to 0.22–0.41 s; dissimilar prompts force full reprocessing. Persistent slot
reuse is therefore narrower than for a pure transformer and must not be generalized.

## Current decision

Best measured experimental route: Q4_K_M resident RAM + single slot + q8 KV + exact
prompt cache + `ngram-mod` for repetitive code/JSON/tool traffic. It is not promoted:
the small deterministic suite ties Qwen9 at 4/5, CPU cold-context latency is too high,
GPU-offload remains unmeasured because only 398 MiB VRAM was free, and MTP is absent
from this GGUF. `legacy` stays default and DeepSeek stays escalation.

## Prefix reuse

The existing provider already sends `cache_prompt=true`, disables thinking, retains
one slot, and records `cached_tokens`. The experimental server adds a 4 GiB
prompt-cache budget. The installed runtime explicitly disables `--cache-reuse` for
this recurrent Qwen context; profiles set it to zero instead of promising KV shifting.
Reuse is accepted only when llama.cpp finds the exact
token prefix in the same model/template slot. Model hash, chat template, system policy,
tool schema, message order, and repository context changes invalidate the token prefix.
Persistent slot files are not enabled until restart/recovery correctness is tested.

## Observability

No duplicate metrics stack is added. The profile enables llama.cpp `/metrics`, `/slots`,
and perf timings; the installed server exports `llamacpp:prompt_tokens_total`,
`llamacpp:prompt_seconds_total`, `llamacpp:tokens_predicted_total`,
`llamacpp:tokens_predicted_seconds_total`, request/slot gauges, and per-response
`cached_tokens`. The benchmark writes JSONL with TTFT, prefill/decode rates, wall time,
tokens, and deterministic success. llama.cpp does not expose routed-expert hit/miss,
churn, predicted-expert, or per-expert I/O metrics, so a Qwen35 cache sweep cannot be
claimed without an upstream runtime patch.

## Router

Qwen35 is opt-in. DeepSeek remains available. Escalation candidates are deterministic:
invalid tool JSON/schema, rejected command, failed patch/test, retry exhaustion, and an
explicit high-complexity class. No self-reported confidence and no new judge LLM.

## Phase 2 gate

Do not download Qwen3.5-122B-A10B until the 35B passes quality and latency gates.
The 122B needs a documented dense/shared resident set, expert size/working set, RAM and
NVMe bandwidth model, then a microbenchmark of NVMe→RAM→VRAM with demand-read priority,
bounded cache, wrong-prefetch accounting, and protected near-term evictions.

Current artifact sizing already explains the gate: 122B Q3_K_M is 52.55 GiB,
Q4_K_M 71.28 GiB, and Q5_K_M 85.23 GiB. Q3 leaves too little of 62 GiB for the
OS, Bottazzi, KV/state, and graph scratch; Q4/Q5 exceed RAM. Very low 34–42 GiB
IQ1/IQ2/Q2 artifacts fit nominally but need a quality gate. Thus phase 2 should compare
low-bit resident RAM against Q3/Q4 expert streaming; Q4 streaming is the most relevant
quality/capacity experiment, not a prerequisite for Qwen35.
