from __future__ import annotations

import json
import time

import pytest

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision, DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from src.google_workspace import (
    ApprovalRequired,
    DraftGeneration,
    DraftValidationError,
    GmailMessage,
    GoogleWorkspaceError,
    GoogleWorkspaceGateway,
    MAX_GENERATION_ATTEMPTS,
    MagnoliaWorkflow,
    RalfReplyGenerator,
    ReplyContextProvider,
    dispatch_magnolia_request,
    _email_scope,
    _validate_draft,
    verify_approved_email_scope,
)
from ralfloop_agent.local_arch.router import LocalRouter, ToolRegistry
from ralfloop_agent.semantic_judge import (
    JudgeAvailabilityError,
    ReviewRisk,
    SemanticIssue,
    SemanticJudgeConfig,
    SemanticReview,
    SemanticReviewResult,
)


class FakeSession:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def list_tools(self):
        return [type("Tool", (), {"name": "manage_email", "input_schema": {
            "required": ["operation", "email"], "properties": {"operation": {"enum": list(("search", "read", "threads", "getThread", "getAttachment", "viewAttachment", "reply", "replyAll", "send", "forward"))}}
        }})()]

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        operation = arguments["operation"]
        return {"structuredContent": self.responses[operation], "content": []}


class FakeGenerator:
    def __init__(self):
        self.packet = None

    def generate(self, context_packet):
        self.packet = context_packet
        return "Ciao, grazie per l'invito alla programmazione. Tiremm Innanz vorrebbe partecipare e speriamo di essere operativi per settembre. Un saluto."


class SequenceGenerator:
    def __init__(self, initial, repair=None, *, initial_fallback=False, repair_fallback=False):
        self.initial = initial
        self.repair = repair
        self.initial_fallback = initial_fallback
        self.repair_fallback = repair_fallback
        self.generate_calls = 0
        self.repair_calls = []

    @staticmethod
    def result(text, fallback=False):
        return DraftGeneration(text, "llama_cpp", "qwen3.5:9b", fallback)

    def generate(self, packet):
        self.generate_calls += 1
        return self.result(self.initial, self.initial_fallback)

    def generate_repair(self, packet, rejected_draft, validation_reason):
        self.repair_calls.append((packet, rejected_draft, validation_reason))
        return self.result(self.repair, self.repair_fallback)


def policy(tmp_path, ttl=3600):
    return DomainApprovalPolicy(enabled=True, auto_execute=False, ttl_sec=ttl, max_pending=20,
        db_path=str(tmp_path / "approval.sqlite"), audit_log=str(tmp_path / "audit.jsonl"))


def responses(two=False):
    messages = [
        {"id": "m-old", "threadId": "t-old", "from": "ARCI Magnolia <eventi@magnolia.it>", "subject": "Altro", "date": "2025-01-01", "snippet": "incontro"},
        {"id": "m-real", "threadId": "t-real", "from": "ARCI Magnolia <eventi@magnolia.it>", "subject": "Partecipazione settembre", "date": "2026-07-30", "snippet": "Ci piacerebbe la vostra partecipazione a settembre"},
    ]
    if not two:
        messages = messages[1:]
    return {
        "search": {"messages": messages},
        "read": {"message": {"id": "m-real", "threadId": "t-real", "from": "ARCI Magnolia <eventi@magnolia.it>", "replyTo": "programma@magnolia.it", "subject": "Partecipazione settembre", "date": "2026-07-30", "body": "Vi invitiamo a partecipare alla programmazione di settembre."}},
        "getThread": {"messages": [{"id": "m-real", "threadId": "t-real", "body": "Vi invitiamo a partecipare alla programmazione di settembre."}]},
    }


def workflow(tmp_path, data=None, ttl=3600):
    p = policy(tmp_path, ttl)
    store = DomainApprovalStore(policy=p)
    session = FakeSession(data or responses())
    gateway = GoogleWorkspaceGateway(session, account="fabio@tiremminnanz.com")
    generator = FakeGenerator()
    flow = MagnoliaWorkflow(gateway, approval_store=store, approval_policy=p,
        outbox_path=tmp_path / "outbox.jsonl", outbox_state_path=tmp_path / "state.json", generator=generator)
    return flow, store, session


def workflow_with_generator(tmp_path, generator, data=None, diagnostic_hook=None, **workflow_options):
    tmp_path.mkdir(parents=True, exist_ok=True)
    p = policy(tmp_path)
    store = DomainApprovalStore(policy=p)
    session = FakeSession(data or responses())
    flow = MagnoliaWorkflow(
        GoogleWorkspaceGateway(session, account="fabio@tiremminnanz.com"),
        approval_store=store, approval_policy=p,
        outbox_path=tmp_path / "outbox.jsonl", outbox_state_path=tmp_path / "state.json",
        generator=generator, diagnostic_hook=diagnostic_hook, **workflow_options,
    )
    return flow, store, session


