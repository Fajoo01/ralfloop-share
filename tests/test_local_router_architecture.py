from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralfloop_agent.local_arch.contracts import ACTION_NAMES, CompactRoute, ContractError, WorkerDelta
from ralfloop_agent.local_arch.director import DIRECTOR_MAX_TOKENS, DIRECTOR_REPAIR_TOKENS, DirectorAdapter, DirectorContext
from ralfloop_agent.local_arch.policy import PROTECTED_ACTIONS, RalfPolicy
from ralfloop_agent.local_arch.router import FunctionGemmaClient, LocalRouter, ToolRegistry
from ralfloop_agent.local_arch.store import VersionedCache


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.load(ROOT / "config/local_arch_tools_v1.json")


def test_01_gguf_metadata_contract():
    config = json.loads((ROOT / "config/functiongemma_router_v1.json").read_text())
    assert config["filename"].endswith("q8_0.gguf")
    assert len(config["revision"]) == 40
    assert config["trust_remote_code"] is False


def test_02_chat_template_versioned_and_disabled_until_canary():
    template = (ROOT / "config/functiongemma_router_chat_template_v1.jinja").read_text()
    assert "add_generation_prompt" in template
    assert "Disabled fallback" in template


def test_03_server_cpu_only():
    script = (ROOT / "scripts/ralf_functiongemma_router").read_text()
    assert "--n-gpu-layers 0" in script
    assert "--threads 4" in script


