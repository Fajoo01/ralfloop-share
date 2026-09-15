# Teacher dialogue checkpoint — 2026-09-15

## Goal

Make Bot-tazzi respond to the student's actual objection/counterexample before repeating a generic lesson, while keeping deterministic support software behind internal MCP boundaries.

## Architecture

Student-facing Teacher MCP surface remains exactly 13 `teacher.*` tools.

Internal deterministic Teacher Core MCP now exposes exactly 5 read-only tools:
- `core.math_check`
- `core.study_plan`
- `core.text_profile`
- `core.extractive_summary`
- `core.classify_turn`

`core.classify_turn` is implemented in C, bounded to 6000 input characters, has no writes or external side effects, and returns a structured move such as `counterexample`, `correction`, `confusion`, `request_example`, `question`, or `neutral`.
## Behavioural fix

The activity question is no longer flattened together with the exercise text. The student's turn is sent separately from bounded activity context (topic, objective, visible prompt/items/choices).

When the Core MCP marks a turn as a counterexample or correction, pedagogy switches to `error_analysis` while preserving the learner's access mode (including L2/low-literacy and Scholar routing). The system contract requires the tutor to address the objection first, state whether it is correct/partial/incorrect, repair oversimplified rules, then reconnect to the lesson.

Regression case: `ma un fazzoletto prende la forma del contenitore ma non è liquido cosa c'entra il ghiaccio` is classified as `counterexample` with confidence >= 0.85.

## UI

The activity page now hides the distant global avatar and places a larger Bot-tazzi surface directly beside the tutor feedback and the `La tua domanda o obiezione` textarea. Avatar state remains synchronized with thinking/speaking and Fish audio-driven jaw movement.

## Gates

- Targeted dialogue/Core tests: 82 passed.
- Expanded Core/pedagogy tests: 19 passed.
- Full Teacher regression: 204 passed, 4 skipped.
- `git diff --check`: clean.
- Student surface: 13 tools; internal Core surface: 5 tools.
