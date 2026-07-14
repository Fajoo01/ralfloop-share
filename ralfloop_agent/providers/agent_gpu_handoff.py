from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable, Iterator, Sequence
from uuid import uuid4

import requests

from ralfloop_agent.providers.gpu_arbiter import GpuArbiterBusy, InferenceGpuArbiter
from ralfloop_agent.providers.llama_cpp_server import LlamaCppServerConfig, LlamaCppServerError, LlamaCppServerManager


class AgentGpuHandoffError(RuntimeError):
    code = "agent_gpu_handoff_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


class AgentGpuBusy(AgentGpuHandoffError):
    code = "engine_busy"


@dataclass(frozen=True)
class AgentGpuHandoffConfig:
    enabled: bool = True
    cleanup_timeout_sec: float = 20.0

    @classmethod
    def from_env(cls) -> "AgentGpuHandoffConfig":
        value = os.getenv("RALF_AGENT_GPU_HANDOFF", "1").strip().lower()
        try:
            timeout = float(os.getenv("RALF_AGENT_OLLAMA_CLEANUP_TIMEOUT", "20"))
        except ValueError:
            timeout = 20.0
        return cls(
            enabled=value not in {"0", "false", "no", "off"},
            cleanup_timeout_sec=timeout if timeout > 0 else 20.0,
        )


class AgentGpuCoordinator:
    def __init__(
        self,
        *,
        server_manager: LlamaCppServerManager | None = None,
        arbiter: InferenceGpuArbiter | None = None,
        session: requests.Session | None = None,
        config: AgentGpuHandoffConfig | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        port_in_use: Callable[[str, int], bool] | None = None,
        audit_fn: Callable[..., None] | None = None,
    ) -> None:
        self.server_manager = server_manager or LlamaCppServerManager()
        self.server_config: LlamaCppServerConfig = self.server_manager.config
        self.arbiter = arbiter or InferenceGpuArbiter(self.server_config.gpu_lock_path)
        self.session = session or requests.Session()
        self.config = config or AgentGpuHandoffConfig.from_env()
        self.sleep_fn = sleep_fn
        self.monotonic = monotonic
        self.port_in_use = port_in_use or self._port_in_use
        self.audit_fn = audit_fn

    def handoff_status(self) -> dict[str, Any]:
        server = self.server_manager.status()
        lock = self.arbiter.status(clean_stale=True)
        return {
            "llama_cpp_running": bool(server.get("managed") and server.get("healthy")),
            "llama_cpp_managed": bool(server.get("managed")),
            "port_19091_free": not self.port_in_use("127.0.0.1", self.server_config.port),
            "gpu_lock": lock.metadata.get("mode") if lock.held else "free",
            "gpu_lock_held": lock.held,
            "gpu_lock_stale_cleaned": lock.stale,
        }

    def switch_agent(self) -> dict[str, Any]:
        stopped = self._stop_managed_server()
        self._require_port_free()
        lock = self.arbiter.status(clean_stale=True)
        if lock.held:
            raise AgentGpuBusy("agent_gpu_lock_busy")
        return {**self.handoff_status(), "mode": "agent_ready", "server_stopped": stopped}

    def switch_chat(self) -> dict[str, Any]:
        lock = self.arbiter.status(clean_stale=True)
        if lock.held and lock.metadata.get("mode") == "agent":
            raise AgentGpuBusy("agent_task_active")
        try:
            started = self.server_manager.ensure_available()
        except LlamaCppServerError as exc:
            raise AgentGpuHandoffError(exc.code) from exc
        return {**self.handoff_status(), "mode": "chat", **started}

    @contextmanager
    def agent_session(self, *, models: Sequence[str], task_id: str | None = None) -> Iterator[dict[str, Any]]:
        if not self.config.enabled:
            yield {"enabled": False, "cleanup": "disabled"}
            return
        safe_task_id = (task_id or uuid4().hex)[:64]
        initial_models = self._ollama_models_or_none()
        stopped = self._stop_managed_server()
        self._require_port_free()
        try:
            fd = self.arbiter.acquire_fd(
                provider="ollama",
                mode="agent",
                task_id=safe_task_id,
            )
        except GpuArbiterBusy as exc:
            raise AgentGpuBusy("agent_gpu_lock_busy") from exc
        self._audit("gpu_handoff_chat_to_agent", task_id=safe_task_id, server_stopped=stopped)
        result: dict[str, Any] = {
            "enabled": True,
            "task_id": safe_task_id,
            "server_stopped": stopped,
            "cleanup": "pending",
        }
        try:
            yield result
        finally:
            try:
                cleanup = self._cleanup_task_models(set(models), initial_models)
                result.update(cleanup)
                self._audit("gpu_agent_cleanup", task_id=safe_task_id, **cleanup)
            finally:
                self.arbiter.release_fd(fd)

    def _stop_managed_server(self) -> bool:
        status = self.server_manager.status()
        if status.get("managed"):
            try:
                result = self.server_manager.stop()
            except LlamaCppServerError as exc:
                raise AgentGpuHandoffError(exc.code) from exc
            return bool(result.get("changed"))
        if status.get("healthy") or self.port_in_use("127.0.0.1", self.server_config.port):
            raise AgentGpuHandoffError("llama_cpp_unmanaged_process_on_port")
        return False

    def _require_port_free(self) -> None:
        deadline = self.monotonic() + 5.0
        while self.monotonic() < deadline:
            if not self.port_in_use("127.0.0.1", self.server_config.port):
                return
            self.sleep_fn(0.1)
        raise AgentGpuHandoffError("llama_cpp_port_not_released")

    def _ollama_models_or_none(self) -> set[str] | None:
        try:
            return set(self.server_manager.ollama_gpu_models())
        except LlamaCppServerError:
            return None

    def _cleanup_task_models(self, requested: set[str], initial: set[str] | None) -> dict[str, Any]:
        if initial is None:
            return {"cleanup": "partial", "cleanup_reason": "initial_ollama_state_unknown"}
        current = self._ollama_models_or_none()
        if current is None:
            return {"cleanup": "partial", "cleanup_reason": "final_ollama_state_unknown"}
        targets = sorted((requested & current) - initial)
        unloaded: list[str] = []
        for model in targets:
            response = None
            try:
                response = self.session.post(
                    f"{self.server_config.ollama_base_url}/api/generate",
                    json={"model": model, "keep_alive": 0},
                    timeout=(1.0, 5.0),
                )
                response.raise_for_status()
                unloaded.append(model)
            except requests.RequestException:
                return {
                    "cleanup": "partial",
                    "cleanup_reason": "ollama_unload_failed",
                    "unloaded_models": unloaded,
                }
            finally:
                if response is not None:
                    response.close()
        deadline = self.monotonic() + self.config.cleanup_timeout_sec
        remaining = set(targets)
        while remaining and self.monotonic() < deadline:
            active = self._ollama_models_or_none()
            if active is None:
                break
            remaining &= active
            if remaining:
                self.sleep_fn(0.2)
        return {
            "cleanup": "complete" if not remaining else "partial",
            "cleanup_reason": "" if not remaining else "ollama_unload_timeout",
            "unloaded_models": unloaded,
            "preserved_models": sorted(initial),
        }

    def _audit(self, event: str, **fields: Any) -> None:
        if self.audit_fn is not None:
            self.audit_fn(event, **fields)

    @staticmethod
    def _port_in_use(host: str, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((host, port)) == 0


__all__ = [
    "AgentGpuBusy",
    "AgentGpuCoordinator",
    "AgentGpuHandoffConfig",
    "AgentGpuHandoffError",
]
