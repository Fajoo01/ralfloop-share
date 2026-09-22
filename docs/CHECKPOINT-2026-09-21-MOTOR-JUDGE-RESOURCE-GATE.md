# Assistant v1 / Motor Judge resource gate checkpoint

Date: 2026-09-21
Branch: `feat/bottazzi-assistant-v1-20260921`

## Motor Judge

- Commit `71feaae` adds fail-closed resource admission for the `19196` sidecar.
- Required headroom: 16 GiB MemAvailable, 1 GiB SwapFree, 6500 MiB free VRAM.
- Startup is denied if Ollama has a loaded model or if `19194`, `19195`, or `19240` is active.
- `/run/ralfloop/inference-gpu.lock` is held for the lifetime of the DS4 process.
- Host cache profile is now 4 GiB unpinned; expert chunk cache 8 GiB unpinned.
- `19194` remained inactive and was never started or reconfigured.

## Live rollout

- Release: `71feaaee8c6056b9a41cf4ee6081ae9e357a3191`.
- Previous release: `dad17d3acd5253e5288deaee568ee8b9009b6b9a`.
- Rollback snapshot: `rollbacks/20260921-063743`.
- Sidecar `19196` passed rollout and is now intentionally stopped/idle; it must not remain resident between Judge calls.
- Canary prompts: 185 / 245 / 305 / 185 tokens.
- Canary durations: 64.037 / 43.702 / 74.263 / 59.450 seconds.
- No kernel OOM occurred during rollout.

## Fast lane

- Existing CPU-only `qwen2.5-3b` service on `19110` is reused instead of loading another Ollama copy.
- Assistant v1 fast-provider canary: 2.45 s wall, provider `assistant_fast_openai_compat`.
- Result stored in `benchmarks/assistant-v1-fast-provider-canary-20260921.json`.
- General/deep lanes remain unpromoted while resource arbitration with the Motor is being completed.


## General lane runtime

- System Ollama backup: `/etc/systemd/system/ollama.service.d/override.conf.pre-vulkan-20260921`.
- `OLLAMA_LLM_LIBRARY` changed from invalid `cuda_v11` to `vulkan`; `OLLAMA_VULKAN=1` enabled.
- Qwen 3.5 offloaded 34/34 layers to the RTX 2070 and unloaded after the request (`keep_alive=0`).
- Cold bounded canary: 16.70 s wall.
- Quality gate failed: APS expansion was correct but the answer cited `Legge 267/1990` as the primary law.
- Legal/administrative general answers therefore remain blocked unless grounded by retrieval/deterministic evidence.
- Result: `benchmarks/assistant-v1-qwen35-vulkan-canary-20260921.json`.

## Grounded normative lane

- APS / ETS / RUNTS informative questions route to `research.deep` through the existing `ModelToolManager` and `deep_web_research_agentcpm_v1` model-tool.
- Profile `italy_third_sector_normative` requires institutional sources, read-only networking, primary evidence, and at least two opened sources before normal completion/fallback policy.
- AgentCPM reuses `/home/sibilla-cumana/.cache/huggingface/hub`; no second model copy is downloaded.
- Final APS canary: PASS, run `5a3a4093d5b647899ea98cbbdcb541d7`, 12.857 s wall.
- Evidence: Ministero del Lavoro `Codice del Terzo Settore`; citations present; `Decreto legislativo 3 luglio 2017 n.117` present.
- Safety gates: `network_mode=read_only`, no approval required, no external write.
- After canary the RTX 2070 returned to 263 MiB used / 7702 MiB free; `19194` and `19196` remained inactive.
- Extractive fallback wording is still rough; no ungrounded Qwen 3.5 synthesis is used for normative authority.

## Regression classification

- Full monolithic suite: 65 failed, 2834 passed, 26 skipped.
- Re-running exactly those 65 failures in a clean process on `HEAD` (`33edab3`) gives 28 failed / 37 passed.
- Re-running the same 65 on the current working tree gives the identical 28 failed / 37 passed set.
- The additional 37 monolithic failures are PyTorch/Triton order-contamination; they pass in a fresh process.
- The 28 persistent failures already exist on `HEAD` and cover historical ABC fixtures, stale absolute paths/checksums, companion integrations, and legacy routing expectations.
- Assistant/model-tool targeted suite: 158/158 passed.
- Assistant real-world routing benchmark: 16/16 passed.
- No regression attributable to the grounded Assistant v1 changes was found.

## Grounded fallback cleanup

- Follow-up after commit `34e5bd1`: the extractive fallback now preserves HTML text-block/newline boundaries before sentence ranking.
- Common navigation fragments such as `Salta al contenuto principale`, `Vai al footer`, and English equivalents are rejected as evidence.
- Complete source lines are considered alongside sentence fragments so legal abbreviations do not destroy otherwise valid evidence.
- Numeric validation now accepts sentence punctuation such as `n.117.` without accepting a prefix of a decimal such as `117` from `117.5`.
- Orphan numeric fragments and claims ending in incomplete `n.` / `art.` / `artt.` are rejected when they do not match the query.
- Extractive answers are rendered as separate cited bullets; no generative paraphrase is introduced.
- Grounded APS quality gate now additionally requires the explicit `associazioni di promozione sociale` expansion and rejects navigation noise.
- Final real canary run: `637811f420b34e13b0392ffb46048d2b`, 12.928 s, PASS.
- Final response contains only the APS/artt. 35 claim and the D.Lgs. 117/2017 anchor, both tied to `[S1]` Ministero del Lavoro.
- Final relevant Assistant/model-tool suite after cleanup: 160/160 PASS; focused fallback/numeric subset: 8/8 PASS.
- The monolithic suite was not re-run after this cleanup because swap is saturated; the earlier HEAD-vs-working-tree classification remains the baseline and no production deploy is being attempted.
- Routing benchmark remains 16/16 PASS.
- After the canary GPU returned to 263 MiB used / 7702 MiB free and `19194` / `19196` stayed inactive.
- Swap is currently saturated (~8 GiB used, ~15 MiB free); no `swapoff`, Motor start, or production promotion was attempted.
- AgentCPM is not resident after the canary; only the lightweight lifecycle broker remains.
