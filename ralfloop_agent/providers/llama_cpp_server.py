from __future__ import annotations

import argparse
import atexit
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import requests

from ralfloop_agent.providers.gpu_arbiter import GpuArbiterBusy, InferenceGpuArbiter


DEFAULT_MODEL_PATH = Path(
    "/usr/share/ollama/.ollama/models/blobs/"
    "sha256-2bada8a7450677000f678be90653b85d364de7db25eb5ea54136ada5f3933730"
)
DEFAULT_MODEL_HASH = "2bada8a7450677000f678be90653b85d364de7db25eb5ea54136ada5f3933730"
DEFAULT_SERVER_BIN = Path("/home/sibilla-cumana/src/llama.cpp/build/bin/llama-server")

class LlamaCppProcessState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass
class _ManagedChild:
    process: Any
    owner_pid: int
    started_at: float
    lifecycle_key: str
    state: LlamaCppProcessState = LlamaCppProcessState.STARTING
    returncode: int | None = None
    waited: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass
class _LifecycleStatus:
    state: LlamaCppProcessState = LlamaCppProcessState.STOPPED
    pid: int | None = None
    owner_pid: int | None = None
    started_at: float | None = None
    returncode: int | None = None


_LOCAL_CHILDREN: dict[int, _ManagedChild] = {}
_LOCAL_CHILDREN_LOCK = threading.RLock()
_LIFECYCLE_STATUS: dict[str, _LifecycleStatus] = {}


def _remember_local_child(process: Any, *, lifecycle_key: str = "") -> None:
    with _LOCAL_CHILDREN_LOCK:
        pid = int(process.pid)
        _LOCAL_CHILDREN[pid] = _ManagedChild(
            process=process,
            owner_pid=os.getpid(),
            started_at=time.time(),
            lifecycle_key=lifecycle_key,
        )
        if lifecycle_key:
            _LIFECYCLE_STATUS[lifecycle_key] = _LifecycleStatus(
                state=LlamaCppProcessState.STARTING,
                pid=pid,
                owner_pid=os.getpid(),
                started_at=_LOCAL_CHILDREN[pid].started_at,
            )


def _local_child(pid: int) -> Any | None:
    with _LOCAL_CHILDREN_LOCK:
        record = _LOCAL_CHILDREN.get(pid)
        return record.process if record is not None else None


def _local_child_record(pid: int) -> _ManagedChild | None:
    with _LOCAL_CHILDREN_LOCK:
        return _LOCAL_CHILDREN.get(pid)


def _local_child_for_lifecycle(lifecycle_key: str) -> _ManagedChild | None:
    with _LOCAL_CHILDREN_LOCK:
        return next(
            (record for record in _LOCAL_CHILDREN.values() if record.lifecycle_key == lifecycle_key),
            None,
        )


def _forget_local_child(pid: int) -> None:
    with _LOCAL_CHILDREN_LOCK:
        _LOCAL_CHILDREN.pop(pid, None)


def _set_lifecycle(
    lifecycle_key: str,
    state: LlamaCppProcessState,
    *,
    pid: int | None = None,
    owner_pid: int | None = None,
    started_at: float | None = None,
    returncode: int | None = None,
) -> None:
    with _LOCAL_CHILDREN_LOCK:
        previous = _LIFECYCLE_STATUS.get(lifecycle_key, _LifecycleStatus())
        reset = state == LlamaCppProcessState.STARTING and pid is None
        _LIFECYCLE_STATUS[lifecycle_key] = _LifecycleStatus(
            state=state,
            pid=None if reset else (pid if pid is not None else previous.pid),
            owner_pid=None if reset else (owner_pid if owner_pid is not None else previous.owner_pid),
            started_at=None if reset else (started_at if started_at is not None else previous.started_at),
            returncode=(
                returncode
                if returncode is not None
                else None
                if state
                in {
                    LlamaCppProcessState.STARTING,
                    LlamaCppProcessState.RUNNING,
                    LlamaCppProcessState.STOPPED,
                    LlamaCppProcessState.FAILED,
                }
                else previous.returncode
            ),
        )


def _lifecycle_status(lifecycle_key: str) -> _LifecycleStatus:
    with _LOCAL_CHILDREN_LOCK:
        return _LIFECYCLE_STATUS.get(lifecycle_key, _LifecycleStatus())


