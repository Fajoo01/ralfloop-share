from __future__ import annotations

import io
import shlex
import subprocess
from pathlib import Path

import pytest

import openshell_backend.app as backend_app
from openshell_backend import chat_api
from ralfloop_agent.cli import terminal_chat as terminal_cli
from ralfloop_agent.integration.execution_provenance import (
    empty_execution_provenance,
    guard_execution_claims,
    metadata_with_provenance,
    provenance_from_results,
)
from ralfloop_agent.integration.interaction_router import classify_interaction
from ralfloop_agent.models.result_envelope import ResultEnvelope
from ralfloop_agent.providers.chat import ChatResult
from ralfloop_agent.integration.system_inspection import (
    ReadOnlySystemExecutor,
    validate_read_only_command,
)
from src import routing_config
from src.models import Evidence
from src.router import route_task


READ_ONLY_GOAL = (
    "Controlla cosa occupa più spazio. "
    "Per ora non cancellare, spostare o modificare nulla."
)
MIXED_GOAL = "Non cancellare i modelli, ma elimina i file temporanei."


@pytest.mark.parametrize(
    "text",
    ("leggi la posta", "invia posta", "CONTROLLA, LA POSTA!"),
)
def test_posta_matches_standalone_word(text: str) -> None:
    assert routing_config.trigger_matches("posta", text)


@pytest.mark.parametrize(
    "text",
    ("spostare", "spostamento", "impostare", "postazione", "ripostare"),
)
def test_posta_does_not_match_inside_other_words(text: str) -> None:
    assert not routing_config.trigger_matches("posta", text)


def test_trigger_matching_handles_case_punctuation_and_accents() -> None:
    assert routing_config.trigger_matches("caffè", "CONTROLLA IL CAFFÈ, ORA.")


@pytest.mark.parametrize(
    "goal",
    (
        "non cancellare nulla",
        "senza cancellare file",
        "non inviare",
        "solo lettura",
        "per ora fammi soltanto un elenco",
        "non eseguire pulizie",
        "soltanto controlli",
    ),
)
def test_read_only_negations_do_not_activate_side_effect(goal: str) -> None:
    route = route_task(goal)
    assert route.mode != "external_action"
    assert route.requires_confirmation is False


def test_non_spostare_does_not_activate_external_action() -> None:
    route = route_task("Controlla lo spazio e non spostare nulla")
    assert route.mode == "read_only_system_inspection"
    assert route.requires_confirmation is False


def test_positive_delete_requires_approval() -> None:
    route = route_task("elimina i file temporanei")
    assert route.mode == "external_action"
    assert route.requires_confirmation is True


def test_partial_negation_does_not_hide_positive_delete() -> None:
    route = route_task(MIXED_GOAL)
    assert route.mode == "external_action"
    assert route.requires_confirmation is True


@pytest.mark.parametrize(
    "command",
    (
        ("df", "-h"),
        ("du", "-x", "-h", "--max-depth=1", "/tmp"),
        ("find", "/tmp", "-type", "f", "-size", "+1G", "-printf", "%s\t%p\n"),
        ("stat", "/tmp"),
        ("ls", "-la", "/tmp"),
        ("journalctl", "--disk-usage", "--no-pager"),
        ("docker", "system", "df"),
        ("ps", "-eo", "pid,comm"),
        ("pgrep", "-af", "llama-server"),
        ("free", "-h"),
        ("uptime",),
        ("nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader"),
        ("sort", "-nr"),
        ("wc", "-l"),
        ("head", "-n", "10"),
        ("tail", "-n", "10"),
        ("grep", "needle"),
    ),
)
def test_read_only_commands_are_allowed(command: tuple[str, ...]) -> None:
    assert validate_read_only_command(command).allowed is True


def test_find_delete_is_forbidden() -> None:
    result = validate_read_only_command(("find", "/tmp", "-type", "f", "-delete"))
    assert result.allowed is False
    assert result.reason == "find_write_action_forbidden"


@pytest.mark.parametrize("command", ("df -h > out.txt", "du -h | tee report.txt"))
def test_shell_redirect_and_pipeline_are_forbidden(command: str) -> None:
    assert validate_read_only_command(command).allowed is False


@pytest.mark.parametrize(
    "command",
    ("df $(touch /tmp/x)", "df `touch /tmp/x`", "df ${PATH}"),
)
def test_command_substitution_is_forbidden(command: str) -> None:
    assert validate_read_only_command(command).allowed is False


@pytest.mark.parametrize(
    "command",
    (
        ("nvidia-smi", "--gpu-reset"),
        ("nvidia-smi", "-pm", "1"),
        ("sort", "-o", "out"),
        ("grep", "needle", "/tmp/file"),
    ),
)
def test_read_only_tools_reject_write_or_file_operands(command: tuple[str, ...]) -> None:
    assert validate_read_only_command(command).allowed is False


