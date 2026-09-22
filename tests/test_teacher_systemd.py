from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

INFERENCE = (
    ROOT
    / "deploy/systemd/ralf-teacher-inference.service"
).read_text()

MCP = (
    ROOT
    / "deploy/systemd/ralf-teacher-mcp-broker.service"
).read_text()

CORE = (
    ROOT
    / "deploy/systemd/ralf-teacher-core-mcp.service"
).read_text()


def test_teacher_mcp_is_student_only_and_has_no_ip_network():
    assert "ralf_teacher_mcp_broker.py" in MCP
    assert "ralf_teacher_mcp_server.py" in MCP
    assert "--max-clients 6" in MCP

    assert (
        "RALF_TEACHER_INFERENCE_SOCKET="
        "/run/ralf-teacher-inference/inference.sock"
    ) in MCP

    assert "RestrictAddressFamilies=AF_UNIX" in MCP
    assert "IPAddressDeny=any" in MCP

    forbidden = (
        "mailchimp",
        "runts",
        "pec_",
        "google_workspace",
        "whatsapp",
        "home_assistant",
        "bottazzi-browser",
    )

    lower = MCP.lower()

    for token in forbidden:
        assert token not in lower


def test_teacher_inference_is_single_shared_qwen_owner():
    assert "ralf_teacher_inference_daemon.py" in INFERENCE
    assert "--idle-timeout 300" in INFERENCE
    assert "--allow-uid 1001" in INFERENCE

    assert "RALF_TEACHER_DB" not in INFERENCE

    assert "IPAddressDeny=any" in INFERENCE
    assert "IPAddressAllow=localhost" in INFERENCE


def test_teacher_mcp_persists_only_teacher_state():
    assert (
        "RALF_TEACHER_DB="
        "/var/lib/ralfloop-teacher/teacher.sqlite3"
    ) in MCP

    assert "StateDirectory=ralfloop-teacher" in MCP


def test_teacher_services_use_current_immutable_release():
    expected = "/home/sibilla-cumana/ralfloop-production/current"

    assert f"WorkingDirectory={expected}" in INFERENCE
    assert f"WorkingDirectory={expected}" in MCP
    assert expected in INFERENCE
    assert expected in MCP


def test_teacher_mcp_depends_on_shared_inference():
    assert "Requires=ralf-teacher-inference.service" in MCP
    assert "After=ralf-teacher-inference.service" in MCP


def test_teacher_runtime_does_not_import_privileged_capabilities():
    import ast

    runtime_files = [
        *sorted(
            (ROOT / "ralfloop_agent/teacher").glob("*.py")
        ),
        ROOT / "scripts/ralf_teacher_mcp_server.py",
        ROOT / "scripts/ralf_teacher_mcp_broker.py",
        ROOT / "scripts/ralf_teacher_inference_daemon.py",
        ROOT / "src/teacher.py",
    ]

    forbidden_prefixes = (
        "src.google_workspace",
        "src.mailchimp",
        "src.whatsapp",
        "ralfloop_agent.unified_assistant.pec",
        "ralfloop_agent.unified_assistant.runts",
        "ralfloop_agent.unified_assistant.email",
        "ralfloop_agent.unified_assistant.whatsapp",
        "ralfloop_agent.unified_assistant.home",
        "ralfloop_agent.integration.eyf_browser",
    )

    imported = []

    for path in runtime_files:
        tree = ast.parse(
            path.read_text(encoding="utf-8"),
            filename=str(path),
        )

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)

            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.append(node.module)

    offenders = sorted({
        module
        for module in imported
        if module.startswith(forbidden_prefixes)
    })

    assert offenders == []


def test_teacher_core_mcp_is_internal_and_sandboxed():
    assert "ralf-teacher-core-mcp" in CORE
    assert "--allow-uid 1001" in CORE
    assert "RestrictAddressFamilies=AF_UNIX" in CORE
    assert "IPAddressDeny=any" in CORE
    assert "ProtectSystem=strict" in CORE
    assert "NoNewPrivileges=yes" in CORE
    assert "RALF_TEACHER_CORE_SOCKET=/run/ralf-teacher-core/core.sock" in MCP
    assert "ralf-teacher-core-mcp.service" in MCP
