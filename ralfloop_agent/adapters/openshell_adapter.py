from __future__ import annotations

import subprocess
import time
from pathlib import Path
from uuid import uuid4

from ralfloop_agent.core.policy import PolicyLayer
from ralfloop_agent.tools.contracts import ToolResult


class OpenShellAdapterStub:
    def __init__(self, base_dir: str = ".sandbox", policy: PolicyLayer | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.policy = policy or PolicyLayer()

    def create_sandbox(self) -> dict:
        sandbox_id = str(uuid4())
        root = self.base_dir / sandbox_id / "workspace"
        (root / "tmp").mkdir(parents=True, exist_ok=True)
        (root / "out").mkdir(parents=True, exist_ok=True)
        return {"id": sandbox_id, "workspace": str(root)}

    def destroy_sandbox(self, sandbox_id: str) -> None:
        # MVP: non cancella automaticamente per facilitare debug locale
        _ = sandbox_id

    def _real_path(self, workspace: str, sandbox_path: str) -> Path:
        rel = sandbox_path.removeprefix("/workspace/").removeprefix("/workspace")
        return Path(workspace) / rel.lstrip("/")

    def exec(self, workspace: str, command: str, timeout_sec: int = 60) -> ToolResult:
        decision = self.policy.check_command(command)
        if not decision.allowed:
            return ToolResult(ok=False, tool_name="sandbox_exec", policy=decision, error_type="policy_denied")
        start = time.perf_counter()
        try:
            proc = subprocess.run(
                ["bash", "-lc", command],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            return ToolResult(
                ok=proc.returncode == 0,
                tool_name="sandbox_exec",
                duration_ms=int((time.perf_counter() - start) * 1000),
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                policy=decision,
                error_type=None if proc.returncode == 0 else "command_failed",
            )
        except subprocess.TimeoutExpired as exc:
            return ToolResult(
                ok=False,
                tool_name="sandbox_exec",
                duration_ms=int((time.perf_counter() - start) * 1000),
                stdout=exc.stdout or "",
                stderr=exc.stderr or "",
                policy=decision,
                error_type="timeout",
            )

    def read_file(self, workspace: str, path: str) -> ToolResult:
        decision = self.policy.check_read_path(path)
        if not decision.allowed:
            return ToolResult(ok=False, tool_name="sandbox_read_file", policy=decision, error_type="policy_denied")
        real = self._real_path(workspace, path)
        try:
            content = real.read_text(encoding="utf-8")
            return ToolResult(ok=True, tool_name="sandbox_read_file", stdout=content, policy=decision)
        except Exception as exc:
            return ToolResult(ok=False, tool_name="sandbox_read_file", stderr=str(exc), policy=decision, error_type=type(exc).__name__)

    def write_file(self, workspace: str, path: str, content: str) -> ToolResult:
        decision = self.policy.check_write_path(path)
        if not decision.allowed:
            return ToolResult(ok=False, tool_name="sandbox_write_file", policy=decision, error_type="policy_denied")
        real = self._real_path(workspace, path)
        try:
            real.parent.mkdir(parents=True, exist_ok=True)
            real.write_text(content, encoding="utf-8")
            return ToolResult(ok=True, tool_name="sandbox_write_file", stdout=str(real), artifacts=[str(real)], policy=decision)
        except Exception as exc:
            return ToolResult(ok=False, tool_name="sandbox_write_file", stderr=str(exc), policy=decision, error_type=type(exc).__name__)

    def list_dir(self, workspace: str, path: str) -> ToolResult:
        decision = self.policy.check_read_path(path)
        if not decision.allowed:
            return ToolResult(ok=False, tool_name="sandbox_list_dir", policy=decision, error_type="policy_denied")
        real = self._real_path(workspace, path)
        try:
            lines = []
            for item in sorted(real.iterdir()):
                suffix = "/" if item.is_dir() else ""
                lines.append(item.name + suffix)
            return ToolResult(ok=True, tool_name="sandbox_list_dir", stdout="\n".join(lines), policy=decision)
        except Exception as exc:
            return ToolResult(ok=False, tool_name="sandbox_list_dir", stderr=str(exc), policy=decision, error_type=type(exc).__name__)
