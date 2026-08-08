# Ralfloop local architecture v1

This is an original local engine inspired by evolutionary program optimization. It is not a clone or reimplementation of Google AlphaEvolve.

Invariant: models propose; deterministic software measures; the evaluator judges; the population selects; Ralf authorizes and orchestrates.

## Flow

`normalizer -> deterministic preflight -> FunctionGemma route proposal -> Ralf policy -> tool/Director/evolver -> sandbox/evaluator -> artifact/CAS -> delta -> result -> approval -> external action`

FunctionGemma only returns Compact Router IR. GLM/Colibrì receives strategic deltas and may return at most three exact assignments. Qwen returns one bounded unified diff. Generated candidate binaries are compiled and run only through bubblewrap. No model can publish or authorize a side effect.

## Compact contracts

Router: `{"v":1,"a":"AF","t":"shortest_path","i":["graph"],"k":["correct"],"c":0.94,"r":"NEW_ALGO"}`.

Worker delta: `{"v":1,"task":"T17","status":"ok","facts":[],"issues":[],"metrics":[],"artifacts":["sha256:..."],"confidence":0.91}`.

Full content remains in the content-addressed artifact store. Cache keys bind input, model, configuration, schema, policy and relevant context hashes. Side effects are never cached.

## Evolver

Required before generation 0: immutable compiling seed, acceptance tests, deterministic evaluator and measurable objectives. SQLite stores experiments, programs, evaluations, parents, mutations, generations, artifacts, metrics, failures, lineages, prompt samples and model calls. Fitness rejects every hard-constraint failure before weighted, Pareto or lexicographic comparison.

Parent selection supports tournament, top-k, novelty, Pareto, lineage diversity and random elite. Stagnation triggers diversity before a bounded Director strategy request. Budgets cover generations, candidates, model calls and wall time.

## Isolation and anti-cheating

Bubblewrap uses a read-only root, empty home/tmp, PID/user/network namespaces, no capabilities, an allowlisted environment and an isolated writable workspace. RLIMITs and systemd cgroup limits bound CPU, RAM, tasks and files. Evaluator, tests and expected outputs are never writable or visible to candidates. Compiler flags, hashes, affinity, seed, environment, warmups, runs, median, p95 and noise are recorded.

## Media

Visual retrieval stores document/page/region/bbox/hash provenance and selects at most three regions. Audiobook planning segments chapters/dialogue and keys TTS cache by text, voice, style, speed, model and config. Social critical text and logos are deterministic SVG overlays. Video begins with storyboard/keyframes/microclips; FFmpeg composes. Local artifacts need no approval; publication always does.

## Resource policy

FunctionGemma may remain CPU-resident. VLM, image, video and TTS workers are on-demand. `gpu_medium`, `gpu_heavy` and `colibri_heavy` are serialized and rejected under swap pressure. Only one heavy model is admitted at a time.

## Deployment

Services are shipped as user-unit templates and are not automatically enabled. FunctionGemma remains optional: deterministic routing and the legacy Colibrì path survive router failure. Release deployment uses immutable commit-named directories, `RELEASE.json`, `MANIFEST.sha256`, an atomic `current` switch and a preserved `previous` rollback target.
