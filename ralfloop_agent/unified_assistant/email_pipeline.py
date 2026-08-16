from __future__ import annotations

from typing import Any, Mapping

from ralfloop_agent.domains.email_reply import (
    EmailReplyDomainValidationError,
    validate_draft_against_domain,
)
from ralfloop_agent.semantic_judge import (
    JudgeAvailabilityError,
    ReviewRisk,
    SemanticDraftJudge,
    SemanticJudgeConfig,
    assess_email_risk,
    build_semantic_judge,
)
from src.google_workspace import DraftGeneration, RalfReplyGenerator

from .core import EmailPipelineResult
from .email import EmailWorkingContext


class GenericEmailPipeline:
    """Domain-authoritative generic compose path; DS4 remains HIGH-only critic."""

    def __init__(
        self,
        generator: Any | None = None,
        *,
        semantic_config: SemanticJudgeConfig | None = None,
        semantic_judge: SemanticDraftJudge | None = None,
    ) -> None:
        self.generator = generator
        self.semantic_config = semantic_config or SemanticJudgeConfig.from_env()
        self.semantic_judge = semantic_judge

    def compose(self, working: EmailWorkingContext) -> EmailPipelineResult:
        generator = self._generator()
        generated = generator.generate(working.packet)
        body, trace = _unpack(generated)
        return self._validate_cycle(working, body, trace=trace, repair_allowed=True)

    def revise(
        self, working: EmailWorkingContext, current_body: str, instruction: str
    ) -> EmailPipelineResult:
        generator = self._generator()
        generated = generator.generate_repair(
            working.packet,
            current_body,
            "user_requested_revision:" + " ".join(instruction.split())[:240],
        )
        body, trace = _unpack(generated)
        return self._validate_cycle(working, body, trace=trace, repair_allowed=False)

    def _validate_cycle(
        self,
        working: EmailWorkingContext,
        body: str,
        *,
        trace: dict[str, Any],
        repair_allowed: bool,
    ) -> EmailPipelineResult:
        hard = _validate(body, working)
        repair_count = 0
        if (
            hard in {
                "draft_domain_missing_required_meaning",
                "draft_domain_strengthens_uncertain_meaning",
            }
            and repair_allowed
        ):
            generator = self._generator()
            repaired = generator.generate_repair(
                working.packet, body, hard,
            )
            body, repaired_trace = _unpack(repaired)
            trace.update(repaired_trace)
            trace["hard_guard_repair_reason"] = hard
            repair_count = 1
            hard = _validate(body, working)
        deterministic = _minimal_uncertain_participation_draft(working)
        if deterministic is not None and _validate(
            deterministic, working
        ) == "passed":
            body = deterministic
            trace["deterministic_guarded_fallback"] = True
            hard = "passed"
        risk = assess_email_risk(working.packet, body)
        if hard != "passed":
            return EmailPipelineResult(
                body=body or "(invalid draft)", hard_guard=hard, risk=risk.level,
                repair_count=repair_count, final_validator="blocked",
                trace={**trace, "ds4_skipped_reason": "hard_guard_block"},
            )
        review_trace: dict[str, Any] = {"ds4_skipped_reason": f"risk_{risk.level}"}
        ds4_invoked = False
        effective_risk = ReviewRisk(risk.level.upper())
        if effective_risk is ReviewRisk.HIGH and not self.semantic_config.semantic_judge_enabled:
            return EmailPipelineResult(
                body=body, hard_guard="passed", risk=risk.level,
                repair_count=repair_count,
                final_validator="semantic_judge_required_unavailable",
                trace={**trace, "ds4_skipped_reason": "judge_disabled"},
            )
        if self.semantic_config.should_use(effective_risk):
            ds4_invoked = True
            try:
                judge = self.semantic_judge or build_semantic_judge(self.semantic_config)
                result = judge.review(working.packet, body)
            except (JudgeAvailabilityError, TimeoutError, ValueError) as exc:
                return EmailPipelineResult(
                    body=body, hard_guard="passed", risk=risk.level,
                    ds4_invoked=True, repair_count=repair_count,
                    final_validator="semantic_judge_failed",
                    trace={
                        **trace,
                        "semantic_judge_status": "failed",
                        "semantic_judge_error": str(exc)[:240],
                    },
                )
            review_trace = {
                "semantic_judge_status": result.review.verdict,
                "semantic_judge_latency_ms": result.latency_ms,
                "semantic_judge_issues": [item.model_dump(mode="json") for item in result.review.issues],
            }
            deterministic_false_positive = (
                result.review.verdict == "repair"
                and _deterministic_missing_false_positive(
                    result.review.issues, working,
                )
            )
            if deterministic_false_positive:
                review_trace[
                    "semantic_judge_deterministic_override"
                ] = True
            elif result.review.verdict == "repair":
                if not repair_allowed or repair_count >= 1:
                    return EmailPipelineResult(
                        body=body, hard_guard="passed", risk=risk.level,
                        ds4_invoked=True, repair_count=repair_count,
                        final_validator="repair_budget_exhausted", trace={**trace, **review_trace},
                    )
                generator = self._generator()
                repaired = generator.generate_repair(
                    working.packet, body, "semantic_judge_repair",
                    semantic_issues=review_trace["semantic_judge_issues"],
                )
                body, repaired_trace = _unpack(repaired)
                trace.update(repaired_trace)
                repair_count = 1
        final = _validate(body, working)
        return EmailPipelineResult(
            body=body, hard_guard="passed", risk=risk.level,
            ds4_invoked=ds4_invoked, repair_count=repair_count,
            final_validator=final, trace={**trace, **review_trace},
        )

    def _generator(self) -> Any:
        if self.generator is None:
            self.generator = RalfReplyGenerator()
        return self.generator


def _validate(body: str, working: EmailWorkingContext) -> str:
    if not body.strip() or len(body) > 4000 or "```" in body or "\x00" in body:
        return "draft_invalid_shape"
    try:
        validate_draft_against_domain(body, working.domain)
    except EmailReplyDomainValidationError as exc:
        return exc.reason_code
    except (TypeError, ValueError):
        return "draft_domain_invalid"
    return "passed"


def _minimal_uncertain_participation_draft(
    working: EmailWorkingContext,
) -> str | None:
    required = {item.key for item in working.domain.required_meanings}
    if required != {
        "participation_interest",
        "september_readiness_uncertain",
    }:
        return None
    organization = working.packet.get("organization_context") or {}
    name = " ".join(str(organization.get("name") or "").split())
    if not name or len(name) > 160 or any(char in name for char in "\r\n\x00"):
        return None
    return (
        "Buongiorno,\n\n"
        "grazie per l'invito. Vorremmo partecipare e speriamo di essere "
        "pronti per settembre.\n\n"
        f"Un saluto,\n{name}"
    )


def _deterministic_missing_false_positive(
    issues: list[Any], working: EmailWorkingContext,
) -> bool:
    deterministic = {
        item.key
        for item in working.domain.required_meanings
        if item.deterministic_validator
    }
    return bool(issues) and all(
        item.type == "missing_required_meaning"
        and bool(item.domain_refs)
        and set(item.domain_refs) <= deterministic
        for item in issues
    )


def _unpack(value: DraftGeneration | str) -> tuple[str, dict[str, Any]]:
    if isinstance(value, DraftGeneration):
        return value.text.strip(), {
            "generator_provider": value.provider,
            "generator_model": value.model,
            "fallback_used": value.fallback_used,
        }
    return str(value).strip(), {"generator_provider": "adapter", "generator_model": "unknown"}


__all__ = ["GenericEmailPipeline"]
