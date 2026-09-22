from __future__ import annotations

import json
from contextlib import contextmanager
import pytest

from ralfloop_agent.domains.email_reply import build_email_reply_domain
from ralfloop_agent.semantic_judge import (
    compact_critic_context,
    DeepSeekV4FlashJudge,
    LlamaCppSemanticJudge,
    JudgeAvailabilityError,
    ReviewRisk,
    SemanticJudgeConfig,
    SemanticIssue,
    SemanticReview,
    SemanticReviewResult,
    classify_email_risk,
    parse_semantic_review,
    parse_compact_patch,
    expand_compact_patch,
)
from ralfloop_agent.semantic_judge.core import semantic_prompt
from ralfloop_agent.semantic_judge.ds4_server import (
    Ds4ServerDiagnostics,
    Ds4ServerReply,
    classify_runtime_failure,
    diagnostics_from_log,
)


VALID = {"verdict": "pass", "issues": [], "summary": "Nessuna incongruenza."}


def domain_packet():
    packet = {
        "source_email": {"subject": "x", "body": "Invito ricevuto.", "thread_context": []},
        "organization_context": {"relevant_facts": ["Fatto verificato."], "signature": {}},
        "user_intent": ["rispondi"],
        "reply_constraints": {"side_effects_allowed": False},
    }
    packet["email_reply_domain_v1"] = build_email_reply_domain(packet).model_dump(mode="json")
    return packet


def test_glm_provider_is_not_available_in_bottazzi_runtime():
    from ralfloop_agent.semantic_judge.core import build_semantic_judge
    with pytest.raises(JudgeAvailabilityError, match="unknown_provider"):
        build_semantic_judge(SemanticJudgeConfig(semantic_judge_provider="glm_colibri"))


def test_valid_json_accepted_and_schema_is_strict():
    assert parse_semantic_review(json.dumps(VALID)).verdict == "pass"
    with pytest.raises(ValueError, match="malformed_json"):
        parse_semantic_review("```json\n{}\n```")
    with pytest.raises(ValueError, match="schema_invalid"):
        parse_semantic_review(json.dumps({**VALID, "extra": True}))


def test_malformed_and_unknown_verdict_rejected():
    with pytest.raises(ValueError, match="malformed_json"):
        parse_semantic_review("{")
    with pytest.raises(ValueError, match="schema_invalid"):
        parse_semantic_review(json.dumps({**VALID, "verdict": "accept"}))


def test_empty_unknown_issue_and_excessive_output_are_rejected():
    with pytest.raises(ValueError, match="empty_output"):
        parse_semantic_review(" ")
    unknown = {"verdict": "repair", "issues": [{"type": "made_up", "draft_text": "x", "reason": "x", "domain_refs": []}], "summary": "x"}
    with pytest.raises(ValueError, match="schema_invalid"):
        parse_semantic_review(json.dumps(unknown))
    with pytest.raises(ValueError, match="output_too_long"):
        parse_semantic_review(json.dumps(VALID) + "x" * 100, max_bytes=32)
    with pytest.raises(ValueError, match="schema_invalid"):
        parse_semantic_review(json.dumps({"verdict": "repair", "issues": [], "summary": "x"}))


@pytest.mark.parametrize("reason", ["could be improved", "Be clearer please", "Check consistency with domain"])
def test_vague_issues_are_not_actionable_for_automatic_repair(reason):
    raw = {
        "verdict": "repair",
        "issues": [{
            "type": "unsupported_claim", "severity": "medium", "draft_text": "claim",
            "reason": reason, "domain_refs": ["fact_1"],
        }],
        "summary": "Repair.",
    }
    with pytest.raises(ValueError, match="schema_invalid"):
        parse_semantic_review(json.dumps(raw))


