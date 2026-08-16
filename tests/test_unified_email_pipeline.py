from __future__ import annotations

from types import SimpleNamespace

from ralfloop_agent.semantic_judge import SemanticIssue, SemanticJudgeConfig, SemanticReview, SemanticReviewResult
from ralfloop_agent.unified_assistant.email import EmailWorkingMemoryBuilder
from ralfloop_agent.unified_assistant.email_pipeline import GenericEmailPipeline
from ralfloop_agent.unified_assistant.memory import MemoryRouter
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from src.google_workspace import DraftGeneration


def working():
    registry = UnifiedRegistryFacade()
    return EmailWorkingMemoryBuilder(MemoryRouter(), registry.domain("email")).build(
        objective="Ringrazia Sonia e conferma che abbiamo ricevuto i documenti",
        recipient="Sonia",
    )


class FakeGenerator:
    def __init__(self, first, repaired=None):
        self.first = first
        self.repaired = repaired or first
        self.generate_calls = 0
        self.repair_calls = 0

    def generate(self, packet):
        self.generate_calls += 1
        return DraftGeneration(self.first, "fake-qwen", "qwen", False)

    def generate_repair(self, packet, body, reason, *, semantic_issues=None):
        self.repair_calls += 1
        return DraftGeneration(self.repaired, "fake-qwen", "qwen", False)


class FakeJudge:
    provider = "deepseek_v4_flash"
    model = "fake-ds4"

    def __init__(self):
        self.calls = 0

    def review(self, packet, draft):
        self.calls += 1
        return SemanticReviewResult(
            SemanticReview(verdict="repair", issues=[SemanticIssue(
                type="unsupported_commitment", severity="high",
                draft_text="Vi contatteremo", reason="Commitment absent from authoritative domain.",
                domain_refs=["forbidden_commitments_without_evidence.contact_after_review"],
            )]),
            self.provider, self.model, 10,
        )


class MissingMeaningJudge:
    provider = "llama_cpp"
    model = "qwen2.5:7b"

    def review(self, packet, draft):
        return SemanticReviewResult(
            SemanticReview(
                verdict="repair",
                issues=[SemanticIssue(
                    type="missing_required_meaning",
                    severity="medium",
                    draft_text="",
                    reason="Participation interest appears absent.",
                    domain_refs=["participation_interest"],
                )],
            ),
            self.provider,
            self.model,
            10,
        )


def test_generic_pipeline_safe_domain_passes_without_ds4():
    generator = FakeGenerator("Grazie Sonia, confermiamo di aver ricevuto i documenti.")
    result = GenericEmailPipeline(generator).compose(working())

    assert result.hard_guard == "passed"
    assert result.final_validator == "passed"
    assert not result.ds4_invoked
    assert generator.repair_calls == 0


def test_hard_guard_blocks_invented_approval_before_ds4():
    generator = FakeGenerator(
        "Grazie Sonia, confermiamo di aver ricevuto i documenti. La proposta è approvata."
    )
    judge = FakeJudge()
    result = GenericEmailPipeline(
        generator,
        semantic_config=SemanticJudgeConfig(semantic_judge_enabled=True),
        semantic_judge=judge,
    ).compose(working())

    assert result.hard_guard == "draft_domain_forbidden_claim"
    assert result.final_validator == "blocked"
    assert judge.calls == 0


def test_uncertain_participation_gets_one_guarded_repair(monkeypatch):
    monkeypatch.setattr(
        "ralfloop_agent.unified_assistant.email_pipeline.assess_email_risk",
        lambda *_: SimpleNamespace(level="normal"),
    )
    registry = UnifiedRegistryFacade()
    context = EmailWorkingMemoryBuilder(
        MemoryRouter(), registry.domain("email")
    ).build(
        objective=(
            "Dire che vorremmo partecipare e speriamo di essere pronti "
            "per settembre"
        ),
        recipient="Caterina",
        source_email={"body": "Siete invitati al festival."},
    )
    generator = FakeGenerator(
        (
            "Vorremmo partecipare e speriamo di essere pronti per settembre; "
            "siamo interessati e parteciperemo."
        ),
        repaired=(
            "Vorremmo partecipare e speriamo di essere pronti per settembre."
        ),
    )

    result = GenericEmailPipeline(generator).compose(context)

    assert generator.repair_calls == 1
    assert result.hard_guard == "passed"
    assert result.repair_count == 1
    assert result.final_validator == "passed"