def run(flow, request="Cerca Magnolia e preparami la risposta", wait=0):
    if "mail" not in request.casefold():
        request = "Cerca la mail di ARCI " + request
    return dispatch_magnolia_request(request, LocalRouter(ToolRegistry.load("config/local_arch_tools_v1.json")), flow, wait_delivery_sec=wait)


def test_human_workflow_disambiguates_drafts_queues_and_persists(tmp_path):
    flow, store, session = workflow(tmp_path, responses(two=True))
    result = run(flow)
    assert [call[1]["operation"] for call in session.calls] == ["search", "read", "getThread"]
    assert all(call[1]["email"] == "fabio@tiremminnanz.com" for call in session.calls)
    assert session.calls[1][1]["messageId"] == "m-real"
    assert result["route"]["t"] == "google_workspace.gmail"
    assert result["mail"]["reply_to"] == "programma@magnolia.it"
    assert "speriamo" in result["draft"] and "saremo pronti" not in result["draft"]
    assert result["email_sent"] is False
    request_id = result["approval"]["request_id"]
    restarted = DomainApprovalStore(db_path=store.db_path, policy=store.policy)
    row = restarted.get_request(request_id)
    assert row["status"] == "pending"
    assert row["scope"]["source_message_id"] == "m-real"
    assert row["scope"]["thread_id"] == "t-real"
    assert row["scope"]["recipient"] == "programma@magnolia.it"
    assert len(row["scope"]["artifact_sha256"]) == 64
    queued = json.loads((tmp_path / "outbox.jsonl").read_text().splitlines()[-1])
    assert "La mail NON è stata inviata" in queued["message"]
    packet = flow.generator.packet
    assert packet["source_email"]["body"] == "Vi invitiamo a partecipare alla programmazione di settembre."
    assert packet["organization_context"]["name"] == "Tiremm Innanz APS"
    assert packet["organization_context"]["signature"] == {
        "required": True, "name": "Fabio", "organization": "Tiremm Innanz",
    }
    assert packet["style"] == {
        "concise": True, "natural": True, "avoid_bureaucratic_language": True,
    }
    assert "Tra le attività ci sono doposcuola e laboratori." in packet["organization_context"]["relevant_facts"]
    assert packet["user_intent"]
    assert packet["reply_constraints"]["side_effects_allowed"] is False
    assert result["trace"]["draft_validation"] == "passed"
    assert row["scope"]["trace"]["context_source_email"] is True


def test_thread_context_reaches_generator_when_previous_message_exists(tmp_path):
    data = responses()
    data["getThread"]["messages"].insert(0, {
        "from": "Fabio <fabio@tiremminnanz.com>", "subject": "Messaggio precedente",
        "date": "2026-07-29", "body": "Un precedente messaggio del thread.",
    })
    flow, _, _ = workflow(tmp_path, data)
    run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert flow.generator.packet["source_email"]["thread_context"] == [{
        "sender": "Fabio <fabio@tiremminnanz.com>", "subject": "Messaggio precedente",
        "date": "2026-07-29", "body": "Un precedente messaggio del thread.",
    }]


def test_user_intent_is_preserved_semantically(tmp_path):
    flow, _, _ = workflow(tmp_path)
    run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre. Prepara la risposta.")
    intent = " ".join(flow.generator.packet["user_intent"]).casefold()
    assert "vorremmo partecipare" in intent
    assert "speriamo di essere pronti per settembre" in intent


def test_draft_validator_rejects_confirmation_and_accepts_prudent_reply():
    packet = {"source_email": {"body": "Festival il 13 settembre 2026"}}
    with pytest.raises(DraftValidationError, match="draft_claims_confirmed_participation"):
        _validate_draft(
            "Tiremm Innanz è interessata: confermiamo la nostra partecipazione e speriamo di essere pronti per settembre.",
            packet,
        )
    assert _validate_draft(
        "Tiremm Innanz: saremmo interessati a partecipare e speriamo di essere pronti per settembre.", packet
    ) == "passed"


def test_required_signature_organization_must_appear_and_authorized_signature_passes():
    packet = {
        "source_email": {"body": "Vi invitiamo a settembre"},
        "organization_context": {"signature": {
            "required": True, "name": "Fabio", "organization": "Tiremm Innanz",
        }},
    }
    without_organization = (
        "Ciao, saremmo interessati a partecipare e speriamo di essere pronti per settembre.\n\nFabio"
    )
    with pytest.raises(DraftValidationError, match="draft_missing_required_identity"):
        _validate_draft(without_organization, packet)
    assert _validate_draft(without_organization + "\nTiremm Innanz", packet) == "passed"


@pytest.mark.parametrize("topic", ["costi", "scadenze", "materiali da consegnare"])
def test_unsupported_operational_topics_are_rejected(topic):
    packet = {"source_email": {"body": "Vi invitiamo a partecipare a settembre"}}
    draft = (
        "Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre. "
        f"Vorremmo informazioni su {topic}."
    )
    with pytest.raises(DraftValidationError, match="draft_introduces_unsupported_operational_topic"):
        _validate_draft(draft, packet)


