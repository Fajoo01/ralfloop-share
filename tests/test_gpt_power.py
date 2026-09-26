from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError
from ralfloop_agent.integration.gpt_frontend import GptWorkController
from ralfloop_agent.integration.gpt_power import PowerGuardedCdp
from ralfloop_agent.integration.gpt_queue_shepherd import GptQueueShepherd
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue
from test_gpt_frontend import FakeCdp, request, running_frontend


def test_power_survives_restart_and_blocks_shepherd(tmp_path):
    queue = GptWorkQueue(tmp_path / "queue.db")
    job = queue.create_job("Keep me", prompt="Work")
    queue.set_power_enabled(False)
    reopened = GptWorkQueue(queue.path)
    assert not reopened.power_enabled()
    report = GptQueueShepherd(reopened, object()).run_once()
    assert report["power_off"] and report["actions"] == []
    assert reopened.get_job(job.job_id).state is GptJobState.QUEUED


def test_power_endpoints_require_existing_mutation_guard(tmp_path):
    queue = GptWorkQueue(tmp_path / "queue.db")
    cdp = FakeCdp()
    with running_frontend(queue, cdp) as (port, origin):
        status, _ = request(port, "POST", "/api/power/off", origin=origin)
        assert status == 403 and queue.power_enabled()
        status, _ = request(port, "POST", "/api/power/off", origin=origin, payload={},
                            extra_headers={"Origin": "https://untrusted.invalid"})
        assert status == 403 and queue.power_enabled()
        status, body = request(port, "POST", "/api/power/off", origin=origin, payload={})
        assert status == 200 and not body["enabled"]
        status, body = request(port, "GET", "/api/snapshot", origin=origin)
        assert status == 200 and not body["power"]["enabled"]
        status, _ = request(port, "POST", "/api/pump", origin=origin, payload={})
        assert status == 409 and not cdp.started
        status, body = request(port, "POST", "/api/power/on", origin=origin, payload={})
        assert status == 200 and body["enabled"]


def test_shutdown_waits_for_inflight_and_rejects_next_browser_call(tmp_path):
    queue = GptWorkQueue(tmp_path / "queue.db")
    entered, release = threading.Event(), threading.Event()
    class SlowCdp:
        def send(self):
            entered.set()
            assert release.wait(3)
    guarded = PowerGuardedCdp(SlowCdp(), queue)
    controller = GptWorkController(queue, SlowCdp())
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(guarded.send)
        assert entered.wait(2)
        stopping = pool.submit(controller.set_power, False)
        # The durable gate closes before shutdown waits on the active call.
        for _ in range(100):
            if not queue.power_enabled():
                break
            threading.Event().wait(.01)
        assert not queue.power_enabled()
        assert not stopping.done()
        release.set()
        pending.result(timeout=2)
        assert stopping.result(timeout=2)["ok"]
    with pytest.raises(CdpError, match="power_off"):
        guarded.send()


@pytest.mark.parametrize("provider", ["chatgpt", "deepseek", "kimi"])
def test_provider_open_respects_shutdown(tmp_path, provider):
    queue = GptWorkQueue(tmp_path / "queue.db")
    queue.set_power_enabled(False)
    cdp = FakeCdp()
    with running_frontend(queue, cdp) as (port, origin):
        status, body = request(port, "POST", "/api/providers/open", origin=origin,
                               payload={"provider": provider})
        assert status == 409 and body["error"] == "gpt_browser_power_off"
        assert not cdp.started
    with pytest.raises(CdpError, match="power_off"):
        GptWorkController(queue, cdp).open_provider(provider)


def test_shutdown_stops_managed_chat_and_preserves_queue(tmp_path):
    queue = GptWorkQueue(tmp_path / "queue.db")
    cdp = FakeCdp()
    stopped = []
    cdp.stop_chatgpt_response = lambda target: stopped.append(target)
    controller = GptWorkController(queue, cdp)
    job = queue.create_job("Keep history", prompt="Work")
    cdp._targets.append(BrowserTarget("managed", "page", "https://chatgpt.com/c/kept", "ChatGPT", "ws://managed"))
    queue.bind_chat(job.job_id, conversation_url="https://chatgpt.com/c/kept",
                    target_id="managed", state=GptJobState.ACTIVE)
    assert controller.set_power(False)["ok"]
    assert stopped == ["managed"] and cdp.closed == ["managed"]
    assert queue.get_job(job.job_id).conversation_url == "https://chatgpt.com/c/kept"
    assert controller.set_power(False)["ok"]  # Idempotent across both apps.
    assert controller.set_power(True)["enabled"]


def test_two_hour_budget_forces_global_hour_rest_and_survives_restart(tmp_path):
    now = [100_000]
    queue = GptWorkQueue(tmp_path / "queue.db", clock=lambda: now[0])
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    job = queue.create_job("Long work", prompt="Work")
    cdp._targets.append(BrowserTarget("managed", "page", "https://chatgpt.com/c/long", "ChatGPT", "ws://managed"))
    queue.bind_chat(job.job_id, conversation_url="https://chatgpt.com/c/long",
                    target_id="managed", state=GptJobState.ACTIVE)
    now[0] += 7199
    assert queue.work_budget_status()["rest_remaining_seconds"] == 0
    now[0] += 1
    with pytest.raises(CdpError, match="cooldown"):
        controller.cdp.queue_human_message("managed", "https://chatgpt.com/c/long", "Continue")
    assert not cdp.messages
    report = GptQueueShepherd(queue, cdp).run_once()
    assert report["cooldown"]["rest_remaining_seconds"] == 3600
    assert cdp.closed == ["managed"]
    assert queue.get_job(job.job_id).last_error == "work_limit_reached"
    reopened = GptWorkQueue(queue.path, clock=lambda: now[0])
    assert reopened.work_budget_status()["rest_remaining_seconds"] == 3600
    assert not GptWorkController(reopened, cdp).set_power(True)["ok"]
    now[0] += 3599
    with pytest.raises(ValueError, match="cooldown"):
        reopened.start_work_budget(job.job_id)
    now[0] += 1
    reopened.start_work_budget(job.job_id)
    assert reopened.work_budget_status()["rest_remaining_seconds"] == 0
    assert GptWorkController(reopened, cdp).set_power(True)["ok"]


def test_rebinding_and_manual_power_toggle_cannot_reset_work_budget(tmp_path):
    now = [100_000]
    queue = GptWorkQueue(tmp_path / "queue.db", clock=lambda: now[0])
    job = queue.create_job("Keep budget", prompt="Work")
    queue.start_work_budget(job.job_id)
    now[0] += 7100
    queue.set_power_enabled(False)
    queue.set_power_enabled(True)
    queue.start_work_budget(job.job_id)
    now[0] += 100
    assert queue.work_budget_status()["rest_remaining_seconds"] == 3600
