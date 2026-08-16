from __future__ import annotations

import os

import pytest

from ralfloop_agent.domains.email_reply import build_email_reply_domain
from ralfloop_agent.semantic_judge import DeepSeekV4FlashJudge, SemanticJudgeConfig


@pytest.mark.integration
@pytest.mark.skipif(
    os.getenv("RALFLOOP_TEST_DS4_RUNTIME") != "1",
    reason="set RALFLOOP_TEST_DS4_RUNTIME=1 for guarded real DS4/GPU test",
)
def test_real_ds4_semantic_critic_optional():
    packet = {
        "source_email": {
            "subject": "Documenti e proposta",
            "body": "Abbiamo ricevuto i documenti. La proposta è ancora da valutare.",
            "thread_context": [],
        },
        "organization_context": {"relevant_facts": [], "signature": {}},
        "user_intent": [
            "Confermare la ricezione dei documenti",
            "Dire che la proposta sarà valutata la settimana prossima",
        ],
        "reply_constraints": {"side_effects_allowed": False},
    }
    packet["email_reply_domain_v1"] = build_email_reply_domain(packet).model_dump(mode="json")
    result = DeepSeekV4FlashJudge(config=SemanticJudgeConfig.from_env()).review(
        packet,
        "Confermiamo di aver ricevuto i documenti. La proposta sarà valutata la settimana prossima.",
    )
    assert result.review.verdict in {"pass", "repair"}
    assert result.provider == "deepseek_v4_flash"
    diagnostics = result.metadata["diagnostics"]
    assert diagnostics["early_prealloc"] >= 1
    assert diagnostics["lazy_alloc"] == 0
    # One complete 43-layer fence set is emitted per prefill chunk.
    assert diagnostics["fences"] >= 43 and diagnostics["fences"] % 43 == 0
    assert diagnostics["up_expert_oom"] == 0
    assert diagnostics["failed"] == 0
    assert result.metadata["request_ms"] > 0