def test_supported_cost_topic_and_generic_organizational_details_are_allowed():
    base = "Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre. "
    assert _validate_draft(
        base + "Vorremmo informazioni sui costi.",
        {"source_email": {"body": "I costi saranno comunicati in seguito."}},
    ) == "passed"
    assert _validate_draft(
        base + "Ci farebbe piacere restare aggiornati sui dettagli organizzativi.",
        {"source_email": {"body": "Vi invitiamo a settembre."}},
    ) == "passed"


@pytest.mark.parametrize("draft,reason", [
    ("Tiremm Innanz speriamo di essere pronti per settembre.", "draft_missing_participation_interest"),
    ("Tiremm Innanz vorremmo partecipare e saremo pronti a settembre.", "draft_missing_september_uncertainty"),
    ("Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre alle 20:30.", "draft_invents_organizational_detail"),
    ("Vorremmo partecipare e speriamo di essere pronti per settembre.", "draft_missing_required_identity"),
    ("", "draft_invalid_shape"),
])
def test_draft_validator_propagates_precise_reason_codes(draft, reason):
    packet = {
        "source_email": {"body": "Invito per settembre"},
        "organization_context": {"name": "Tiremm Innanz APS"},
    }
    with pytest.raises(DraftValidationError) as raised:
        _validate_draft(draft, packet)
    assert raised.value.reason_code == reason


def test_correct_initial_draft_uses_one_generation_and_no_repair(tmp_path):
    assert MAX_GENERATION_ATTEMPTS == 2
    generator = SequenceGenerator(
        "Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre."
    )
    flow, _, _ = workflow_with_generator(tmp_path, generator)
    result = run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert generator.generate_calls == 1
    assert generator.repair_calls == []
    assert result["trace"]["generation_attempts"] == 1
    assert result["trace"]["repair_attempted"] is False
    assert result["trace"]["final_validation"] == "passed"


@pytest.mark.parametrize("initial,reason", [
    ("Tiremm Innanz è interessata a partecipare: confermiamo la nostra partecipazione e speriamo di essere pronti per settembre.", "draft_claims_confirmed_participation"),
    ("Tiremm Innanz siamo interessati a partecipare e saremo pronti a settembre.", "draft_missing_september_uncertainty"),
    ("Tiremm Innanz speriamo di essere pronti per settembre.", "draft_missing_participation_interest"),
    ("Vorremmo partecipare e speriamo di essere pronti per settembre.\n\nFabio", "draft_missing_required_identity"),
    ("Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre. Chiediamo costi e scadenze.", "draft_introduces_unsupported_operational_topic"),
])
def test_repairable_initial_rejection_gets_exactly_one_successful_repair(tmp_path, initial, reason):
    repaired = "Vorremmo partecipare e speriamo di essere pronti per settembre.\n\nFabio\nTiremm Innanz"
    generator = SequenceGenerator(initial, repaired)
    observed = []
    flow, _, _ = workflow_with_generator(tmp_path, generator, diagnostic_hook=lambda *args: observed.append(args))
    result = run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert len(generator.repair_calls) == 1
    packet, rejected, repair_reason = generator.repair_calls[0]
    assert rejected == initial and repair_reason == reason
    assert packet == result["context_packet"]
    assert result["trace"]["generation_attempts"] == 2
    assert result["trace"]["initial_validation_reason"] == reason
    assert result["trace"]["repair_validation_reason"] is None
    assert result["trace"]["final_validation"] == "passed"
    assert result["trace"]["rejected_draft_sha256"]
    assert observed == [("initial", initial, reason), ("repair", repaired, None)]


def test_failed_repair_stops_without_approval_telegram_or_gmail(tmp_path):
    initial = "Tiremm Innanz parteciperemo a settembre."
    generator = SequenceGenerator(initial, "Tiremm Innanz saremo presenti a settembre.")
    flow, store, session = workflow_with_generator(tmp_path, generator)
    with pytest.raises(DraftValidationError, match="claims_confirmed"):
        run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert generator.generate_calls == 1 and len(generator.repair_calls) == 1
    assert store.list_pending() == []
    assert not (tmp_path / "outbox.jsonl").exists()
    assert not [call for call in session.calls if call[1]["operation"] in {"reply", "send"}]
    trace = json.loads((tmp_path / "outbox.jsonl.workflow-trace.jsonl").read_text().splitlines()[-1])
    assert trace["generation_attempts"] == 2
    assert trace["repair_attempted"] is True
    assert trace["final_validation"] == "failed"
    assert trace["draft_validation_reason"] == "draft_claims_confirmed_participation"
    assert len(trace["draft_sha256"]) == 64


def test_fallback_provider_stops_before_repair(tmp_path):
    generator = SequenceGenerator("Tiremm Innanz parteciperemo a settembre.", "unused", initial_fallback=True)
    flow, store, _ = workflow_with_generator(tmp_path, generator)
    with pytest.raises(GoogleWorkspaceError, match="fallback_forbidden"):
        run(flow, "Magnolia")
    assert generator.repair_calls == [] and store.list_pending() == []


