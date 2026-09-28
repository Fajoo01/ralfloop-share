#!/usr/bin/env python3
import asyncio, json, os, time, traceback
from pathlib import Path
from ralf_remote_commander.internal_android_server import android_package_status, android_package_install, android_package_prepare

DEVICE = os.environ.get("TIREMM_ANDROID_WATCHDOG_DEVICE", "9120ca9c-0972-424b-81a9-14baf5483a7b")
STATE = Path("/home/bandi/.local/state/tiremm-android-package-watchdog.json")
REOPEN_AFTER = 90

async def main():
    status = await android_package_status(DEVICE)
    if not isinstance(status, dict):
        return
    phase = str(status.get("phase") or "")
    now = int(time.time())
    state = {}
    if STATE.exists():
        try: state = json.loads(STATE.read_text())
        except Exception: state = {}
    if phase == "installed":
        if STATE.exists(): STATE.unlink()
        return
    if phase == "ready":
        await android_package_install(DEVICE)
        state["last_reopen"] = now
    elif phase == "pending_user_action":
        last = int(state.get("last_reopen", 0) or 0)
        if now - last >= REOPEN_AFTER:
            await android_package_install(DEVICE)
            state["last_reopen"] = now
    elif phase == "error" and "VERIFICATION_FAILURE" in str(status.get("detail") or ""):
        url = str(status.get("source_url") or "")
        sha = str(status.get("sha256") or "")
        pkg = str(status.get("package") or "")
        if url and sha and pkg:
            await android_package_prepare(url, sha, pkg, DEVICE)
            state["last_reopen"] = now
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        detail = "".join(traceback.format_exception(exc))
        if "Resource conflict" not in detail:
            raise