def test_missing_required_meaning_gets_one_guarded_repair(monkeypatch):
    monkeypatch.setattr(
        "ralfloop_agent.unified_assistant.email_pipeline.assess_email_risk",
        lambda *_: SimpleNamespace(level="normal"),
    )
    registry = UnifiedRegistryFacade()
    context = EmailWorkingMemoryBuilder(
        MemoryRouter(), registry.domain("email")
    ).build(
        objective=(
            "Dire che vorremmo partecipare e speriamo di essere pronti "
            "per settembre"
        ),
        recipient="Caterina",
        source_email={"body": "Siete invitati al festival."},
    )
    generator = FakeGenerator(
        "Grazie per l'invito.",
        repaired=(
            "Vorremmo partecipare e speriamo di essere pronti per settembre."
        ),
    )

    result = GenericEmailPipeline(generator).compose(context)

    assert generator.repair_calls == 1
    assert result.hard_guard == "passed"
    assert result.repair_count == 1
    assert result.final_validator == "passed"


def test_failed_model_repair_uses_domain_bounded_minimal_fallback(monkeypatch):
    monkeypatch.setattr(
        "ralfloop_agent.unified_assistant.email_pipeline.assess_email_risk",
        lambda *_: SimpleNamespace(level="normal"),
    )
    registry = UnifiedRegistryFacade()
    context = EmailWorkingMemoryBuilder(
        MemoryRouter(), registry.domain("email")
    ).build(
        objective=(
            "Dire che vorremmo partecipare e speriamo di essere pronti "
            "per settembre"
        ),
        recipient="Caterina",
        source_email={"body": "Siete invitati al festival."},
    )
    generator = FakeGenerator(
        "Grazie per l'invito.",
        repaired=(
            "Vorremmo partecipare e speriamo di essere pronti per settembre; "
            "siamo interessati e parteciperemo."
        ),
    )

    result = GenericEmailPipeline(generator).compose(context)

    assert result.body == (
        "Buongiorno,\n\n"
        "grazie per l'invito. Vorremmo partecipare e speriamo di essere "
        "pronti per settembre.\n\n"
        "Un saluto,\nTiremm Innanz APS"
    )
    assert result.hard_guard == "passed"
    assert result.repair_count == 1
    assert result.final_validator == "passed"
    assert result.trace["deterministic_guarded_fallback"] is True


def test_domain_canonicalization_removes_unrequested_stand(monkeypatch):
    monkeypatch.setattr(
        "ralfloop_agent.unified_assistant.email_pipeline.assess_email_risk",
        lambda *_: SimpleNamespace(level="normal"),
    )
    registry = UnifiedRegistryFacade()
    context = EmailWorkingMemoryBuilder(
        MemoryRouter(), registry.domain("email")
    ).build(
        objective=(
            "Dire che vorremmo partecipare e speriamo di essere pronti "
            "per settembre"
        ),
        recipient="Caterina",
        source_email={"body": "Siete invitati al festival con eventuali stand."},
    )
    generator = FakeGenerator(
        "Vorremmo partecipare con uno stand e speriamo di essere pronti "
        "per settembre."
    )

    result = GenericEmailPipeline(generator).compose(context)

    assert "stand" not in result.body.casefold()
    assert result.final_validator == "passed"
    assert result.trace["deterministic_guarded_fallback"] is True


def test_deterministic_validator_overrides_matching_critic_false_positive(
    monkeypatch,
):
    monkeypatch.setattr(
        "ralfloop_agent.unified_assistant.email_pipeline.assess_email_risk",
        lambda *_: SimpleNamespace(level="high"),
    )
    registry = UnifiedRegistryFacade()
    context = EmailWorkingMemoryBuilder(
        MemoryRouter(), registry.domain("email")
    ).build(
        objective=(
            "Dire che vorremmo partecipare e speriamo di essere pronti "
            "per settembre"
        ),
        recipient="Caterina",
        source_email={"body": "Siete invitati al festival."},
    )
    generator = FakeGenerator(
        "Vorremmo partecipare e speriamo di essere pronti per settembre."
    )
    result = GenericEmailPipeline(
        generator,
        semantic_config=SemanticJudgeConfig(semantic_judge_enabled=True),
        semantic_judge=MissingMeaningJudge(),
    ).compose(context)

    assert result.hard_guard == "passed"
    assert result.final_validator == "passed"
    assert result.trace["semantic_judge_deterministic_override"] is True


def test_high_ds4_repairs_once_then_final_validator_decides(monkeypatch):
    monkeypatch.setattr(
        "ralfloop_agent.unified_assistant.email_pipeline.assess_email_risk",
        lambda *_: SimpleNamespace(level="high"),
    )
    generator = FakeGenerator(
        "Grazie Sonia, confermiamo di aver ricevuto i documenti.",
        repaired="Grazie Sonia, confermiamo di aver ricevuto i documenti.",
    )
    judge = FakeJudge()
    result = GenericEmailPipeline(
        generator,
        semantic_config=SemanticJudgeConfig(semantic_judge_enabled=True),
        semantic_judge=judge,
    ).compose(working())

    assert result.ds4_invoked
    assert judge.calls == 1
    assert generator.repair_calls == 1
    assert result.repair_count == 1
    assert result.final_validator == "passed"