@pytest.mark.parametrize(
    "command",
    (
        ("rm", "-f", "x"),
        ("mv", "x", "y"),
        ("cp", "x", "y"),
        ("truncate", "-s", "0", "x"),
        ("tee", "x"),
        ("chmod", "600", "x"),
        ("chown", "root", "x"),
        ("apt", "clean"),
        ("docker", "system", "prune"),
        ("journalctl", "--vacuum-time=1d"),
    ),
)
def test_mutating_commands_are_forbidden(command: tuple[str, ...]) -> None:
    assert validate_read_only_command(command).allowed is False


def test_fast_chat_without_tools_cannot_claim_execution() -> None:
    provenance = empty_execution_provenance()
    text, blocked = guard_execution_claims("Ecco i risultati: ho eseguito df.", provenance)
    metadata = metadata_with_provenance({}, provenance, execution_claim_blocked=blocked)
    assert blocked is True
    assert "nessuno strumento" in text
    assert metadata["tools_executed"] is False
    assert metadata["command_count"] == 0
    assert metadata["result_ids"] == []


def test_real_result_envelope_can_cite_execution() -> None:
    evidence = Evidence(command="df -h", path="/tmp", exit_code=0, stdout="real-output", stderr="")
    provenance = provenance_from_results((("result-1", evidence),))
    text, blocked = guard_execution_claims("Ho eseguito df: real-output", provenance)
    envelope = ResultEnvelope(
        route=route_task(READ_ONLY_GOAL),
        evidence=evidence,
        answer=text,
        provenance=provenance,
    )
    assert blocked is False
    assert envelope.answer == "Ho eseguito df: real-output"
    assert envelope.provenance.command_count == 1
    assert envelope.provenance.result_ids == ["result-1"]


class _ClaimingProvider:
    name = "fake"
    default_model = "fake-model"

    def chat(self, messages, *, model=None):
        return ChatResult(
            text="La scansione mostra 50 GB liberi.",
            provider=self.name,
            model=model or self.default_model,
            metadata={},
        )


def test_chat_api_blocks_unsupported_execution_claim() -> None:
    response = chat_api.chat(
        chat_api.ChatRequest(message="controlla il disco"),
        _ClaimingProvider(),
    )
    assert "nessuno strumento" in response.response
    assert response.metadata["command_count"] == 0
    assert response.metadata["execution_claim_blocked"] is True


