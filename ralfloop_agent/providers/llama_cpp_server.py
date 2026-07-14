from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
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
_LOCAL_CHILDREN: dict[int, Any] = {}
_LOCAL_CHILDREN_LOCK = threading.Lock()


def _remember_local_child(process: Any) -> None:
    with _LOCAL_CHILDREN_LOCK:
        _LOCAL_CHILDREN[int(process.pid)] = process


def _local_child(pid: int) -> Any | None:
    with _LOCAL_CHILDREN_LOCK:
        return _LOCAL_CHILDREN.get(pid)


def _forget_local_child(pid: int) -> None:
    with _LOCAL_CHILDREN_LOCK:
        _LOCAL_CHILDREN.pop(pid, None)


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
    fallback: str = "ollama"
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
            fallback=os.getenv("RALF_LLAMA_CPP_FALLBACK", "ollama").strip().lower() or "ollama",
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


class LlamaCppServerManager:
    def __init__(
        self,
        config: LlamaCppServerConfig | None = None,
        *,
        session: requests.Session | None = None,
        popen_factory: Callable[..., Any] = subprocess.Popen,
        identity_reader: Callable[[int], ProcessIdentity | None] = read_process_identity,
        kill_fn: Callable[[int, int], None] = os.kill,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        arbiter: InferenceGpuArbiter | None = None,
    ) -> None:
        self.config = config or LlamaCppServerConfig.from_env()
        self.session = session or requests.Session()
        self.popen_factory = popen_factory
        self.identity_reader = identity_reader
        self.kill_fn = kill_fn
        self.sleep_fn = sleep_fn
        self.monotonic = monotonic
        self.arbiter = arbiter or InferenceGpuArbiter(self.config.gpu_lock_path)

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

    def ollama_gpu_models(self) -> list[str]:
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
        return [
            str(item.get("name") or item.get("model") or "unknown")
            for item in models
            if isinstance(item, dict)
        ]

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
            gpu_models = self.ollama_gpu_models()
            if gpu_models:
                raise LlamaCppServerBusy("ollama_model_already_loaded")
            gpu_fd = self._acquire_gpu_lock()
            started = self.monotonic()
            process = None
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
                _remember_local_child(process)
                identity = self._wait_identity(process.pid, timeout_sec=2.0)
                if identity is None:
                    raise LlamaCppServerUnavailable("llama_cpp_process_identity_unavailable")
                metadata = {
                    "pid": process.pid,
                    "provider": "llama_cpp",
                    "mode": "chat",
                    "timestamp": int(time.time()),
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
            except Exception:
                if process is not None and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                if process is not None:
                    _forget_local_child(process.pid)
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
                return {"provider": "llama_cpp", "status": "stopped", "changed": False}
            identity = self._validated_identity(record)
            if identity is None:
                self.config.pid_path.unlink(missing_ok=True)
                self._cleanup_gpu_lock(int(record.get("pid") or 0))
                return {"provider": "llama_cpp", "status": "stopped", "changed": False}
            pid = identity.pid
            self.kill_fn(pid, signal.SIGTERM)
            local_process = _local_child(pid)
            if local_process is not None:
                try:
                    local_process.wait(timeout=timeout_sec)
                except subprocess.TimeoutExpired:
                    self.kill_fn(pid, signal.SIGKILL)
                    try:
                        local_process.wait(timeout=5.0)
                    except subprocess.TimeoutExpired as exc:
                        raise LlamaCppServerError("llama_cpp_child_would_not_stop") from exc
                finally:
                    _forget_local_child(pid)
            else:
                deadline = self.monotonic() + timeout_sec
                while self.monotonic() < deadline and self.identity_reader(pid) is not None:
                    self.sleep_fn(0.1)
                if self.identity_reader(pid) is not None:
                    self.kill_fn(pid, signal.SIGKILL)
                    deadline = self.monotonic() + 5.0
                    while self.monotonic() < deadline and self.identity_reader(pid) is not None:
                        self.sleep_fn(0.1)
                if self.identity_reader(pid) is not None:
                    raise LlamaCppServerError("llama_cpp_child_would_not_stop")
            self.config.pid_path.unlink(missing_ok=True)
            self._cleanup_gpu_lock(pid)
            self._append_log("stopped", pid=pid)
            return {"provider": "llama_cpp", "status": "stopped", "changed": True, "pid": pid}

    def status(self) -> dict[str, Any]:
        record = self._read_json(self.config.pid_path)
        managed = False
        pid = None
        if record is not None:
            pid = record.get("pid")
            try:
                managed = self._validated_identity(record) is not None
            except LlamaCppServerOwnershipError:
                managed = False
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
        try:
            return self._validated_identity(record) is not None
        except LlamaCppServerOwnershipError:
            return False

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
