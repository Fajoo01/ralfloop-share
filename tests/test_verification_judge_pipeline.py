from pathlib import Path

from ralfloop_agent.integration.capability_adapter import route_task
from ralfloop_agent.integration.verification_judge import (
    build_verification_case,
    verification_blocks,
)
from src.models import Evidence, PatchEvidence


def test_patch_route_selects_bottazzi_motor_judge():
    route = route_task("fix bug concreto con test")
    assert route.mode == "patch_allowed"
    assert route.verification_policy.verifier_type == "combined"
    assert route.verification_policy.judge_provider == "bottazzi_motor"


def test_verification_case_uses_structured_evidence_not_raw_payload():
    route = route_task("fix bug concreto con test")
    patch = PatchEvidence(
        command="git diff",
        path=".",
        exit_code=0,
        stdout="SECRET_STDOUT",
        stderr=None,
        diff="SECRET_DIFF",
        tests=["9 passed"],
    )
    case = build_verification_case(
        "fix bug concreto con test",
        route,
        patch,
        "candidate",
        {"task_id": "task-1", "judge_facts": ["reviewer_note=clean"]},
    )
    joined = "\n".join(case.facts)
    assert "SECRET_STDOUT" not in joined
    assert "SECRET_DIFF" not in joined
    assert "stdout_len=13" in joined
    assert "diff_len=11" in joined
    assert "9 passed" in joined
    assert "reviewer_note=clean" in joined


def test_blocking_policy_stops_on_nonadvancing_gate():
    route = route_task("fix bug concreto con test")
    trace = {"gate": {"proceed_to_next_stage": False}}
    assert verification_blocks(route, trace) is True


def test_reasoning_cycle_obeys_blocking_judge(monkeypatch):
    from ralfloop_agent.nodes import reasoning

    route = route_task("fix bug concreto con test")

    class FakeExecutor:
        def __init__(self, task_id):
            self.base_dir = Path("/tmp/fake-sandbox")

        def run_in_sandbox(self, command, cwd="."):
            return Evidence(command="git diff", path=".", exit_code=0, stdout="diff", stderr=None)

    trace = {"gate": {"proceed_to_next_stage": False, "status": "judge_uncertain"}}
    monkeypatch.setattr(reasoning, "route_task", lambda *args, **kwargs: route)
    monkeypatch.setattr(reasoning, "ShellExecutor", FakeExecutor)
    monkeypatch.setattr(reasoning, "run_verification_judge", lambda *args, **kwargs: trace)
    monkeypatch.setattr(reasoning.audit, "log_operation", lambda *args, **kwargs: None)

    result = reasoning.run_capability_reasoning_cycle(
        "fix bug concreto con test", {"task_id": "task-pipeline"}
    )
    assert result.answer == "verification_judge_blocked"
    assert result.meta["verification_judge"]["gate"]["status"] == "judge_uncertain"
