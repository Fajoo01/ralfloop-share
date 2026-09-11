# Bot-tazzi Teacher — verified delivery, 2026-09-11

| Area | Implemented and verified | Remaining scope |
| --- | --- | --- |
| Problem | Existing Teacher MCP extended into an operational local student tutor | Full national production rollout is not claimed |
| Architecture | Student UI → authenticated API → learning engines/SQLite → exact 13-tool Teacher client | Separate educator product remains future work |
| Frontend | Eleven routes, mobile 390×844, controlled renderers, error feedback, keyboard controls | Wider device/accessibility audit |
| Branding | Original supplied Bot-tazzi JPEG, matching ivory/charcoal/red design | Supplied artwork licensing remains owner's responsibility |
| Card login | Stable hashed card association, scrypt credential, opaque HttpOnly SameSite session; three explicit demo students | Real students require operator enrollment |
| Curriculum | Versioned 5/3/5 grade structure, three upper-track categories, eight subject registry entries | Four original pilot topics only; full programmes and editorial validation pending |
| Adaptive tutor | Visual entry, matching, guided/independent practice, prerequisite selection, error response and spaced review | Richer personalized sequences and automatic return from prerequisite remediation |
| Games | Multiple choice, true/false, free answer, matching, grouping, ordering, fill blank, flashcards, memory, definition match, sequence, timed challenge, guided exercise | Some presentations share matching controls; no claim of Wordwall parity |
| Gamification | Transactional XP ledger, idempotency, repeated-content protection, levels, badges, streak, daily goal | Current mission is five distinct activities; richer mission selection pending |
| Mastery | Evidence counts, bounded score, reduced retry evidence, difficulty and review transitions | Not a standardized assessment; general semantic accuracy limited by model |
| Simulations | Native fractions and constant-speed motion; prediction required before observation, then reflection grading | Advanced providers and STEM simulations pending |
| Books/materials | Owned text/book records, chapters/pages, curriculum mapping, summaries, exercises, quiz preparation | No PDF/OCR; rights assertion is not automatic license verification |
| Audio | Real MCP prepare_reading, segmentation, TTS abstraction, tracks, browser player controls and stored position | No server speech engine/downloadable audiobook; browser voice required |
| Security | Server identity, ownership, CSRF/origin, input limits, no generic tool/file/shell endpoint, no frontend tokens, strict surface check | External publication requires chosen HTTPS origin and reverse proxy |
| Storage | SQLite WAL, transactional schema, private files, migration/reopen/backup tests | Long-term retention policy and operator tools can be expanded |
| Deploy | Non-root user service, immutable separate web release, protected env, SQLite backups, health and rollback | Loopback only; no external student URL selected |
| Production | Four existing Teacher services active; MCP reachable; protected bridge retained | Shared production release changed externally during this task; never switched by this installer |

## Evidence

| Run | Pass | Fail | Skip |
| --- | ---: | ---: | ---: |
| Scoped Teacher regression and new web/core tests | 137 | 0 | 2 |
| Release/manifest/rollback tests | 4 | 0 | 0 |
| Real API → MCP → generated exercise → check_answer → XP/mastery | 1 | 0 | 0 |
| Chrome mobile frontend E2E, renderers, simulation and error handling | 1 | 0 | 0 |
| Installed-service Chrome smoke, material/audio and cross-student isolation | 1 | 0 | 0 |

**144 distinct passing tests.** The two opt-in skips in the scoped run were later
executed successfully in their dedicated runs. No unresolved test failure.
Integration-worktree verification additionally passed 67 new application/core/
deploy tests (overlapping the above total). The broad Ralfloop suite was not run.

Real tests found and fixed:

1. Legacy Teacher output did not reliably support nested JSON. Existing text
   responses now enter a controlled, validated semantic activity envelope;
   malformed activity JSON retries once, then safely falls back.
2. Teacher incorrectly rejected `1/2 + 1/2 = 1`. Exact fraction arithmetic now
   independently controls recognized numeric results; general semantic mistakes
   remain a limitation, not a solved problem.
