from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
import gc
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .registry import ModelToolRegistry, ModelToolSpec, validate_schema_value


class ModelToolEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    tool_id: str
    model_id: str
    revision: str | None
    device: str
    duration_ms: int = Field(ge=0)
    input_hash: str
    output: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error_type: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)


class ResourceDecision(BaseModel):
    ok: bool
    reason: str
    ram_available_mb: int | None = None
    vram_available_mb: int | None = None


Runner = Callable[[ModelToolSpec, Path, dict[str, Any]], dict[str, Any]]
ResourceProbe = Callable[[ModelToolSpec], ResourceDecision]
AuditSink = Callable[[str, dict[str, Any]], None]
GpuSessionFactory = Callable[[ModelToolSpec, str], AbstractContextManager[dict[str, Any]]]


def _input_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _resident_agentcpm_ready(spec: ModelToolSpec) -> bool:
    if spec.backend != "agentcpm_llama_cpp" or spec.device != "cuda":
        return False
    try:
        from ralfloop_agent.providers.agentcpm_lifecycle import (
            AgentCpmLifecycleClient,
            AgentCpmLifecycleError,
        )

        state = AgentCpmLifecycleClient(timeout=2.0).status()
    except (AgentCpmLifecycleError, OSError, ValueError):
        return False
    return bool(
        state.get("active")
        and state.get("port_19093")
        and state.get("model") == "AgentCPM-Explore"
    )


def _resource_probe(spec: ModelToolSpec) -> ResourceDecision:
    try:
        import psutil

        ram_available = int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        ram_available = None
    if spec.max_ram_mb and ram_available is not None and ram_available < spec.max_ram_mb:
        return ResourceDecision(ok=False, reason="ram_gate_failed", ram_available_mb=ram_available)
    if spec.device == "cuda":
        if os.getenv("RALF_MODEL_TOOLS_ALLOW_CUDA", "0") != "1":
            return ResourceDecision(ok=False, reason="cuda_opt_in_required", ram_available_mb=ram_available)
        if _resident_agentcpm_ready(spec):
            return ResourceDecision(
                ok=True,
                reason="resident_agentcpm_reuse",
                ram_available_mb=ram_available,
            )
        try:
            import torch

            if not torch.cuda.is_available():
                return ResourceDecision(ok=False, reason="cuda_unavailable", ram_available_mb=ram_available)
            free_bytes, _ = torch.cuda.mem_get_info()
            vram_available = int(free_bytes / (1024 * 1024))
        except Exception:
            return ResourceDecision(ok=False, reason="cuda_probe_failed", ram_available_mb=ram_available)
        if spec.max_vram_mb and vram_available < spec.max_vram_mb:
            return ResourceDecision(
                ok=False,
                reason="vram_gate_failed",
                ram_available_mb=ram_available,
                vram_available_mb=vram_available,
            )
        return ResourceDecision(
            ok=True,
            reason="resource_gate_passed",
            ram_available_mb=ram_available,
            vram_available_mb=vram_available,
        )
    return ResourceDecision(ok=True, reason="resource_gate_passed", ram_available_mb=ram_available)


def _isolated_environment(spec: ModelToolSpec) -> dict[str, str]:
    allowed = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
        if key in os.environ
    }
    allowed.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "PYTHONNOUSERSITE": "1",
            "CUDA_VISIBLE_DEVICES": "" if spec.device == "cpu" else os.getenv("CUDA_VISIBLE_DEVICES", "0"),
        }
    )
    for key in (
        "RALFLOOP_SEARXNG_URL",
        "RALF_AGENTCPM_LLAMA_SERVER_BIN",
        "RALF_MODEL_TOOL_STATE_DIR",
        "RALF_MODEL_TOOL_LOG_ROOTS",
        "RALF_REMOTE_CODE_APPROVAL_DIR",
    ):
        value = os.getenv(key, "").strip()
        if value:
            allowed[key] = value
    return allowed