def test_agent_read_only_uses_mock_evidence_without_approval(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(self, command):
        argv = tuple(command)
        calls.append(argv)
        return Evidence(
            command=shlex.join(argv),
            path=str(self.root),
            exit_code=0,
            stdout="real-mock-output",
            stderr="",
        )

    def fail_confirmation(*args, **kwargs):
        raise AssertionError("approval must not be created")

    monkeypatch.setenv("RALF_READONLY_INSPECTION_ROOT", str(tmp_path))
    monkeypatch.setattr(ReadOnlySystemExecutor, "run", fake_run)
    monkeypatch.setattr(backend_app, "audit", lambda *args, **kwargs: None)
    import ralfloop_agent.integration.confirmation_store as confirmation_store

    monkeypatch.setattr(confirmation_store, "request_confirmation", fail_confirmation)
    request = backend_app.TaskRunRequest(
        user_goal=READ_ONLY_GOAL,
        extra_context={"terminal_client": {"cwd": str(tmp_path)}},
    )
    payload = backend_app._run_task_impl(request)

    assert payload["capability_route"]["task_mode"] == "read_only_system_inspection"
    assert payload["external_action"] is False
    assert payload["approval_required"] is False
    assert payload["stop_reason"] == "read_only_inspection_completed"
    assert payload["result_envelope"]["provenance"]["command_count"] == 3
    assert len(payload["result_envelope"]["provenance"]["result_ids"]) == 3
    assert {command[0] for command in calls} == {"df", "du", "find"}


def test_agent_destructive_request_creates_mock_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ralfloop_agent.integration.confirmation_store as confirmation_store

    created: list[str] = []

    def fake_request(action_type, details):
        created.append(action_type)
        return "mock-confirmation-id"

    monkeypatch.setattr(confirmation_store, "request_confirmation", fake_request)
    monkeypatch.setattr(confirmation_store, "get_confirmation", lambda confirmation_id: None)
    payload = backend_app._run_task_impl(backend_app.TaskRunRequest(user_goal="elimina i temporanei"))

    assert payload["stop_reason"] == "human_confirmation_required"
    assert payload["pending_confirmation_id"] == "mock-confirmation-id"
    assert created == ["external_action"]


def test_agent_yes_does_not_set_human_confirmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __init__(self):
            self.payload = None

        def post_task(self, payload):
            self.payload = payload
            return {
                "pending_confirmation_id": "mock-id",
                "stop_reason": "human_confirmation_required",
            }

    monkeypatch.chdir(tmp_path)
    client = Client()
    args = terminal_cli.build_parser().parse_args(["agent", "--yes", "elimina", "temporanei"])
    rc = terminal_cli.run_agent(args, client=client, out=io.StringIO(), err=io.StringIO())
    assert rc == 0
    assert "human_confirmed" not in client.payload.get("extra_context", {})
    terminal = client.payload["extra_context"]["terminal_client"]
    assert terminal["auto_execute_protected_actions"] is False
    assert terminal["provider"] == "llama_cpp"


def test_agent_accepts_explicit_llama_cpp_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        def __init__(self):
            self.payload = None

        def post_task(self, payload):
            self.payload = payload
            return {"ok": True, "stop_reason": "mock-read-only"}

    monkeypatch.chdir(tmp_path)
    client = Client()
    args = terminal_cli.build_parser().parse_args(
        ["agent", "--provider", "llama_cpp", "--yes", READ_ONLY_GOAL]
    )
    rc = terminal_cli.run_agent(args, client=client, out=io.StringIO(), err=io.StringIO())

    assert rc == 0
    assert client.payload["extra_context"]["terminal_client"]["provider"] == "llama_cpp"


def test_existing_capability_routes_remain_compatible() -> None:
    assert route_task("controlla log jellyfin").mode == "check_only"
    assert route_task("fix bug concreto con test").mode == "patch_allowed"
    assert route_task("invia telegram con risultato finale").mode == "external_action"


def test_read_only_canary_route() -> None:
    route = route_task(READ_ONLY_GOAL)
    assert route.mode == "read_only_system_inspection"
    assert route.requires_confirmation is False
    assert route.mcp_used == []


def test_mixed_canary_route() -> None:
    route = route_task(MIXED_GOAL)
    assert route.mode == "external_action"
    assert route.requires_confirmation is True


def test_natural_interaction_routes_chat_agent_and_protected() -> None:
    assert classify_interaction("Ciao Ralf").interaction_mode == "chat"
    inspection = classify_interaction(READ_ONLY_GOAL)
    assert inspection.interaction_mode == "agent"
    assert inspection.capability == "read_only_system_inspection"
    protected = classify_interaction(MIXED_GOAL)
    assert protected.interaction_mode == "agent"
    assert protected.capability == "protected_external_action"


def test_posta_and_sposta_are_separate_natural_routes() -> None:
    posta = route_task("Leggi la posta")
    sposta = route_task("Sposta il file")
    assert posta.mode == "external_action"
    assert "google_workspace.gmail" in posta.mcp_used
    assert sposta.mode == "external_action"
    assert "google_workspace.gmail" not in sposta.mcp_used


def test_protected_components_are_not_modified() -> None:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    changed = set(result.stdout.splitlines())
    protected = {
        "integrations/cheshire_cat/ralfloop_bridge/main_plugin.py",
        "ralfloop_agent/domains/domain_approval_executor.py",
        "ralfloop_agent/domains/telegram_approval_api.py",
    }
    assert changed.isdisjoint(protected)
    assert not any("abc_" in path for path in changed)
    assert not any("recursive_mas" in path for path in changed)

def test_run_task_read_only_bypasses_gpu_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []

    def fake_impl(request):
        observed.append(request.user_goal)
        return {"ok": True, "stop_reason": "mock-read-only"}

    monkeypatch.setattr(backend_app, "_run_task_impl", fake_impl)
    payload = backend_app.run_task(backend_app.TaskRunRequest(user_goal=READ_ONLY_GOAL))
    assert payload["stop_reason"] == "mock-read-only"
    assert observed == [READ_ONLY_GOAL]


def test_ralf_ask_declares_zero_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ralfloop_agent.cli.session_store import SessionStore

    class Client:
        def post_chat_stream(self, payload):
            return iter(
                (
                    {"type": "start", "provider": "fake", "model": "fake", "session_id": "s"},
                    {"type": "token", "text": "risposta"},
                    {
                        "type": "done",
                        "ok": True,
                        "provider": "fake",
                        "model": "fake",
                        "session_id": "s",
                        "metadata": {
                            "tools_executed": False,
                            "command_count": 0,
                            "result_ids": [],
                        },
                    },
                )
            )

    monkeypatch.chdir(tmp_path)
    err = io.StringIO()
    args = terminal_cli.build_parser().parse_args(["ask", "--provider", "ollama", "ciao"])
    rc = terminal_cli.run_ask(
        args,
        client=Client(),
        store=SessionStore(tmp_path / "sessions"),
        out=io.StringIO(),
        err=err,
    )
    assert rc == 0
    assert "tools_executed=false" in err.getvalue()
