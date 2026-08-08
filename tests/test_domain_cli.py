import json
import os
import subprocess
import sys


def _run_domain_cli(*args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", None)
    return subprocess.run(
        [sys.executable, "-m", "ralfloop_agent.domains.cli", *args],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )


def _json_stdout(completed: subprocess.CompletedProcess[str]) -> dict:
    assert "Traceback" not in completed.stderr
    assert completed.stdout.strip()
    return json.loads(completed.stdout)


def test_cli_answer_deterministic_stdout_json():
    completed = _run_domain_cli("answer", "--goal", "Quanto fa 2 + 3?", "--domain", "arithmetic_basic")
    body = _json_stdout(completed)
    assert completed.returncode == 0
    assert str(body["answer"]) == "5"
    assert body["jury_invoked"] is False


def test_cli_answer_jury_disabled_stdout_json():
    completed = _run_domain_cli(
        "answer",
        "--goal",
        "Qual è la strategia più prudente per questo incidente?",
        "--domain",
        "incident_triage",
    )
    body = _json_stdout(completed)
    assert completed.returncode == 4
    assert body["jury_required"] is True
    assert body["jury_status"] == "disabled"
    assert not body.get("answer")


def test_cli_resolve_missing_stdout_json():
    completed = _run_domain_cli("resolve", "--goal", "Valuta il rischio di un dominio mai registrato")
    body = _json_stdout(completed)
    assert completed.returncode == 3
    assert body["status"] in {"missing", "domain_creation_required"}


def test_cli_input_invalid_stdout_json():
    completed = _run_domain_cli("answer", "--domain", "arithmetic_basic")
    body = _json_stdout(completed)
    assert completed.returncode == 2
    assert body["status"] == "input_invalid"
