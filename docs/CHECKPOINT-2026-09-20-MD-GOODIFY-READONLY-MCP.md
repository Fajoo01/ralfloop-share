# MD / Goodify — read-only protocol MCP checkpoint

Date: 2026-09-20
Branch: `feat/md-goodify-mcp-20260920`

## Goal completed

The reverse-engineered MD Goodify protocol is now represented in Bot-tazzi
without implementing any live `purchasedonation` request.

Added a pure protocol module:

- `ralfloop_agent/unified_assistant/md_goodify_api.py`

It performs no network I/O. It can:

- parse the APK response shape `payload[0].Donation[]`;
- normalize only the 15 verified Gson fields;
- build the local `getdonation` body `{token}`;
- build the local `purchasedonation` body `{token, qr_code}`;
- produce a redacted MCP preview without receiving the access token.

## MCP surface

Added two semantic tools:

- `md_goodify_parse_api_response`
- `md_goodify_build_purchase_payload`

`md_goodify_build_purchase_payload` accepts only `qr_code`; it returns a
redacted template with `token_source=runtime_access_token`. Raw access tokens
are therefore not passed through the MCP argument/result path.

## Safety boundary

There is no HTTP client or submit method in the protocol module. The
`purchasedonation` URL is documented as protocol metadata only.

No donation, purchase or redemption was performed during development.
No phone interaction or traffic capture was required.

## Validation

Targeted tests: `14 passed`.
The production MCP runtime was not restarted or changed in this checkpoint.

## Live rollout

Released commit `19757dd` to:

`/home/sibilla-cumana/ralf-md-goodify-mcp/releases/19757dd`

and atomically switched `current` to that release.

Restarted only:

- `ralf-md-goodify-mcp-broker.service`
- `ralf-md-goodify-worker.service`

`ralfloop-backend.service` remained active.

Live broker smoke test from the production UID reports 7 tools. Both new tools
returned successfully with zero side effects. The payload builder reported
`submission_performed=false` and did not receive or expose a raw access token.
Post-restart journal contains clean stop/start events and no errors.

## Read-only getdonation wiring

Added `ralfloop_agent/unified_assistant/md_goodify_readonly.py`.
The only live MD/Goodify API operation implemented there is donation-history
reading via `catalogomdapp.dedagroupwiz.it:443` and exact path
`/api/goodify/getdonation`.

The token is loaded internally from a private regular file, default:
`/var/lib/ralfloop/md-goodify/access-token`.
It is rejected if the file is a symlink, has group/world permissions, has an
unexpected owner, is empty, or is oversized. The token is never an MCP input,
result field, log field, fixture, or Git-tracked value.

New MCP tool: `md_goodify_get_donations`, with an empty input schema.
It reports `network_requests=1`, `mutations=0`,
`submission_performed=false`, and parsed donation history only.

## One-time normal MD login enrollment

Added `md_goodify_auth.py` and `scripts/ralf_md_goodify_enroll.py`.
Enrollment binds only to `127.0.0.1:19197`, uses a random form nonce,
disables request logging, and never persists the email/password.
It calls only the APK-confirmed `POST /auth/login` on
`api.platform-backend.mdspa.it` with the APK-confirmed Android headers.

On success only `access-token` and `refresh-token` are stored under
`/var/lib/ralfloop/md-goodify/` with mode `0600`; MCP never accepts them as
arguments. The client API key is extracted locally from the installed APK into
a `0600` runtime file and its value is not present in Git.

Targeted suite after this addition: 20 passed.
