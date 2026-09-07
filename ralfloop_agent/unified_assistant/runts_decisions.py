"""Versioned, exercise-scoped facts. Human decisions are evidence, never LLM memory."""
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import Field

from .contracts import StrictModel
from .memory_service import MemoryEntity, MemoryEvent
from .platform import SourceRef
from .runts_accounting import version


class Authority(IntEnum):
    OFFICIAL = 1
    APPROVED_DOCUMENT = 2
    PRIMARY_FINANCIAL = 3
    EXPLICIT_HUMAN = 4
    RECORDED_DECISION = 5
    DB_RAW = 6
    LEGACY_NOTE = 7
    DETERMINISTIC = 8
    LLM = 9


class AccountRole(StrEnum):
    ASSOCIATION_DIRECT_ACCOUNT = "ASSOCIATION_DIRECT_ACCOUNT"
    ASSOCIATION_ASSET_HELD_BY_MEMBER = "ASSOCIATION_ASSET_HELD_BY_MEMBER"
    PERSONAL_SUPPORT_ACCOUNT = "PERSONAL_SUPPORT_ACCOUNT"
    PERSONAL_PRIVATE_ACCOUNT = "PERSONAL_PRIVATE_ACCOUNT"
    UNKNOWN_OWNER = "UNKNOWN_OWNER"

    @property
    def include_personal_balance(self):
        return self in {self.ASSOCIATION_DIRECT_ACCOUNT,self.ASSOCIATION_ASSET_HELD_BY_MEMBER}


class DecisionFact(StrictModel):
    decision_id: str = Field(min_length=1)
    practice_id: str
    exercise: int = Field(ge=2000, le=2200)
    fact_type: Literal["FORM","APPROVED_TOTALS","TRANSACTION","LEGAL_OWNER","ECONOMIC_OWNER","CLASSIFICATION","TAX_PRESENTATION","IDENTITY","PRESENTATION"]
    subject: str
    value: dict[str, str]
    source_type: Authority
    source: SourceRef
    decision_version: int = Field(ge=1)
    status: Literal["ACTIVE","REVOKED"] = "ACTIVE"
    conflicts: tuple[str, ...] = ()

    @property
    def storage_id(self):
        return "runts.decision." + version((self.practice_id,self.exercise,self.decision_id,self.decision_version))[:32]

    def persist(self, memory):
        if not self.source.content_hash:
            raise ValueError("decision_source_hash_required")
        data=self.model_dump(mode="json")
        observed=datetime.fromisoformat(self.source.observed_at.replace("Z","+00:00"))
        data.update(authority_level=int(self.source_type),source_id=self.source.native_id,
                    source_hash=self.source.content_hash,observed_at=self.source.observed_at)
        existing=memory.get_entity(self.storage_id)
        if existing and existing.data!=data:
            raise ValueError("decision_version_conflict")
        head_id="runts.decision.head."+version((self.practice_id,self.exercise,self.decision_id))[:32]
        head=memory.get_entity(head_id)
        if head and head.data["decision_version"]>self.decision_version:
            raise ValueError("STALE_DECISION_VERSION")
        entity=MemoryEntity.build(entity_id=self.storage_id,domain="runts",entity_type="MODEL_D_DECISION",
            status=self.status,updated_at=observed,data=data,provenance=(self.source,))
        memory.put_entity(entity)
        memory.put_entity(MemoryEntity.build(entity_id=head_id,domain="runts",entity_type="MODEL_D_DECISION_HEAD",
            status=self.status,updated_at=observed,data=data,provenance=(self.source,)))
        memory.append_event(MemoryEvent.build(event_id="event."+version(data)[:32],type="RUNTS_DECISION_RECORDED",
            source="runts.decisions",source_id=self.storage_id,occurred_at=observed,
            observed_at=observed,entity_refs=(self.practice_id,),payload=data,provenance=(self.source,)))


def resolve_facts(facts):
    """Rank by fact type, retain conflicts; incompatible approved totals fail closed."""
    if not facts:raise ValueError("decision_evidence_required")
    keys={(f.practice_id,f.exercise,f.fact_type,f.subject) for f in facts}
    if len(keys)!=1:raise ValueError("different_fact_types_or_scopes")
    latest={}
    for f in facts:
        prior=latest.get(f.decision_id)
        if prior and f.decision_version==prior.decision_version and f!=prior:
            raise ValueError("decision_version_conflict")
        if prior is None or f.decision_version>prior.decision_version:latest[f.decision_id]=f
    active=[f for f in latest.values() if f.status=="ACTIVE" and f.source_type!=Authority.LLM and f.source.content_hash]
    allowed={"FORM":(Authority.OFFICIAL,),"APPROVED_TOTALS":(Authority.APPROVED_DOCUMENT,Authority.EXPLICIT_HUMAN,Authority.DETERMINISTIC),
             "TRANSACTION":(Authority.PRIMARY_FINANCIAL,Authority.DB_RAW,Authority.LEGACY_NOTE,Authority.DETERMINISTIC),
             "ECONOMIC_OWNER":(Authority.APPROVED_DOCUMENT,Authority.EXPLICIT_HUMAN,Authority.RECORDED_DECISION,Authority.DB_RAW),
             "CLASSIFICATION":(Authority.EXPLICIT_HUMAN,Authority.RECORDED_DECISION,Authority.DB_RAW,Authority.LEGACY_NOTE,Authority.DETERMINISTIC)}
    order=allowed.get(facts[0].fact_type,tuple(Authority)[:-1])
    active=[f for f in active if f.source_type in order]
    if not active:raise ValueError("decision_evidence_required")
    active.sort(key=lambda f:(order.index(f.source_type),f.decision_id))
    winner=active[0]
    conflicts=[{"type":"SOURCE_CONFLICT","fact_type":f.fact_type,"source_a":winner.source.native_id,
                "source_b":f.source.native_id,"authority_a":int(winner.source_type),"authority_b":int(f.source_type),
                "impact":"MODEL_D_BLOCKER" if f.fact_type=="APPROVED_TOTALS" else "REVIEW_REQUIRED"}
               for f in active[1:] if f.value!=winner.value]
    if conflicts and winner.fact_type=="APPROVED_TOTALS":raise ValueError("APPROVED_TOTAL_CONFLICT")
    if any(f.value!=winner.value and f.source_type==winner.source_type for f in active[1:]):
        raise ValueError("SOURCE_CONFLICT")
    return winner,conflicts