def test_nonrepairable_invented_detail_stops_without_repair(tmp_path):
    generator = SequenceGenerator(
        "Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre alle 20:30.",
        "unused",
    )
    flow, store, _ = workflow_with_generator(tmp_path, generator)
    with pytest.raises(DraftValidationError, match="invents_organizational_detail"):
        run(flow, "Magnolia")
    assert generator.repair_calls == [] and store.list_pending() == []


def test_missing_source_stops_before_generation_or_repair(tmp_path):
    data = responses()
    data["search"]["messages"][0]["snippet"] = ""
    data["read"]["message"]["body"] = ""
    data["getThread"]["messages"][0]["body"] = ""
    generator = SequenceGenerator("unused", "unused")
    flow, store, _ = workflow_with_generator(tmp_path, generator, data=data)
    with pytest.raises(GoogleWorkspaceError, match="source_email_missing"):
        run(flow, "Magnolia")
    assert generator.generate_calls == 0 and generator.repair_calls == []
    assert store.list_pending() == []


def test_model_receives_packet_only_and_cannot_receive_side_effect_authority():
    class Provider:
        def __init__(self): self.messages = None
        def chat(self, messages):
            self.messages = messages
            return type("Result", (), {"text": "bozza", "provider": "llama_cpp", "model": "qwen3.5:9b", "metadata": {"fallback_used": False}})()
    provider = Provider()
    packet = {"task": "draft_email_reply", "reply_constraints": {"side_effects_allowed": False}}
    generated = RalfReplyGenerator(provider).generate(packet)
    assert generated.text == "bozza"
    assert json.loads(provider.messages[1]["content"].split("\n", 1)[1]) == packet
    assert "non chiamare strumenti" in provider.messages[0]["content"]
    assert "no new operational topics" in provider.messages[0]["content"].casefold()
    assert "coerente con i nostri valori" in provider.messages[0]["content"].casefold()


def test_llama_reply_generation_uses_transactional_gpu_scheduler():
    events = []

    class Scheduler:
        class Session:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, *_):
                events.append("exit")

        def engine_session(self, engine, *, task_id=""):
            events.append((engine, task_id.startswith("email-draft-")))
            return self.Session()

    class Provider:
        def chat(self, messages):
            events.append("chat")
            return type("Result", (), {
                "text": "bozza", "provider": "llama_cpp", "model": "qwen3.5:9b",
                "metadata": {"fallback_used": False},
            })()

    result = RalfReplyGenerator(Provider(), scheduler=Scheduler()).generate({"task": "reply"})

    assert result.text == "bozza"
    assert events == [("qwen_chat", True), "enter", "chat", "exit"]


def test_writer_packet_compacts_duplicate_domain_evidence_but_keeps_semantics():
    from src.google_workspace import _writer_context_packet

    packet = {
        "source_email": {"body": "evidence"},
        "structured_artifacts": [{"content_role": "data", "facts": [{"statement": "duplicate"}]}],
        "memory_refs": ["artifact:1"],
        "email_reply_domain_v1": {
            "schema_version": "email_reply_domain_v1",
            "required_meanings": [{
                "key": "interest", "text": "Interest is real", "certainty": "asserted",
                "deterministic_validator": None, "evidence_refs": ["user_intent[0]"],
            }],
            "supported_facts": [{
                "key": "fact_1", "statement": "Role remains unresolved",
                "certainty": "uncertain", "kind": "fact", "actor_refs": [],
                "evidence_refs": ["artifact:1"],
            }],
            "temporal_statements": [{"statement": "duplicate"}],
            "evidence": [{"ref": "artifact:1", "excerpt": "x" * 6000, "sha256": "a" * 64}],
            "forbidden_claims_without_evidence": ["proposal_approved"],
        },
    }

    compact = _writer_context_packet(packet)

    domain = compact["email_reply_domain_v1"]
    assert domain["required_meanings"][0]["text"] == "Interest is real"
    assert domain["supported_facts"][0]["statement"] == "Role remains unresolved"
    assert domain["forbidden_claims_without_evidence"] == ["proposal_approved"]
    assert "evidence" not in domain
    assert "temporal_statements" not in domain
    assert "structured_artifacts" not in compact
    assert "memory_refs" not in compact
    assert len(json.dumps(compact)) < len(json.dumps(packet)) / 4


def test_repair_prompt_preserves_packet_reason_and_rejected_draft_without_side_effect_authority():
    class Provider:
        def __init__(self): self.messages = None
        def chat(self, messages):
            self.messages = messages
            return type("Result", (), {"text": "nuova bozza", "provider": "llama_cpp", "model": "qwen3.5:9b", "metadata": {"fallback_used": False}})()
    provider = Provider()
    packet = {
        "source_email": {"body": "Invito Magnolia"},
        "organization_context": {"name": "Tiremm Innanz APS"},
        "user_intent": ["vorremmo partecipare", "speriamo di essere pronti per settembre"],
        "reply_constraints": {"side_effects_allowed": False},
    }
    generated = RalfReplyGenerator(provider).generate_repair(
        packet, "bozza rifiutata", "draft_missing_september_uncertainty"
    )
    payload = json.loads(provider.messages[1]["content"].split("\n", 1)[1])
    assert generated.text == "nuova bozza"
    assert payload == {
        "original_context_packet": packet,
        "rejected_draft": "bozza rifiutata",
        "validation_reason": "draft_missing_september_uncertainty",
    }
    assert "correggendo esclusivamente" in provider.messages[0]["content"]
    assert "firma autorizzata" in provider.messages[0]["content"]
    assert "non supportati" in provider.messages[0]["content"]
    assert "non provocare side effect" in provider.messages[0]["content"].casefold()


