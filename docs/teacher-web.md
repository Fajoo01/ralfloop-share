# Bot-tazzi Teacher student application

## Boundaries and operation

`ralfloop_agent/teacher/web` provides a mobile web app, same-origin student API,
SQLite learning state, versioned curriculum, deterministic adaptive selection,
original interactive activities, material library and audio preparation.
The existing Teacher MCP remains exactly 13 tools; no administrative interfaces
are imported or published. All routes derive student identity from an opaque
HttpOnly SameSite cookie. The browser cannot select a tool, Teacher student ID,
session ID, path, transport, shell command or service operation.

The new application's SQLite database is the source of truth for profile,
credentials, web sessions, mastery, attempts, XP ledger, badges, daily challenges,
materials, mappings, plans and audio metadata. Existing Teacher storage retains
its established diagnostic records; its progress is never combined with web XP
or used as the web mastery source. Raw card IDs are not stored. A stable hashed
card association and scrypt credential separate identity from authentication.
Teacher receives only a stable web pseudonym and school context.

Learning writes use SQLite WAL and `BEGIN IMMEDIATE`; request deduplication,
completed-activity checks and content fingerprints prevent repeated awards.
Identical completed content carries no additional evidence or XP for seven days.
Daily XP is capped. Flashcard flips, refreshes, chat, simulation sliders and audio
controls never award XP. Retry success has reduced mastery evidence and a small
recovery bonus. Every awarded point has a deduplicated ledger entry.

Model activities must pass a strict Pydantic contract. Existing Teacher text
responses are wrapped in controlled semantic activity data; choice activities
require a complete validated content object. Malformed nested JSON gets at most
one retry, then original curriculum fallback. A material-specific generation
failure never presents fallback as content from that student's book. Semantic
grading unavailable means no update or reward. Fixed-choice tasks grade locally.
No model HTML or JavaScript executes. CSP and text-only DOM rendering apply.
A conservative Fraction-based arithmetic guard independently verifies simple
fraction addition/subtraction and missing-numerator exercises: model mistakes
cannot reverse a recognized numeric result. This is not a general symbolic
checker; other semantic judgments retain model limitations.

## Local start

Use the existing Python environment or install `.[teacher-web]`. No new database
server, node bundler or frontend framework is required.

```bash
/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python scripts/ralf_teacher_web.py --demo --seed-demo --db /home/bandi/.local/state/ralf-teacher-web-demo/student.sqlite3
```

Open `http://127.0.0.1:19139/login`. Explicit non-sensitive demo credentials:

| Card | Credential | Profile |
| --- | --- | --- |
| DEMO-PRIMARY | StudioDemo!2026 | Primary, grade 4 |
| DEMO-MIDDLE | StudioDemo!2026 | Middle, grade 2 |
| DEMO-UPPER | StudioDemo!2026 | Upper, grade 2, liceo |

Omit `--demo` to use the actual local Teacher MCP. `--seed-demo` is explicit;
production never seeds public credentials automatically. Operator enrollment:
`scripts/ralf_teacher_web.py --enroll`; the credential uses hidden terminal input.
There is deliberately no educator/admin HTTP endpoint.

## Curriculum and teaching references

Catalog version `2026.09.11.1` has the full 5/3/5 grade structure, all three upper
school track categories and eight subject names. Implemented original pilot
topics: equal parts, equivalent fractions, states of water, uniform motion.
The rest of the programme is **not implemented**. Missing coverage is never
generated as an invented national requirement. Grade placement is editorial and
needs educator review, especially technical/professional track-specific paths.

Official references checked 2026-09-11:

