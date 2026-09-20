# ACT 2026 real-bando checkpoint

Date: 2026-09-21
Branch: `feat/motor-bando-pipeline-20260921`

Real local sources used:
- ACT 2026 regulation: rules/source of eligibility constraints.
- Tiremm ACT 2026 submitted form: applicant/project evidence.
- Budget, logical framework, statute: supporting evidence.

Privacy rule: source text stays local. Committed benchmark reports contain hashes, metrics and verdict metadata only.

Pipeline correction from the real dossier:
- each chunk now gets a section-local goal;
- a chunk must not request facts that belong to other sections;
- final aggregation is fail-closed with REJECT > REQUEST_REVIEW > PASS;
- only a global PASS with all gates open can proceed.

Real application preflight after correction:
- 29 chunks;
- max rendered prompt: 359 tokens;
- total rendered prompts: 9,733 tokens;
- guard fallbacks: 0.

Live sample:
- start/middle/end sections were exercised on Judge 19196;
- no CUDA/prefill crash in these samples;
- one middle response hit the 72-token generation cap and was correctly marked runtime-incomplete;
- chunk prompt was tightened to limit missing-evidence verbosity.

Focused real financial check:
- extracted total cost: 100,000 EUR;
- extracted requested ACT contribution: 25,000 EUR (25%);
- extracted total listed income: 50,000 EUR;
- regulation: requested contribution must be 75,000-150,000 EUR and 20%-75% of total costs;
- rendered prompt: 273 tokens;
- Motor live verdict: REJECT;
- risk: HIGH;
- confidence: 0.90;
- runtime complete: true;
- gate closed;
- no CUDA errors; response ended with `finish=stop`.

Runtime restoration after canary:
- 19196 stopped;
- 19240 restored from saved command/environment;
- 19194 remained inactive and untouched.
