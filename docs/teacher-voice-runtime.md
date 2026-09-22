# Teacher voice runtime and handoff

## Implemented

- `/admin` stores at most ten canonical HTTPS YouTube video references.
- References are metadata, not voice activation. Save is admin-only, CSRF checked
  and audited. No reference sample is served by student or admin HTTP routes.
- Browser speech drives `BottazziAvatarController.bindSpeech`; cancellation resets
  the state. Reduced motion and disabled avatar suppress animation.
- `bindAudio(HTMLAudioElement)` connects a local analyser and returns cleanup.
  The caller must invoke cleanup before replacing the player. No audio is uploaded.
- `bindRecognition` follows actual start/end/error events.
- `VoiceProvider` remains the synthesis contract; cloning is optional.

## Isolated proof runtime

Python 3.11 environment: `~/.local/share/ralf-teacher-voice/venv311`.
Torch and torchaudio 2.6.0 CPU; chatterbox-tts 0.1.6.
Run `scripts/ralf_teacher_voice_synthesize.py --help` with that Python.
It requires explicit consent/rights flags and a reviewed single-speaker WAV,
uses two CPU threads, produces a fixed Italian synthetic-voice disclosure and
refuses to overwrite an existing output. No production GPU service is involved.

A film excerpt can contain several speakers. Do not use its first seconds as a
speaker reference without review. Operator must supply an interval or clean WAV.
Generated proof must be reviewed before registering an active production voice.

## Outstanding, not stable for frontend-only handoff

- Reviewed speaker-only sample and real synthesis quality check.
- Authenticated per-student synthesized-audio serving, quotas/expiry and provider
  wiring into the existing learning pipeline.
- Real audio playback integration (controller analyser adapter is provided).
- Full admin data/RBAC/audit and Italian-language mastery requirements from phase 1.

Do not describe the entire admin foundation as completed: the original three
foundation tests did not prove those requirements. `voice_allowed` is a helper;
it must be enforced at every future synthesis/selection boundary.
