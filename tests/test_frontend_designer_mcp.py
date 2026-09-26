from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ralfloop_agent.frontend_designer import FrontendDesigner, FrontendDesignerConfig
from ralfloop_agent.frontend_designer.mcp import FrontendDesignerMCPServer
from ralfloop_agent.unified_assistant.frontend_designer_mcp_adapter import FrontendDesignerMCPContext
from src.mcp_transport import MCPClientSession, MCPError, StdioMCPTransport


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts" / "ralf_frontend_designer_mcp_server.py"


def _config(tmp_path: Path) -> FrontendDesignerConfig:
    work = tmp_path / "work"
    work.mkdir()
    artifacts = tmp_path / "artifacts"
    return FrontendDesignerConfig(
        allowed_roots=(work,),
        artifact_root=artifacts,
        android_sdk=tmp_path / "sdk",
        android_avd_home=tmp_path / "avd",
        mcp_remote_bin=Path("/bin/false"),
    )


def test_phone_requires_successful_matching_emulator_report(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    designer = FrontendDesigner(cfg)
    package = "org.example.app"
    cfg.artifact_root.mkdir(parents=True)
    report = cfg.artifact_root / "report.json"

    report.write_text(json.dumps({"ok": False, "gate": "emulator_first", "package": package}))
    with pytest.raises(RuntimeError, match="frontend_emulator_gate_not_passed"):
        designer.test_android_phone(package=package, emulator_report=report)

    report.write_text(json.dumps({"ok": True, "gate": "emulator_first", "package": "org.example.other"}))
    with pytest.raises(RuntimeError, match="frontend_emulator_gate_package_mismatch"):
        designer.test_android_phone(package=package, emulator_report=report)

    calls = []
    monkeypatch.setattr(designer, "_remote_android", lambda tool, arguments: calls.append((tool, dict(arguments))) or {"tool": tool})
    monkeypatch.setattr("ralfloop_agent.frontend_designer.core.time.sleep", lambda _seconds: None)
    report.write_text(json.dumps({"ok": True, "gate": "emulator_first", "package": package}))
    result = designer.test_android_phone(package=package, emulator_report=report, device_id="device-1")
    assert result["ok"] is True
    assert [name for name, _ in calls] == ["android_inspect", "android_launch", "android_inspect"]


def test_stdio_mcp_discovery_and_inspect_roundtrip(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "package.json").write_text(json.dumps({"dependencies": {"react": "1"}, "scripts": {"test": "true"}}))
    env = os.environ.copy()
    env["RALF_FRONTEND_WORKTREE_ROOTS"] = str(work)
    env["RALF_FRONTEND_ARTIFACT_ROOT"] = str(tmp_path / "artifacts")
    transport = StdioMCPTransport([sys.executable, str(SERVER)], env=env, cwd=str(ROOT))
    try:
        session = MCPClientSession(transport, timeout=10)
        init = session.initialize()
        assert init["serverInfo"]["name"] == "ralf-frontend-designer"
        tools = session.list_tools()
        names = {tool.name for tool in tools}
        assert "frontend_inspect_project" in names
        assert "frontend_test_android_phone" in names
        result = session.call_tool("frontend_inspect_project", {"workdir": str(work)})
        payload = result["structuredContent"]
        assert payload["ok"] is True
        assert payload["result"]["frameworks"] == ["react"]
        assert payload["result"]["writes"] == 0
    finally:
        transport.close()


def test_environment_defaults_to_api35(tmp_path, monkeypatch):
    monkeypatch.delenv("RALF_FRONTEND_ANDROID_AVD", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    config = FrontendDesignerConfig.from_environment(project_root=ROOT)
    assert config.android_avd_name == "ralf_frontend_ci_api35"


def test_run_timeout_returns_fail_closed_completed_process(monkeypatch):
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(["fake"], 2, output=b"partial", stderr=b"late")

    monkeypatch.setattr(subprocess, "run", _timeout)
    result = FrontendDesigner._run(["fake"], timeout=2)
    assert result.returncode == 124
    assert result.stdout == "partial"
    assert "frontend_subprocess_timeout:2s" in result.stderr


def test_mcp_timeout_error_is_structured(tmp_path, monkeypatch):
    designer = FrontendDesigner(_config(tmp_path))

    def _timeout(_workdir):
        raise subprocess.TimeoutExpired(["fake"], 7)

    monkeypatch.setattr(designer, "inspect_project", _timeout)
    result = FrontendDesignerMCPServer(designer).call(
        "frontend_inspect_project", {"workdir": str(tmp_path / "work")}
    )
    assert result["isError"] is True
    assert result["structuredContent"]["error"] == "frontend_subprocess_timeout:7s"


def test_emulator_start_preserves_avd_hardware_profile(tmp_path, monkeypatch):
    designer = FrontendDesigner(_config(tmp_path))
    captured = {}

    monkeypatch.setattr(designer, "emulator_status", lambda: {
        "configured_running": [],
        "configured_ready": False,
        "available_avds": [designer.config.android_avd_name],
    })
    monkeypatch.setattr(designer, "_emulator", lambda: Path("/fake/emulator"))

    class StopLaunch(Exception):
        pass

    def _popen(argv, **_kwargs):
        captured["argv"] = list(argv)
        raise StopLaunch

    monkeypatch.setattr(subprocess, "Popen", _popen)
    with pytest.raises(StopLaunch):
        designer._ensure_emulator(timeout=1)
    argv = captured["argv"]
    assert "-no-snapshot-load" in argv
    assert "-memory" not in argv
    assert "-cores" not in argv


def test_unified_adapter_uses_bounded_long_android_timeout(monkeypatch):
    monkeypatch.delenv("RALF_FRONTEND_DESIGNER_MCP_TIMEOUT", raising=False)
    assert FrontendDesignerMCPContext.from_environment().timeout == 600.0
    monkeypatch.setenv("RALF_FRONTEND_DESIGNER_MCP_TIMEOUT", "9999")
    assert FrontendDesignerMCPContext.from_environment().timeout == 1800.0
    monkeypatch.setenv("RALF_FRONTEND_DESIGNER_MCP_TIMEOUT", "not-a-number")
    assert FrontendDesignerMCPContext.from_environment().timeout == 600.0
