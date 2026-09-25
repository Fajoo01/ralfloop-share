# Rizzo Flow point-and-click benchmark — 2026-09-25

## Scope

Goal: evaluate `Rizzo-AI-Academy/rizzo-flow` as a low-latency semantic target selector for BrowserGPT / Browser MCP. No production browser write path was changed.

Upstream tested: Rizzo Flow commit `c30cc633f5bb0cde920e0998fad4f617ed138f44` with Spark-X2.5-4B Q4_K_M on Vulkan. Sibilla's existing production `llama-server` stayed running and untouched.

The test corpus was captured read-only from live Browser MCP accessibility snapshots across Google Forms, WordPress, Google Search, WireGuard UI, Google Sheets and YouTube settings. The raw snapshot contained live field values and was deleted after sanitization. `benchmarks/browser_point_click_real_sanitized.json` retains only role, accessible label, ref, visibility/disabled state, bounding box and benchmark goals/expected refs.

## Baselines

Existing Browser MCP benchmark:

- routing: ~28 ms p50;
- persistent Playwright click primitive: ~613 ms p50;
- resident-pool approval+execute: ~848 ms p50.

Rizzo synthetic point-and-click:

- Spark 1.7B Q4 forced choice: 48/60 = 80%, ~358 ms median;
- Spark 4B Q4 forced choice: 60/60 = 100% on the simple synthetic set, ~840 ms median.

Rizzo as a general Bot-tazzi judge is rejected: on `jev_semantic_heldout_v2` it scored 32/42 = 76.19% with 9 false PASS cases.

## Real Browser MCP corpus

### Choice across broad candidate sets

With up to 25 UI candidates plus an explicit `no_action` option, Spark strongly over-selected `no_action`; that formulation is unsuitable for point-and-click.

Forced choice on the real corpus:

| Variant | Model cases | Model accuracy | End-to-end accuracy | Median model latency |
|---|---:|---:|---:|---:|
| shortlist K=8 | 43 | 30/43 = 69.8% | 30/46 = 65.2% | ~1,091 ms |
| shortlist K=3, first ranker | 41 | 36/41 = 87.8% | 36/46 = 78.3% | ~785 ms |
| shortlist K=3, improved ranker | 43 | 38/43 = 88.4% | 38/46 = 82.6% | ~801 ms |
| three boolean questions instead of one choice | 41 | 33/41 = 80.5% | 33/46 = 71.7% | ~1,418 ms |

The boolean-per-candidate formulation is slower and less accurate; reject it.

### Deterministic prefilter

A small BM25/IDF-style ranker over accessible names and roles achieved:

- top-1: 34/46 = 73.9%;
- top-3 recall: 44/46 = 95.7%;
- conservative gate (`score >= 4`, `margin >= 5`): 19/19 correct on this corpus.

A separate lexical/semantic gate used with the improved K=3 Rizzo results reached 41/46 = 89.1% end-to-end on this corpus. This is the best observed hybrid result, but the corpus is too small to justify production activation.

Rizzo confidence is not a safe gate. Wrong choices were observed at confidence >= 0.99, so confidence must not authorize a click.

## Architecture decision

Keep the existing Browser MCP safety pipeline authoritative. Semantic target selection must be a pure resolver:

`accessibility snapshot -> deterministic candidate filter -> optional Rizzo choice among <=3 candidates -> exact ref -> existing approval/hash/stale-ref checks -> browser_click`

Rules:

1. Never let Rizzo call browser tools directly.
2. Never let Rizzo bypass exact snapshot refs.
3. Disabled/off-screen candidates are removed before model evaluation.
4. A model result outside the provided shortlist fails closed.
5. Rizzo targeting remains disabled by default (`RALFLOOP_BROWSER_RIZZO_TARGETING=0`).
6. Existing pre-snapshot hash binding, approval CAS, no blind retry and post-action readback remain unchanged.
7. Do not add Rizzo in front of an already-resolved exact ref; it would only add latency.

## Experimental implementation

`ralfloop_agent/unified_assistant/browser_target_selector.py` provides:

- accessibility candidate parsing;
- visibility/disabled filtering;
- deterministic BM25/IDF-style ranking;
- conservative deterministic fast path;
- optional Rizzo HTTP choice client;
- strict shortlist membership validation and fail-closed fallback;
- no browser write capability.

`browser_target_shadow.py` adds the production-safe observation stage:

- asynchronous bounded queue; no Rizzo latency on the authoritative click path;
- exact current target remains authoritative;
- raw page snapshots, typed values and raw user goals are never persisted;
- Rizzo endpoint is restricted to loopback HTTP;
- empty/meaningless exact-ref commands are skipped instead of polluting the dataset;
- JSONL events record only hashes, refs, shortlist membership, proposal source, confidence and latency;
- `tools/browser_rizzo_shadow_report.py` summarizes live accuracy and separates shortlist misses from selector misses.

Shadow flags:

- `RALFLOOP_BROWSER_RIZZO_SHADOW=1` enables observation;
- `RALFLOOP_BROWSER_RIZZO_TARGETING=0` keeps live target selection disabled;
- `RALFLOOP_BROWSER_RIZZO_ENDPOINT=http://127.0.0.1:18017/v1/decisions`;
- `RALFLOOP_BROWSER_RIZZO_SHADOW_AUDIT=.../browser-rizzo-shadow.jsonl`.

Tests: selector, shadow privacy/fail-open and Browser MCP adapter/approval regressions are green.

## Promotion criteria

Do not enable in production yet. Before promotion:

- expand the sanitized corpus substantially with duplicate labels, icon-only controls, nested menus, dialogs, stale refs and scrolling;
- measure target accuracy separately from click execution latency;
- require zero safety regressions in disabled/stale target cases;
- compare against the current semantic target selection on exactly the same snapshots;
- test CUDA when a compatible runtime/driver path is available;
- activate first as shadow mode, logging only resolver proposals, then canary behind the feature flag.
