# Ollama removal — 2026-09-16

## Verified
- Ollama model blobs are GGUF and reusable directly by llama.cpp.
- llama-server build 9542 passed OpenAI chat, strict JSON schema and embeddings canaries CPU-only.
- Qwen3 Embedding 4B returns 2560-dimensional vectors via `/v1/embeddings`.
- Gemma4 12B plus its separate projector loads as a multimodal llama.cpp model.
- First Gemma4 CPU image canary took about 49 seconds; CPU cluster is suitable for asynchronous review, not realtime vision.

## Live migration completed
- Canonical llama.cpp runtime: `/opt/ralf/llama/bin` including shared libraries.
- Canonical model hard-links: `/var/lib/ralf-models/*`; no duplicate model storage.
- `ralf-qwen-small.service`: Qwen2.5 3B on `127.0.0.1:19110`, OpenAI-compatible.
- `ralf-embeddings.service`: Qwen3 Embedding 4B on `127.0.0.1:19111`.
- Main fast-chat already used `llama_cpp`; `RALF_LLAMA_CPP_FALLBACK` is now `none`.
- Main Qwen2.5 7B path points to `/var/lib/ralf-models/qwen2.5-7b/model.gguf`.
- Ollama stays running only until remaining hard-coded callers are migrated.

## Remaining runtime callers
1. Garden detector/deferred reviewer: migrate image `/api/generate` to OpenAI multimodal Gemma service.
2. ABC formula semantic extraction: migrate to `OpenAICompatRuntime` on port 19110.
3. Cheshire bridge: replace `_ollama_generate` planner/coder/final calls with OpenAI-compatible generation.
4. Draft service: replace `OllamaDraftBackend` with llama-server/OpenAI-compatible backend.
5. `local_arch_tools_v1.json`: replace `ollama_on_demand` entries with ports 19110/19111.
6. llama.cpp resource gate/GPU handoff still probes Ollama `/api/ps`; remove after production Ollama caller count reaches zero.
7. Legacy provider code/tests can remain temporarily for rollback but production must not select Ollama.

## Cluster vision
Temistocle and Parmenione are equivalent CPU compute nodes. Run one Gemma reviewer per prepared compute node behind a ClusterIP service. YOLO/tracking stays realtime on Sibilla; only ambiguous frames enter the asynchronous vision queue. Label `ralf-gemma-vision=ready` only after runtime and Gemma files are staged and verified on that node.
