# Bottazzi operational shadow runtime

Date: 2026-09-03. Local/shadow composition only. No deployment or service activation.

`BottazziOperationalRuntime` owns one shared Memory Service, metrics registry, Event Router, Bandi Service, Media Quality Service, Nightly queue and Nightly Worker. Bandi poll results now optionally traverse Event Router before persistence/workflow routing; legacy direct-Memory behavior remains when no router is injected.

Enabled shadow workflows are `bandi.review` and `media.triage`. Both enqueue persistent Nightly jobs and never invoke a model inline. Source inconsistency/stale-practice wakes use the same queue sink. Bando close remains store-only because no approved close workflow is installed.

The runtime exposes explicit `poll_bandi()` and `run_nightly_once()` entrypoints. It owns no daemon, timer, credentials or production paths. Scheduling and source/provider configuration remain external promotion concerns.

Integration evidence proves Bandi and Media events share one persistent queue across restart, shared metrics update, and deterministic Nightly handling uses zero model calls.