3. User-service `PrivateTmp=yes` denied access to the existing protected MCP
   socket. Removed only from the new web unit; existing socket permissions and
   Teacher services were preserved. `NoNewPrivileges` remains enabled.
4. Rollback now restores both the previous release symlink and previous unit.

## Commits and integration

| Teacher branch | Integration branch | Change |
| --- | --- | --- |
| `f44ebdc` | `dbdc86c` | Persistent learning engines and MCP orchestration |
| `346a7f2` | `7035c68` | Bot-tazzi frontend, authenticated API, tests |
| `6825183` | `f25ee11` | Isolated releases and rollback |
| `c7715d6` | `1097565` | User-service protected socket compatibility |

The final rollback/unit-restoration test and this report are a further focused
commit; exact installed commit is recorded in the immutable web release's
`RELEASE.json`, reached through the `current` link below.

ATM/Gmail unstaged diff SHA-256 before and after integration:
`88d8040e14b3622ba092a0aab3a783f069299f852a5ad6455d80bf37dc72070f`.
No ATM/Gmail changes were staged, reformatted, stashed or committed by this work.

## Running installation

- Service: `systemctl --user status ralf-teacher-web.service`
- Login: <http://127.0.0.1:19139/login>
- Health: <http://127.0.0.1:19139/health> → `{"status":"ok"}`
- Binding: `127.0.0.1:19139`, never `0.0.0.0`
- Release: `/home/bandi/.local/share/ralf-teacher-web/current`
- Database: `/home/bandi/.local/state/ralf-teacher-web/student.sqlite3`
- Environment: `/home/bandi/.config/ralf-teacher-web.env`, mode `0600`
- Backups: `student.sqlite3.backup-<timestamp>`, mode `0600`
- Logs: `journalctl --user -u ralf-teacher-web.service`
- User manager already had `Linger=yes`; no linger policy changed.

Three demo cards and their explicit non-sensitive credential are in
[the operator guide](teacher-web.md). They also exist in the local installed DB.
The demo browser has separate state under `ralf-teacher-web-demo`; the persistent
service uses the real MCP client and its own DB.

Shared `/home/sibilla-cumana/ralfloop-production/current` was observed at
`c3a35df...` initially and `99fb605...` later, due to concurrent external work.
This task never published that symlink or restarted existing Teacher services.
No WireGuard, PSK, AllowedIPs, routing, FunctionGemma or Kubernetes changes.

Mobile screenshots: `/home/bandi/.local/state/ralf-teacher-web-demo/evidence/`
(`login-mobile.png`, `home-mobile.png`, `activity-mobile.png`, `simulation-mobile.png`).
Supplied and packaged logo SHA-256 match:
`745671bd1ce146f451ff8aff63e8b5f09f6cdfdb423ddc253d0b7948551dc456`.

## Verified markers (pilot/local scope)

```text
TEACHER_CARD_LOGIN_READY
TEACHER_STUDENT_SESSION_ISOLATION_OK
TEACHER_CURRICULUM_ENGINE_READY
TEACHER_STUDENT_STATE_READY
TEACHER_GAMIFICATION_ENGINE_OK
TEACHER_XP_ANTIFARMING_OK
TEACHER_MASTERY_ENGINE_OK
TEACHER_GAME_ENGINE_READY
TEACHER_SIMULATION_ENGINE_READY
TEACHER_BOOK_LIBRARY_READY
TEACHER_MATERIAL_CURRICULUM_MAPPING_READY
TEACHER_AUDIO_PIPELINE_READY_PROVIDER_REQUIRED
TEACHER_FRONTEND_READY
TEACHER_FRONTEND_E2E_OK
TEACHER_STUDENT_ISOLATION_OK
TEACHER_FRONTEND_LOCAL_HEALTHY
```

These markers describe tested pilot capabilities, not completion of every
curriculum, game feature, semantic evaluation case or deployment environment.
