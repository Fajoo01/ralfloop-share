# Bottazzi selective speculative memory

Production remains `legacy`. This design does not change the deployed runtime.

## Three independent systems

### A. Knowledge retrieval

RAG selects relevant documents and may recommend n-gram pool identifiers. It never
returns a checkpoint identifier.

```text
RetrievalDecision {
  documents: string[]
  ngram_pools: string[]
}
```

### B. Speculative memory

N-grams contain procedural continuation patterns, not authoritative knowledge. Pools
may be `global`, `domain:*`, `project:*`, or `session:*`. The target model verifies
every proposed token. A deterministic selector may return `NGRAM_OFF`.

### C. Recurrent/prompt state

Model state is never selected semantically. Restore requires exact equality of a key
containing model SHA-256, llama.cpp commit, tokenizer identity, chat-template identity,
inference parameters, exact token-prefix hash, and prefix token count. Token IDs are
hashed as signed 32-bit integers with an explicit count; raw text is not hashed.

```text
PromptStateDecision {
  exact_prefix_hash: string
  checkpoint_hit: bool
  checkpoint_id: string | null
}
```

Any mismatch is a miss. Quotas are hard; LRU eviction is bounded by both count and
bytes. The prototype defaults are intentionally conservative: one 16 MiB global pool,
at most 8 project pools, 16 session pools, 256 MiB aggregate speculative metadata.
Checkpoint quotas must be configured from measured state size; the observed recurrent
checkpoint was about 62.8 MiB, so eight checkpoints already require about 502 MiB.

## Installed llama.cpp `ngram-mod` audit

Build `6b80c74f2` creates one `common_ngram_mod` object per speculative server context,
explicitly described in source as shared across all sequences. It contains 4,194,304
`int32_t` entries: 16 MiB. The hash maps an N-token sequence to one following token;
collisions overwrite the previous value.

Lifecycle and mutation:

- constructed and zeroed when the server speculative context starts;
- every request `begin()` inserts all prompt n-grams;
- generation inserts new chunks after the tracked position advances by 32 tokens;
- occupancy above 25% resets the entire pool;
- five consecutive draft rounds below 25% acceptance reset the entire pool;
- destroyed when the server/speculative context exits;
- no save/load or request-reset API exists;
- no mutex exists inside `common_ngram_mod`; caller-side server scheduling serializes
  access in the installed path, so a new multi-pool API must preserve that ownership.

`--lookup-cache-static` and `--lookup-cache-dynamic` are passed only to the distinct
`ngram-cache` implementation. They neither serialize nor partition `ngram-mod`.
`ngram-simple`, map variants, lookup decoding, `ngram-cache`, and `ngram-mod` must not
be treated as aliases.

## Deterministic pool selector

```text
exact session identity -> session:<id>
repository/workspace    -> project:<id>
shell/system task       -> domain:<tool>
otherwise               -> NGRAM_OFF
```

Host and service metadata remain part of benchmark contamination labels; they do not
override a known project/session. No LLM participates in selection.

## Simulation before runtime patch

Because the installed runtime cannot reset or name `ngram-mod`, isolated sequential
server processes simulate pools without keeping duplicate models resident:

- OFF: speculation disabled;
- GLOBAL: prewarm with all project/domain patterns;
- PROJECT: fresh process, relevant project patterns only;
- SESSION: fresh process, exact session history only.

Each process exits before the next starts. This measures the upper bound of pool
selection while preserving production memory bounds. It is not a deployable pool
manager.

## Patch gate

Only propose a llama.cpp patch if PROJECT or SESSION repeatedly beats GLOBAL on
acceptance, decode throughput, correctness, or contamination resistance. The minimal
candidate is one request `pool_id` selecting completely separate fixed-size maps. Do
not begin with layered or weighted lookup. Session→project→global fallback is a later
experiment because it changes collision and lookup cost.