def test_issue_requires_concrete_excerpt_and_domain_reference():
    base = {
        "verdict": "repair",
        "issues": [{
            "type": "unsupported_commitment", "severity": "high", "draft_text": "",
            "reason": "No commitment exists in authoritative domain.", "domain_refs": ["contact_after_review"],
        }],
        "summary": "Repair.",
    }
    with pytest.raises(ValueError, match="schema_invalid"):
        parse_semantic_review(json.dumps(base))
    base["issues"][0]["draft_text"] = "Vi ricontatteremo."
    base["issues"][0]["domain_refs"] = []
    with pytest.raises(ValueError, match="schema_invalid"):
        parse_semantic_review(json.dumps(base))


def test_policy_low_bypasses_but_enabled_normal_and_high_use_slow_critic():
    normal_opt_in = SemanticJudgeConfig(True, semantic_judge_allow_normal=True)
    high_only = SemanticJudgeConfig(True)
    assert not normal_opt_in.should_use(ReviewRisk.LOW)
    assert normal_opt_in.should_use(ReviewRisk.NORMAL)
    assert not high_only.should_use(ReviewRisk.NORMAL)
    assert high_only.should_use(ReviewRisk.HIGH)
    assert not SemanticJudgeConfig(False).should_use(ReviewRisk.HIGH)


def test_high_risk_classifier_covers_commitments_money_legal_and_partnership():
    for text in ("impegno formale", "costo 50 euro", "questione legale", "partnership", "candidatura al bando"):
        assert classify_email_risk({"user_intent": [text]}) is ReviewRisk.HIGH
    assert classify_email_risk({"user_intent": ["saluta e ringrazia"]}) is ReviewRisk.NORMAL


def test_prompt_has_review_context_but_no_recipient_or_mutation_authority():
    packet = domain_packet()
    packet["source_email"].update({"message_id": "secret-id", "thread_id": "secret-thread", "sender_email": "to@example.org"})
    packet.update({"gmail_write_tools": ["reply", "send"], "recipient_mutation_authority": True})
    prompt = semantic_prompt(packet, "bozza")
    assert "Invito ricevuto" in prompt and "bozza" in prompt and "Fatto verificato" in prompt
    assert "DOMAIN is authoritative" in prompt
    assert "secret-id" not in prompt and "secret-thread" not in prompt and "to@example.org" not in prompt
    assert "gmail_write_tools" not in prompt and "recipient_mutation_authority" not in prompt


def test_semantic_prompt_is_bounded_for_document_heavy_domain():
    packet = domain_packet()
    packet["user_intent"] = ["interest true, participation role unresolved " + "x" * 2000]
    packet["email_reply_domain_v1"]["supported_facts"] = [
        {
            "key": f"fact_{index}", "statement": "evidence " + "x" * 2000,
            "certainty": "asserted", "evidence_refs": [f"structured_artifacts[0].facts[{index}]"],
            "actor_refs": [],
        }
        for index in range(64)
    ]

    prompt = semantic_prompt(packet, "draft " + "y" * 700)

    assert len(prompt.encode("utf-8")) < 5000


def test_deepseek_fails_closed_without_runtime():
    config = SemanticJudgeConfig(deepseek_executable="/missing/ds4", deepseek_model_path="/missing/model.gguf")
    with pytest.raises(JudgeAvailabilityError, match="server_unavailable"):
        DeepSeekV4FlashJudge(config=config).review(domain_packet(), "draft")


