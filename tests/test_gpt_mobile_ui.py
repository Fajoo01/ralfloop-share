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
        self.interrupted = []

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

    def stop_chatgpt_response(self, target_id):
        self.interrupted.append(target_id)
        return {"stopped": True, "last_assistant_text": "Risposta parziale"}

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
            "context_url": "https://chatgpt.com/g/g-p-test-gpt-browser/c/current-chat",
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
    queue.set_last_assistant_text(job.job_id, "Risposta precedente")
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    result = controller.send_message(job.job_id, "continua")
    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.conversation_url == "https://chatgpt.com/c/current-chat"
    assert rebound.last_assistant_text == ""
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


def test_streaming_reply_is_persisted_while_job_stays_active(tmp_path: Path):
    class StreamingCdp(FakeCdp):
        def chatgpt_ui_state(self, target_id):
            return {"user_turns": 2, "assistant_turns": 1, "response_in_progress": True, "response_pending": True, "response_idle_ms": 1000}

        def chatgpt_companion_state(self, target_id):
            return {"focused": False, "busy": True, "composer_chars": 0, "last_assistant_text": "Nuovo testo live"}

    queue = make_queue(tmp_path)
    job = queue.create_job("Work", conversation_url="https://chatgpt.com/c/current-chat", conversation_context_url="https://chatgpt.com/c/current-chat", target_id="managed", state=GptJobState.ACTIVE)
    cdp = StreamingCdp()
    cdp._targets.append(BrowserTarget("managed", "page", "https://chatgpt.com/c/current-chat", "Work", "ws://managed"))
    GptQueueShepherd(queue, cdp).run_once(auto_start=False)
    saved = queue.get_job(job.job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.last_assistant_text == "Nuovo testo live"


def test_short_final_footer_does_not_replace_fuller_live_snapshot(tmp_path: Path):
    class FooterCdp(FakeCdp):
        def chatgpt_companion_state(self, target_id):
            return {"focused": False, "busy": False, "composer_chars": 0, "last_assistant_text": "Elaborato per 6m\nStrumenti richiamati\n+1"}

    queue = make_queue(tmp_path)
    job = queue.create_job("Work", conversation_url="https://chatgpt.com/c/current-chat", conversation_context_url="https://chatgpt.com/c/current-chat", target_id="managed", state=GptJobState.ACTIVE)
    fuller = "Testo live sostanziale. " * 40
    queue.set_last_assistant_text(job.job_id, fuller)
    cdp = FooterCdp()
    cdp._targets.append(BrowserTarget("managed", "page", "https://chatgpt.com/c/current-chat", "Work", "ws://managed"))
    GptQueueShepherd(queue, cdp).run_once(auto_start=False)
    saved = queue.get_job(job.job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.last_assistant_text == fuller.strip()


def test_project_history_loads_selected_project_chats(tmp_path: Path):
    queue = make_queue(tmp_path)
    controller = GptWorkController(queue, FakeCdp())
    result = controller.project_history("Gpt browser")
    assert result["project"]["title"] == "Gpt browser"
    assert result["count"] == 1
    assert result["chats"][0]["title"] == "Stato browser GPT"
    assert result["chats"][0]["project_name"] == "Gpt browser"
    assert result["chats"][0]["conversation_context_url"] == "https://chatgpt.com/g/g-p-test-gpt-browser/c/current-chat"


def test_failed_project_job_rebinds_exact_context_before_resume(tmp_path: Path):
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Stato browser GPT",
        project_name="Gpt browser",
        project_url="https://chatgpt.com/g/g-p-test/project",
        conversation_url="https://chatgpt.com/c/current-chat",
        conversation_context_url="https://chatgpt.com/g/g-p-test/c/current-chat",
        state=GptJobState.FAILED,
    )
    queue.set_state(job.job_id, GptJobState.FAILED, last_error="conversation_navigation_timeout")
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    result = controller.resume_history_chat(
        "https://chatgpt.com/c/current-chat",
        "Stato browser GPT",
        conversation_context_url="https://chatgpt.com/g/g-p-test-gpt-browser/c/current-chat",
        project_name="Gpt browser",
        project_url="https://chatgpt.com/g/g-p-test/project",
    )
    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.last_error is None
    assert rebound.conversation_context_url == "https://chatgpt.com/g/g-p-test-gpt-browser/c/current-chat"
    assert result["server_chat_created"] is False


def test_interrupt_saves_partial_response_and_keeps_job_active(tmp_path: Path):
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
    controller = GptWorkController(queue, cdp)
    result = controller.interrupt_job(job.job_id)
    saved = queue.get_job(job.job_id)
    assert result["stopped"] is True
    assert cdp.interrupted == ["managed"]
    assert saved.state is GptJobState.ACTIVE
    assert saved.last_assistant_text == "Risposta parziale"


def test_new_job_form_starts_chat_immediately_and_surfaces_launch_errors() -> None:
    html = (Path(__file__).resolve().parents[1] / "web" / "gpt_queue.html").read_text(encoding="utf-8")
    assert "Crea e avvia nuova chat" in html
    assert "auto_start:true" in html
    assert "const launch=result.start||result.pump" in html


def test_done_button_is_highlighted_for_completed_review_only() -> None:
    html = (Path(__file__).resolve().parents[1] / "web" / "gpt_queue.html").read_text(encoding="utf-8")
    assert ".done-ready{" in html
    assert "j.state==='review'&&!j.last_error&&!j.live_busy&&Boolean(completedText)" in html
    assert "data-action=\"done\"" in html
    assert "Risposta conclusa: premi tu per chiudere il lavoro" in html


def test_temporary_access_queue_hold_is_self_clearing() -> None:
    source = (Path(__file__).resolve().parents[1] / "ralfloop_agent" / "integration" / "gpt_browser_cdp.py").read_text(encoding="utf-8")
    assert "localStorage.setItem(holdKey, `rate:${Date.now()}`)" in source
    assert "previousHold.startsWith('rate:')" in source
    assert "localStorage.removeItem(holdKey)" in source
    assert "localStorage.setItem(key, `rate:${Date.now()}`)" in source


def test_live_probe_reads_current_streaming_turn() -> None:
    source = (Path(__file__).resolve().parents[1] / "ralfloop_agent" / "integration" / "gpt_browser_cdp.py").read_text(encoding="utf-8")
    assert "section[data-turn=\"assistant\"]" in source
    assert "[data-streaming-response-status]" in source
    assert "responseInProgress && streamingNode" in source


def test_live_sample_cache_masks_single_probe_failure() -> None:
    root = Path(__file__).resolve().parents[1]
    frontend = (root / "ralfloop_agent" / "integration" / "gpt_frontend.py").read_text(encoding="utf-8")
    html = (root / "web" / "gpt_queue.html").read_text(encoding="utf-8")
    assert "self._companion_cache_ttl_s = 8.0" in frontend
    assert "now - cached[0] <= self._companion_cache_ttl_s" in frontend
    assert 'job["live_cached"]' in frontend
    assert "ultimo campione valido" in html
    assert "aggiornamento in ritardo" in html
