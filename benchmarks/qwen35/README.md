# Bottazzi Qwen3.5 benchmark

Production is not stopped or reconfigured. Experimental servers use port `19196`,
explicit profiles, and a separate Git worktree.

First-pass models:

Weights/config are Qwen upstream; the GGUF quantization artifact is the community
`unsloth/Qwen3.5-35B-A3B-GGUF` conversion and is pinned by size and SHA-256.

| Quant | GGUF bytes | Decision |
|---|---:|---|
| Q4_K_M | 22,016,023,168 | Primary quality candidate |
| Q3_K_M | 16,356,392,960 | Download only if Q4 latency/RAM fails |

Controls:

Q4_K_M expected SHA-256: `3b46d1066bc91cc2d613e3bc22ce691dd77e6f0d33c9060690d24ce6de494375`.

- `legacy`: unchanged production route.
- `qwen35`: no speculation.
- `qwen35_ngram`: `ngram-mod` only.
- `qwen35_experimental`: `draft-mtp` only; combined modes fail closed.
- SSD expert streaming is excluded from phase 1 until a resident-RAM result exists.

JSONL rows are summarized with:

```bash
python3 scripts/bottazzi_llm_summarize.py benchmarks/qwen35/*.jsonl
```

Validate the exact opt-in launch without starting anything:

```bash
set -a
. config/bottazzi_llm_profiles.env.example
set +a
BOTTAZZI_LLM_PROFILE=qwen35 PYTHONPATH=. \
  python3 scripts/bottazzi_llm_profile.py \
  --server /home/sibilla-cumana/src/llama.cpp/build/bin/llama-server
```

`--execute` additionally requires the pinned SHA-256 and loopback host. Production
deployment remains a separate, explicit lifecycle-broker change.
