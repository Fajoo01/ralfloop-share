from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ralfloop_agent.frontend_designer import FrontendDesigner, FrontendDesignerConfig
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
