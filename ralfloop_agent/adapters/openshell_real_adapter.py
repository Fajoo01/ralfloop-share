from __future__ import annotations

import os
import time
import json

import requests

from ralfloop_agent.core.policy import PolicyLayer
from ralfloop_agent.tools.contracts import ToolResult


class OpenShellAdapterReal:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        policy: PolicyLayer | None = None,
        local_fallback=None,
    ) -> None:
        self.base_url = (base_url or os.getenv("OPENSHELL_BASE_URL") or "http://127.0.0.1:19090").rstrip("/")
        self.api_key = api_key or os.getenv("OPENSHELL_API_KEY")
        self.policy = policy or PolicyLayer()
        self.local_fallback = local_fallback

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    def _envelope(
        self,
        tool_name: str,
        started: float,
        ok: bool = True,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        allowed: bool = True,
        reason: str = "allowed",
        artifacts: list[str] | None = None,
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

    def create_sandbox(self) -> dict:
        r = requests.post(f"{self.base_url}/sandboxes", headers=self._headers(), timeout=15)
        r.raise_for_status()
        data = r.json()
        return {
            "id": data["id"],
            "root": data["root"],
            "status": data.get("status", "ready"),
        }

    def destroy_sandbox(self, sandbox_id: str) -> None:
        try:
            requests.delete(f"{self.base_url}/sandboxes/{sandbox_id}", headers=self._headers(), timeout=15)
        except Exception:
            pass

    def write_file(self, sandbox: dict, path: str, content: str) -> ToolResult:
        started = time.time()
        try:
            r = requests.post(
                f"{self.base_url}/sandboxes/{sandbox['id']}/write",
                headers=self._headers(),
                json={"path": path, "content": content},
                timeout=15,
            )
            if r.status_code == 404:
                return self._envelope(
                    "sandbox_write_file",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr="sandbox not found",
                    error_type="not_found",
                )
            if r.status_code >= 400:
                return self._envelope(
                    "sandbox_write_file",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr=f"http_status:{r.status_code}",
                    error_type="request_error",
                )

            data = r.json()
            return self._envelope(
                "sandbox_write_file",
                started,
                ok=True,
                exit_code=0,
                stdout=f"written {data.get('path', path)}",
                artifacts=[data.get("path", path)],
            )
        except Exception as e:
            return self._envelope(
                "sandbox_write_file",
                started,
                ok=False,
                exit_code=1,
                stderr=str(e),
                error_type="request_error",
            )

    def read_file(self, sandbox: dict, path: str) -> ToolResult:
        started = time.time()
        try:
            r = requests.get(
                f"{self.base_url}/sandboxes/{sandbox['id']}/read",
                headers=self._headers(),
                params={"path": path},
                timeout=15,
            )
            if r.status_code == 404:
                return self._envelope(
                    "sandbox_read_file",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr="file not found",
                    error_type="not_found",
                )
            if r.status_code >= 400:
                return self._envelope(
                    "sandbox_read_file",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr=f"http_status:{r.status_code}",
                    error_type="request_error",
                )

            data = r.json()
            return self._envelope(
                "sandbox_read_file",
                started,
                ok=True,
                exit_code=0,
                stdout=data.get("content", ""),
            )
        except Exception as e:
            return self._envelope(
                "sandbox_read_file",
                started,
                ok=False,
                exit_code=1,
                stderr=str(e),
                error_type="request_error",
            )

    def list_dir(self, sandbox: dict, path: str) -> ToolResult:
        started = time.time()
        try:
            r = requests.get(
                f"{self.base_url}/sandboxes/{sandbox['id']}/list",
                headers=self._headers(),
                params={"path": path},
                timeout=15,
            )
            if r.status_code == 404:
                return self._envelope(
                    "sandbox_list_dir",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr="path not found",
                    error_type="not_found",
                )
            if r.status_code >= 400:
                return self._envelope(
                    "sandbox_list_dir",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr=f"http_status:{r.status_code}",
                    error_type="request_error",
                )

            data = r.json()
            entries = data.get("entries", [])
            return self._envelope(
                "sandbox_list_dir",
                started,
                ok=True,
                exit_code=0,
                stdout="\n".join(entries),
            )
        except Exception as e:
            return self._envelope(
                "sandbox_list_dir",
                started,
                ok=False,
                exit_code=1,
                stderr=str(e),
                error_type="request_error",
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
            r = requests.post(
                f"{self.base_url}/sandboxes/{sandbox['id']}/exec",
                headers=self._headers(),
                json={"command": command, "timeout_sec": timeout_sec},
                timeout=max(timeout_sec + 5, 15),
            )
            if r.status_code == 404:
                return self._envelope(
                    "sandbox_exec",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr="sandbox not found",
                    error_type="not_found",
                )
            if r.status_code >= 400:
                return self._envelope(
                    "sandbox_exec",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr=f"http_status:{r.status_code}",
                    error_type="request_error",
                )

            data = r.json()
            return self._envelope(
                "sandbox_exec",
                started,
                ok=data.get("ok", False),
                exit_code=data.get("exit_code", 0),
                stdout=data.get("stdout", ""),
                stderr=data.get("stderr", ""),
            )
        except Exception as e:
            return self._envelope(
                "sandbox_exec",
                started,
                ok=False,
                exit_code=1,
                stderr=str(e),
                error_type="request_error",
            )


    def probe_stream(self, sandbox: dict, url: str, referer: str | None = None, user_agent: str | None = None) -> ToolResult:
        started = time.time()
        try:
            r = requests.post(
                f"{self.base_url}/sandboxes/{sandbox['id']}/probe_stream",
                headers=self._headers(),
                json={"url": url, "referer": referer, "user_agent": user_agent},
                timeout=40,
            )
            if r.status_code >= 400:
                return self._envelope(
                    "sandbox_probe_stream",
                    started,
                    ok=False,
                    exit_code=1,
                    stderr=f"http_status:{r.status_code}",
                    error_type="request_error",
                )
            data = r.json()
            return self._envelope(
                "sandbox_probe_stream",
                started,
                ok=True,
                exit_code=0,
                stdout=json.dumps(data, ensure_ascii=False),
                artifacts=[data.get("artifact")] if data.get("artifact") else [],
            )
        except Exception as e:
            return self._envelope(
                "sandbox_probe_stream",
                started,
                ok=False,
                exit_code=1,
                stderr=str(e),
                error_type="request_error",
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
            return self._envelope(
                "sandbox_http_fetch",
                started,
                ok=(200 <= response.status_code < 300),
                exit_code=0 if 200 <= response.status_code < 300 else response.status_code,
                stdout=response.text,
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
