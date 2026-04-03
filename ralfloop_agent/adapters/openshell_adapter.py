from __future__ import annotations

import shutil
import subprocess
import time
import uuid

import requests
from pathlib import Path

from ralfloop_agent.core.policy import PolicyLayer
from ralfloop_agent.tools.contracts import ToolResult


class OpenShellAdapterStub:
    def __init__(self, base_dir: str = ".sandbox", policy: PolicyLayer | None = None) -> None:
        self.base_dir = Path(base_dir)
        self.policy = policy or PolicyLayer()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def create_sandbox(self) -> dict:
        sandbox_id = str(uuid.uuid4())
        root = self.base_dir / sandbox_id / "workspace"
        (root / "out").mkdir(parents=True, exist_ok=True)
        (root / "tmp").mkdir(parents=True, exist_ok=True)
        return {
            "id": sandbox_id,
            "root": str(root),
            "out": str(root / "out"),
            "tmp": str(root / "tmp"),
            "status": "running",
        }

    def destroy_sandbox(self, sandbox_id: str) -> None:
        target = self.base_dir / sandbox_id
        if target.exists():
            shutil.rmtree(target)

    def _policy_path(self, path: str) -> str:
        rel = path.lstrip("/")
        return f"/workspace/{rel}" if rel else "/workspace"

    def _full_path(self, sandbox: dict, path: str) -> Path:
        return Path(sandbox["root"]) / path.lstrip("/")

    def _envelope(
        self,
        tool_name: str,
        started: float,
        ok: bool = True,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        artifacts: list[str] | None = None,
        allowed: bool = True,
        reason: str = "allowed",
        error_type: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            ok=ok,
            tool_name=tool_name,
            duration_ms=int((time.time() - started) * 1000),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            artifacts=artifacts or [],
            policy={"allowed": allowed, "reason": reason},
            error_type=error_type,
        )

    def write_file(self, sandbox: dict, path: str, content: str) -> ToolResult:
        started = time.time()
        decision = self.policy.check_write_path(self._policy_path(path))
        if not decision.allowed:
            return self._envelope(
                "sandbox_write_file",
                started,
                ok=False,
                exit_code=1,
                stderr=decision.reason,
                allowed=False,
                reason=decision.reason,
                error_type="policy_denied",
            )

        full_path = self._full_path(sandbox, path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content, encoding="utf-8")
        return self._envelope(
            "sandbox_write_file",
            started,
            stdout=f"written {full_path}",
            artifacts=[str(full_path)],
        )

    def read_file(self, sandbox: dict, path: str) -> ToolResult:
        started = time.time()
        decision = self.policy.check_read_path(self._policy_path(path))
        if not decision.allowed:
            return self._envelope(
                "sandbox_read_file",
                started,
                ok=False,
                exit_code=1,
                stderr=decision.reason,
                allowed=False,
                reason=decision.reason,
                error_type="policy_denied",
            )

        full_path = self._full_path(sandbox, path)
        if not full_path.exists():
            return self._envelope(
                "sandbox_read_file",
                started,
                ok=False,
                exit_code=1,
                stderr=f"file not found: {full_path}",
                error_type="not_found",
            )

        return self._envelope(
            "sandbox_read_file",
            started,
            stdout=full_path.read_text(encoding="utf-8"),
        )

    def list_dir(self, sandbox: dict, path: str) -> ToolResult:
        started = time.time()
        decision = self.policy.check_read_path(self._policy_path(path))
        if not decision.allowed:
            return self._envelope(
                "sandbox_list_dir",
                started,
                ok=False,
                exit_code=1,
                stderr=decision.reason,
                allowed=False,
                reason=decision.reason,
                error_type="policy_denied",
            )

        full_path = self._full_path(sandbox, path)
        if not full_path.exists():
            return self._envelope(
                "sandbox_list_dir",
                started,
                ok=False,
                exit_code=1,
                stderr=f"path not found: {full_path}",
                error_type="not_found",
            )

        entries = []
        for p in sorted(full_path.iterdir()):
            entries.append(f"{'d' if p.is_dir() else 'f'} {p.name}")

        return self._envelope(
            "sandbox_list_dir",
            started,
            stdout="\n".join(entries),
        )

    def exec(self, sandbox: dict, command: str, timeout_sec: int = 20) -> ToolResult:
        started = time.time()
        decision = self.policy.check_command(command)
        if not decision.allowed:
            return self._envelope(
                "sandbox_exec",
                started,
                ok=False,
                exit_code=1,
                stderr=decision.reason,
                allowed=False,
                reason=decision.reason,
                error_type="policy_denied",
            )

        try:
            proc = subprocess.run(
                ["/bin/bash", "-lc", command],
                cwd=sandbox["root"],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            return self._envelope(
                "sandbox_exec",
                started,
                ok=(proc.returncode == 0),
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except subprocess.TimeoutExpired as e:
            return self._envelope(
                "sandbox_exec",
                started,
                ok=False,
                exit_code=124,
                stdout=e.stdout or "",
                stderr=e.stderr or "command timed out",
                error_type="timeout",
            )


    def http_fetch(self, sandbox: dict, url: str, method: str = "GET", headers: dict | None = None) -> ToolResult:
        started = time.time()
        decision = self.policy.check_url(url)
        if not decision.allowed:
            return self._envelope(
                "sandbox_http_fetch",
                started,
                ok=False,
                exit_code=1,
                stderr=decision.reason,
                allowed=False,
                reason=decision.reason,
                error_type="policy_denied",
            )

        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=headers or {},
                timeout=30,
            )
            body = response.text
            return self._envelope(
                "sandbox_http_fetch",
                started,
                ok=(200 <= response.status_code < 300),
                exit_code=0 if 200 <= response.status_code < 300 else response.status_code,
                stdout=body,
                stderr="" if 200 <= response.status_code < 300 else f"http_status:{response.status_code}",
            )
        except Exception as e:
            return self._envelope(
                "sandbox_http_fetch",
                started,
                ok=False,
                exit_code=1,
                stderr=str(e),
                error_type="request_error",
            )
