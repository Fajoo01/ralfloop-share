from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from ralfloop_agent.providers.gpu_arbiter import GpuArbiterBusy, InferenceGpuArbiter
from ralfloop_agent.providers.agentcpm_lifecycle import AgentCpmLifecycleClient, AgentCpmLifecycleError
from ralfloop_agent.providers.llama_cpp_server import LlamaCppServerManager, ProcessIdentity, read_process_identity


class GpuEngineTransitionError(RuntimeError):
    def __init__(self, message: str, *, primary_error: BaseException | None = None, restore_error: BaseException | None = None) -> None:
        super().__init__(message)
        self.primary_error = primary_error
        self.restore_error = restore_error


@dataclass(frozen=True)
class EngineProvenance:
    name: str
    port: int
    model: str
    model_path: Path
    manager: str
    unit: str = ""
    owner_uid: int = -1
    pid: int | None = None
    parent_pid: int | None = None
    argv: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExternalEngineConfig:
    name: str = "agentcpm"
    host: str = "127.0.0.1"
    port: int = 19093
    model: str = "AgentCPM-Explore"
    model_path: Path = Path("/home/sibilla-cumana/.cache/huggingface/hub/models--openbmb--AgentCPM-Explore-GGUF/blobs/16e4f54d55b9e76a2a1636464d772b659bd352f599ef3950ed98c6a26571adea")
    systemd_unit: str = "ralfloop-agentcpm.service"
    expected_uid: int = 1001
    stop_timeout_sec: float = 30.0
    startup_timeout_sec: float = 120.0

    @classmethod
    def agentcpm_from_env(cls) -> "ExternalEngineConfig":
        return cls(
            model_path=Path(os.getenv("RALF_AGENTCPM_MODEL_PATH", str(cls.model_path))),
            systemd_unit="ralfloop-agentcpm.service",
            expected_uid=1001,
        )


