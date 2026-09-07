import pytest
from ralfloop_agent.unified_assistant.runts_decisions import Authority, AccountRole, DecisionFact, resolve_facts
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.platform import SourceRef


def fact(authority, kind="TRANSACTION", value="1", **kw):
    return DecisionFact(decision_id=str(authority),practice_id="synthetic",exercise=2025,fact_type=kind,
        subject="item",value={"value":value},source_type=authority,decision_version=kw.pop("decision_version",1),
        source=SourceRef(system="synthetic",native_id=str(authority),locator="synthetic://evidence",observed_at="2026-09-07T00:00:00Z",content_hash="a"*64),**kw)


def test_primary_statement_beats_db_mapping_for_transaction_fact():
    winner,conflicts=resolve_facts([fact(Authority.DB_RAW,value="2"),fact(Authority.PRIMARY_FINANCIAL)])
    assert winner.source_type==Authority.PRIMARY_FINANCIAL and conflicts[0]["type"]=="SOURCE_CONFLICT"


def test_approved_balance_conflicting_derivation_fails_closed():
    with pytest.raises(ValueError,match="APPROVED_TOTAL_CONFLICT"):
        resolve_facts([fact(Authority.APPROVED_DOCUMENT,"APPROVED_TOTALS","2283.08"),fact(Authority.DETERMINISTIC,"APPROVED_TOTALS","2283.15")])


def test_legal_and_economic_owner_are_distinct_facts():
    legal=fact(Authority.PRIMARY_FINANCIAL,"LEGAL_OWNER","member")
    economic=fact(Authority.EXPLICIT_HUMAN,"ECONOMIC_OWNER","association")
    assert resolve_facts([legal])[0].value!={"value":"association"}
    assert resolve_facts([economic])[0].value=={"value":"association"}


def test_existing_manual_classification_beats_classifier():
    winner,conflicts=resolve_facts([fact(Authority.RECORDED_DECISION,"CLASSIFICATION","A5"),fact(Authority.DETERMINISTIC,"CLASSIFICATION","A2")])
    assert winner.value=={"value":"A5"} and conflicts


def test_llm_hypothesis_never_promotes_fact():
    with pytest.raises(ValueError,match="decision_evidence_required"):resolve_facts([fact(Authority.LLM)])


def test_same_authority_conflict_not_silently_resolved():
    a=fact(Authority.EXPLICIT_HUMAN,"ECONOMIC_OWNER","member")
    b=a.model_copy(update={"decision_id":"other","value":{"value":"association"}})
    with pytest.raises(ValueError,match="SOURCE_CONFLICT"):resolve_facts([a,b])


def test_decisions_persist_idempotently_and_version_revocation_survives_restart(tmp_path):
    a=fact(Authority.EXPLICIT_HUMAN,"ECONOMIC_OWNER")
    revoked=a.model_copy(update={"decision_version":2,"status":"REVOKED"})
    with MemoryService(tmp_path/'memory.sqlite') as m:
        a.persist(m);a.persist(m);revoked.persist(m)
        assert len(m.timeline("synthetic"))==2
    with MemoryService(tmp_path/'memory.sqlite') as m:
        assert m.get_entity(revoked.storage_id).status=="REVOKED"
        with pytest.raises(ValueError,match="STALE_DECISION_VERSION"):a.persist(m)
    with pytest.raises(ValueError,match="decision_evidence_required"):resolve_facts([a,revoked])


def test_personal_support_and_private_balances_are_not_association_assets():
    assert not AccountRole.PERSONAL_SUPPORT_ACCOUNT.include_personal_balance
    assert not AccountRole.PERSONAL_PRIVATE_ACCOUNT.include_personal_balance
    assert not AccountRole.UNKNOWN_OWNER.include_personal_balance
    assert AccountRole.ASSOCIATION_ASSET_HELD_BY_MEMBER.include_personal_balance
