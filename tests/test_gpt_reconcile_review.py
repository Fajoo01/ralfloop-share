from pathlib import Path

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget
from ralfloop_agent.integration.gpt_frontend import GptWorkController
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue


class FakeCdp:
    endpoint = "http://127.0.0.1:9238"

    def __init__(self) -> None:
        self._targets = [
            BrowserTarget(
                "stale-target",
                "page",
                "https://chatgpt.com/c/job",
                "Reviewed",
                "ws://stale-target",
            )
        ]

    def targets(self):
        return list(self._targets)


def test_reconcile_does_not_reactivate_review_from_stale_browser_snapshot(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)
    job = queue.create_job(
        "Reviewed",
        conversation_url="https://chatgpt.com/c/job",
        conversation_context_url="https://chatgpt.com/c/job",
        state=GptJobState.REVIEW,
    )
    controller = GptWorkController(queue, FakeCdp())

    controller.reconcile()

    reviewed = queue.get_job(job.job_id)
    assert reviewed.state is GptJobState.REVIEW
    assert reviewed.target_id is None


def test_reconcile_detaches_review_target_reused_by_another_chat(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)
    job = queue.create_job(
        "Reviewed",
        conversation_url="https://chatgpt.com/c/job",
        conversation_context_url="https://chatgpt.com/g/g-p-demo/c/job",
        target_id="stale-target",
        state=GptJobState.REVIEW,
    )
    cdp = FakeCdp()
    cdp._targets = [
        BrowserTarget(
            "stale-target",
            "page",
            "https://chatgpt.com/c/other-job",
            "Other",
            "ws://stale-target",
        )
    ]
    controller = GptWorkController(queue, cdp)

    controller.reconcile()

    reviewed = queue.get_job(job.job_id)
    assert reviewed.state is GptJobState.REVIEW
    assert reviewed.target_id is None
    assert reviewed.conversation_url == "https://chatgpt.com/c/job"
    assert reviewed.conversation_context_url == "https://chatgpt.com/g/g-p-demo/c/job"
