# Teacher C Core + Avatar checkpoint — 2026-09-14

Branch: `teacher/universal-tutor-20260914`
Base checkpoint: `20d95a6`

## Deterministic-first execution

Internal `ralf-teacher-core-mcp` is written in C11 and is not exposed to students.
It currently provides four bounded read-only tools:

- `core.math_check`: arithmetic/fraction parsing and numeric equivalence.
- `core.study_plan`: deterministic pedagogical timeboxing.
- `core.text_profile`: reading-density metrics and chunk sizing.
- `core.extractive_summary`: source-faithful bounded sentence extraction.

Teacher uses these paths first and falls back to Qwen only when the input is not safely recognized.
## Measured result

500 real UNIX-MCP `math_check` calls, including connect/initialize/tool discovery:
median 0.257 ms, p95 0.322 ms, p99 0.377 ms, max 0.402 ms.

The end-to-end Teacher MCP path was verified without an inference socket:
`teacher.check_answer` and `teacher.study_plan` returned `deterministic_core` results.
Tests use a model stub that raises if invoked, proving model bypass on accepted fast paths.

## Avatar

The avatar has real `idle`, `thinking`, `speaking`, `listening`, success and error states.
Fish/server audio drives a smoothed Web Audio RMS jaw rig; browser speech has a procedural fallback.
The lower-face overlay is clipped and animated independently, so the whole portrait is no longer scaled.
Chrome geometry check: 112×112 aligned rig, active jaw transform, zero severe console errors.
This is amplitude-driven jaw animation, not phoneme/viseme lip-sync.
## Next C candidates

Promote only deterministic, high-frequency tasks with a fail-closed contract:

1. exact/unit-aware numeric checker for science problems;
2. Italian orthography/accent rule checks backed by the Grammar DB;
3. curriculum prerequisite and mastery routing;
4. bounded lexical retrieval/ranking over approved local material;
5. deterministic exercise templates and distractor validation.

Do not move semantic explanation, ambiguous grading, Scholar critique or L2 dialogue into C.

## Fish 1.5 GPU lifecycle hardening — 2026-09-15

- Local Peppone synthesis was validated against Fish Speech 1.5: 44.1 kHz mono WAV, 4.412 s audio from a short phrase, 13.441 s synthesis on RTX 2070.
- A concurrent transient DS4 benchmark reproduced CUDA OOM on the 8 GB GPU; therefore Fish must not assume exclusive VRAM.
- FishTTSCache now applies a bounded free-VRAM gate before queueing and immediately before generation. Default deployed threshold: 1024 MiB.
- Generation subprocess timeout is bounded to 30–300 s (150 s deployed default), replacing the former 43,300 s timeout.
- Optional local lifecycle management controls only the fixed `ralf-teacher-fish15.service`: cold-start with POST `/v1/health`, then idle stop after 300 s by default.
- Web installer defaults the helper to the Fish 1.5 venv because the general Teacher venv intentionally does not carry Fish-specific `ormsgpack`.
- The Fish service unit is installed but disabled for boot when lifecycle management is enabled; student input never controls service names or arbitrary commands.
