from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from ralfloop_agent.integration.gpt_browser_cdp import CdpError
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


def test_shutdown_stops_managed_chat_and_preserves_queue(tmp_path):
    queue = GptWorkQueue(tmp_path / "queue.db")
    cdp = FakeCdp()
    stopped = []
    cdp.stop_chatgpt_response = lambda target: stopped.append(target)
    controller = GptWorkController(queue, cdp)
    job = queue.create_job("Keep history", prompt="Work")
    queue.bind_chat(job.job_id, conversation_url="https://chatgpt.com/c/kept",
                    target_id="managed", state=GptJobState.ACTIVE)
    assert controller.set_power(False)["ok"]
    assert stopped == ["managed"] and cdp.closed == ["managed"]
    assert queue.get_job(job.job_id).conversation_url == "https://chatgpt.com/c/kept"
    assert controller.set_power(True)["enabled"]