class ExternalEngineLifecycle:
    """AgentCPM observation plus broker-only lifecycle authority."""

    def __init__(self, config: ExternalEngineConfig | None = None, *, client: AgentCpmLifecycleClient | None = None, identity_reader: Callable[[int], ProcessIdentity | None] = read_process_identity, audit_fn: Callable[..., None] | None = None, **_legacy: Any) -> None:
        self.config = config or ExternalEngineConfig.agentcpm_from_env()
        self.client = client or AgentCpmLifecycleClient()
        self.identity_reader = identity_reader
        self.audit_fn = audit_fn

    def healthy(self) -> bool:
        try:
            state = self.client.status()
            return bool(state["active"] and state.get("port_19093") and state.get("model") == self.config.model)
        except AgentCpmLifecycleError:
            return False

    def _matches(self, identity: ProcessIdentity) -> bool:
        argv = list(identity.argv)
        def value(*flags: str) -> str | None:
            for flag in flags:
                if flag in argv and argv.index(flag) + 1 < len(argv):
                    return argv[argv.index(flag) + 1]
            return None
        try:
            model_matches = Path(value("--model", "-m") or "").resolve() == self.config.model_path.resolve()
        except OSError:
            model_matches = False
        return identity.uid == self.config.expected_uid and value("--port") == str(self.config.port) and model_matches

    def discover(self) -> EngineProvenance | None:
        state = self.client.status()
        if not state["active"]:
            return None
        pid = state["main_pid"]
        identity = self.identity_reader(pid)
        # Broker has already performed authoritative provenance validation. /proc
        # discovery enriches audit data when hidepid policy permits it.
        if identity is not None and not self._matches(identity):
            raise GpuEngineTransitionError("agentcpm_broker_process_provenance_mismatch")
        if identity is not None:
            return self._provenance(identity, "broker_systemd_user", self.config.systemd_unit)
        return EngineProvenance(self.config.name, self.config.port, self.config.model,
                                self.config.model_path, "broker_systemd_user",
                                self.config.systemd_unit, self.config.expected_uid, pid)

    def _provenance(self, identity: ProcessIdentity, manager: str, unit: str = "") -> EngineProvenance:
        parent_pid = None
        try:
            stat = (Path("/proc") / str(identity.pid) / "stat").read_text()
            parent_pid = int(stat[stat.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            pass
        return EngineProvenance(self.config.name, self.config.port, self.config.model, self.config.model_path, manager, unit, identity.uid, identity.pid, parent_pid, identity.argv)

    def stop(self, provenance: EngineProvenance) -> None:
        self._audit("gpu_engine_stop_requested", engine=self.config.name, manager=provenance.manager, pid=provenance.pid)
        if provenance.manager != "broker_systemd_user" or provenance.unit != self.config.systemd_unit:
            raise GpuEngineTransitionError("agentcpm_non_broker_mutation_denied")
        state = self.client.stop()
        if state["active"] or state["main_pid"] != 0 or state.get("port_19093"):
            raise GpuEngineTransitionError("agentcpm_broker_stop_not_quiescent")
        self._audit("gpu_engine_stopped", engine=self.config.name)

    def start(self, provenance: EngineProvenance) -> None:
        self._audit("gpu_engine_restore_requested", engine=self.config.name, manager=provenance.manager)
        if provenance.manager != "broker_systemd_user" or provenance.unit != self.config.systemd_unit:
            raise GpuEngineTransitionError("agentcpm_non_broker_mutation_denied")
        state = self.client.start()
        if not state["active"] or state.get("model") != self.config.model or not state.get("port_19093"):
            raise GpuEngineTransitionError("agentcpm_broker_restore_not_ready")
        self._audit("gpu_engine_restored", engine=self.config.name)

    def _audit(self, event: str, **fields: Any) -> None:
        if self.audit_fn:
            self.audit_fn(event, **fields)


class TransactionalGpuScheduler:
    def __init__(self, *, chat: LlamaCppServerManager | None = None, external: ExternalEngineLifecycle | None = None, arbiter: InferenceGpuArbiter | None = None, audit_fn: Callable[..., None] | None = None) -> None:
        self.chat = chat or LlamaCppServerManager()
        self.external = external or ExternalEngineLifecycle(audit_fn=audit_fn)
        self.arbiter = arbiter or InferenceGpuArbiter(self.chat.config.gpu_lock_path)
        self.audit_fn = audit_fn

    @contextmanager
    def engine_session(self, engine: str, *, task_id: str = "") -> Iterator[dict[str, Any]]:
        if engine not in {"qwen_chat", "deepseek"}:
            raise ValueError("unsupported_gpu_engine")
        try:
            fd = self.arbiter.acquire_fd(provider=engine, mode="scheduler", task_id=task_id)
        except GpuArbiterBusy as exc:
            raise GpuEngineTransitionError("gpu_scheduler_busy") from exc
        initial: EngineProvenance | None = None
        result: dict[str, Any] = {"engine": engine, "initial_engine": "unknown", "external_stopped": False, "external_restored": False}
        primary_error: BaseException | None = None
        chat_started_here = False
        try:
            healthy_qwen = (
                engine == "qwen_chat"
                and callable(getattr(self.chat, "health", None))
                and self.chat.health()
            )
            if healthy_qwen:
                result["initial_engine"] = "qwen_chat_coexisting"
                result["external_preserved"] = True
                started = self.chat.ensure_available(preacquired_gpu_fd=fd)
            else:
                initial = self.external.discover()
                result["initial_engine"] = "agentcpm" if initial else "free"
                if initial:
                    self.external.stop(initial)
                    result["external_stopped"] = True
                gate = self.chat.resource_gate_status(llama_cpp_running=False)
                result["vram_after_release_mib"] = gate.get("gpu_free_mib")
                result["required_gpu_memory_mib"] = gate.get("required_gpu_memory_mib")
                if gate.get("gpu_free_mib") is None or int(gate["gpu_free_mib"]) < int(gate["required_gpu_memory_mib"]):
                    raise GpuEngineTransitionError("insufficient_gpu_memory_after_release")
                started = self.chat.ensure_available(preacquired_gpu_fd=fd) if engine == "qwen_chat" else {}
            chat_started_here = bool(started.get("server_started"))
            result.update(started)
            yield result
        except BaseException as exc:
            primary_error = exc
            result["primary_error"] = repr(exc)
            raise
        finally:
            cleanup_error: BaseException | None = None
            restore_error: BaseException | None = None
            try:
                if engine == "qwen_chat" and chat_started_here:
                    status = self.chat.status()
                    if status.get("managed"):
                        self.chat.stop()
                    if self.chat.status().get("managed"):
                        raise GpuEngineTransitionError("qwen_stop_verification_failed")
                    observer = getattr(self.chat, "gpu_observer", None)
                    if callable(observer):
                        observed = observer()
                        if not isinstance(observed, dict) or not isinstance(observed.get("free_mib"), int):
                            raise GpuEngineTransitionError("qwen_vram_release_unverifiable")
                        result["vram_after_qwen_stop_mib"] = observed["free_mib"]
            except BaseException as exc:
                cleanup_error = exc
                result["cleanup_error"] = repr(exc)
            try:
                if initial and result["external_stopped"]:
                    self.external.start(initial)
                    result["external_restored"] = True
            except BaseException as exc:
                restore_error = exc
                result["restore_error"] = repr(exc)
                self._audit("gpu_engine_restore_failed", primary_error=repr(primary_error), restore_error=repr(exc))
            finally:
                if fd >= 0:
                    self.arbiter.release_fd(fd)
            if primary_error is not None:
                if cleanup_error is not None:
                    primary_error.add_note(f"Qwen cleanup also failed: {cleanup_error!r}")
                if restore_error is not None:
                    primary_error.add_note(f"AgentCPM restore also failed: {restore_error!r}")
            elif cleanup_error is not None:
                if restore_error is not None:
                    cleanup_error.add_note(f"AgentCPM restore also failed: {restore_error!r}")
                raise cleanup_error
            elif restore_error is not None:
                raise GpuEngineTransitionError("agentcpm_restore_failed", restore_error=restore_error) from restore_error

    def _audit(self, event: str, **fields: Any) -> None:
        if self.audit_fn:
            self.audit_fn(event, **fields)


__all__ = ["EngineProvenance", "ExternalEngineConfig", "ExternalEngineLifecycle", "GpuEngineTransitionError", "TransactionalGpuScheduler"]