def _reap_children_at_exit() -> None:
    with _LOCAL_CHILDREN_LOCK:
        records = list(_LOCAL_CHILDREN.items())
    for pid, record in records:
        if record.owner_pid != os.getpid():
            continue
        with record.lock:
            process = record.process
            try:
                if process.poll() is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    returncode = process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    returncode = process.wait(timeout=5.0)
            except ChildProcessError:
                returncode = getattr(process, "returncode", None)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                continue
            record.returncode = returncode
            record.waited = True
            record.state = LlamaCppProcessState.STOPPED
            _set_lifecycle(record.lifecycle_key, LlamaCppProcessState.STOPPED, returncode=returncode)
            _forget_local_child(pid)


atexit.register(_reap_children_at_exit)


class LlamaCppServerError(RuntimeError):
    code = "llama_cpp_server_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


class LlamaCppServerUnavailable(LlamaCppServerError):
    code = "llama_cpp_server_unavailable"


class LlamaCppServerBusy(LlamaCppServerError):
    code = "llama_cpp_gpu_lock_busy"


class LlamaCppServerOwnershipError(LlamaCppServerError):
    code = "llama_cpp_pid_not_owned"


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class LlamaCppServerConfig:
    base_url: str = "http://127.0.0.1:19091"
    model: str = "qwen2.5:7b"
    model_path: Path = DEFAULT_MODEL_PATH
    model_hash: str = DEFAULT_MODEL_HASH
    server_bin: Path = DEFAULT_SERVER_BIN
    request_timeout_sec: float = 180.0
    idle_timeout_sec: float = 60.0
    startup_timeout_sec: float = 180.0
    context: int = 4096
    gpu_layers: int = 24
    threads: int = 6
    slots: int = 1
    cache_prompt: bool = True
    cache_ram_mib: int = 1024
    ngram: bool = False
    autostart: bool = True
    fallback: str = "none"
    state_dir: Path = Path.home() / ".local" / "state" / "ralf"
    ollama_base_url: str = "http://127.0.0.1:11434"

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.port is None
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("llama_cpp_endpoint_must_be_loopback_http")
        if self.slots != 1:
            raise ValueError("llama_cpp_slots_must_equal_one")
        if len(self.model_hash) != 64 or any(char not in "0123456789abcdef" for char in self.model_hash.lower()):
            raise ValueError("invalid_llama_cpp_model_hash")
        if self.fallback not in {"ollama", "none"}:
            raise ValueError("invalid_llama_cpp_fallback")
        if self.ngram:
            raise ValueError("llama_cpp_ngram_disabled")

    @property
    def port(self) -> int:
        parsed = urlparse(self.base_url)
        assert parsed.port is not None
        return parsed.port

    @property
    def pid_path(self) -> Path:
        return self.state_dir / "llama_cpp.pid.json"

    @property
    def engine_lock_path(self) -> Path:
        return self.state_dir / "llama_cpp-engine.lock"

    @property
    def gpu_lock_path(self) -> Path:
        return self.state_dir / "inference-gpu.lock"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "logs" / "llama_cpp.log"

    @classmethod
    def from_env(cls) -> "LlamaCppServerConfig":
        return cls(
            base_url=os.getenv("RALF_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19091").rstrip("/"),
            model=os.getenv("RALF_LLAMA_CPP_MODEL", "qwen2.5:7b").strip() or "qwen2.5:7b",
            model_path=Path(os.getenv("RALF_LLAMA_CPP_MODEL_PATH", str(DEFAULT_MODEL_PATH))).expanduser(),
            model_hash=os.getenv(
                "RALF_LLAMA_CPP_MODEL_SHA256",
                os.getenv("RALF_LLAMA_CPP_MODEL_HASH", DEFAULT_MODEL_HASH),
            ).strip().lower(),
            server_bin=Path(os.getenv("RALF_LLAMA_CPP_SERVER_BIN", str(DEFAULT_SERVER_BIN))).expanduser(),
            request_timeout_sec=_env_float("RALF_LLAMA_CPP_TIMEOUT", 180.0),
            idle_timeout_sec=_env_float("RALF_LLAMA_CPP_IDLE_TIMEOUT", 60.0),
            startup_timeout_sec=_env_float("RALF_LLAMA_CPP_STARTUP_TIMEOUT", 180.0),
            context=_env_int("RALF_LLAMA_CPP_CONTEXT", 4096),
            gpu_layers=_env_int("RALF_LLAMA_CPP_GPU_LAYERS", 24),
            threads=_env_int("RALF_LLAMA_CPP_THREADS", 6),
            slots=_env_int("RALF_LLAMA_CPP_SLOTS", 1),
            cache_prompt=_env_bool("RALF_LLAMA_CPP_CACHE_PROMPT", True),
            cache_ram_mib=_env_int("RALF_LLAMA_CPP_CACHE_RAM_MIB", 1024),
            ngram=_env_bool("RALF_LLAMA_CPP_NGRAM", False),
            autostart=_env_bool("RALF_LLAMA_CPP_AUTOSTART", True),
            fallback=os.getenv("RALF_LLAMA_CPP_FALLBACK", "none").strip().lower() or "none",
            state_dir=Path(
                os.getenv("RALF_LLAMA_CPP_STATE_DIR", str(Path.home() / ".local" / "state" / "ralf"))
            ).expanduser(),
            ollama_base_url=os.getenv("RALF_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"),
        )


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    uid: int
    start_ticks: int
    executable: str
    argv: tuple[str, ...]