def test_04_health_unavailable_is_false(monkeypatch):
    monkeypatch.setattr("ralfloop_agent.local_arch.router.urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert FunctionGemmaClient().health() is False


def test_05_loopback_enforced():
    with pytest.raises(ValueError, match="loopback"):
        FunctionGemmaClient("http://10.0.0.1:19104")


def test_06_parser_compact_json():
    route = CompactRoute.parse_model_output('{"v":1,"a":"AF","t":"shortest_path","c":0.94}')
    assert route.a == "AF"
    assert " " not in route.compact_json()


@pytest.mark.parametrize("action", tuple(ACTION_NAMES), ids=lambda action: f"valid_{action}")
def test_07_19_all_real_enum_actions(action):
    route = CompactRoute.from_mapping({"v": 1, "a": action, "t": "target", "c": 1.0})
    assert route.a == action


def test_20_invalid_action_rejected():
    with pytest.raises(ContractError, match="invalid_action"):
        CompactRoute.from_mapping({"v": 1, "a": "XX", "t": "x", "c": 1})


def test_21_pipe_enum_rejected():
    with pytest.raises(ContractError, match="invalid_action"):
        CompactRoute.from_mapping({"v": 1, "a": "ET|AF|LM", "t": "x", "c": 1})


def test_22_schema_echo_rejected():
    with pytest.raises(ContractError, match="schema_echo"):
        CompactRoute.parse_model_output('{"v":1,"a":"AF","t":"x","c":1,"properties":{}}')


def test_23_prose_rejected():
    with pytest.raises(ContractError, match="prose"):
        CompactRoute.parse_model_output('Scelgo: {"v":1,"a":"AF","t":"x","c":1}')


def test_24_hallucinated_tool_falls_back(registry):
    class Client:
        def health(self): return True
        def classify(self, text, catalog): return CompactRoute(1, "ET", "invented", c=0.99)
    result = LocalRouter(registry, Client()).classify("richiesta non deterministica")
    assert result.source == "fallback"


def test_25_low_confidence_falls_back(registry):
    class Client:
        def health(self): return True
        def classify(self, text, catalog): return CompactRoute(1, "AF", "shortest_path", c=0.1)
    assert LocalRouter(registry, Client()).classify("problema inedito").route.r == "LOW_CONFIDENCE"


def test_26_unavailable_router_fallback(registry):
    class Client:
        def health(self): return False
    result = LocalRouter(registry, Client()).classify("decisione strategica non ovvia")
    assert result.route.a == "LM"
    assert result.route.t == "bottazzi_motor"
    assert result.error == "router_unavailable"


def test_unavailable_router_protected_operation_still_asks_approval(registry):
    class Client:
        def health(self): return False
    result = LocalRouter(registry, Client()).classify("pubblica il post")
    assert result.route.a == "AP"
    assert not RalfPolicy().evaluate(result.route, operation="social_publish").allowed


def test_healthy_router_protected_operation_bypasses_model(registry):
    class Client:
        def health(self): return True
        def classify(self, text, catalog):
            raise AssertionError("FunctionGemma must not see obvious protected actions")

    result = LocalRouter(registry, Client()).classify("pubblica il post")
    assert result.source == "deterministic"
    assert result.route.a == "AP"
    assert result.route.t == "external_action"
    assert result.route.r == "PROTECTED_ACTION"
    assert not RalfPolicy().evaluate(result.route, operation="social_publish").allowed


def test_router_cache_requires_complete_binding(tmp_path):
    cache = VersionedCache(tmp_path)
    with pytest.raises(ValueError, match="incomplete"):
        cache.key({"input_hash": "x"})


def test_router_side_effect_never_cached(tmp_path):
    with pytest.raises(ValueError, match="side_effect"):
        VersionedCache(tmp_path).put("a" * 64, {}, side_effect=True)


def test_router_deterministic_algorithm(registry):
    result = LocalRouter(registry).classify("trova il percorso minimo")
    assert result.route.a == "AF" and result.route.t == "shortest_path"


def test_router_preflights_scanned_pdf_to_visual_rag_before_model(registry):
    class WrongClient:
        calls = 0
        def health(self): return True
        def classify(self, text, catalog):
            self.calls += 1
            return CompactRoute(1, "AB", "audiobook_factory", c=1.0)

    client = WrongClient()
    result = LocalRouter(registry, client).classify(
        "richiesta su PDF scannerizzato con una tabella"
    )
    assert (result.route.a, result.route.t) == ("VR", "visual_rag")
    assert result.source == "deterministic"
    assert client.calls == 0


def test_router_does_not_force_plain_pdf_audiobook_to_visual_rag(registry):
    class AudiobookClient:
        calls = 0
        def health(self): return True
        def classify(self, text, catalog):
            self.calls += 1
            return CompactRoute(1, "AB", "pdf", c=1.0)

    client = AudiobookClient()
    result = LocalRouter(registry, client).classify("crea un audiolibro dal PDF")
    assert client.calls == 1
    assert result.route.a != "VR"
    # audiobook_factory è attualmente degraded: il fallback LM è il comportamento
    # fail-closed già esistente, ma il nuovo preflight non deve alterare l'intento.
    assert result.route.a == "LM"


def test_router_approval_is_proposal_not_authorization(registry):
    route = LocalRouter(registry).classify("pubblica il post").route
    decision = RalfPolicy().evaluate(route, operation="social_publish")
    assert route.a == "AP" and not decision.allowed


@pytest.mark.parametrize("operation", sorted(PROTECTED_ACTIONS))
def test_every_external_operation_requires_bound_approval(operation):
    route = CompactRoute(1, "AP", "external_action", c=1)
    assert not RalfPolicy().evaluate(route, operation=operation).allowed
    approval = {"authorized": True, "operation": operation, "binding_hash": "a" * 64}
    assert RalfPolicy().evaluate(route, operation=operation, approval=approval).allowed


def test_worker_delta_artifact_provenance():
    delta = WorkerDelta.from_mapping({"v": 1, "task": "T17", "status": "ok", "facts": [], "issues": [], "metrics": [], "artifacts": ["sha256:" + "a" * 64], "confidence": 0.91})
    assert delta.task == "T17"


def test_director_delta_context_and_exact_task():
    outputs = iter(['{"v":1,"task":"wrong","assignments":[]}', '{"v":1,"task":"T17","assignments":[],"artifact_refs":[],"verified_facts":[],"known_gaps":[]}'])
    adapter = DirectorAdapter(lambda prompt, tokens: next(outputs))
    context = DirectorContext("T17", (), (), (), ())
    assert adapter.plan(context).exact_task == "T17"


def test_director_one_retry_and_token_budgets():
    calls = []
    def generate(prompt, tokens):
        calls.append(tokens)
        return "bad" if len(calls) == 1 else '{"v":1,"task":"T","assignments":[]}'
    DirectorAdapter(generate).plan(DirectorContext("T", (), (), (), ()))
    assert calls == [DIRECTOR_MAX_TOKENS, DIRECTOR_REPAIR_TOKENS]


def test_director_max_three_assignments():
    raw = {"v": 1, "task": "T", "assignments": [{"task": str(i), "capability": "ET", "input_refs": []} for i in range(4)]}
    adapter = DirectorAdapter(lambda prompt, tokens: json.dumps(raw))
    with pytest.raises(ContractError, match="assignment_limit"):
        adapter.plan(DirectorContext("T", (), (), (), ()))
