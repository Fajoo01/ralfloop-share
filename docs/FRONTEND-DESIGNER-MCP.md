# Frontend Designer MCP

Reusable UI design and validation MCP for Bot-tazzi/GPT. It composes with Tiremm Remote Desktop rather than replacing desktop control.

## Responsibilities

- inspect an isolated frontend worktree;
- generate a structured design contract;
- apply bounded frontend-only edits through the Programmer agent;
- validate local/private web UIs at mobile, tablet and desktop sizes;
- validate Android emulator-first;
- allow real-phone validation only after a successful matching emulator report.

The MCP does not commit, push or deploy application changes.

## Host configuration

The systemd unit is `deploy/systemd/ralf-frontend-designer-mcp-broker.service` and publishes `/run/ralf-frontend-designer-mcp/mcp.sock` through the standard Ralf MCP broker.

Verified Sibilla runtime paths:

- Android SDK: `/home/sibilla-cumana/Dati/android-sdk`
- AVD home: `/home/sibilla-cumana/Dati/android-avd`
- frontend AVD: `ralf_frontend_ci_api23`
- Tiremm Android MCP: `http://127.0.0.1:19232/mcp`
- MCP HTTP bridge: `/opt/ralf-canva-mcp/node_modules/.bin/mcp-remote`

Worktrees are restricted by `RALF_FRONTEND_WORKTREE_ROOTS`; the production unit uses `/home/sibilla-cumana/tmp`. Evidence is written below `RALF_FRONTEND_ARTIFACT_ROOT`.

## MCP tools

- `frontend_inspect_project`
- `frontend_design_contract`
- `frontend_design_apply`
- `frontend_test_web`
- `frontend_emulator_status`
- `frontend_test_android_emulator`
- `frontend_test_android`
- `frontend_test_android_phone`

For Android, `frontend_test_android_phone` rejects missing, failed or package-mismatched emulator evidence. `frontend_test_android` runs the emulator gate first and reaches the phone only when explicitly requested and the gate passes.

## GPT / Tiremm Remote flow

1. GPT uses Tiremm Remote for machine-level inspection and normal desktop interaction.
2. GPT calls this MCP for UI-specific inspection, design contracts and validation.
3. Mobile candidates are tested on the configured emulator first.
4. The successful `report.json` is the capability token/evidence required before a real-phone validation.
5. Screenshots, UI dumps and reports are reviewed before promotion.

This separation keeps raw computer control in Tiremm Remote and UI reasoning/verification in the Frontend Designer MCP.

## Verification

Run the deterministic tests from the repository environment:

```sh
python -m pytest -q tests/test_frontend_designer_mcp.py tests/test_mcp_live_catalog.py
```

A valid release also requires a live broker/socket check and an Android app-level emulator report before the issue can be closed.
