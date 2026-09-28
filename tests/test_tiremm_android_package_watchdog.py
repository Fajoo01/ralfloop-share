import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

FAKE = types.ModuleType("ralf_remote_commander.internal_android_server")
async def _unused(*args, **kwargs):
    return None
FAKE.android_package_status = _unused
FAKE.android_package_install = _unused
FAKE.android_package_prepare = _unused
PARENT = types.ModuleType("ralf_remote_commander")
PARENT.internal_android_server = FAKE
sys.modules.setdefault("ralf_remote_commander", PARENT)
sys.modules.setdefault("ralf_remote_commander.internal_android_server", FAKE)

SCRIPT = Path(__file__).parents[1] / "scripts" / "tiremm_android_package_watchdog.py"
SPEC = importlib.util.spec_from_file_location("tiremm_watchdog", SCRIPT)
watchdog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(watchdog)


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        watchdog.STATE = Path(self.tmp.name) / "state.json"

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def run_case(self, status, now=1000, state=None):
        calls = {"install": 0, "prepare": []}
        if state is not None:
            watchdog.STATE.write_text(json.dumps(state))
        async def package_status(device):
            return status
        async def package_install(device):
            calls["install"] += 1
        async def package_prepare(url, sha, package, device):
            calls["prepare"].append((url, sha, package, device))
        watchdog.android_package_status = package_status
        watchdog.android_package_install = package_install
        watchdog.android_package_prepare = package_prepare
        watchdog.time.time = lambda: now
        await watchdog.main()
        return calls

    async def test_ready_reopens_installation(self):
        calls = await self.run_case({"phase": "ready"})
        self.assertEqual(calls["install"], 1)
        self.assertTrue(watchdog.STATE.exists())

    async def test_pending_user_action_reopens_only_after_90_seconds(self):
        calls = await self.run_case({"phase": "pending_user_action"}, now=1089, state={"last_reopen": 1000})
        self.assertEqual(calls["install"], 0)
        calls = await self.run_case({"phase": "pending_user_action"}, now=1090, state={"last_reopen": 1000})
        self.assertEqual(calls["install"], 1)

    async def test_verification_failure_restages(self):
        status = {
            "phase": "error",
            "detail": "VERIFICATION_FAILURE",
            "source_url": "https://example.invalid/app.apk",
            "sha256": "abc123",
            "package": "it.tiremminnanz.navigatore",
        }
        calls = await self.run_case(status)
        self.assertEqual(len(calls["prepare"]), 1)
        self.assertEqual(calls["prepare"][0][0], status["source_url"])
        self.assertEqual(calls["prepare"][0][1], status["sha256"])
        self.assertEqual(calls["prepare"][0][2], status["package"])

    async def test_installed_clears_state_and_stops(self):
        calls = await self.run_case({"phase": "installed"}, state={"last_reopen": 1})
        self.assertEqual(calls["install"], 0)
        self.assertEqual(calls["prepare"], [])
        self.assertFalse(watchdog.STATE.exists())


if __name__ == "__main__":
    unittest.main()
