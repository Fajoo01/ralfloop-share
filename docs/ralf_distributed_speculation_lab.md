# Ralf distributed speculation laboratory

All production integrations are opt-in and disabled by default:

```text
RALF_REMOTE_SPECULATION_ENABLED=0
RALF_DSPARK_DISTRIBUTED_ENABLED=0
RALF_REMOTE_DOMAIN_ASSISTANCE_ENABLED=0
RALF_RECURSIVE_HYBRID_ENABLED=0
```

`remote_token_speculation` and `distributed_dspark` are separate designs.

## Remote token speculation

Temistocle runs fixed `qwen2.5:0.5b` behind the bounded HMAC protocol
`ralf-draft-v1`. The service binds only its VPN address. It exposes health,
tokenization, draft-token, and non-binding advisory endpoints; it never exposes
the full Ollama API.

The installed llama.cpp build supports local `--spec-draft-model`, but has no
external draft-token verification API. In addition, the two installed Qwen
GGUFs have different full vocabularies (151936 versus 152064 entries). The 100
canary strings use equal IDs, but full-vocabulary identity is required for
distribution-preserving speculative decoding. Therefore real remote speculation
is blocked as `tokenizer_incompatible`; normal target decoding is the fallback.

## DSpark contract

The existing checkpoint is a Qwen3-8B DSpark block, not a generic small draft
model. It receives concatenated target hidden states from layers 1, 9, 17, 25,
and 33 plus draft IDs, positions, and a draft `DynamicCache`. It produces seven
full-vocabulary draft logits/probabilities, then the Qwen3 target verifies K+1
tokens.

For a 4096-token prefill, activation payload is:

```text
1 * 4096 * 4096 * 5 * 2 = 167772160 bytes
```

Each seven-token bf16 logits block is 2127104 bytes. The checkpoint alone is
4742170330 bytes. Temistocle has no CUDA and only about 7.6 GiB RAM; the live
target is Qwen2.5 rather than Qwen3. The result is
`distributed_dspark_not_feasible_on_temistocle`. No hidden states are sent.

## Domain and RecursiveMAS boundaries

Remote domain output is advisory. The deterministic local resolver always wins;
the local registry, source jury, and Telegram approval gate remain authoritative.
No real domain is promoted.

`recursive_mas_native` remains entirely on Sibilla and keeps its latent
`inputs_embeds`, hidden-state, adapter, `CrossModelAdapter`, and `gpu_stagewise`
path unchanged. `recursive_mas_text_hybrid` adds only bounded text preprocessing
and a non-binding structured critic; it cannot rewrite the native answer or
authorize tools.

## Benchmark separation

Inference benchmark configurations are llama.cpp baseline, remote token
speculation, distributed DSpark, and existing local DSpark. Agent benchmark
configurations are single Qwen, domains, native RecursiveMAS, text hybrid, and
Codex. Codex is never used as judge and is not compared with DSpark on raw decode
speed. Each agent candidate receives an isolated fixture tree.