class SemanticFakeJudge:
    provider = "colibri_glm"
    model = "glm-test"

    def __init__(self, verdict="pass", *, error=None, issue_type="unsupported_commitment",
                 draft_text="promessa", reason="No such commitment exists in domain.",
                 domain_refs=None):
        self.verdict = verdict
        self.error = error
        self.calls = []
        self.issue_type = issue_type
        self.draft_text = draft_text
        self.reason = reason
        self.domain_refs = domain_refs or ["contact_after_review"]

    def review(self, packet, draft):
        self.calls.append((packet, draft))
        if self.error:
            raise self.error
        review = SemanticReview(
            verdict=self.verdict,
            issues=[SemanticIssue(
                type=self.issue_type,
                draft_text=self.draft_text,
                reason=self.reason,
                domain_refs=self.domain_refs,
            )] if self.verdict == "repair" else [],
            summary="Repair unsupported commitment." if self.verdict == "repair" else "Safe.",
        )
        return SemanticReviewResult(review, self.provider, self.model, 17)


class SemanticRepairGenerator(SequenceGenerator):
    def generate_repair(self, packet, rejected_draft, validation_reason, *, semantic_issues=None):
        self.repair_calls.append((packet, rejected_draft, validation_reason, semantic_issues))
        return self.result(self.repair, self.repair_fallback)


def semantic_config(*, enabled=True, benchmark=1.0, fail_open=False):
    return SemanticJudgeConfig(
        semantic_judge_enabled=enabled, semantic_judge_benchmark_sec=benchmark,
        semantic_judge_fail_open_normal=fail_open, semantic_judge_allow_normal=True,
    )


def test_semantic_pass_is_traced_and_called_once(tmp_path):
    judge = SemanticFakeJudge("pass")
    generator = SequenceGenerator("Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre.")
    flow, _, _ = workflow_with_generator(tmp_path, generator, semantic_judge=judge,
                                          semantic_judge_config=semantic_config())
    result = run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert len(judge.calls) == 1
    assert result["trace"]["semantic_judge_status"] == "pass"
    assert result["trace"]["semantic_judge_used"] is True
    assert result["trace"]["semantic_judge_latency_ms"] == 17


def test_semantic_repair_propagates_structured_issues_to_single_repair(tmp_path):
    judge = SemanticFakeJudge("repair")
    generator = SemanticRepairGenerator(
        "Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre.",
        "Vorremmo partecipare e speriamo di essere pronti per settembre.\n\nFabio\nTiremm Innanz",
    )
    flow, _, _ = workflow_with_generator(tmp_path, generator, semantic_judge=judge,
                                          semantic_judge_config=semantic_config())
    result = run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert len(judge.calls) == 1 and len(generator.repair_calls) == 1
    assert generator.repair_calls[0][2] == "semantic_judge_repair"
    assert generator.repair_calls[0][3][0] == {
        "type": "unsupported_commitment",
        "severity": "medium",
        "draft_text": "promessa",
        "reason": "No such commitment exists in domain.",
        "domain_refs": ["contact_after_review"],
    }
    assert result["trace"]["generation_attempts"] == 2
    assert result["trace"]["final_validation"] == "passed"


def domain_case_data():
    data = responses()
    body = (
        "Abbiamo ricevuto i documenti. La proposta è ancora da valutare. "
        "Vi invitiamo a partecipare alla programmazione di settembre."
    )
    data["read"]["message"]["body"] = body
    data["getThread"]["messages"][0]["body"] = body
    return data


DOMAIN_CASE_REQUEST = (
    "Magnolia. Confermare la ricezione dei documenti. "
    "Dire che la proposta sarà valutata la settimana prossima. "
    "Vorremmo partecipare e speriamo di essere pronti per settembre."
)
SAFE_DOMAIN_DRAFT = (
    "Confermiamo di aver ricevuto i documenti. La proposta sarà valutata la settimana prossima. "
    "Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre."
)


