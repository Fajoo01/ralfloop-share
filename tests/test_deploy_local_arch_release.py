from __future__ import annotations

from copy import deepcopy

import pytest

from tools.deploy_local_arch_release import evaluate_quality_gate


def legacy_gate() -> dict[str, object]:
    baseline_ids = [f"tests.test_legacy::test_{index}" for index in range(61)]
    candidate_ids = baseline_ids[:53]
    return {
        "scoped_tests": True,
        "sandbox": True,
        "no_host_candidate_execution": True,
        "benchmark_minimum": True,
        "functiongemma_real": True,
        "runtime_canary": "pass",
        "policy_bypass": 0,
        "approval_miss": 0,
        "enough_ram": True,
        "enough_swap": True,
        "port_19104_free": True,
        "full_suite_clean": False,
        "preexisting_failures_only": True,
        "no_regression": True,
        "new_failures": 0,
        "baseline_suite": {"tests": 1331, "failures": 61, "errors": 0},
        "candidate_suite": {"tests": 1474, "failures": 53, "errors": 0},
        "baseline_failure_ids": baseline_ids,
        "candidate_failure_ids": candidate_ids,
        "suite_report_complete": True,
        "suite_exit_preexisting_same": True,
    }


def assert_blocked(data: dict[str, object]) -> None:
    assert evaluate_quality_gate(data)[0] is False


def test_full_clean_passes() -> None:
    data = legacy_gate()
    data["full_suite_clean"] = True
    data["preexisting_failures_only"] = False
    assert evaluate_quality_gate(data) == (True, "full_suite_clean", [])


def test_preexisting_failures_only_passes() -> None:
    assert evaluate_quality_gate(legacy_gate()) == (True, "preexisting_failures_only", [])


def test_one_new_failure_blocks() -> None:
    data = legacy_gate()
    data["new_failures"] = 1
    data["no_regression"] = False
    assert_blocked(data)


def test_same_count_with_different_identity_blocks() -> None:
    data = legacy_gate()
    candidate_ids = data["candidate_failure_ids"]
    assert isinstance(candidate_ids, list)
    data["candidate_failure_ids"] = candidate_ids[:-1] + ["tests.test_new::test_failure"]
    assert_blocked(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scoped_tests", False),
        ("functiongemma_real", False),
        ("policy_bypass", 1),
        ("approval_miss", 1),
        ("enough_swap", False),
        ("suite_exit_preexisting_same", False),
        ("suite_report_complete", False),
    ],
)
def test_required_legacy_evidence_blocks(field: str, value: object) -> None:
    data = legacy_gate()
    data[field] = value
    assert_blocked(data)


def test_candidate_error_blocks() -> None:
    data = deepcopy(legacy_gate())
    candidate = data["candidate_suite"]
    assert isinstance(candidate, dict)
    candidate["errors"] = 1
    assert_blocked(data)
