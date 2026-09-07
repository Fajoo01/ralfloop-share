"""Shadow Telegram workflow. Live financial integration requires an explicit local binding."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import os

import pytest

from ralfloop_agent.unified_assistant.pec_runts import RuntsPractice, RuntsMessage
from ralfloop_agent.unified_assistant.pec_runts_telegram import decision_for_telegram, execute_telegram_prepare
from ralfloop_agent.unified_assistant.platform import SourceRef
from ralfloop_agent.unified_assistant.runts_response_prepare import RuntsPrepareBinding, RuntsSuiteResponsePreparer
from ralfloop_agent.unified_assistant.runtime import unified_route_probe

TEXT="Controlla la pratica RUNTS 2603942, prepara la risposta con il Modello D corretto e mostrami tutto per approvazione."
NOW=datetime(2026,9,7,tzinfo=timezone.utc)


class Pec:
    writes=0
    def list_messages(self, *, limit):return ()


class Runts:
    writes=0
    def __init__(self, message_hash="a"*64):
        self.calls=[]
        source=SourceRef(system="runts",native_id="523278",locator="synthetic://runts/message",observed_at=NOW.isoformat(),content_hash=message_hash)
        self.message=RuntsMessage.build(native_id="523278",practice_id="2603942",subject="Sanitized contract: Model D correction",
            body="Correct presentation; reply through existing practice messaging.",published_at=NOW,observed_at=NOW,source=source)
    def get_practice(self, identity):
        self.calls.append("get_practice")
        return RuntsPractice(native_id=identity,status_raw="TRA",title="Synthetic balance practice",updated_at=NOW,observed_at=NOW,source=self.message.source,content_hash="b"*64)
    def list_messages(self, practice_id, *, limit):
        self.calls.append("list_messages");return (self.message,)
    def get_message(self, identity):
        self.calls.append("get_message");assert identity==self.message.native_id;return self.message


def test_telegram_prepare_rag_and_route_stay_typed(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT","1")
    d=decision_for_telegram(TEXT)
    assert d.tool_id=="runts_prepare_practice_response" and d.arguments.practice_id=="2603942"
    assert unified_route_probe(TEXT,{"source":"telegram_natural"})["task_mode"]=="tool_backed_prepare"
    with pytest.raises(ValueError):type(d).model_validate(d.model_dump()|{"execute":True})


def test_unconfigured_prepare_fails_closed_without_write(tmp_path, monkeypatch):
    monkeypatch.delenv(
        "BOTTAZZI_RUNTS_PREPARE_BINDING",
        raising=False,
    )
    result=execute_telegram_prepare(TEXT,memory_path=tmp_path/'memory.sqlite',pec_provider=Pec(),runts_provider=Runts())
    assert not result["ok"] and "RUNTS_PREPARE_NOT_CONFIGURED" in result["response"]
    assert result["metadata"]["writes"]==0


def test_telegram_shadow_end_to_end_real_generator_golden_and_unchanged_db(tmp_path, monkeypatch):
    binding_path=os.getenv("BOTTAZZI_TEST_RUNTS_BINDING")
    if not binding_path:pytest.skip("Explicit local read-only financial/golden binding required")
    binding=RuntsPrepareBinding.model_validate_json(Path(binding_path).read_text()).model_copy(update={"output":str(tmp_path/'artifacts')})
    db=Path(binding.suite)/'runts_suite.db';before=hashlib.sha256(db.read_bytes()).hexdigest()
    pec,runts=Pec(),Runts(binding.authoritative_message_hash)
    result=execute_telegram_prepare(TEXT,memory_path=tmp_path/'memory.sqlite',pec_provider=pec,runts_provider=runts,response_preparer=RuntsSuiteResponsePreparer(binding))
    assert result["ok"],result
    p=result["metadata"]["proposal"]
    assert (p["practice_id"],p["authoritative_message_id"],p["status"])==("2603942","523278","READY_FOR_HUMAN_APPROVAL")
    assert runts.calls==["get_practice","list_messages","get_message","get_practice"]
    assert Path(p["review_context"]["pdf_path"]).is_file()
    assert p["final_validation"]["semantic_sha256"]==p["final_validation"]["golden_semantic_sha256"]
    assert p["proposed_reply"] and not p["executable"]
    assert result["human_review_required"] and not result["approval_required"]
    assert "PDF:" in result["response"]
    assert p["writes"]==pec.writes==runts.writes==0
    assert hashlib.sha256(db.read_bytes()).hexdigest()==before
    assert result["metadata"]["mcp_calls"]==["pec_find_by_runts_reference","runts_prepare_practice_response"]
    from ralfloop_agent.unified_assistant import pec_runts_telegram, runtime
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT","1")
    monkeypatch.setenv("RALFLOOP_OPERATIONAL_MEMORY_PATH",str(tmp_path/'ingress.sqlite'))
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR",str(tmp_path/'sessions'))
    monkeypatch.setattr(pec_runts_telegram,"execute_telegram_read",lambda text,**kwargs: execute_telegram_prepare(text,**kwargs,pec_provider=pec,runts_provider=runts,response_preparer=RuntsSuiteResponsePreparer(binding)))
    ingress=runtime.run_unified_telegram(
        TEXT,
        {
            "source":"telegram_natural",
            "telegram_user_id":11,
            "telegram_chat_id":22,
            "telegram_message_id":1,
            "telegram_chat_type":"private",
        },
    )
    assert ingress["metadata"]["proposal"]["status"]=="READY_FOR_HUMAN_APPROVAL"



def test_runtime_runts_approval_reaches_executor_but_write_flag_blocks(
    tmp_path,
    monkeypatch,
):
    binding_path = os.getenv("BOTTAZZI_TEST_RUNTS_BINDING")

    if not binding_path:
        pytest.skip(
            "Explicit local RUNTS binding required"
        )

    binding = (
        RuntsPrepareBinding
        .model_validate_json(
            Path(binding_path).read_text()
        )
        .model_copy(
            update={
                "output": str(
                    tmp_path / "artifacts"
                )
            }
        )
    )

    suite_db = Path(binding.suite) / "runts_suite.db"

    before = hashlib.sha256(
        suite_db.read_bytes()
    ).hexdigest()

    pec = Pec()
    runts = Runts(
        binding.authoritative_message_hash
    )

    from ralfloop_agent.unified_assistant import (
        pec_runts_telegram,
        runtime,
    )

    monkeypatch.setenv(
        "RALFLOOP_UNIFIED_ASSISTANT",
        "1",
    )

    monkeypatch.setenv(
        "RALFLOOP_OPERATIONAL_MEMORY_PATH",
        str(tmp_path / "memory.sqlite"),
    )

    monkeypatch.setenv(
        "RALFLOOP_UNIFIED_SESSION_DIR",
        str(tmp_path / "sessions"),
    )

    monkeypatch.setenv(
        "RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE",
        "1",
    )

    monkeypatch.setenv(
        "RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS",
        "11",
    )

    monkeypatch.setenv(
        "RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS",
        "22",
    )

    monkeypatch.setenv(
        "RALFLOOP_TELEGRAM_APPROVAL_REQUIRE_PRIVATE_CHAT",
        "1",
    )

    monkeypatch.setenv(
        "RALFLOOP_TELEGRAM_APPROVAL_DB",
        str(tmp_path / "approvals.sqlite"),
    )

    monkeypatch.setenv(
        "RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG",
        str(tmp_path / "approval-audit.jsonl"),
    )

    # Hard invariant under test.
    monkeypatch.setenv(
        "BOTTAZZI_RUNTS_WRITE_ENABLED",
        "0",
    )

    def prepare(text, **kwargs):
        return execute_telegram_prepare(
            text,
            memory_path=kwargs["memory_path"],
            pec_provider=pec,
            runts_provider=runts,
            response_preparer=
                RuntsSuiteResponsePreparer(
                    binding
                ),
        )

    monkeypatch.setattr(
        pec_runts_telegram,
        "execute_telegram_read",
        prepare,
    )

    # Approval executor must reread the
    # authoritative synthetic RUNTS source.
    monkeypatch.setattr(
        runtime,
        "_runts_reader",
        lambda: runts,
    )

    class ForbiddenWriter:
        def preflight(self, scope):
            raise AssertionError(
                "RUNTS writer preflight called "
                "while feature flag is OFF"
            )

        def execute(self, scope):
            raise AssertionError(
                "RUNTS writer execute called "
                "while feature flag is OFF"
            )

    monkeypatch.setattr(
        runtime,
        "_runts_writer",
        lambda: ForbiddenWriter(),
    )

    prepare_context = {
        "source": "telegram_natural",
        "telegram_user_id": 11,
        "telegram_chat_id": 22,
        "telegram_message_id": 1001,
        "telegram_chat_type": "private",
    }

    prepared = runtime.run_unified_telegram(
        TEXT,
        prepare_context,
    )

    assert prepared["ok"] is True
    assert (
        prepared["capability"]
        == "runts_prepare_practice_response"
    )
    assert prepared["approval_required"] is True

    metadata = prepared["metadata"]

    assert metadata["pending_domain"] == "runts"
    assert (
        metadata["pending_action"]
        == "runts_practice_reply"
    )
    assert metadata["writes"] == 0
    assert metadata["approval_request_id"]

    proposal = metadata["proposal"]

    assert proposal["practice_id"] == "2603942"
    assert (
        proposal["authoritative_message_id"]
        == "523278"
    )
    assert (
        proposal["status"]
        == "READY_FOR_HUMAN_APPROVAL"
    )

    assert (
        proposal["final_validation"][
            "semantic_sha256"
        ]
        ==
        proposal["final_validation"][
            "golden_semantic_sha256"
        ]
    )

    approved = runtime.run_unified_telegram(
        "Approvo risposta RUNTS 2603942",
        {
            **prepare_context,
            "telegram_message_id": 1002,
        },
    )

    assert approved["ok"] is True
    assert (
        approved["capability"]
        == "runts_practice_reply"
    )

    result = approved["metadata"]

    assert (
        result["status"]
        == "runts_write_disabled"
    )
    assert result["writes"] == 0

    execution = result["execution"]

    assert execution["executed"] is False
    assert execution["writes"] == 0
    assert (
        execution["provider_call_attempted"]
        is False
    )
    assert execution["retry_allowed"] is True

    # Accounting source remains immutable.
    after = hashlib.sha256(
        suite_db.read_bytes()
    ).hexdigest()

    assert after == before
