# Bot-tazzi Assistant v1 — small-first checkpoint — 2026-09-21

## Scope

Branch: `feat/bottazzi-assistant-v1-20260921`

Base: `15da42b43ff06f1ca16066bb05d346a88dfb0951`

Goal: keep one self-hosted assistant stack and reuse Unified Assistant, deterministic/JEV pipelines, MCP gates and Motor instead of creating a second parallel stack.

No production promotion was performed.

## Assistant v1

`POST /assistant/v1/chat` now applies a configurable small-first policy:

- trivial/local chat -> `fast` model lane;
- general knowledge or acronym ambiguity -> `general` lane;
- existing tool-backed workflows -> Unified Assistant;
- deterministic/JEV workflows remain inside the existing domain pipelines;
- complex local requests can select `deep`, but production Motor escalation is not considered available until the existing Motor Judge service is healthy.

Model roles are configuration, not hardcoded model names:

- `BOTTAZZI_ASSISTANT_FAST_MODEL`
- `BOTTAZZI_ASSISTANT_GENERAL_MODEL`
- `BOTTAZZI_ASSISTANT_DEEP_MODEL`

The Assistant v1-only Unified gate was extended to expose domains that already existed but were hidden behind the historical Telegram-oriented text gate: documents, research, code and infrastructure. Legacy surfaces are unchanged.

## Provider bounding

The first end-to-end Qwen3.5 canary exposed an unbounded-generation defect in `OllamaChatProvider`: a short APS question decoded more than 1700 tokens and hit the 60 s provider inactivity timeout.

`ralfloop_agent/providers/chat.py` now accepts runtime `chat_request_options` for both Ollama and OpenAI-compatible providers. For Ollama, reserved fields `model`, `messages` and `stream` cannot be overridden by configuration.

The safe canary used:

```json
{
  "options": {
    "temperature": 0,
    "num_ctx": 2048,
    "num_predict": 256
  },
  "think": false,
  "keep_alive": 0
}
```

This is runtime configuration, not a model-specific hardcode.

## APS canary results

Prompt: `Cos'è una APS in Italia?`

### Qwen2.5 7B

Isolated Vulkan canary, cold start:

- wall: 8.381 s
- load: 5.382 s
- prompt eval: 0.725 s
- decode: 2.269 s / 96 tokens
- result: FAIL — expanded APS incorrectly as `Ateneo Permanente di Specializzazione`.

Conclusion: `qwen2.5:7b` must not be the general-knowledge default.

### Qwen3.5 9B

Direct isolated Vulkan canary, cold start:

- wall: 13.252 s
- load: 8.804 s
- prompt eval: 1.859 s
- decode: 2.586 s / 96 tokens
- result: PARTIAL — correctly recognized `Associazione di Promozione Sociale`, but the first direct answer cited the wrong law.

Bounded Assistant v1 end-to-end canary:

- lane: `general`
- wall: 15.03 s cold
- load: 7.767 s
- prompt eval: 0.713 s
- decode: 6.508 s / 239 tokens
- result: PARTIAL PASS — correct APS expansion and correct `D.Lgs. 117/2017`; fiscal wording remains too broad to use as a normative source without retrieval/verification.

### Qwen2.5 3B fast lane

Prompt: `Rispondi soltanto con: ciao Fabio`

- lane: `fast`
- wall: 4.47 s cold
- load: 3.935 s
- output: exactly `ciao Fabio`

This supports the small-first split: the fast lane is materially faster for trivial work, while Qwen3.5 is reserved for ambiguity/general knowledge.

Full structured data: `benchmarks/assistant-v1-model-canary-20260921.json`.

## Real-world routing benchmark

Added:

- `benchmarks/assistant-v1-real-world-cases-v1.json`
- `tools/benchmark_assistant_v1.py`
- `benchmarks/assistant-v1-routing-results-20260921.json`

The benchmark is route-only and side-effect-free: it calls the route planner/probe but never executes Unified Assistant, MCP calls or external writes.

Coverage: 16 cases across 8 requested categories:

- bandi
- email
- troubleshooting tecnico
- sviluppo
- documenti
- amministrazione Tiremm
- ricerca
- domotica

Initial run found two genuine routing defects:

1. `email ricevute da ...` was not parsed as `email.search`;
2. `Riassumi il PDF ... indica le fonti` was routed to generic research instead of the concrete document domain.

Both were corrected by extending the existing Gmail parser and preferring a concrete document artifact over generic research cues.

Final result: `16/16`, pass rate `1.0`.

## Regression suite

Relevant suite after all changes:

`178 passed, 2 warnings`

Warnings are the pre-existing FastAPI `on_event` deprecation warnings.

## Sibilla runtime diagnosis

### OOM and 19240

At 06:55 the kernel OOM killer terminated a `ds4-server` process. The saved `19240` experimental configuration shows approximately:

- 6 GiB pinned host cache;
- about 5.4 GiB GPU cache;
- additional staging/reserve memory.

Loading Qwen while this experimental DS4 was resident created unsafe memory pressure. `19240` was therefore left OFF during Assistant v1 canaries.

Swap remains close to full after the event; no `swapoff` was attempted because that would itself be unsafe under the current memory pressure.

### Motor / production DS4

Read-only listener checks found `19194`, `19195` and `19196` not listening during this checkpoint.

`19194` was NOT started, restarted, stopped or reconfigured. It remains protected exactly as requested.

The existing Motor is a process judge, not a free-form chat generator. The correct future composition is candidate generation -> existing Motor Judge verification -> gate, not pointing `deep_chat` directly at the production DS4 endpoint.

Until the existing Motor Judge path is healthy again, Assistant v1 must not claim live DS4 escalation.

### Ollama GPU path

The system Ollama unit currently forces:

`OLLAMA_LLM_LIBRARY=cuda_v11`

but the installed Ollama backends are `cuda_v12`, `cuda_v13` and `vulkan`. The main Ollama instance therefore fell back to CPU.

A forced isolated `cuda_v12` canary failed with `device kernel image is invalid` on the current NVIDIA 535 driver / bundled Ollama CUDA combination.

For benchmarking only, an isolated Ollama instance was started on `127.0.0.1:11435` with Vulkan. Vulkan correctly used the RTX 2070 and allowed safe sequential Qwen canaries. The system Ollama configuration was not changed.

## Promotion decision

DO NOT promote Assistant v1 to production yet.

Blocking items:

1. restore a healthy Motor Judge path without touching production DS4 unsafely;
2. add resource arbitration/mutual exclusion before co-resident DS4 + Qwen workloads;
3. fix or deliberately replace the stale system Ollama GPU backend configuration;
4. re-run quality benchmarks with grounded legal/administrative questions, not only routing;
5. keep Qwen3.5 behind retrieval/verification for normative/legal/fiscal claims.

## Safety invariants retained

- external writes still pass through existing approval gates;
- local chat provenance guard blocks false claims of executed side effects;
- no second assistant stack was introduced;
- no push to `origin` / `ralfloop-share`;
- no production deployment performed;
- `19194` remained untouched.