def test_domain_safe_path_calls_critic_once_without_repair(tmp_path):
    judge = SemanticFakeJudge("pass")
    generator = SequenceGenerator(SAFE_DOMAIN_DRAFT)
    flow, _, session = workflow_with_generator(
        tmp_path, generator, data=domain_case_data(), semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    result = run(flow, DOMAIN_CASE_REQUEST)
    assert result["status"] == "pending_approval"
    assert result["context_packet"]["email_reply_domain_v1"]["schema_version"] == "email_reply_domain_v1"
    assert len(judge.calls) == 1 and generator.repair_calls == []
    assert not [call for call in session.calls if call[1]["operation"] in {"reply", "send"}]


def test_enabled_critic_requires_hard_guard_pass_before_ds4_and_repair(tmp_path):
    unsafe = SAFE_DOMAIN_DRAFT.replace(
        "Tiremm Innanz vorremmo partecipare",
        "Tiremm Innanz confermiamo la nostra partecipazione",
    )
    judge = SemanticFakeJudge("pass")
    generator = SemanticRepairGenerator(unsafe, SAFE_DOMAIN_DRAFT)
    flow, store, _ = workflow_with_generator(
        tmp_path, generator, data=domain_case_data(), semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    with pytest.raises(DraftValidationError, match="draft_claims_confirmed_participation"):
        run(flow, DOMAIN_CASE_REQUEST)
    assert judge.calls == [] and generator.repair_calls == [] and store.list_pending() == []


def test_shadow_workflow_returns_before_approval_outbox_or_telegram(tmp_path, monkeypatch):
    judge = SemanticFakeJudge("pass")
    generator = SequenceGenerator(SAFE_DOMAIN_DRAFT)
    flow, store, session = workflow_with_generator(
        tmp_path,
        generator,
        data=domain_case_data(),
        semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    artifact_root = tmp_path / "shadow-artifacts"
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW", "1")
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW_ARTIFACT_DIR", str(artifact_root))
    result = run(flow, DOMAIN_CASE_REQUEST)
    assert result["status"] == "shadow_complete"
    assert result["risk"]["level"] == "high" and len(judge.calls) == 1
    assert result["approval"] is None and result["would_reach_approval"] is False
    assert result["telegram"] == {"status": "not_created", "message_ids": []}
    assert store.list_pending() == []
    assert not (tmp_path / "outbox.jsonl").exists()
    assert not (tmp_path / "outbox.jsonl.workflow-trace.jsonl").exists()
    assert len(list(artifact_root.glob("*.json"))) == 1
    assert not [call for call in session.calls if call[1]["operation"] in {"reply", "send"}]


def test_shadow_hard_guard_block_records_skip_without_ds4_or_side_effect(tmp_path, monkeypatch):
    unsafe = SAFE_DOMAIN_DRAFT.replace(
        "Tiremm Innanz vorremmo partecipare",
        "Tiremm Innanz confermiamo la nostra partecipazione",
    )
    judge = SemanticFakeJudge("pass")
    flow, store, session = workflow_with_generator(
        tmp_path,
        SemanticRepairGenerator(unsafe, SAFE_DOMAIN_DRAFT),
        data=domain_case_data(),
        semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW", "1")
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW_ARTIFACT_DIR", str(tmp_path / "shadow"))
    result = run(flow, DOMAIN_CASE_REQUEST)
    assert result["status"] == "shadow_complete"
    assert result["hard_guard"] == "draft_claims_confirmed_participation"
    assert result["ds4_skipped_reason"] == "hard_guard_block"
    assert result["ds4_invoked"] is False and judge.calls == []
    assert store.list_pending() == [] and not (tmp_path / "outbox.jsonl").exists()
    assert not [call for call in session.calls if call[1]["operation"] in {"reply", "send"}]


@pytest.mark.parametrize(
    "initial,repaired,issue_type,draft_text,domain_ref",
    [
        (
            SAFE_DOMAIN_DRAFT + " Vi contatteremo dopo la valutazione.",
            SAFE_DOMAIN_DRAFT,
            "unsupported_commitment",
            "Vi contatteremo dopo la valutazione.",
            "contact_after_review",
        ),
        (
            SAFE_DOMAIN_DRAFT.replace("sarà valutata", "è stata approvata e sarà valutata"),
            SAFE_DOMAIN_DRAFT,
            "contradiction",
            "La proposta è stata approvata.",
            "proposal_approved",
        ),
        (
            SAFE_DOMAIN_DRAFT.replace("La proposta sarà valutata la settimana prossima. ", ""),
            SAFE_DOMAIN_DRAFT,
            "missing_required_meaning",
            "",
            "proposal_review_next_week",
        ),
    ],
)
def test_domain_issue_gets_one_qwen_repair_then_final_validation(
    tmp_path, initial, repaired, issue_type, draft_text, domain_ref,
):
    judge = SemanticFakeJudge(
        "repair", issue_type=issue_type, draft_text=draft_text,
        reason="Draft conflicts with authoritative domain.", domain_refs=[domain_ref],
    )
    generator = SemanticRepairGenerator(initial, repaired)
    flow, _, session = workflow_with_generator(
        tmp_path, generator, data=domain_case_data(), semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    result = run(flow, DOMAIN_CASE_REQUEST)
    assert result["status"] == "pending_approval"
    assert len(judge.calls) == 1 and len(generator.repair_calls) == 1
    assert generator.repair_calls[0][3][0]["type"] == issue_type
    assert result["trace"]["generation_attempts"] == 2
    assert result["trace"]["final_validation"] == "passed"
    assert not [call for call in session.calls if call[1]["operation"] in {"reply", "send"}]


@pytest.mark.parametrize(
    "error,expected",
    [
        (ValueError("semantic_review_malformed_json"), "semantic_judge_invalid_output"),
        (JudgeAvailabilityError("deepseek_v4_flash_gpu_unavailable"), "semantic_judge_unavailable"),
    ],
)
def test_semantic_invalid_or_gpu_unavailable_has_no_approval_telegram_or_gmail(tmp_path, error, expected):
    judge = SemanticFakeJudge(error=error)
    flow, store, session = workflow_with_generator(
        tmp_path, SequenceGenerator(SAFE_DOMAIN_DRAFT), data=domain_case_data(), semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    with pytest.raises(GoogleWorkspaceError, match=expected):
        run(flow, DOMAIN_CASE_REQUEST)
    assert len(judge.calls) == 1 and store.list_pending() == []
    assert not (tmp_path / "outbox.jsonl").exists()
    assert not [call for call in session.calls if call[1]["operation"] in {"reply", "send"}]


def test_final_validator_rejects_issue_left_by_single_repair_without_second_ds4_call(tmp_path):
    unsafe = SAFE_DOMAIN_DRAFT + " Vi contatteremo dopo la valutazione."
    judge = SemanticFakeJudge("repair")
    generator = SemanticRepairGenerator(unsafe, unsafe)
    flow, store, session = workflow_with_generator(
        tmp_path, generator, data=domain_case_data(), semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    with pytest.raises(DraftValidationError, match="draft_domain_forbidden_claim"):
        run(flow, DOMAIN_CASE_REQUEST)
    assert len(judge.calls) == 1 and len(generator.repair_calls) == 1
    assert store.list_pending() == [] and not (tmp_path / "outbox.jsonl").exists()
    assert not [call for call in session.calls if call[1]["operation"] in {"reply", "send"}]


def test_semantic_pass_cannot_authorize_domain_forbidden_claim(tmp_path):
    unsafe = SAFE_DOMAIN_DRAFT.replace("sarà valutata", "è stata approvata e sarà valutata")
    judge = SemanticFakeJudge("pass")
    flow, store, _ = workflow_with_generator(
        tmp_path, SequenceGenerator(unsafe), data=domain_case_data(), semantic_judge=judge,
        semantic_judge_config=semantic_config(),
    )
    with pytest.raises(DraftValidationError, match="draft_domain_forbidden_claim"):
        run(flow, DOMAIN_CASE_REQUEST)
    assert len(judge.calls) == 1 and store.list_pending() == []


def test_low_bypasses_and_disabled_normal_bypasses(tmp_path):
    for risk, config in (
        (ReviewRisk.LOW, semantic_config()),
        (ReviewRisk.NORMAL, semantic_config(enabled=False)),
    ):
        judge = SemanticFakeJudge()
        generator = SequenceGenerator("Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre.")
        flow, _, _ = workflow_with_generator(tmp_path / f"{risk}-{config.semantic_judge_enabled}-{config.semantic_judge_benchmark_sec}", generator,
            semantic_judge=judge, semantic_judge_config=config, risk_classification=risk)
        result = run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
        assert judge.calls == [] and result["trace"]["semantic_judge_status"] == "disabled"


def test_enabled_normal_uses_critic_even_when_benchmark_is_slow(tmp_path):
    judge = SemanticFakeJudge()
    generator = SequenceGenerator("Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre.")
    flow, _, _ = workflow_with_generator(
        tmp_path, generator, semantic_judge=judge,
        semantic_judge_config=semantic_config(benchmark=60), risk_classification=ReviewRisk.NORMAL,
    )
    result = run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert len(judge.calls) == 1 and result["trace"]["semantic_judge_status"] == "pass"


def test_timeout_is_not_silent_and_high_unavailable_fails_closed(tmp_path):
    draft = "Tiremm Innanz vorremmo partecipare e speriamo di essere pronti per settembre."
    timeout_judge = SemanticFakeJudge(error=TimeoutError("timeout"))
    flow, store, _ = workflow_with_generator(tmp_path / "timeout", SequenceGenerator(draft),
        semantic_judge=timeout_judge, semantic_judge_config=semantic_config())
    with pytest.raises(GoogleWorkspaceError, match="semantic_judge_timeout"):
        run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert len(timeout_judge.calls) == 1 and store.list_pending() == []

    flow, store, _ = workflow_with_generator(tmp_path / "high", SequenceGenerator(draft),
        semantic_judge_config=semantic_config(enabled=False), risk_classification=ReviewRisk.HIGH)
    with pytest.raises(GoogleWorkspaceError, match="required_unavailable"):
        run(flow, "Magnolia. Vorremmo partecipare e speriamo di essere pronti per settembre.")
    assert store.list_pending() == []


def test_functiongemma_prefilter_gets_compact_google_workspace_target():
    registry = ToolRegistry.load("config/local_arch_tools_v1.json")
    decision = LocalRouter(registry).classify(
        "Cerca la mail di ARCI Magnolia, prepara la risposta e mandami la bozza su Telegram"
    )
    assert decision.route.a == "ET"
    assert decision.route.t == "google_workspace.gmail"
    assert "google_workspace.gmail" in decision.candidates


def test_empty_search_does_not_invent(tmp_path):
    data = responses()
    data["search"] = {"messages": []}
    flow, store, session = workflow(tmp_path, data)
    with pytest.raises(GoogleWorkspaceError, match="search_empty"):
        run(flow, "Magnolia")
    assert store.list_pending() == []


def test_llm_ids_and_write_attempt_are_rejected():
    gateway = GoogleWorkspaceGateway(FakeSession(responses()), account="fabio@tiremminnanz.com")
    gateway.discover()
    with pytest.raises(GoogleWorkspaceError, match="unobserved"):
        gateway.validate_observed(message_id="invented")
    with pytest.raises(ApprovalRequired):
        gateway.invoke("send", to="x@example.org")


def test_modified_draft_stale_expired_and_replay(tmp_path):
    flow, store, _ = workflow(tmp_path)
    result = run(flow, "Magnolia")
    req = result["approval"]
    decision = DomainApprovalDecision(req["request_id"], "approve", 111, 111, 9, idempotency_key="once")
    assert store.decide(decision, scope_digest_short=req["scope_digest_short"])["status"] == "approved"
    assert store.decide(decision, scope_digest_short=req["scope_digest_short"])["status"] == "approved"
    changed = dict(store.get_request(req["request_id"])["scope"])
    changed["body"] += "!"
    with pytest.raises(ApprovalRequired, match="stale"):
        verify_approved_email_scope(store, req["request_id"], changed)
    assert store.get_request(req["request_id"])["status"] == "stale"

    flow2, store2, _ = workflow(tmp_path / "expired", ttl=-1)
    result2 = run(flow2, "Magnolia")
    with store2.connect() as conn:
        conn.execute("update approval_requests set status='approved' where request_id=?", (result2["approval"]["request_id"],))
    with pytest.raises(ApprovalRequired, match="expired"):
        verify_approved_email_scope(store2, result2["approval"]["request_id"], store2.get_request(result2["approval"]["request_id"])["scope"])


def test_telegram_failure_never_claims_delivery(tmp_path):
    flow, _, _ = workflow(tmp_path)
    result = run(flow, "Magnolia")
    assert result["telegram"] == {"status": "queued_not_verified", "message_ids": []}


def test_delivery_state_returns_real_message_id(tmp_path):
    flow, _, _ = workflow(tmp_path)
    original = flow.store.create_request
    # Predetermine an ID by wrapping creation, then simulate Meowgram state after queue.
    captured = {}
    def create(**kwargs):
        out = original(**kwargs)
        captured["id"] = out["request"]["request_id"]
        (tmp_path / "state.json").write_text(json.dumps({"delivered_request_ids": [captured["id"]], "reply_map": {"111:456": {"request_id": captured["id"]}}}))
        return out
    flow.store.create_request = create
    result = run(flow, "Magnolia")
    assert result["telegram"] == {"status": "delivered", "message_ids": [456]}


def test_workflow_cannot_self_assign_route(tmp_path):
    flow, _, _ = workflow(tmp_path)
    with pytest.raises(TypeError):
        flow.run("Magnolia")


def test_approved_reply_is_exactly_once_and_bound(tmp_path):
    flow, store, session = workflow(tmp_path)
    result = run(flow)
    scope = store.get_request(result["approval"]["request_id"])["scope"]
    request_id = result["approval"]["request_id"]
    with pytest.raises(ApprovalRequired):
        flow.gateway.reply_approved(store, request_id, scope)
    assert not [c for c in session.calls if c[1]["operation"] == "reply"]
    decision = DomainApprovalDecision(request_id, "approve", 111, 111, 9, idempotency_key="reply-once")
    store.decide(decision, scope_digest_short=result["approval"]["scope_digest_short"])
    session.responses["reply"] = {"id": "sent-id"}
    flow.gateway.reply_approved(store, request_id, scope)
    replies = [c for c in session.calls if c[1]["operation"] == "reply"]
    assert len(replies) == 1 and replies[0][1]["body"] == scope["body"]
    with pytest.raises(ApprovalRequired):
        flow.gateway.reply_approved(store, request_id, scope)
    assert len([c for c in session.calls if c[1]["operation"] == "reply"]) == 1


@pytest.mark.parametrize("field,value", [("body", "changed"), ("recipient", "other@example.org"), ("thread_id", "other")])
def test_changed_approved_scope_never_replies(tmp_path, field, value):
    flow, store, session = workflow(tmp_path)
    result = run(flow)
    request_id = result["approval"]["request_id"]
    scope = dict(store.get_request(request_id)["scope"]); scope[field] = value
    store.decide(DomainApprovalDecision(request_id, "approve", 1, 1, 1, idempotency_key=field), scope_digest_short=result["approval"]["scope_digest_short"])
    with pytest.raises(ApprovalRequired):
        flow.gateway.reply_approved(store, request_id, scope)
    assert not [c for c in session.calls if c[1]["operation"] == "reply"]