def _worker_command(spec: ModelToolSpec) -> list[str]:
    project_root = Path(__file__).resolve().parents[2]
    bootstrap = (
        "import runpy,sys;"
        f"sys.path.insert(0,{str(project_root)!r});"
        "runpy.run_module('ralfloop_agent.model_tools.worker',run_name='__main__')"
    )
    return [spec.python_executable, "-I", "-c", bootstrap]


def _terminate_worker_group(process: subprocess.Popen[str], *, timeout: float = 5.0) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=timeout)


def _document_input_policy(spec: ModelToolSpec, payload: dict[str, Any]) -> str | None:
    if spec.capability != "document_to_markdown":
        return None
    raw = Path(str(payload.get("file_path") or "")).expanduser()
    try:
        if raw.is_symlink():
            return "document_symlink_forbidden"
        path = raw.resolve(strict=True)
    except OSError:
        return "document_missing"
    if not path.is_file():
        return "document_not_file"
    configured = os.getenv("RALF_MODEL_TOOL_DOCUMENT_ROOTS", "").strip()
    roots = [Path(item).expanduser().resolve() for item in configured.split(os.pathsep) if item.strip()]
    if not roots:
        roots = [
            (Path.home() / ".local" / "state" / "ralf").resolve(),
            (Path.home() / "ralfloop_data").resolve(),
        ]
    if not any(path.is_relative_to(root) for root in roots):
        return "document_path_forbidden"
    if path.suffix.casefold() not in {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"}:
        return "document_type_forbidden"
    if path.stat().st_size > 100 * 1024 * 1024:
        return "document_too_large"
    return None


def _gpu_session(spec: ModelToolSpec, tool_id: str) -> AbstractContextManager[dict[str, Any]]:
    if spec.device != "cuda" or spec.backend != "agentcpm_llama_cpp":
        return nullcontext({"enabled": False})
    if _resident_agentcpm_ready(spec):
        return nullcontext({"enabled": False, "resident_agentcpm": True})

    from ralfloop_agent.providers.agent_gpu_handoff import AgentGpuCoordinator

    coordinator = AgentGpuCoordinator()
    # A short DS4/Qwen handoff may temporarily own the shared GPU lock while
    # AgentCPM is stopped and then restored. Deep research is a long-running
    # read operation, so queue behind that handoff instead of surfacing a
    # transient agent_gpu_lock_busy error to the user. If AgentCPM comes back
    # while we wait, reuse the resident server and skip a duplicate handoff.
    try:
        configured_wait = float(os.getenv("RALF_MODEL_TOOL_GPU_WAIT_SEC", "120"))
    except ValueError:
        configured_wait = 120.0
    wait_sec = max(0.0, min(configured_wait, max(0.0, float(spec.timeout_sec) - 5.0)))
    deadline = time.monotonic() + wait_sec
    while coordinator.arbiter.status(clean_stale=True).held and time.monotonic() < deadline:
        if _resident_agentcpm_ready(spec):
            return nullcontext({"enabled": False, "resident_agentcpm": True, "waited_for_gpu": True})
        time.sleep(0.25)
    if _resident_agentcpm_ready(spec):
        return nullcontext({"enabled": False, "resident_agentcpm": True, "waited_for_gpu": True})
    return coordinator.agent_session(models=(), task_id=f"model-tool-{tool_id}"[:64])


def _subprocess_runner(spec: ModelToolSpec, snapshot: Path, payload: dict[str, Any]) -> dict[str, Any]:
    request = {
        "spec": spec.model_dump(mode="json"),
        "snapshot": str(snapshot),
        "input": payload,
    }
    tmp_root = Path(
        os.environ.get(
            "RALFLOOP_MODEL_TOOL_TMPDIR",
            str(Path.home() / ".local" / "state" / "ralf" / "repair" / "tmp"),
        )
    )
    tmp_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(
        prefix="ralf-model-tool-",
        dir=tmp_root,
    ) as cwd:
        process = subprocess.Popen(
            _worker_command(spec),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=_isolated_environment(spec),
            shell=False,
            start_new_session=True,
        )

        stop_memory_watch = threading.Event()
        memory_limit_hit = threading.Event()

        def memory_watch() -> None:
            if not spec.max_ram_mb:
                return
            limit_kb = int(spec.max_ram_mb) * 1024
            status_path = Path(f"/proc/{process.pid}/status")

            while not stop_memory_watch.wait(0.05):
                try:
                    lines = status_path.read_text(
                        encoding="ascii",
                        errors="ignore",
                    ).splitlines()
                except (FileNotFoundError, ProcessLookupError, OSError):
                    return

                rss_kb = 0
                for line in lines:
                    if line.startswith("VmRSS:"):
                        try:
                            rss_kb = int(line.split()[1])
                        except (IndexError, ValueError):
                            rss_kb = 0
                        break

                if rss_kb > limit_kb:
                    memory_limit_hit.set()
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return

        watcher = threading.Thread(
            target=memory_watch,
            name=f"ralf-model-memory-{spec.tool_id}",
            daemon=True,
        )
        watcher.start()

        try:
            stdout, stderr = process.communicate(
                json.dumps(request, ensure_ascii=False),
                timeout=spec.timeout_sec,
            )
        except subprocess.TimeoutExpired:
            _terminate_worker_group(process)
            raise
        except BaseException:
            _terminate_worker_group(process)
            raise
        finally:
            stop_memory_watch.set()
            watcher.join(timeout=1.0)

        if memory_limit_hit.is_set():
            raise RuntimeError("model_tool_memory_limit_exceeded")
    if process.returncode != 0:
        stderr_lines = stderr.strip().splitlines()
        detail = stderr_lines[-1] if stderr_lines else "no_stderr"
        raise RuntimeError(f"model_tool_worker_failed:{process.returncode}:{detail[:500]}")
    try:
        output = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("model_tool_worker_invalid_json") from exc
    if not isinstance(output, dict):
        raise RuntimeError("model_tool_worker_non_object")
    return output


class ModelToolManager:
    def __init__(
        self,
        registry: ModelToolRegistry,
        *,
        runner: Runner = _subprocess_runner,
        resource_probe: ResourceProbe = _resource_probe,
        gpu_session_factory: GpuSessionFactory = _gpu_session,
        audit: AuditSink | None = None,
        circuit_threshold: int = 3,
    ) -> None:
        self.registry = registry
        self.runner = runner
        self.resource_probe = resource_probe
        self.gpu_session_factory = gpu_session_factory
        self.audit = audit or (lambda event, fields: None)
        self.circuit_threshold = max(1, circuit_threshold)
        self._failures: dict[str, int] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._heavy_lock = threading.Lock()

    def _envelope(
        self,
        spec: ModelToolSpec,
        payload: dict[str, Any],
        started: float,
        *,
        ok: bool,
        output: dict[str, Any] | None = None,
        error_type: str | None = None,
        warnings: list[str] | None = None,
        snapshot: Path | None = None,
    ) -> ModelToolEnvelope:
        return ModelToolEnvelope(
            ok=ok,
            tool_id=spec.tool_id,
            model_id=spec.model_id,
            revision=spec.revision,
            device=spec.device,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            input_hash=_input_hash(payload),
            output=output or {},
            warnings=warnings or [],
            error_type=error_type,
            provenance={
                "snapshot": str(snapshot) if snapshot else None,
                "backend": spec.backend,
                "runtime": "isolated_process_per_call",
                "local_files_only": spec.local_files_only,
                "trust_remote_code": spec.trust_remote_code,
                "network_mode": "read_only" if spec.capability == "deep_web_research" else "none",
                "gpu_handoff": spec.device == "cuda" and spec.backend == "agentcpm_llama_cpp",
                "storage_mode": "mmap_ssd" if spec.backend == "agentcpm_llama_cpp" else None,
            },
        )

    def invoke(self, tool_id: str, payload: dict[str, Any]) -> ModelToolEnvelope:
        started = time.monotonic()
        try:
            spec = self.registry.get(tool_id)
        except KeyError:
            return ModelToolEnvelope(
                ok=False,
                tool_id=tool_id,
                model_id="unknown",
                revision=None,
                device="cpu",
                duration_ms=0,
                input_hash=_input_hash(payload),
                error_type="tool_unavailable",
                provenance={"runtime": "not_started"},
            )
        status = self.registry.availability(spec)
        if not status.ok:
            return self._envelope(
                spec,
                payload,
                started,
                ok=False,
                error_type=status.status,
                warnings=["tool_unavailable", "no_fallback"],
                snapshot=status.path,
            )
        valid, schema_error = validate_schema_value(payload, spec.input_schema)
        if not valid:
            return self._envelope(
                spec, payload, started, ok=False, error_type="invalid_input_schema", warnings=[str(schema_error)]
            )
        policy_error = _document_input_policy(spec, payload)
        if policy_error:
            return self._envelope(spec, payload, started, ok=False, error_type=policy_error)
        if self._failures.get(tool_id, 0) >= self.circuit_threshold:
            return self._envelope(spec, payload, started, ok=False, error_type="circuit_open", snapshot=status.path)
        lock = self._locks.setdefault(tool_id, threading.Lock())
        if not lock.acquire(timeout=spec.timeout_sec):
            return self._envelope(spec, payload, started, ok=False, error_type="tool_lock_timeout", snapshot=status.path)
        try:
            with self._heavy_lock:
                with self.gpu_session_factory(spec, tool_id) as handoff:
                    resources = self.resource_probe(spec)
                    if not resources.ok:
                        return self._envelope(
                            spec,
                            payload,
                            started,
                            ok=False,
                            error_type=resources.reason,
                            warnings=["resource_gate_not_bypassed"],
                            snapshot=status.path,
                        )
                    self.audit(
                        "model_tool_started",
                        {
                            "tool_id": tool_id,
                            "input_hash": _input_hash(payload),
                            "gpu_handoff": bool(handoff.get("enabled")),
                        },
                    )
                    output = self.runner(spec, status.path, payload)
            valid, schema_error = validate_schema_value(output, spec.output_schema)
            if not valid:
                raise ValueError(f"invalid_output_schema:{schema_error}")
            self._failures[tool_id] = 0
            envelope = self._envelope(spec, payload, started, ok=True, output=output, snapshot=status.path)
            self.audit("model_tool_finished", envelope.model_dump(mode="json"))
            return envelope
        except subprocess.TimeoutExpired:
            self._failures[tool_id] = self._failures.get(tool_id, 0) + 1
            return self._envelope(spec, payload, started, ok=False, error_type="tool_timeout", snapshot=status.path)
        except Exception as exc:
            self._failures[tool_id] = self._failures.get(tool_id, 0) + 1
            error = "sandboxed_remote_code_required" if "trust_remote_code" in str(exc) else "tool_runtime_error"
            return self._envelope(
                spec,
                payload,
                started,
                ok=False,
                error_type=error,
                warnings=[type(exc).__name__, str(exc)[:200]],
                snapshot=status.path,
            )
        finally:
            lock.release()
            self.unload(tool_id)

    def unload(self, tool_id: str) -> None:
        gc.collect()
        self.audit("model_tool_unloaded", {"tool_id": tool_id})

    def health(self) -> dict[str, Any]:
        return {
            **self.registry.health(),
            "circuits": {
                tool_id: {
                    "failures": count,
                    "open": count >= self.circuit_threshold,
                }
                for tool_id, count in self._failures.items()
            },
            "lifecycle": "isolated_process_per_call",
            "simultaneous_heavy_models": 1,
        }


__all__ = ["ModelToolEnvelope", "ModelToolManager", "ResourceDecision"]