def read_process_identity(pid: int) -> ProcessIdentity | None:
    proc = Path("/proc") / str(pid)
    try:
        stat_text = (proc / "stat").read_text(encoding="utf-8")
        after_name = stat_text[stat_text.rfind(")") + 2 :].split()
        start_ticks = int(after_name[19])
        argv = tuple(part.decode("utf-8", errors="replace") for part in (proc / "cmdline").read_bytes().split(b"\0") if part)
        executable = os.readlink(proc / "exe")
        uid = proc.stat().st_uid
    except (OSError, ValueError, IndexError):
        return None
    return ProcessIdentity(pid=pid, uid=uid, start_ticks=start_ticks, executable=executable, argv=argv)


def verify_model_hash(path: Path, expected_hash: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(8 * 1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise LlamaCppServerUnavailable("llama_cpp_model_unreadable") from exc
    actual = digest.hexdigest()
    if actual != expected_hash:
        raise LlamaCppServerError("llama_cpp_model_hash_mismatch")
    return actual


def _observe_nvidia_gpu() -> dict[str, Any]:
    try:
        free_result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        process_result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
        free_values = [int(line.strip()) for line in free_result.stdout.splitlines() if line.strip()]
        processes = []
        for line in process_result.stdout.splitlines():
            parts = [part.strip() for part in line.rsplit(",", 2)]
            if len(parts) != 3:
                raise ValueError("invalid_nvidia_process_row")
            processes.append(
                {
                    "pid": int(parts[0]),
                    "process_name": parts[1],
                    "used_gpu_memory_mib": int(parts[2]),
                }
            )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise LlamaCppServerUnavailable("gpu_resource_state_unavailable") from exc
    if not free_values:
        raise LlamaCppServerUnavailable("gpu_resource_state_unavailable")
    return {"free_mib": min(free_values), "processes": processes}


def classify_gpu_resource_state(
    *,
    ollama_server_running: bool,
    ollama_models: list[dict[str, Any]],
    gpu_free_mib: int | None,
    gpu_processes: list[dict[str, Any]],
    llama_cpp_running: bool,
) -> dict[str, Any]:
    ambiguous = gpu_free_mib is None or gpu_free_mib < 0
    loaded_models: list[str] = []
    gpu_models: list[str] = []
    ollama_vram_bytes = 0
    for item in ollama_models:
        if not isinstance(item, dict):
            ambiguous = True
            continue
        name = str(item.get("name") or item.get("model") or "unknown")
        loaded_models.append(name)
        size_vram = item.get("size_vram")
        if isinstance(size_vram, bool) or not isinstance(size_vram, (int, float)) or size_vram < 0:
            ambiguous = True
            continue
        size_vram_int = int(size_vram)
        ollama_vram_bytes += size_vram_int
        if size_vram_int > 0:
            gpu_models.append(name)
    if loaded_models and not ollama_server_running:
        ambiguous = True

    ollama_runner_pids: list[int] = []
    llama_cpp_pids: list[int] = []
    foreign_gpu_pids: list[int] = []
    for item in gpu_processes:
        try:
            pid = int(item["pid"])
            process_name = str(item["process_name"]).lower()
            used_mib = int(item["used_gpu_memory_mib"])
        except (KeyError, TypeError, ValueError):
            ambiguous = True
            continue
        if used_mib < 0:
            ambiguous = True
            continue
        if "llama-server" in process_name or "llama_cpp" in process_name:
            llama_cpp_pids.append(pid)
        elif "ollama" in process_name:
            ollama_runner_pids.append(pid)
        else:
            foreign_gpu_pids.append(pid)

    llama_running = bool(llama_cpp_running or llama_cpp_pids)
    gpu_available = bool(gpu_free_mib is not None and gpu_free_mib > 0)
    if ambiguous:
        reason = "gpu_resource_state_ambiguous"
    elif llama_running:
        reason = "llama_cpp_already_running"
    elif ollama_vram_bytes > 0:
        reason = "ollama_model_already_loaded"
    elif not gpu_available:
        reason = "gpu_unavailable"
    else:
        reason = "gpu_available"
    return {
        "ollama_server_running": bool(ollama_server_running),
        "ollama_model_loaded": bool(loaded_models),
        "ollama_models": loaded_models,
        "ollama_gpu_models": gpu_models,
        "ollama_gpu_memory_in_use": ollama_vram_bytes > 0,
        "ollama_gpu_memory_bytes": ollama_vram_bytes,
        "ollama_runner_pids": sorted(ollama_runner_pids),
        "foreign_gpu_process_present": bool(foreign_gpu_pids),
        "foreign_gpu_pids": sorted(foreign_gpu_pids),
        "llama_cpp_running": llama_running,
        "llama_cpp_pids": sorted(llama_cpp_pids),
        "gpu_available": gpu_available,
        "gpu_free_mib": gpu_free_mib,
        "ambiguous": ambiguous,
        "gate_reason": reason,
    }


class LlamaCppServerManager:
    def __init__(
        self,
        config: LlamaCppServerConfig | None = None,
        *,
        session: requests.Session | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        identity_reader: Callable[[int], ProcessIdentity | None] = read_process_identity,
        kill_fn: Callable[[int, int], None] = os.kill,
        waitpid_fn: Callable[[int, int], tuple[int, int]] = os.waitpid,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        arbiter: InferenceGpuArbiter | None = None,
        gpu_observer: Callable[[], dict[str, Any]] = _observe_nvidia_gpu,
    ) -> None:
        self.config = config or LlamaCppServerConfig.from_env()
        self.session = session or requests.Session()
        self.popen_factory = popen_factory
        self.identity_reader = identity_reader
        self.kill_fn = kill_fn
        self.waitpid_fn = waitpid_fn
        self.sleep_fn = sleep_fn
        self.monotonic = monotonic
        self.arbiter = arbiter or InferenceGpuArbiter(self.config.gpu_lock_path)
        self.gpu_observer = gpu_observer
        self.lifecycle_key = str(self.config.pid_path.resolve())

    def _signal_local(self, process: Any, pid: int, *, kill: bool) -> None:
        method = getattr(process, "kill" if kill else "terminate", None)
        try:
            if callable(method):
                method()
            else:
                self.kill_fn(pid, signal.SIGKILL if kill else signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _reap_local_child(
        self,
        pid: int,
        *,
        timeout_sec: float,
        terminate: bool,
        final_state: LlamaCppProcessState,
    ) -> int | None:
        record = _local_child_record(pid)
        if record is None:
            return None
        if record.owner_pid != os.getpid():
            raise LlamaCppServerOwnershipError("llama_cpp_child_owner_mismatch")
        with record.lock:
            process = record.process
            record.state = LlamaCppProcessState.STOPPING
            _set_lifecycle(
                self.lifecycle_key,
                LlamaCppProcessState.STOPPING,
                pid=pid,
                owner_pid=record.owner_pid,
                started_at=record.started_at,
            )
            poll = getattr(process, "poll", None)
            already_exited = callable(poll) and poll() is not None
            if terminate and not already_exited:
                self._signal_local(process, pid, kill=False)
            try:
                returncode = process.wait(timeout=timeout_sec)
            except subprocess.TimeoutExpired:
                self._signal_local(process, pid, kill=True)
                try:
                    returncode = process.wait(timeout=5.0)
                except subprocess.TimeoutExpired as exc:
                    record.state = LlamaCppProcessState.FAILED
                    _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.FAILED, pid=pid)
                    raise LlamaCppServerError("llama_cpp_child_would_not_stop") from exc
            except ProcessLookupError:
                try:
                    returncode = process.wait(timeout=5.0)
                except (ProcessLookupError, subprocess.TimeoutExpired) as exc:
                    record.state = LlamaCppProcessState.FAILED
                    _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.FAILED, pid=pid)
                    raise LlamaCppServerError("llama_cpp_child_not_reaped") from exc
            except ChildProcessError:
                returncode = getattr(process, "returncode", None)
            record.returncode = int(returncode) if returncode is not None else None
            record.waited = True
            record.state = final_state
            _set_lifecycle(
                self.lifecycle_key,
                final_state,
                pid=pid,
                owner_pid=record.owner_pid,
                started_at=record.started_at,
                returncode=record.returncode,
            )
            _forget_local_child(pid)
            return record.returncode

    def _waitpid_owned_child(self, pid: int, *, timeout_sec: float, terminate: bool) -> int | None:
        _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.STOPPING, pid=pid, owner_pid=os.getpid())
        if terminate:
            try:
                self.kill_fn(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = self.monotonic() + timeout_sec
        while True:
            try:
                waited_pid, status = self.waitpid_fn(pid, os.WNOHANG)
            except ChildProcessError:
                returncode = None
                break
            if waited_pid == pid:
                returncode = os.waitstatus_to_exitcode(status)
                break
            if self.monotonic() >= deadline:
                try:
                    self.kill_fn(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                kill_deadline = self.monotonic() + 5.0
                while True:
                    try:
                        waited_pid, status = self.waitpid_fn(pid, os.WNOHANG)
                    except ChildProcessError:
                        returncode = None
                        break
                    if waited_pid == pid:
                        returncode = os.waitstatus_to_exitcode(status)
                        break
                    if self.monotonic() >= kill_deadline:
                        _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.FAILED, pid=pid)
                        raise LlamaCppServerError("llama_cpp_child_would_not_stop")
                    self.sleep_fn(0.05)
                break
            self.sleep_fn(0.05)
        _set_lifecycle(
            self.lifecycle_key,
            LlamaCppProcessState.STOPPED,
            pid=pid,
            owner_pid=os.getpid(),
            returncode=returncode,
        )
        return returncode

    def command(self) -> list[str]:
        cfg = self.config
        command = [
            str(cfg.server_bin),
            "--model",
            str(cfg.model_path),
            "--alias",
            cfg.model,
            "--host",
            "127.0.0.1",
            "--port",
            str(cfg.port),
            "--ctx-size",
            str(cfg.context),
            "--n-gpu-layers",
            str(cfg.gpu_layers),
            "--threads",
            str(cfg.threads),
            "--threads-batch",
            str(cfg.threads),
            "--parallel",
            str(cfg.slots),
            "--cache-ram",
            str(cfg.cache_ram_mib),
            "--cache-idle-slots",
            "--metrics",
            "--timeout",
            str(int(cfg.request_timeout_sec)),
            "--offline",
            "--log-disable",
        ]
        if cfg.cache_prompt:
            command.append("--cache-prompt")
        return command

    def health(self) -> bool:
        response = None
        models_response = None
        try:
            response = self.session.get(f"{self.config.base_url}/health", timeout=(1.0, 2.0))
            if response.status_code != 200 or response.json() != {"status": "ok"}:
                return False
            models_response = self.session.get(f"{self.config.base_url}/v1/models", timeout=(1.0, 2.0))
            if models_response.status_code != 200:
                return False
            payload = models_response.json()
            models = payload.get("data") if isinstance(payload, dict) else None
            return isinstance(models, list) and any(
                isinstance(item, dict) and item.get("id") == self.config.model for item in models
            )
        except (requests.RequestException, TypeError, ValueError):
            return False
        finally:
            if response is not None:
                response.close()
            if models_response is not None:
                models_response.close()

    def ensure_available(self) -> dict[str, Any]:
        if self.health():
            if not self._managed_running():
                raise LlamaCppServerOwnershipError("llama_cpp_unmanaged_process_on_port")
            return {"server_started": False, "server_reused": True, "server_startup_ms": None}
        if not self.config.autostart:
            raise LlamaCppServerUnavailable("llama_cpp_server_unavailable_autostart_disabled")
        return self.start()

    def _ollama_ps_models(self) -> list[dict[str, Any]]:
        response = None
        try:
            response = self.session.get(f"{self.config.ollama_base_url}/api/ps", timeout=(1.0, 3.0))
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, TypeError, ValueError) as exc:
            raise LlamaCppServerUnavailable("ollama_gpu_state_unavailable") from exc
        finally:
            if response is not None:
                response.close()
        models = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(models, list):
            raise LlamaCppServerUnavailable("ollama_gpu_state_invalid")
        if any(not isinstance(item, dict) for item in models):
            raise LlamaCppServerUnavailable("ollama_gpu_state_invalid")
        return models

    def ollama_gpu_models(self) -> list[str]:
        return [
            str(item.get("name") or item.get("model") or "unknown")
            for item in self._ollama_ps_models()
        ]

    def resource_gate_status(self, *, llama_cpp_running: bool = False) -> dict[str, Any]:
        models = self._ollama_ps_models()
        observed = self.gpu_observer()
        if not isinstance(observed, dict):
            raise LlamaCppServerUnavailable("gpu_resource_state_unavailable")
        processes = observed.get("processes")
        if not isinstance(processes, list):
            raise LlamaCppServerUnavailable("gpu_resource_state_unavailable")
        free_mib = observed.get("free_mib")
        if isinstance(free_mib, bool) or not isinstance(free_mib, int):
            free_mib = None
        return classify_gpu_resource_state(
            ollama_server_running=True,
            ollama_models=models,
            gpu_free_mib=free_mib,
            gpu_processes=processes,
            llama_cpp_running=llama_cpp_running,
        )

    def start(self, *, dry_run: bool = False) -> dict[str, Any]:
        command = self.command()
        if dry_run:
            return {
                "provider": "llama_cpp",
                "model": self.config.model,
                "endpoint": self.config.base_url,
                "dry_run": True,
                "command": command,
            }
        if self.health():
            if not self._managed_running():
                raise LlamaCppServerOwnershipError("llama_cpp_unmanaged_process_on_port")
            return {
                "provider": "llama_cpp",
                "status": "already_healthy",
                "managed": True,
                "server_started": False,
                "server_reused": True,
            }
        self._ensure_state_dirs()
        with self._exclusive_lock(self.config.engine_lock_path):
            if self.health():
                if not self._managed_running():
                    raise LlamaCppServerOwnershipError("llama_cpp_unmanaged_process_on_port")
                return {
                    "provider": "llama_cpp",
                    "status": "already_healthy",
                    "managed": True,
                    "server_started": False,
                    "server_reused": True,
                }
            if self._managed_running():
                raise LlamaCppServerUnavailable("llama_cpp_managed_process_unhealthy")
            if self._port_in_use():
                raise LlamaCppServerOwnershipError("llama_cpp_unmanaged_process_on_port")
            lock = self.arbiter.status(clean_stale=True)
            if lock.held:
                code = "agent_task_active" if lock.metadata.get("mode") == "agent" else "llama_cpp_gpu_lock_busy"
                raise LlamaCppServerBusy(code)
            if not self.config.server_bin.is_file() or not os.access(self.config.server_bin, os.X_OK):
                raise LlamaCppServerUnavailable("llama_cpp_server_binary_unavailable")
            verify_model_hash(self.config.model_path, self.config.model_hash)
            resource_gate = self.resource_gate_status(llama_cpp_running=False)
            gate_reason = str(resource_gate["gate_reason"])
            if gate_reason != "gpu_available":
                raise LlamaCppServerBusy(gate_reason)
            gpu_fd = self._acquire_gpu_lock()
            started = self.monotonic()
            process = None
            _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.STARTING)
            try:
                process = self.popen_factory(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    pass_fds=(gpu_fd,),
                    start_new_session=True,
                )
                _remember_local_child(process, lifecycle_key=self.lifecycle_key)
                identity = self._wait_identity(process.pid, timeout_sec=2.0)
                if identity is None:
                    raise LlamaCppServerUnavailable("llama_cpp_process_identity_unavailable")
                local_record = _local_child_record(process.pid)
                assert local_record is not None
                metadata = {
                    "pid": process.pid,
                    "owner_pid": os.getpid(),
                    "provider": "llama_cpp",
                    "mode": "chat",
                    "timestamp": int(time.time()),
                    "started_at": local_record.started_at,
                    "model_hash": self.config.model_hash,
                    "process_start_ticks": identity.start_ticks,
                }
                self.arbiter.write_metadata(gpu_fd, metadata)
                self._atomic_json(
                    self.config.pid_path,
                    {
                        **metadata,
                        "owner_uid": os.getuid(),
                        "start_ticks": identity.start_ticks,
                        "server_bin": str(self.config.server_bin.resolve()),
                        "model_path": str(self.config.model_path),
                        "port": self.config.port,
                    },
                )
                while self.monotonic() - started < self.config.startup_timeout_sec:
                    if process.poll() is not None:
                        raise LlamaCppServerUnavailable("llama_cpp_server_exited_during_startup")
                    if self.health():
                        startup_ms = (self.monotonic() - started) * 1000
                        local_record.state = LlamaCppProcessState.RUNNING
                        _set_lifecycle(
                            self.lifecycle_key,
                            LlamaCppProcessState.RUNNING,
                            pid=process.pid,
                            owner_pid=local_record.owner_pid,
                            started_at=local_record.started_at,
                        )
                        self._append_log("started", pid=process.pid, startup_ms=round(startup_ms, 3))
                        return {
                            "provider": "llama_cpp",
                            "status": "started",
                            "managed": True,
                            "pid": process.pid,
                            "server_started": True,
                            "server_reused": False,
                            "server_startup_ms": startup_ms,
                        }
                    self.sleep_fn(0.2)
                raise LlamaCppServerUnavailable("llama_cpp_startup_timeout")
            except BaseException:
                if process is not None:
                    self._reap_local_child(
                        process.pid,
                        timeout_sec=5.0,
                        terminate=True,
                        final_state=LlamaCppProcessState.FAILED,
                    )
                else:
                    _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.FAILED)
                self.config.pid_path.unlink(missing_ok=True)
                self.arbiter.release_fd(gpu_fd)
                gpu_fd = -1
                raise
            finally:
                if gpu_fd >= 0:
                    os.close(gpu_fd)

    def stop(self, *, timeout_sec: float = 15.0) -> dict[str, Any]:
        self._ensure_state_dirs()
        with self._exclusive_lock(self.config.engine_lock_path):
            record = self._read_json(self.config.pid_path)
            if record is None:
                orphan = _local_child_for_lifecycle(self.lifecycle_key)
                if orphan is not None:
                    returncode = self._reap_local_child(
                        int(orphan.process.pid),
                        timeout_sec=timeout_sec,
                        terminate=True,
                        final_state=LlamaCppProcessState.STOPPED,
                    )
                    return {
                        "provider": "llama_cpp",
                        "status": "stopped",
                        "changed": True,
                        "pid": int(orphan.process.pid),
                        "returncode": returncode,
                    }
                return {"provider": "llama_cpp", "status": "stopped", "changed": False}
            try:
                pid = int(record["pid"])
                owner_pid = int(record["owner_pid"]) if record.get("owner_pid") is not None else None
            except (KeyError, TypeError, ValueError) as exc:
                raise LlamaCppServerOwnershipError("llama_cpp_pid_record_invalid") from exc
            identity = self._validated_identity(record)
            local_record = _local_child_record(pid)
            if identity is None:
                returncode = None
                if local_record is not None:
                    returncode = self._reap_local_child(
                        pid,
                        timeout_sec=timeout_sec,
                        terminate=False,
                        final_state=LlamaCppProcessState.STOPPED,
                    )
                elif owner_pid == os.getpid():
                    returncode = self._waitpid_owned_child(pid, timeout_sec=timeout_sec, terminate=False)
                self.config.pid_path.unlink(missing_ok=True)
                self._cleanup_gpu_lock(pid)
                return {
                    "provider": "llama_cpp",
                    "status": "stopped",
                    "changed": local_record is not None or owner_pid == os.getpid(),
                    "pid": pid,
                    "returncode": returncode,
                }
            returncode = None
            if local_record is not None:
                returncode = self._reap_local_child(
                    pid,
                    timeout_sec=timeout_sec,
                    terminate=True,
                    final_state=LlamaCppProcessState.STOPPED,
                )
            elif owner_pid == os.getpid():
                returncode = self._waitpid_owned_child(pid, timeout_sec=timeout_sec, terminate=True)
            elif owner_pid is not None:
                raise LlamaCppServerOwnershipError("llama_cpp_stop_requires_owner_process")
            else:
                try:
                    self.kill_fn(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                deadline = self.monotonic() + timeout_sec
                while self.monotonic() < deadline and self.identity_reader(pid) is not None:
                    self.sleep_fn(0.1)
                if self.identity_reader(pid) is not None:
                    try:
                        self.kill_fn(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    deadline = self.monotonic() + 5.0
                    while self.monotonic() < deadline and self.identity_reader(pid) is not None:
                        self.sleep_fn(0.1)
                if self.identity_reader(pid) is not None:
                    _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.FAILED, pid=pid)
                    raise LlamaCppServerError("llama_cpp_child_would_not_stop")
                _set_lifecycle(self.lifecycle_key, LlamaCppProcessState.STOPPED, pid=pid)
            self.config.pid_path.unlink(missing_ok=True)
            self._cleanup_gpu_lock(pid)
            self._append_log("stopped", pid=pid, returncode=returncode)
            return {
                "provider": "llama_cpp",
                "status": "stopped",
                "changed": True,
                "pid": pid,
                "returncode": returncode,
            }

    def status(self) -> dict[str, Any]:
        record = self._read_json(self.config.pid_path)
        managed = False
        pid = None
        if record is not None:
            pid = record.get("pid")
            if not self._reap_exited_local(record):
                try:
                    managed = self._validated_identity(record) is not None
                except LlamaCppServerOwnershipError:
                    managed = False
        lifecycle = _lifecycle_status(self.lifecycle_key)
        return {
            "provider": "llama_cpp",
            "model": self.config.model,
            "endpoint": self.config.base_url,
            "healthy": self.health(),
            "managed": managed,
            "pid": pid if managed else None,
            "autostart": self.config.autostart,
            "gpu_layers": self.config.gpu_layers,
            "slots": self.config.slots,
            "prompt_cache": self.config.cache_prompt,
            "cache_ram_mib": self.config.cache_ram_mib,
            "ngram": self.config.ngram,
            "gpu_lock_held": self.arbiter.status(clean_stale=True).held,
            "lifecycle_state": lifecycle.state.value,
            "lifecycle_owner_pid": lifecycle.owner_pid,
            "lifecycle_started_at": lifecycle.started_at,
            "last_returncode": lifecycle.returncode,
        }

    def _ensure_state_dirs(self) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.state_dir, 0o700)
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.config.log_path.parent, 0o700)

    @contextmanager
    def _exclusive_lock(self, path: Path) -> Iterator[int]:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LlamaCppServerBusy("llama_cpp_engine_operation_busy") from exc
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _acquire_gpu_lock(self) -> int:
        try:
            return self.arbiter.acquire_fd(
                provider="llama_cpp",
                mode="chat",
                model_hash=self.config.model_hash,
            )
        except GpuArbiterBusy as exc:
            raise LlamaCppServerBusy() from exc

    def _wait_identity(self, pid: int, *, timeout_sec: float) -> ProcessIdentity | None:
        deadline = self.monotonic() + timeout_sec
        while self.monotonic() < deadline:
            identity = self.identity_reader(pid)
            if identity is not None:
                return identity
            self.sleep_fn(0.01)
        return None

    def _managed_running(self) -> bool:
        record = self._read_json(self.config.pid_path)
        if record is None:
            return False
        if self._reap_exited_local(record):
            return False
        try:
            return self._validated_identity(record) is not None
        except LlamaCppServerOwnershipError:
            return False

    def _reap_exited_local(self, record: dict[str, Any]) -> bool:
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        local_record = _local_child_record(pid)
        if local_record is None:
            return False
        poll = getattr(local_record.process, "poll", None)
        if not callable(poll) or poll() is None:
            return False
        returncode = self._reap_local_child(
            pid,
            timeout_sec=0.0,
            terminate=False,
            final_state=LlamaCppProcessState.STOPPED,
        )
        self.config.pid_path.unlink(missing_ok=True)
        self._cleanup_gpu_lock(pid)
        self._append_log("reaped", pid=pid, returncode=returncode)
        return True

    def _validated_identity(self, record: dict[str, Any]) -> ProcessIdentity | None:
        try:
            pid = int(record["pid"])
            owner_uid = int(record["owner_uid"])
            start_ticks = int(record["start_ticks"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LlamaCppServerOwnershipError("llama_cpp_pid_record_invalid") from exc
        identity = self.identity_reader(pid)
        if identity is None:
            return None
        required = {
            "--alias": self.config.model,
            "--port": str(self.config.port),
            "--model": str(self.config.model_path),
        }
        argv = list(identity.argv)
        valid_args = all(key in argv and argv.index(key) + 1 < len(argv) and argv[argv.index(key) + 1] == value for key, value in required.items())
        if (
            owner_uid != os.getuid()
            or identity.uid != os.getuid()
            or identity.start_ticks != start_ticks
            or not valid_args
            or Path(identity.executable).resolve() != self.config.server_bin.resolve()
        ):
            raise LlamaCppServerOwnershipError()
        return identity

    def _atomic_json(self, path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_fd_json(fd: int, value: dict[str, Any]) -> None:
        payload = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _cleanup_gpu_lock(self, pid: int) -> None:
        self.arbiter.cleanup_owned(pid)

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex(("127.0.0.1", self.config.port)) == 0

    @staticmethod
    def _lock_is_held(path: Path) -> bool:
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def _append_log(self, event: str, **fields: Any) -> None:
        self._ensure_state_dirs()
        fd = os.open(self.config.log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            payload = {"event": event, "timestamp": int(time.time()), **fields}
            os.write(fd, (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        finally:
            os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ralf-llama-cpp-engine")
    parser.add_argument("action", choices=("start", "stop", "status", "health"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    manager = LlamaCppServerManager()
    try:
        if args.action == "start":
            result = manager.start(dry_run=args.dry_run)
        elif args.action == "stop":
            result = manager.stop()
        elif args.action == "health":
            result = manager.status()
            print(json.dumps(result, sort_keys=True))
            return 0 if result.get("healthy") is True else 1
        else:
            result = manager.status()
    except LlamaCppServerError as exc:
        print(json.dumps({"ok": False, "error": exc.code}, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
