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
        self.attachments = []

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

    def attach_chatgpt_file(self, target_id, conversation_url, path, *, image_only=False):
        self.attachments.append((target_id, conversation_url, path.name, path.read_bytes(), image_only))
        return {"attached": True}

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


def test_attachment_reopens_review_chat_and_attaches_to_same_conversation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("BOTTAZZI_GPT_UPLOAD_DIR", str(tmp_path / "uploads"))
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Allegati",
        conversation_url="https://chatgpt.com/c/current-chat",
        conversation_context_url="https://chatgpt.com/g/g-p-test/c/current-chat",
        state=GptJobState.REVIEW,
    )
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    result = controller.attach_file(job.job_id, b"pdf-bytes", "documento.pdf", "application/pdf")
    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.conversation_url == "https://chatgpt.com/c/current-chat"
    assert result["action"] == "attached"
    assert cdp.attachments[-1][0].startswith("reopen-")
    assert cdp.attachments[-1][1] == "https://chatgpt.com/g/g-p-test/c/current-chat"
    assert cdp.attachments[-1][2].endswith("documento.pdf")
    assert cdp.attachments[-1][3] == b"pdf-bytes"


def test_completed_reply_is_saved_before_tab_release(tmp_path: Path):
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Work",
        conversation_url="https://chatgpt.com/c/current-chat",
        conversation_context_url="https://chatgpt.com/c/current-chat",
        target_id="managed",
        state=GptJobState.ACTIVE,
    )
    class CompletedCdp(FakeCdp):
        def chatgpt_companion_state(self, target_id):
            return {"focused": False, "busy": False, "composer_chars": 0, "last_assistant_text": "Risposta finale\n[[BOTTAZZI_GOAL_REACHED]]"}

    cdp = CompletedCdp()
    cdp._targets.append(BrowserTarget("managed", "page", "https://chatgpt.com/c/current-chat", "Work", "ws://managed"))
    shepherd = GptQueueShepherd(
        queue,
        cdp,
        policy=GptQueueShepherdPolicy(complete_idle_ms=60000, stalled_idle_ms=180000),
        completion_notifier=lambda title: {"ok": True, "title": title},
    )
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
            return {"focused": False, "busy": False, "composer_chars": 0, "last_assistant_text": "Elaborato per 6m\nStrumenti richiamati\n+1\n[[BOTTAZZI_GOAL_REACHED]]"}

    queue = make_queue(tmp_path)
    job = queue.create_job("Work", conversation_url="https://chatgpt.com/c/current-chat", conversation_context_url="https://chatgpt.com/c/current-chat", target_id="managed", state=GptJobState.ACTIVE)
    fuller = "Testo live sostanziale. " * 40
    queue.set_last_assistant_text(job.job_id, fuller)
    cdp = FooterCdp()
    cdp._targets.append(BrowserTarget("managed", "page", "https://chatgpt.com/c/current-chat", "Work", "ws://managed"))
    GptQueueShepherd(
        queue,
        cdp,
        completion_notifier=lambda title: {"ok": True, "title": title},
    ).run_once(auto_start=False)
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


def test_send_feedback_voice_and_global_queue_order_are_visible() -> None:
    html = (Path(__file__).resolve().parents[1] / "web" / "gpt_queue.html").read_text(encoding="utf-8")
    assert "Inviato · GPT sta lavorando" in html
    assert "state.drafts.delete(id);if(box)box.value=''" in html
    assert "data-action=\"voice\"" in html
    assert "/api/audio/transcribe" in html
    assert "Tieni premuto per parlare" in html
    assert "Registrazione… rilascia per inviare" in html
    assert "document.addEventListener('pointerdown'" in html
    assert "document.addEventListener('pointerup'" in html
    assert "if(a==='voice')return;" in html
    assert "jobs=[...(s.jobs||[])].sort((a,b)=>Number(a.rank)-Number(b.rank))" in html


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
    assert "self._companion_probe_interval_s = 4.0" in frontend
    assert "now - cached[0] < self._companion_probe_interval_s" in frontend


def test_human_input_observers_are_reused_and_throttled() -> None:
    source = (Path(__file__).resolve().parents[1] / "ralfloop_agent" / "integration" / "gpt_browser_cdp.py").read_text(encoding="utf-8")
    assert "if (priorState && priorState.observerTimer) clearTimeout(priorState.observerTimer)" in source
    assert "state.observerTimer = setTimeout" in source
    assert "}, 250);" in source
    assert "const activeStateV3 = window.__bottazziHumanInputTargetV3" in source
    assert "if (activeStateV3 && activeStateV3.observer) activeStateV3.observer.disconnect()" in source
    assert "if (activeStateV3 && activeStateV3.observerTimer) clearTimeout(activeStateV3.observerTimer)" in source


