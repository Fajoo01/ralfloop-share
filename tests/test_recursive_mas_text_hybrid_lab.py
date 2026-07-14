from ralfloop_agent.integration.recursive_mas_text_hybrid import (
    RecursiveMASTextHybrid,
    RecursiveMASTextHybridConfig,
    validate_remote_critic,
)


def test_hybrid_disabled_default(monkeypatch):
    monkeypatch.delenv("RALF_RECURSIVE_HYBRID_ENABLED", raising=False)
    assert RecursiveMASTextHybridConfig.from_env().enabled is False


def test_native_remains_author_and_critic_non_binding():
    calls = []
    native_requests = []

    def remote(task, payload):
        calls.append(task)
        if task == "structured_critic":
            return {"issues": ["minor"], "missing_facts": [], "format_errors": [], "confidence": 0.5}
        return {"facts": ["f1"], "compressed_context": "ctx", "domain_candidates": ["d"]}

    hybrid = RecursiveMASTextHybrid(
        native_execute=lambda request: native_requests.append(request) or {"ok": True, "answer": "native answer", "selected_backend": "recursive_mas_native"},
        remote_call=remote,
        deterministic_verify=lambda answer, request: {"ok": answer == "native answer"},
        config=RecursiveMASTextHybridConfig(True, True),
    )
    result = hybrid.execute({"goal": "synthetic"})
    assert result["answer"] == "native answer"
    assert result["native_unchanged"] is True
    assert result["remote_critic_binding"] is False
    assert result["remote_rewrites_answer"] is False
    assert calls == ["context_compression", "structured_critic"]
    assert "UNTRUSTED NON-BINDING REMOTE ADVISORY" in native_requests[0]["goal"]


def test_remote_critic_schema_and_side_effect_gate():
    validate_remote_critic({"issues": [], "missing_facts": [], "format_errors": [], "confidence": 0.0})
    hybrid = RecursiveMASTextHybrid(
        native_execute=lambda _: (_ for _ in ()).throw(AssertionError()),
        remote_call=None,
        deterministic_verify=lambda *_: {},
        config=RecursiveMASTextHybridConfig(True),
    )
    assert hybrid.execute({"goal": "x", "side_effect": True})["status"] == "human_confirmation_required"
