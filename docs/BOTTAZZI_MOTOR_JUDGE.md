# Bot-tazzi Motor Judge

Bot-tazzi Motor Judge integrates the local DeepSeek V4.1 Flash runtime as a bounded process judge inside Ralfloop. It is not a chat interface and it is not an executor.

## Pipeline

```text
route -> deterministic evidence -> verification dossier
      -> Bot-tazzi Motor Judge -> validated verdict
      -> deterministic gate -> next stage / human review
```

The model never receives an execution capability. A model `PASS` can only advance the pipeline stage. It cannot authorize email, browser, filesystem, payment, RUNTS, PEC, or any other external side effect.

## Verdict contract

The provider accepts a `JudgeCase` containing a goal, structured facts, authoritative rules, candidate actions, and optional candidate answer. It returns only a validated verdict with:

- `decision`
- `confidence`
- `risk`
- `reason`
- `missing_evidence`

Any malformed output, unlisted decision, low confidence, missing evidence, runtime failure, or explicit `UNCERTAIN` fails closed.
## Current integration point

`patch_allowed` routes use `verification_policy.verifier_type=combined` with `judge_provider=bottazzi_motor`.

The verification adapter sends only bounded structured evidence to the model. Raw stdout and patch bodies are not included by default; their presence and lengths are supplied instead. Additional reviewed facts can be passed explicitly through `context["judge_facts"]`.

The gate remains authoritative after the model returns. External actions are still controlled independently by the existing human-confirmation policy.

## Sibilla canary

Validated on the isolated Bot-tazzi Motor sidecar at `127.0.0.1:19196`, with production remaining on `19194`.

Judge workload profile used for the successful pipeline canary:

- `prefill_chunk=128`
- `DS4_CUDA_LOW_VRAM_STAGE_MB=768`
- `DS4_CUDA_LOW_VRAM_RESERVE_MB=512`
- expert window 32
- four expert reader threads
- frame-direct expert pack I/O

The live pipeline dossier was 279 prompt tokens. DeepSeek returned `REQUEST_REVIEW`, confidence `0.86`, risk `MEDIUM`; the deterministic gate blocked progression because evidence was incomplete. Total model request time was 123.878 s, including 47 generated tokens at 0.87 token/s.

## Service deployment

The judge sidecar is designed to run as a separate systemd service on `127.0.0.1:19196`.
It never replaces or restarts the production DeepSeek service on `19194`.

Deployment assets:

- `deploy/systemd/bottazzi-motor-judge.service`
- `deploy/systemd/bottazzi-motor-judge.env.example`
- `ralfloop_agent.integration.bottazzi_motor_judge_service`

Copy the env example to `/etc/ralfloop/bottazzi-motor-judge.env` and review the binary,
model and expert-pack paths before enabling the unit. The launcher rejects production ports,
checks all three artifacts, binds loopback only, and uses the validated `128` prefill / `768 MiB`
staging profile by default.

The service uses `Restart=on-failure`, a dedicated DS4 lock file, and waits for the judge listener
to become reachable before systemd marks startup complete. Ralfloop itself remains fail-closed if
the judge sidecar is unavailable.
