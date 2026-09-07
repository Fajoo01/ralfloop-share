"""Trusted local Suite binding. Only a practice ID is supplied by the caller."""
from datetime import datetime, timezone
from pathlib import Path

from .contracts import StrictModel
from .memory_service import MemoryEntity
from .runts_modeld_plan import ModelDPlan


class RuntsPrepareBinding(StrictModel):
    suite: str
    output: str
    decision_plan: str
    official_model: str
    golden_pdf: str
    golden_sha256: str
    authoritative_message_hash: str


class RuntsSuiteResponsePreparer:
    def __init__(self, binding: RuntsPrepareBinding):
        self.binding=binding

    def __call__(self, practice, messages, memory):
        from scripts.runts_modd_prepare import main
        b=self.binding
        plan=ModelDPlan.model_validate_json(Path(b.decision_plan).read_text())
        if practice.native_id!=plan.practice_id:raise ValueError("practice_binding_mismatch")
        authoritative=[m for m in messages if m.native_id==plan.message_id and m.practice_id==practice.native_id]
        if len(authoritative)!=1 or authoritative[0].source.content_hash!=b.authoritative_message_hash:
            raise ValueError("STALE_PROPOSAL")
        message=authoritative[0]
        if any(m.native_id!=message.native_id and (m.published_at is None or message.published_at is None or m.published_at>=message.published_at) for m in messages):
            raise ValueError("STALE_PROPOSAL")
        plan.persist(memory)
        # Consume the structured persisted decision versions, not a prompt blob.
        reloaded=[]
        for fact in plan.facts:
            entity=memory.get_entity(fact.storage_id)
            reloaded.append(type(fact).model_validate({k:entity.data[k] for k in type(fact).model_fields}))
        plan=plan.model_copy(update={"facts":tuple(reloaded)})
        plan.validate_scope()
        totals=plan.fact("APPROVED_TOTALS","balance").value
        args=["--suite",b.suite,"--output",b.output,"--year",str(plan.exercise),"--practice-id",plan.practice_id,
              "--message-id",plan.message_id,"--message-hash",b.authoritative_message_hash,"--official-model",b.official_model,
              "--approved-income",totals["income"],"--approved-expense",totals["expense"],"--approved-surplus",totals["surplus"],
              "--approved-closing",totals["deposits"],"--decision-plan",b.decision_plan,"--golden-pdf",b.golden_pdf,"--golden-sha256",b.golden_sha256]
        proposal=main(args)
        memory.put_entity(MemoryEntity.build(entity_id=proposal.proposal_id,domain="runts",entity_type="RUNTS_DOCUMENT_PROPOSAL",
            status=proposal.status,updated_at=datetime.now(timezone.utc),data=proposal.model_dump(mode="json"),provenance=proposal.provenance))
        return proposal
