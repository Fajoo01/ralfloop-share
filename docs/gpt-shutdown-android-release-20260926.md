# Android work controls — 2026-09-26

## Releases

- Bot-tazzi 1.4.3 (versionCode 9): shared GPT power control; native Italian TTS with long-text chunks.
- GPT Browser 1.0.6 (versionCode 7): shared power/rest status; notification service stops when GPT is switched off.
- Grande Timoniere 0.3.14 (versionCode 18) was already installed on Xiaomi 24094RAD4G. Its GitHub release branch is `fix/grande-timoniere-launch-visibility-20260926` at `3a9c403`.

## Runtime behavior

`POST /assistant/v1/gpt/power/off` and `/on` use the existing authenticated gateway. The queue persists the switch in SQLite. The frontend and shepherd share a file lock around browser calls, and both check the durable gate. Shutdown closes managed chat tabs while retaining jobs and conversation URLs; resumption remains explicit. Bot-tazzi continues working.

Each GPT job has a persisted 7200-second budget. Rebinding, retries, and power toggles cannot reset it. Exhaustion blocks further browser calls and triggers a global pause of at least 3600 seconds. The existing shepherd checks every 15 seconds; stopping an already generating response therefore depends on that polling interval and browser availability. Pending stop failures keep the gate closed. After the rest, an explicitly resumed job gets a new budget. Work already marked done/cancelled does not trigger a later pause.

The Bot-tazzi web interface preserves the deployed accounting, attachments, authentication recovery, and mobile layout. It adds speech controls, microphone recording with stop/preview/send/cancel, and work closure/archive/reopen/reordering. A chat is linked to an existing backend task queue entry on first submission or queue action. This does not add an autonomous worker that executes every open conversation. Conversation text and archive presentation remain in the existing per-browser storage.

The deployed Bot-tazzi UI used as the compatibility baseline had SHA-256 `1ae208f5bf59eba351e15a8c913484f689650ab4899279b3df4a701fb8a0f5b9`.

## Validation

- 75 Python tests passed across `test_gpt_power.py`, `test_gpt_frontend.py`, and `test_gpt_queue_shepherd.py`.
- Simulated-clock tests cover the exact two-hour boundary, the full one-hour rest, restart persistence, and rejected manual early resume.
- `tests/check_bottazzi_work_ui.py` passes in real headless Chrome with fake microphone/native speech and mocked HTTP: preview/cancel/send, speech/stop, queue closure/reopen, no JavaScript errors.
- Both Android builds passed. Signing certificate matches the previous APKs; no uninstall/data reset is required.
- Live backend power off/on and health were verified against the empty queue. Later user-selected power state is preserved.

## APK integrity

- Bot-tazzi: `480360205b9859efa6733de0be39fbab6ef78bbbb258b2b94be77f7abd14f929`
- GPT Browser: `c836531d84ddd2dd754a0c4c3d22f27498e051f1c77a74e6fa60ace7e1d3b9a1`

## Rollback

Previous GPT runtime remains at `/home/bandi/.local/share/bottazzi-gpt-browser/runtime-99ea506` and the first shutdown runtime at `runtime-476bee4`. The current runtime is selected by `runtime-current`.

The gateway retains `bottazzi_ui.before-work-queue.html` and `bottazzi_gpt_mobile_ui.before-work-budget.html` alongside the deployed templates. Database additions are additive.

## Call recording check

ShizuCallRecorder 1.3.3 is installed; Shizuku was running and its permission was granted when USB was available. The displayed detection mode was InCallService. No test call or recording was started. These facts alone do not confirm that automatic recording is currently enabled or that both audio directions are captured.
# Follow-up fixes — 2026-09-27

- Bot-tazzi 1.4.4 (code 10) keeps the pending WebView microphone request until
  Android returns the permission result. Permission is requested on use, avoiding
  overlapping notification/microphone dialogs at launch. Xiaomi installer confirms
  this version installed. ASR transcribed a synthetic Italian voice sample correctly.
- Tuya broker failed because `scripts/bot-tazzi-tuya-mcp` in the active memory
  release was mode 0444. Restored 0555 and restarted the broker. Real kitchen-light
  state read succeeded with zero writes. Preserve executable bits in future releases.
- Disabled the legacy `bottazzi-browser-bridge.service`; it ignored the queue power
  setting. Chrome now starts on `about:blank` without restoring the prior session.
- Integrated the concurrent provider-selector/Kimi changes through 8c228cf with
  shutdown and work budgets. 100 scoped tests pass, including off-state provider
  opens. No model prompt was sent during validation.
- Concurrent deployments twice replaced `runtime-current` with versions lacking
  power controls. Queue and shepherd now use `runtime-shutdown-verified`, currently
  runtime-9ec225b-integrated, via 90-shutdown-runtime.conf service drop-ins. Future
  upgrades must promote this pointer only after power/budget checks pass.
- Live GET /api/power reports disabled; provider opening is rejected while off.
- Bot-tazzi APK SHA256:
  f75b2ec1e4a02668c304e6b0b240d7d92a5145282e8273ac92d7b9a1244dee13.
