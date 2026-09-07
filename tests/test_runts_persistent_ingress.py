"""Meowgram's two HTTP calls, persistent observations, real MCP and staging.

Synthetic READ providers and document preparer; no network or financial DB.
"""
import hashlib
import ast
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
from openshell_backend import app as backend
from ralfloop_agent.unified_assistant import runtime, pec_runts_telegram
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.pec_runts import PecRuntsService
from ralfloop_agent.unified_assistant.event_router import RoutedEvent, EventOrigin, event_id
from test_runts_response_workflow import Runts, Pec, NOW


TEXT = "Controlla la pratica RUNTS 2603942 e prepara la risposta con il Modello D corretto. Mostrami esattamente il messaggio e l'allegato prima di inviare."


def test_meowgram_http_prepare_reobserves_persistent_sources_and_stages(tmp_path, monkeypatch):
    for key, value in {
        "RALFLOOP_UNIFIED_ASSISTANT": "1",
        "BOTTAZZI_RUNTS_WRITE_ENABLED": "0",
        "RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE": "1",
        "RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS": "11",
        "RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS": "11",
        "RALFLOOP_OPERATIONAL_MEMORY_PATH": str(tmp_path / "memory.sqlite"),
        "RALFLOOP_UNIFIED_SESSION_DIR": str(tmp_path / "sessions"),
        "RALFLOOP_TELEGRAM_APPROVAL_DB": str(tmp_path / "approval.sqlite"),
        "RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(backend, "audit", lambda *a, **kw: None)
    def forbidden(*a, **kw):
        raise AssertionError("WRITE executor must not be constructed")
    monkeypatch.setattr(runtime, "_runts_writer", forbidden)
    reader, pec = Runts(), Pec()
    artifact = tmp_path / "synthetic.pdf"
    artifact.write_bytes(b"%PDF-1.4\nSynthetic fixture, not a financial document\n%%EOF")
    proposal = {
        "proposal_id": "proposal.synthetic", "practice_id": "2603942",
        "authoritative_message_id": "523278", "status": "READY_FOR_HUMAN_APPROVAL",
        "blockers": [], "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "review_context": {"pdf_path": str(artifact)},
        "final_validation": {"exercise": 2025},
        "provenance": [reader.message.source.model_dump(mode="json")],
        "proposed_reply": "Synthetic review response", "attachments": [], "writes": 0,
    }
    def prepare(*args):
        return SimpleNamespace(model_dump=lambda **kw: proposal)
    monkeypatch.setattr(pec_runts_telegram, "execute_telegram_read", lambda text, **kw:
        pec_runts_telegram.execute_telegram_prepare(text, **kw, pec_provider=pec,
            runts_provider=reader, response_preparer=prepare))
    client = TestClient(backend.app)
    context = {"source": "telegram_natural", "telegram_user_id": 11,
               "telegram_chat_id": 11, "telegram_message_id": 1001}
    payload = {"user_goal": TEXT, "extra_context": context}
    route = client.post("/tasks/run", json={**payload, "mode": "route_only"}).json()
    assert route["capability_route"]["task_mode"] == "tool_backed_prepare"
    # When Meowgram is installed, execute its actual two-step client method;
    # replace only HTTP transport, never Telegram sends or the backend router.
    meowgram = Path("/srv/projects/Meowgram/src/meowgram/bot.py")
    call_meowgram = None
    if meowgram.is_file():
        tree = ast.parse(meowgram.read_text())
        method = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                      and n.name == "_call_ralfloop_natural_backend")
        namespace = {"requests": SimpleNamespace(post=lambda url, **kw:
                     client.post(url, json=kw["json"]))}
        method.returns = None
        for arg in method.args.args:
            arg.annotation = None
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(meowgram), "exec"), namespace)
        call_meowgram = namespace[method.name]
    for iteration in range(2):
        if call_meowgram:
            result = call_meowgram(SimpleNamespace(_ralfloop_natural_base_url=lambda: "http://testserver"),
                TEXT, SimpleNamespace(sender_id=11, chat_id=11, message=SimpleNamespace(id=1001)))
        else:
            result = client.post("/tasks/run", json=payload).json()
        assert result["ok"], result
        assert result["capability"] == "runts_prepare_practice_response"
        assert result["approval_required"] and result["human_review_required"]
        assert "Approvo 2603942" in result["response"]
        meta = result["metadata"]
        assert meta["practice_status"] == "TRA"
        assert meta["pending_action"] == "runts_practice_reply"
        assert meta["proposal"]["status"] == "READY_FOR_HUMAN_APPROVAL"
        assert meta["proposal"]["authoritative_message_id"] == "523278"
        assert meta["mcp_invoked"] and meta["writes"] == 0
        from ralfloop_agent.unified_assistant.conversation import SessionConversationAdapter
        pending = SessionConversationAdapter(runtime._session_store()).load("telegram-11-11").state.pending.runts
        assert pending.payload["body"] in result["response"]
        assert pending.payload["pdf_sha256"] in result["response"]
        assert pending.approval_ref
        now = NOW + timedelta(minutes=iteration + 1)
        reader.message = reader.message.model_copy(update={"observed_at": now,
            "source": reader.message.source.model_copy(update={"observed_at": now.isoformat()})})
    assert reader.writes == pec.writes == 0


def test_changed_source_revision_is_not_deduplicated(tmp_path):
    reader = Runts()
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        service = PecRuntsService(memory, Pec(), reader)
        service._put_runts(reader.message)
        changed = reader.message.model_copy(update={
            "source": reader.message.source.model_copy(update={"content_hash": "c" * 64}),
            "body": "New authoritative revision", "content_hash": "d" * 64,
        })
        service._put_runts(changed)
        assert memory.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_legacy_production_event_reobservation_keeps_original_evidence(tmp_path):
    reader = Runts()
    message = reader.message
    entity_id = "runts.message." + hashlib.sha256(message.native_id.encode()).hexdigest()[:24]
    payload = {"event_type": "RUNTS_MESSAGE_DISCOVERED"}
    old = RoutedEvent(event_id=event_id("runts", message.native_id, "RUNTS_MESSAGE_DISCOVERED", payload),
        event_type="RUNTS_MESSAGE_DISCOVERED", origin=EventOrigin.API_WATCHER,
        source="runts", source_id=message.native_id, occurred_at=NOW, observed_at=NOW,
        entity_refs=(entity_id,), payload=payload, provenance=(message.source,)).memory_event()
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        memory.append_event(old)
        now = NOW + timedelta(hours=1)
        reread = message.model_copy(update={"observed_at": now,
            "source": message.source.model_copy(update={"observed_at": now.isoformat()})})
        PecRuntsService(memory, Pec(), reader)._put_runts(reread)
        assert memory.get_event(old.event_id) == old
        assert memory.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
