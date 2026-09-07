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


def test_unconfigured_prepare_fails_closed_without_write(tmp_path):
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
    assert runts.calls==["get_practice","list_messages","get_message"]
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
    monkeypatch.setattr(pec_runts_telegram,"execute_telegram_read",lambda text,**kwargs: execute_telegram_prepare(text,**kwargs,pec_provider=pec,runts_provider=runts,response_preparer=RuntsSuiteResponsePreparer(binding)))
    ingress=runtime.run_unified_telegram(TEXT,{"source":"telegram_natural"})
    assert ingress["metadata"]["proposal"]["status"]=="READY_FOR_HUMAN_APPROVAL"