def test_llama_cpp_judge_is_review_only_and_strict():
    class Manager:
        calls = 0

        def ensure_available(self):
            self.calls += 1
            return {"server_reused": True}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "model": "qwen2.5:7b",
                "choices": [{"message": {"content": json.dumps(VALID)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 8},
            }

        def close(self):
            return None

    class Session:
        payload = None

        def post(self, _url, *, json, timeout):
            self.payload = json
            assert timeout == (2.0, 180.0)
            return Response()

    manager, session = Manager(), Session()
    result = LlamaCppSemanticJudge(
        manager=manager, session=session,
    ).review(
        domain_packet(), "Bozza"
    )

    assert manager.calls == 1
    assert result.review.verdict == "pass"
    assert result.provider == "llama_cpp"
    assert result.model == "qwen2.5:7b"
    assert result.metadata["json_schema_constrained"] is True
    messages = session.payload["messages"]
    assert "never rewrite" in messages[0]["content"]
    assert "DOMAIN is authoritative" in messages[1]["content"]
    assert session.payload["response_format"]["type"] == "json_schema"


class FakeScheduler:
    def __init__(self):
        self.events = []

    @contextmanager
    def engine_session(self, engine, *, task_id=""):
        self.events.append(("enter", engine, task_id))
        try:
            yield {"engine": engine}
        finally:
            self.events.append(("exit", engine, task_id))


def runtime_config(tmp_path):
    executable = tmp_path / "ds4-server"
    executable.write_text("fake")
    executable.chmod(0o700)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"fake")
    return SemanticJudgeConfig(
        semantic_judge_enabled=True,
        deepseek_executable=str(executable),
        deepseek_model_path=str(model),
        semantic_judge_timeout_sec=123,
    )


class FakeServer:
    def __init__(self, profile, *, reply=None, error=None):
        self.profile = profile
        self.reply = reply or Ds4ServerReply(
            content=json.dumps(VALID), request_ms=9, input_tokens=20, output_tokens=8, usage={},
            diagnostics=Ds4ServerDiagnostics(1, 0, 43, 0, 0, 1),
            prefill_ms=4, decode_ms=5,
        )
        self.error = error
        self.startup_ms = 7
        self.events = []

    def __enter__(self):
        self.events.append("enter")
        return self

    def review_prompt(self, prompt, *, max_tokens=None):
        self.events.append(("review", prompt, max_tokens))
        if self.error:
            raise self.error
        return self.reply

    def __exit__(self, *_args):
        self.events.append("exit")


def test_deepseek_runtime_profile_is_validated_and_uses_gpu_scheduler(tmp_path):
    scheduler = FakeScheduler()
    servers = []

    def factory(profile):
        servers.append(FakeServer(profile))
        return servers[-1]

    judge = DeepSeekV4FlashJudge(config=runtime_config(tmp_path), scheduler=scheduler, server_factory=factory)
    result = judge.review(domain_packet(), "Bozza")
    command = judge.command()
    assert result.review.verdict == "pass"
    assert command[command.index("--backend") + 1] == "cuda"
    assert command[command.index("--ctx") + 1] == "1024"
    assert command[command.index("--prefill-chunk") + 1] == "128"
    assert "--ssd-streaming" in command and "--cuda-low-vram-stream" in command and "--ssd-streaming-cold" in command
    assert "--gpu-vram" not in command and "--gpu-devices" not in command
    assert "--ssd-streaming-cache-experts" not in command
    assert judge.environment_overrides() == {
        "DS4_CUDA_LOW_VRAM_STAGE_MB": "1280",
        "DS4_CUDA_LOW_VRAM_RESERVE_MB": "384",
        "DS4_CUDA_WEIGHT_CACHE_VERBOSE": "1",
        "DS4_CUDA_WEIGHT_CACHE_LIMIT_GB": "3",
    }
    request = judge.request_payload("prompt")
    assert request["model"] == "deepseek-chat" and request["temperature"] == 0 and request["top_p"] == 1
    assert request["thinking"] == {"type": "disabled"} and request["think"] is False
    assert "seed" not in request and request["max_tokens"] == 128
    assert result.metadata["critic_protocol"] == "semantic_review_v1"
    assert result.metadata["prefill_ms"] == 4 and result.metadata["decode_ms"] == 5
    assert result.metadata["diagnostics"] == {
        "early_prealloc": 1, "lazy_alloc": 0, "fences": 43,
        "up_expert_oom": 0, "failed": 0, "host_registration_skipped": 1,
    }
    assert scheduler.events[0][0] == "enter" and scheduler.events[-1][0] == "exit"
    assert servers[0].events[0] == "enter" and servers[0].events[-1] == "exit"