def test_goal_loop_requires_github_as_persistent_diary() -> None:
    root = Path(__file__).resolve().parents[1]
    frontend = (root / "ralfloop_agent" / "integration" / "gpt_frontend.py").read_text(encoding="utf-8")
    shepherd = (root / "ralfloop_agent" / "integration" / "gpt_queue_shepherd.py").read_text(encoding="utf-8")
    requirement = "repository/issue GitHub associato come diario tecnico persistente e fonte di verità"
    assert requirement in frontend
    assert requirement in shepherd
    assert "non affidarti alla sola memoria della chat" in frontend
    assert "non affidarti alla sola memoria della chat" in shepherd


def test_gpt_browser_mobile_surface_is_distinct_from_bottazzi_chat_app() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "openshell_backend" / "bottazzi_gpt_mobile_ui.html").read_text(encoding="utf-8")
    bot_main = (root / "android" / "bottazzi-app" / "app" / "src" / "main" / "java" / "org" / "tiremminnanz" / "bottazzi" / "MainActivity.java").read_text(encoding="utf-8")
    gpt_gradle = (root / "android" / "gpt-browser-app" / "app" / "build.gradle").read_text(encoding="utf-8")
    gpt_manifest = (root / "android" / "gpt-browser-app" / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert "GPT Browser" in html
    assert "Peppone" not in html
    assert "Bot-tazzi" not in html
    assert "webView.loadUrl(BuildConfig.APP_URL);" in bot_main
    assert "gptUiUrl()" not in bot_main
    assert "applicationId 'org.tiremminnanz.gptbrowser'" in gpt_gradle
    assert 'android:label="GPT Browser"' in gpt_manifest

def test_gpt_browser_whatsapp_surface_has_apk_voice_attachments_and_completion_notifications() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "openshell_backend" / "bottazzi_gpt_mobile_ui.html").read_text(encoding="utf-8")
    service = (root / "android" / "gpt-browser-app" / "app" / "src" / "main" / "java" / "org" / "tiremminnanz" / "bottazzi" / "BotTazziNotifyService.java").read_text(encoding="utf-8")
    main = (root / "android" / "gpt-browser-app" / "app" / "src" / "main" / "java" / "org" / "tiremminnanz" / "bottazzi" / "MainActivity.java").read_text(encoding="utf-8")
    assert 'id="attachBtn"' in html
    assert 'id="sendMic"' in html
    assert 'id="apkBtn"' in html
    assert "/gpt-browser.apk" in html
    assert "navigator.mediaDevices.getUserMedia({audio:true})" in html
    assert "r.start(250)" in html
    assert "Registrazione… tocca ■ per inviare" in html
    assert "addEventListener('pointerdown'" not in html
    assert "window.BotTazziNative.speak(text)===true" in html
    assert "speechSynthesis.speak(u)" in html
    assert "public boolean speak(String text)" in main
    assert "textToSpeechReady" in main
    assert "jobs/${j.job_id}/attachment" in html
    assert '"review".equals(current)' in service
    assert '"✅ Finito · "' in service
    assert '"✋ Serve una tua azione · "' in service
    assert '"⏳ GPT in pausa · "' in service
    assert '"🔥 Priorità alta · "' in service
    assert "['⚠️ Avvio non riuscito','err']" in html
    assert "GPT Browser non è riuscito a collegare questo lavoro alla nuova chat. Il lavoro va riprovato." in html
    assert "return'Problema tecnico del lavoro. Apri GPT Browser per vedere cosa serve e riprovare.'" in html
    assert '"job_start_binding_missing".equals(error)' in service
    assert "Bot-tazzi non è riuscito a collegare questo lavoro alla nuova chat. Aprilo per riprovare." in service
    assert '"bottazzi_gpt_notifications_v2"' in service


def test_gpt_rate_limit_hold_survives_tab_recycle_and_warning_stays_visible() -> None:
    root = Path(__file__).resolve().parents[1]
    cdp = (root / "ralfloop_agent" / "integration" / "gpt_browser_cdp.py").read_text(encoding="utf-8")
    assert "BOTTAZZI_GPT_RATE_LIMIT_HOLD_MS" in cdp
    assert "now - heldAt >= rateLimitHoldMs" in cdp
    assert "hideRateLimitUi" not in cdp
    assert "bottazziRateLimitHidden" not in cdp


