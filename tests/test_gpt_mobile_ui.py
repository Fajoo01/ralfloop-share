from pathlib import Path

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget
from ralfloop_agent.integration.gpt_frontend import GptWorkController
from ralfloop_agent.integration.gpt_queue_shepherd import GptQueueShepherd, GptQueueShepherdPolicy
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue


class FakeCdp:
    endpoint = "http://127.0.0.1:9238"

    def __init__(self):
        self._targets = []
        self.closed = []
        self.messages = []
        self.installed = []
        self.created = 0

    def targets(self):
        return list(self._targets)

    def chatgpt_companion_state(self, target_id):
        return {"focused": False, "busy": True, "composer_chars": 0, "last_assistant_text": "Risposta finale"}

    def chatgpt_ui_state(self, target_id):
        return {"user_turns": 1, "assistant_turns": 1, "response_in_progress": False, "response_pending": False, "response_idle_ms": 70000}

    def close_target(self, target_id):
        self.closed.append(target_id)
        self._targets = [t for t in self._targets if t.target_id != target_id]

    def create_chatgpt_target(self, *, clear_cache=False, background=True):
        self.created += 1
        tid = f"reopen-{self.created}"
        self._targets.append(BrowserTarget(tid, "page", "https://chatgpt.com/", "ChatGPT", f"ws://{tid}"))
        return tid

    def navigate_chatgpt_conversation(self, target_id, url):
        for i, target in enumerate(self._targets):
            if target.target_id == target_id:
                self._targets[i] = BrowserTarget(target_id, "page", url, "Chat", target.websocket_url)
                return {"authenticated": True, "ready": True}
        raise RuntimeError("missing")

    def install_human_input_target(self, target_id, conversation_url):
        self.installed.append((target_id, conversation_url))
        return {"ok": True}

    def queue_human_message(self, target_id, conversation_url, text):
        self.messages.append((target_id, conversation_url, text))
        return {"queued": True}

    def create_target(self, url, *, background=False):
        self.created += 1
        tid = f"project-{self.created}"
        self._targets.append(BrowserTarget(tid, "page", url, "Projects", f"ws://{tid}"))
        return tid

    def resolve_project_url(self, target_id, project_name, *, wait_timeout_s=12.0):
        assert project_name == "Gpt browser"
        return "https://chatgpt.com/g/g-p-test/project"

    def project_conversation_records(self, target_id, *, project_url, wait_timeout_s=8.0):
        return [{
            "url": "https://chatgpt.com/c/current-chat",
            "context_url": "https://chatgpt.com/g/g-p-test/c/current-chat",
            "title": "Stato browser GPT",
            "project_id": "g-p-test",
        }]


def make_queue(tmp_path: Path):
    return GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1000000)


def test_review_message_reopens_same_conversation(tmp_path: Path):
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Stato browser GPT",
        conversation_url="https://chatgpt.com/c/current-chat",
        conversation_context_url="https://chatgpt.com/g/g-p-test/c/current-chat",
        state=GptJobState.REVIEW,
    )
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    result = controller.send_message(job.job_id, "continua")
    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.conversation_url == "https://chatgpt.com/c/current-chat"
    assert cdp.messages[-1][2] == "continua"
    assert result["action"] == "queued"


def test_completed_reply_is_saved_before_tab_release(tmp_path: Path):
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Work",
        conversation_url="https://chatgpt.com/c/current-chat",
        conversation_context_url="https://chatgpt.com/c/current-chat",
        target_id="managed",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp()
    cdp._targets.append(BrowserTarget("managed", "page", "https://chatgpt.com/c/current-chat", "Work", "ws://managed"))
    shepherd = GptQueueShepherd(queue, cdp, policy=GptQueueShepherdPolicy(complete_idle_ms=60000, stalled_idle_ms=180000))
    shepherd.run_once(auto_start=False)
    saved = queue.get_job(job.job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.last_assistant_text == "Risposta finale"
    assert saved.target_id is None


def test_project_history_loads_selected_project_chats(tmp_path: Path):
    queue = make_queue(tmp_path)
    controller = GptWorkController(queue, FakeCdp())
    result = controller.project_history("Gpt browser")
    assert result["project"]["title"] == "Gpt browser"
    assert result["count"] == 1
    assert result["chats"][0]["title"] == "Stato browser GPT"
    assert result["chats"][0]["project_name"] == "Gpt browser"