def test_compact_patch_is_strict_bounded_and_expands_deterministically():
    compact = compact_critic_context(
        domain_packet(), "Grazie per i documenti e vi contatteremo domani."
    )
    assert compact.prompt.startswith("Email semantic critic.")
    assert len(compact.draft_clauses) == 2
    forbidden = next(index for index, item in enumerate(compact.entries) if item.domain_ref == "contact_after_review")
    patch = parse_compact_patch(
        json.dumps({"v": "R", "t": "UC", "d": 1, "e": forbidden}), compact,
    )
    review = expand_compact_patch(patch, compact)
    assert review.verdict == "repair"
    assert review.issues[0].type == "unsupported_commitment"
    assert review.issues[0].draft_text == "vi contatteremo domani"
    assert review.issues[0].domain_refs == ["contact_after_review"]
    assert parse_compact_patch('{"v":"P"}', compact).v == "P"
    for invalid in (
        '{"v":"P","t":null}',
        '{"v":"R","t":"BAD","d":1,"e":0}',
        '{"v":"R","t":"MR","d":0,"e":0}',
        '{"v":"R","t":"UC","d":99,"e":0}',
    ):
        with pytest.raises(ValueError):
            parse_compact_patch(invalid, compact)


def test_deepseek_timeout_still_exits_scheduler_session(tmp_path):
    scheduler = FakeScheduler()

    judge = DeepSeekV4FlashJudge(
        config=runtime_config(tmp_path), scheduler=scheduler,
        server_factory=lambda profile: FakeServer(profile, error=TimeoutError("semantic_judge_timeout")),
    )
    with pytest.raises(TimeoutError, match="semantic_judge_timeout"):
        judge.review(domain_packet(), "Bozza")
    assert [event[0] for event in scheduler.events] == ["enter", "exit"]


def test_runtime_error_classification_ignores_nonfatal_host_registration_oom():
    harmless = (
        "ds4: CUDA host registration skipped: out of memory\n"
        "ds4: CUDA low-VRAM prefill fence after layer 0\n"
        "ds4-server: listening"
    )
    assert classify_runtime_failure(harmless) == "runtime_error"
    assert diagnostics_from_log(harmless).host_registration_skipped == 1
    assert diagnostics_from_log(harmless).fences == 1
    fatal = harmless + "\nds4: CUDA streaming up experts allocation failed for 528 MiB: out of memory"
    assert classify_runtime_failure(fatal) == "gpu_out_of_memory"
    values = diagnostics_from_log(fatal)
    assert values.up_expert_oom == 1 and values.failed == 1
    async_oom = "CUDA error: out of memory\n  call: cudaMallocAsync(&ptr, size, stream)"
    assert classify_runtime_failure(async_oom) == "gpu_out_of_memory"
    assert diagnostics_from_log(async_oom).failed == 1


class FakeJudge:
    provider = "fake_big"
    model = "fake-model"

    def __init__(self, review=None, error=None):
        self.value = review or SemanticReview.model_validate(VALID)
        self.error = error
        self.calls = []

    def review(self, context, draft):
        self.calls.append((context, draft))
        if self.error:
            raise self.error
        return SemanticReviewResult(self.value, self.provider, self.model, 12, 20, 10)


def test_fake_judge_exposes_pass_repair_and_timeout_contract():
    assert FakeJudge().review({}, "x").review.verdict == "pass"
    repair = SemanticReview(
        verdict="repair",
        issues=[SemanticIssue(type="contradiction", draft_text="approvata", reason="Domain says pending", domain_refs=["proposal_approved"])],
        summary="Contraddizione.",
    )
    assert FakeJudge(repair).review({}, "x").review.issues[0].type == "contradiction"
    with pytest.raises(TimeoutError):
        FakeJudge(error=TimeoutError("semantic_judge_timeout")).review({}, "x")