- [MIM, 2012 first-cycle indications](https://www.mim.gov.it/documents/20182/51310/DM+254_2012.pdf).
- [Official 2026 transition regulation](https://www.gazzettaufficiale.it/atto/serie_generale/caricaArticoloDefault/originario?atto.codiceRedazionale=26G00021&atto.dataPubblicazioneGazzetta=2026-01-27&atto.tipoProvvedimento=DECRETO): new first-cycle indications start with first primary/middle classes in 2026/27; intermediate cohorts continue under previous indications through completion. Do not relabel all current middle-school years as the new cohort.
- [Licei regulation reference, DM 211/2010](https://www.normattiva.it/atto/caricaDettaglioAtto?atto.codiceRedazionale=010G0232&atto.dataPubblicazioneGazzetta=2010-12-14&currentPage=1): reference only, not a claim that the pilot covers every current upper-school programme.
- [Wordwall features](https://wordwall.net/features): content can be reused with different presentation templates. This implementation uses original content and renderers.
- [Teaching with PhET](https://phet.colorado.edu/en/teaching-resources/teaching-with-phet): inquiry and exploration motivate the prediction → manipulation → observation → explanation flow. Native fraction and uniform-motion computations; no copied PhET code, assets or embedded resources.

## Frontend, materials and audio

Routes: `/login`, `/home`, `/study`, `/activity`, `/quiz`, `/simulations`, `/books`,
`/progress`, `/badges`, `/audio`, `/profile`. Controlled renderers cover all 13
requested initial game types plus simulation. The `fractions` pilot exposes every
renderer so each presentation path is reachable end-to-end; content and presentation
remain independent. Matching/definition/grouping share accessible selection controls. Memory combines ungraded card exploration and
scored associations. Timed challenge is an optional personal pace cue; time does
not affect mastery. A two-minute countdown is optional and non-punitive.

The owner-provided Bot-tazzi JPEG is preserved unchanged in `static/bot-tazzi.jpeg`.
The ivory, charcoal and red palette follows that reference, with original CSS.
No claim of independent licensing is made for the supplied artwork.

Materials accept text registration or UTF-8 `.txt` import (10,000 characters,
30 materials/student), title, chapter, pages, rights assertion and curriculum
keyword mapping. PDF extraction, OCR, automated rights verification and full book
conversion are not included. Original authorized demo books are provided.

`teacher.prepare_reading` → validated chunks → normalization/segmentation →
`TTSProvider` → per-student tracks → audio page. `BrowserTTS` uses installed browser
speech when available. `PendingTTS` provides a truthful testable provider-required
state. **No server speech engine or downloadable audiobook is claimed.** Playback,
pause/resume, rate, chapters and stored character position are supported; saved
position uses character offsets, not fabricated audio seconds. Browser voices
may be absent and are device-dependent. No XP derives from playback.

## Tests

```bash
/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/pytest -q tests/test_teacher_web.py tests/test_teacher_mcp.py tests/test_teacher_capabilities.py tests/test_teacher_shared_inference.py tests/test_teacher_systemd.py tests/test_teacher_tcp_bridge.py
TEACHER_WEB_LIVE=1 /home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/pytest -q tests/test_teacher_web_live.py
TEACHER_WEB_BROWSER=1 /home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/pytest -q tests/test_teacher_web_browser.py
# To test an isolated local demo without touching the installed service:
# TEACHER_WEB_BASE_URL=http://127.0.0.1:19140 TEACHER_WEB_BROWSER=1 .../pytest -q tests/test_teacher_web_browser.py
```

Live and browser tests skip by default; live test creates only demo pedagogical
records, requests one controlled fraction exercise (at most one format retry)
and one semantic correction. Browser test requires the seeded local demo server,
Chrome and the cached ChromeDriver (override `TEACHER_CHROMEDRIVER` if needed).
It uses a new temporary browser profile. Screenshots are written outside Git to
`~/.local/state/ralf-teacher-web-demo/evidence/`.

## Release and rollback

`tools/install_teacher_web.py` reuses the existing immutable release builder and
manifest format, in `~/.local/share/ralf-teacher-web/releases/<commit>`. Without
flags it only prepares and verifies the release. `--install` backs up existing
web SQLite state, publishes the **web-only** `current` symlink and installs the
non-root `systemctl --user` service `ralf-teacher-web.service`. Environment file
is mode 0600, binding is always `127.0.0.1:19139`, health is `/health`, logs use the
user journal. On failed health, the previous web release is restored. Rollback:
`--install --rollback <previous-40-character-commit>`. Additive schema rollback
retains data; restore a backup only after stopping the web service, when needed.

On this host `PrivateTmp=yes` in a **user** service caused `Permission denied`
when connecting to the owner's protected MCP socket. The web unit therefore
keeps `NoNewPrivileges` and private state permissions without `PrivateTmp`.
The existing MCP socket permissions and all existing Teacher units are unchanged.

No shared production/current switch, existing Teacher restart, VPN modification
or k3s modification is needed. Public access requires an independently chosen
HTTPS reverse proxy/origin; configure `TEACHER_WEB_ORIGIN` to the exact HTTPS
origin and preserve its Host header. The backend must stay private. No external
publication has been assumed. User-service persistence across logout/reboot
depends on the host's existing user manager/linger policy.

## Residual scope

Full national curriculum and subject content, educator assignment workflows,
PDF/OCR, server TTS, advanced simulations/providers, comprehensive personalisation
from books, exact error classification reliability, persistent study-session
narratives, curriculum editorial review and public deployment remain future work.
The current daily mission is five distinct completed activities; richer weak-topic
missions are not yet chosen automatically. Mastery is transparent evidence, not
a standardized school grade. Simulations require real Teacher for semantic
reflection; demo mode explicitly cannot grade arbitrary explanations.