def test_gpt_browser_mobile_queue_can_add_link_reorder_and_remove_without_server_delete() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "openshell_backend" / "bottazzi_gpt_mobile_ui.html").read_text(encoding="utf-8")
    frontend = (root / "ralfloop_agent" / "integration" / "gpt_frontend.py").read_text(encoding="utf-8")
    assert 'id="chatUrl"' in html
    assert 'id="linkAdd"' in html
    assert "gpt('history/resume'" in html
    assert 'data-qaction="up"' in html
    assert 'data-qaction="down"' in html
    assert 'data-qaction="remove"' in html
    assert "jobs/${id}/rank" in html
    assert "JSON.stringify({state:'cancelled'})" in html
    assert "La conversazione su ChatGPT non verrà cancellata" in html
    assert 'server_chat_deleted": False' in frontend


def test_gpt_browser_surfaces_live_activity_and_bounded_goal_protocol() -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "openshell_backend" / "bottazzi_gpt_mobile_ui.html").read_text(encoding="utf-8")
    frontend = (root / "ralfloop_agent" / "integration" / "gpt_frontend.py").read_text(encoding="utf-8")
    cdp = (root / "ralfloop_agent" / "integration" / "gpt_browser_cdp.py").read_text(encoding="utf-8")
    shepherd = (root / "ralfloop_agent" / "integration" / "gpt_queue_shepherd.py").read_text(encoding="utf-8")
    assert "Attività visibile" in html
    assert "ultimo progresso" in html
    assert "live_activity_text" in html
    assert "live_progress_idle_ms" in html
    assert "goal_status_missing" in html
    assert 'job["live_activity_text"]' in frontend
    assert 'job["live_tool_activity_count"]' in frontend
    assert "status_text: statusText" in cdp
    assert "activity_text: activityText" in cdp
    assert "window.__bottazziGptTelemetryV3 || window.__bottazziGptTelemetryV2" in cdp
    assert "[[BOTTAZZI_GOAL_CONTINUE]]" in shepherd
    assert 'last_error="goal_status_missing"' in shepherd


def test_dedicated_browser_exposes_deepseek_and_kimi_provider_switches(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    html = (root / "web" / "gpt_queue.html").read_text(encoding="utf-8")
    mobile = (root / "openshell_backend" / "bottazzi_gpt_mobile_ui.html").read_text(encoding="utf-8")
    frontend = (root / "ralfloop_agent" / "integration" / "gpt_frontend.py").read_text(encoding="utf-8")
    assert 'data-provider="chatgpt"' in html
    assert 'data-provider="deepseek"' in html
    assert 'data-provider="kimi"' in html
    assert "/api/providers/open" in html
    assert 'data-provider-open="deepseek"' in mobile
    assert 'data-provider-open="kimi"' in mobile
    assert "https://chat.deepseek.com/" in frontend
    assert "https://www.kimi.com/" in frontend
    assert '"queue_managed": name == "chatgpt"' in frontend

    class ProviderCdp(FakeCdp):
        def __init__(self):
            super().__init__()
            self.browser_calls = []

        def _browser_call(self, method, params=None):
            self.browser_calls.append((method, dict(params or {})))
            return {}

    cdp = ProviderCdp()
    cdp._targets.append(BrowserTarget("deepseek-existing", "page", "https://chat.deepseek.com/a/chat/s/test", "DeepSeek", "ws://deepseek-existing"))
    controller = GptWorkController(make_queue(tmp_path), cdp)

    existing = controller.open_provider("deepseek")
    assert existing["target_id"] == "deepseek-existing"
    assert existing["created"] is False
    assert existing["queue_managed"] is False
    assert cdp.browser_calls[-1] == ("Target.activateTarget", {"targetId": "deepseek-existing"})

    created = controller.open_provider("kimi")
    assert created["created"] is True
    assert created["queue_managed"] is False
    kimi_target = next(t for t in cdp.targets() if t.target_id == created["target_id"])
    assert kimi_target.url == "https://www.kimi.com/"

    providers = {row["provider"]: row for row in controller.provider_status()}
    assert providers["chatgpt"]["queue_managed"] is True
    assert providers["deepseek"]["open"] is True
    assert providers["kimi"]["open"] is True
