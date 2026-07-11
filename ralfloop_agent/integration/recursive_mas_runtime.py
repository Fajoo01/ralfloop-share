from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid
from typing import Any


DEFAULT_ROOT = Path("/home/sibilla-cumana/RecursiveMAS")
DEFAULT_PYTHON = DEFAULT_ROOT / ".venv-recursivemas" / "bin" / "python"
DEFAULT_CACHE = DEFAULT_ROOT / ".hf-recursivemas"
WORKER_PATH = Path(__file__).with_name("recursive_mas_worker.py")
TOKEN_ENV_NAMES = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN")
SECRET_MARKERS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "Authorization", "Bearer")


@dataclass
class RecursiveMASRuntimeConfig:
    enabled: bool = False
    allow_text_fallback: bool = False
    execution_mode: str = "gpu_stagewise"
    python_path: str = str(DEFAULT_PYTHON)
    upstream_root: str = str(DEFAULT_ROOT)
    cache_path: str = str(DEFAULT_CACHE)
    style: str = "sequential_light"
    rounds: int = 3
    timeout_sec: int = 120
    lock_path: str = ""
    circuit_path: str = "./runtime/recursive_mas_circuit.json"
    max_failures: int = 3
    cooldown_sec: int = 900

    @classmethod
    def from_env(cls) -> "RecursiveMASRuntimeConfig":
        uid = os.getuid()
        default_lock = f"/run/user/{uid}/ralfloop-recursivemas.lock"
        if not Path(f"/run/user/{uid}").is_dir():
            default_lock = f"/tmp/ralfloop-{uid}-recursivemas.lock"
        lock = os.getenv("RALFLOOP_RECURSIVE_MAS_LOCK_PATH") or default_lock
        lock = lock.replace("%UID%", str(uid))
        return cls(
            enabled=os.getenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "0") == "1",
            allow_text_fallback=os.getenv("RALFLOOP_ALLOW_TEXT_MAS_FALLBACK", "0") == "1",
            execution_mode=os.getenv("RALFLOOP_RECURSIVE_MAS_EXECUTION_MODE", "gpu_stagewise"),
            python_path=os.getenv("RALFLOOP_RECURSIVE_MAS_PYTHON", str(DEFAULT_PYTHON)),
            upstream_root=os.getenv("RALFLOOP_RECURSIVE_MAS_ROOT", str(DEFAULT_ROOT)),
            cache_path=os.getenv("RALFLOOP_RECURSIVE_MAS_CACHE", str(DEFAULT_CACHE)),
            style=os.getenv("RALFLOOP_RECURSIVE_MAS_STYLE", "sequential_light"),
            rounds=int(os.getenv("RALFLOOP_RECURSIVE_MAS_ROUNDS", "3")),
            timeout_sec=int(os.getenv("RALFLOOP_RECURSIVE_MAS_TIMEOUT_SEC", "120")),
            lock_path=lock,
            circuit_path=os.getenv("RALFLOOP_RECURSIVE_MAS_CIRCUIT_PATH", "./runtime/recursive_mas_circuit.json"),
            max_failures=int(os.getenv("RALFLOOP_RECURSIVE_MAS_MAX_FAILURES", "3")),
            cooldown_sec=int(os.getenv("RALFLOOP_RECURSIVE_MAS_COOLDOWN_SEC", "900")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecursiveMASRuntimeStatus:
    enabled: bool
    busy: bool
    lock_owner_pid: int | None
    lock_started_at: str | None
    circuit_state: str
    consecutive_failures: int
    cooldown_until: float | None
    backend_ready: bool
    backend_reason: str
    last_execution: dict[str, Any] | None
    last_error: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecursiveMASRuntimeResult:
    ok: bool
    status: str
    task_id: str
    requested_backend: str = "recursive_mas_native"
    selected_backend: str = "recursive_mas_native"
    implementation_level: str = "native_latent"
    execution_mode: str = "gpu_stagewise"
    native_latent_verified: bool = False
    closed_loop_verified: bool = False
    answer: str | None = None
    duration_ms: int = 0
    fallback_used: bool = False
    verification: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None
    audit_id: str | None = None
    retryable: bool = False
    message: str | None = None
    error_type: str | None = None
    error: str | None = None
    cleanup_requested: bool = False
    cleanup_completed: bool = False
    worker_exit_code: int | None = None
    worker_pid: int | None = None
    orphan_detected: bool = False
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["backend"] = self.requested_backend
        return data


class RecursiveMASRuntimeController:
    def __init__(self, config: RecursiveMASRuntimeConfig | None = None) -> None:
        self.config = config or RecursiveMASRuntimeConfig.from_env()
        self._lock_fd: int | None = None
        self._lock_metadata: dict[str, Any] | None = None

    @classmethod
    def from_env(cls) -> "RecursiveMASRuntimeController":
        return cls(RecursiveMASRuntimeConfig.from_env())

    def status(self) -> RecursiveMASRuntimeStatus:
        circuit = self._read_circuit()
        busy, meta = self._lock_status()
        ready, reason = self._backend_ready()
        last_execution = circuit.get("last_execution") if isinstance(circuit.get("last_execution"), dict) else None
        return RecursiveMASRuntimeStatus(
            enabled=self.config.enabled,
            busy=busy,
            lock_owner_pid=_int_or_none(meta.get("pid")) if meta else None,
            lock_started_at=str(meta.get("started_at")) if meta and meta.get("started_at") else None,
            circuit_state=str(circuit.get("state", "closed")),
            consecutive_failures=int(circuit.get("consecutive_failures") or 0),
            cooldown_until=_float_or_none(circuit.get("cooldown_until")),
            backend_ready=ready,
            backend_reason=reason,
            last_execution=last_execution,
            last_error=circuit.get("last_error"),
        )

    def health(self) -> dict[str, Any]:
        status = self.status().to_dict()
        status["ok"] = bool(status["enabled"] and status["backend_ready"] and status["circuit_state"] != "open")
        return status

    def acquire(self, task_id: str | None = None) -> dict[str, Any]:
        if self._lock_fd is not None:
            return {"ok": False, "status": "busy", "lock": self._lock_metadata or {}}
        path = Path(self.config.lock_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            meta = _read_json_from_fd(fd)
            os.close(fd)
            return {"ok": False, "status": "busy", "lock": meta}
        meta = {"pid": os.getpid(), "started_at": _now_iso(), "task_id": task_id or "", "hostname": socket.gethostname()}
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(meta, sort_keys=True).encode("utf-8"))
        os.fsync(fd)
        self._lock_fd = fd
        self._lock_metadata = meta
        return {"ok": True, "status": "acquired", "lock": meta}

    def release(self) -> None:
        fd = self._lock_fd
        self._lock_fd = None
        self._lock_metadata = None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def reset_circuit(self) -> dict[str, Any]:
        state = _default_circuit()
        self._write_circuit(state)
        return state

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        task_id = str(request.get("task_id") or uuid.uuid4())
        audit_id = str(uuid.uuid4())
        goal = str(request.get("goal") or request.get("question") or "").strip()
        if not goal:
            return self._final_result(False, "invalid_request", task_id, started, audit_id, error_type="invalid_request", error="goal is required")
        if _requires_human_confirmation(request):
            return self._final_result(False, "human_confirmation_required", task_id, started, audit_id, message="Human confirmation is required before side effects")
        if not self.config.enabled:
            return self._final_result(False, "disabled", task_id, started, audit_id, error_type="backend_disabled", error="RecursiveMAS native backend is disabled")
        circuit_before = self._read_circuit()
        gate = self._circuit_gate(circuit_before)
        if not gate["ok"]:
            return self._blocked_by_circuit(task_id, started, audit_id, circuit_before, gate)
        ready, reason = self._backend_ready()
        if not ready:
            return self._native_unavailable(task_id, started, audit_id, "backend_unavailable", reason, circuit_before)
        lock = self.acquire(task_id)
        if not lock.get("ok"):
            result = {
                "ok": False,
                "status": "busy",
                "backend": "recursive_mas_native",
                "requested_backend": "recursive_mas_native",
                "selected_backend": "recursive_mas_native",
                "retryable": True,
                "fallback_used": False,
                "message": "RecursiveMAS native backend is already running",
                "lock": _public_lock(lock.get("lock")),
            }
            self._audit(audit_id, task_id, request, result, circuit_before, circuit_before, lock_acquired=False, busy=True)
            return result
        try:
            worker = self._run_worker(goal, request, task_id)
            result = self._result_from_worker(worker, task_id, started, audit_id)
            circuit_after = self._record_success(result) if result.get("ok") else self._record_failure(result)
            self._audit(audit_id, task_id, request, result, circuit_before, circuit_after, lock_acquired=True, busy=False)
            return result
        except Exception as exc:
            result = self._final_result(False, "error", task_id, started, audit_id, retryable=True, error_type=type(exc).__name__, error=str(exc), cleanup_requested=True, cleanup_completed=False)
            circuit_after = self._record_failure(result)
            self._audit(audit_id, task_id, request, result, circuit_before, circuit_after, lock_acquired=True, busy=False)
            return result
        finally:
            self.release()

    def _run_worker(self, goal: str, request: dict[str, Any], task_id: str) -> dict[str, Any]:
        argv = [self.config.python_path, str(WORKER_PATH), "--gpu-stagewise-canary"]
        payload = {
            "upstream_root": self.config.upstream_root,
            "style": str(request.get("style") or self.config.style),
            "device": str(request.get("device") or os.getenv("RALFLOOP_RECURSIVE_MAS_DEVICE", "cuda:0")),
            "rounds": int(request.get("rounds") or self.config.rounds),
            "question": goal,
            "profile": str(request.get("profile") or "deterministic_diagnostic"),
            "task_id": task_id,
        }
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._worker_env(),
            shell=False,
        )
        out = err = ""
        killed = False
        timed_out = False
        try:
            out, err = proc.communicate(json.dumps(payload, ensure_ascii=False), timeout=self.config.timeout_sec)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                out, err = proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                killed = True
                proc.kill()
                out, err = proc.communicate()
        parsed = None
        parse_error = None
        if out:
            try:
                parsed = json.loads(out)
            except Exception as exc:
                parse_error = f"{type(exc).__name__}: {exc}"
        return {
            "pid": proc.pid,
            "exit_code": proc.returncode,
            "stdout": _limit(out),
            "stderr": _limit(err),
            "json": parsed,
            "parse_error": parse_error,
            "timed_out": timed_out,
            "killed": killed,
            "orphan_detected": proc.poll() is None,
        }

    def _result_from_worker(self, worker: dict[str, Any], task_id: str, started: float, audit_id: str) -> dict[str, Any]:
        payload = worker.get("json") if isinstance(worker.get("json"), dict) else {}
        cleanup_completed = bool(not worker.get("orphan_detected") and worker.get("exit_code") is not None)
        base = {
            "task_id": task_id,
            "audit_id": audit_id,
            "requested_backend": "recursive_mas_native",
            "selected_backend": "recursive_mas_native",
            "implementation_level": "native_latent",
            "execution_mode": self.config.execution_mode,
            "fallback_used": False,
            "duration_ms": _elapsed_ms(started),
            "worker_pid": worker.get("pid"),
            "worker_exit_code": worker.get("exit_code"),
            "cleanup_requested": True,
            "cleanup_completed": cleanup_completed,
            "orphan_detected": bool(worker.get("orphan_detected")),
        }
        if worker.get("timed_out"):
            return {**base, "ok": False, "status": "timeout", "retryable": True, "error_type": "timeout", "error": "RecursiveMAS worker timed out", "killed": bool(worker.get("killed"))}
        if worker.get("parse_error"):
            return {**base, "ok": False, "status": "error", "retryable": True, "error_type": "invalid_json", "error": worker.get("parse_error")}
        if worker.get("exit_code") != 0 or not payload.get("ok"):
            return {**base, "ok": False, "status": "error", "retryable": True, "native_latent_verified": False, "closed_loop_verified": False, "error_type": payload.get("error_type") or payload.get("error") or "worker_error", "error": payload.get("error") or worker.get("stderr")}
        return {
            **base,
            "ok": True,
            "status": "completed",
            "retryable": False,
            "native_latent_verified": bool(payload.get("native_latent_verified")),
            "closed_loop_verified": bool(payload.get("closed_loop_verified")),
            "answer": payload.get("raw_answer"),
            "verification": {
                "answer_correct": payload.get("answer_correct"),
                "format_compliant": payload.get("format_compliant"),
                "boxed_answer": payload.get("boxed_answer"),
                "normalized_answer": payload.get("normalized_answer"),
            },
            "memory": {
                "rss_peak_bytes": payload.get("rss_peak_bytes"),
                "vram_peak_bytes": (payload.get("stage_summary") or {}).get("vram_peak_bytes"),
            },
            "worker": _worker_public_summary(payload),
        }

    def _worker_env(self) -> dict[str, str]:
        keep = ("PATH", "HOME", "LANG", "LC_ALL", "PYTHONPATH")
        env = {name: os.environ[name] for name in keep if name in os.environ}
        env.update({
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "HF_HOME": self.config.cache_path,
            "HUGGINGFACE_HUB_CACHE": str(Path(self.config.cache_path) / "hub"),
            "HF_XET_CACHE": str(Path(self.config.cache_path) / "xet"),
            "RALFLOOP_RECURSIVE_MAS_ROOT": self.config.upstream_root,
            "RALFLOOP_RECURSIVE_MAS_CACHE": self.config.cache_path,
            "RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE": "1",
        })
        for name in TOKEN_ENV_NAMES:
            env.pop(name, None)
        return env

    def _backend_ready(self) -> tuple[bool, str]:
        if self.config.execution_mode != "gpu_stagewise":
            return False, "execution_mode_not_gpu_stagewise"
        if not Path(self.config.python_path).is_file():
            return False, "recursive_mas_python_missing"
        if not Path(self.config.upstream_root).is_dir():
            return False, "recursive_mas_root_missing"
        if not Path(self.config.cache_path).is_dir():
            return False, "recursive_mas_cache_missing"
        if self.config.style != "sequential_light":
            return False, "unsupported_style"
        return True, "ready"

    def _lock_status(self) -> tuple[bool, dict[str, Any]]:
        if self._lock_fd is not None:
            return True, self._lock_metadata or {}
        path = Path(self.config.lock_path)
        if not path.exists():
            return False, {}
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True, _read_json_from_fd(fd)
            meta = _read_json_from_fd(fd)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False, meta
        finally:
            os.close(fd)

    def _read_circuit(self) -> dict[str, Any]:
        path = Path(self.config.circuit_path)
        if not path.exists():
            return _default_circuit()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("circuit file must be object")
            return {**_default_circuit(), **data}
        except Exception:
            corrupt = path.with_suffix(path.suffix + f".corrupt-{int(time.time())}")
            try:
                path.replace(corrupt)
            except Exception:
                pass
            data = _default_circuit()
            data["last_error_type"] = "corrupt_circuit_file"
            data["last_error"] = str(corrupt)
            self._write_circuit(data)
            return data

    def _write_circuit(self, data: dict[str, Any]) -> None:
        path = Path(self.config.circuit_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _circuit_gate(self, circuit: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        state = str(circuit.get("state") or "closed")
        cooldown_until = _float_or_none(circuit.get("cooldown_until"))
        if state == "open" and cooldown_until and now < cooldown_until:
            return {"ok": False, "state": "open", "cooldown_until": cooldown_until}
        if state == "open" and cooldown_until and now >= cooldown_until:
            circuit["state"] = "half_open"
            self._write_circuit(circuit)
        return {"ok": True, "state": circuit.get("state", "closed")}

    def _record_success(self, result: dict[str, Any]) -> dict[str, Any]:
        data = _default_circuit()
        data["last_success_at"] = time.time()
        data["last_execution"] = {"status": result.get("status"), "duration_ms": result.get("duration_ms"), "task_id": result.get("task_id")}
        self._write_circuit(data)
        return data

    def _record_failure(self, result: dict[str, Any]) -> dict[str, Any]:
        data = self._read_circuit()
        failures = int(data.get("consecutive_failures") or 0) + 1
        data["consecutive_failures"] = failures
        data["last_error_type"] = result.get("error_type") or result.get("status")
        data["last_error"] = _limit(str(result.get("error") or result.get("message") or ""), 1000)
        if failures >= self.config.max_failures:
            data["state"] = "open"
            data["opened_at"] = time.time()
            data["cooldown_until"] = time.time() + self.config.cooldown_sec
        else:
            data["state"] = "closed"
        self._write_circuit(data)
        return data

    def _blocked_by_circuit(self, task_id: str, started: float, audit_id: str, before: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
        result = self._native_unavailable(task_id, started, audit_id, "circuit_open", "RecursiveMAS circuit breaker is open", before)
        result["cooldown_until"] = gate.get("cooldown_until")
        return result

    def _native_unavailable(self, task_id: str, started: float, audit_id: str, error_type: str, reason: str, circuit_before: dict[str, Any]) -> dict[str, Any]:
        if self.config.allow_text_fallback:
            result = self._text_fallback(task_id, started, audit_id, reason)
        else:
            result = self._final_result(False, error_type, task_id, started, audit_id, retryable=True, error_type=error_type, error=reason)
        self._audit(audit_id, task_id, {"goal": ""}, result, circuit_before, circuit_before, lock_acquired=False, busy=False)
        return result

    def _text_fallback(self, task_id: str, started: float, audit_id: str, reason: str) -> dict[str, Any]:
        return {
            "ok": True,
            "status": "completed",
            "task_id": task_id,
            "audit_id": audit_id,
            "requested_backend": "recursive_mas_native",
            "selected_backend": "text_mas_proxy",
            "implementation_level": "text_proxy",
            "execution_mode": "text_proxy",
            "native_latent_verified": False,
            "closed_loop_verified": False,
            "answer": "text_mas_proxy fallback selected; native backend not executed",
            "duration_ms": _elapsed_ms(started),
            "fallback_used": True,
            "fallback_reason": reason,
        }

    def _final_result(self, ok: bool, status: str, task_id: str, started: float, audit_id: str, **extra: Any) -> dict[str, Any]:
        return RecursiveMASRuntimeResult(ok=ok, status=status, task_id=task_id, duration_ms=_elapsed_ms(started), audit_id=audit_id, **extra).to_dict()

    def _audit(self, audit_id: str, task_id: str, request: dict[str, Any], result: dict[str, Any], circuit_before: dict[str, Any], circuit_after: dict[str, Any], *, lock_acquired: bool, busy: bool) -> None:
        path = Path("logs/recursive_mas_runtime.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        goal = str(request.get("goal") or request.get("question") or "")
        record = {
            "timestamp": _now_iso(),
            "audit_id": audit_id,
            "task_id": task_id,
            "request_hash": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            "prompt_preview": _redact(goal)[:120],
            "enabled": self.config.enabled,
            "lock_acquired": lock_acquired,
            "busy": busy,
            "circuit_before": _circuit_public(circuit_before),
            "circuit_after": _circuit_public(circuit_after),
            "requested_backend": result.get("requested_backend"),
            "selected_backend": result.get("selected_backend"),
            "execution_mode": result.get("execution_mode"),
            "style": request.get("style") or self.config.style,
            "rounds": request.get("rounds") or self.config.rounds,
            "worker_pid": result.get("worker_pid"),
            "worker_exit_code": result.get("worker_exit_code"),
            "duration_ms": result.get("duration_ms"),
            "native_latent_verified": result.get("native_latent_verified"),
            "closed_loop_verified": result.get("closed_loop_verified"),
            "answer_present": bool(result.get("answer")),
            "fallback_used": result.get("fallback_used"),
            "fallback_reason": result.get("fallback_reason"),
            "error_type": result.get("error_type"),
            "error": _redact(str(result.get("error") or ""))[:1000],
            "cleanup_completed": result.get("cleanup_completed"),
            "orphan_detected": result.get("orphan_detected"),
            "vram_peak_bytes": (result.get("memory") or {}).get("vram_peak_bytes"),
            "rss_peak_bytes": (result.get("memory") or {}).get("rss_peak_bytes"),
        }
        line = json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except PermissionError:
            fallback = Path(".ralf_run/recursive_mas_runtime.jsonl")
            fallback.parent.mkdir(parents=True, exist_ok=True)
            with fallback.open("a", encoding="utf-8") as handle:
                handle.write(line)


def _default_circuit() -> dict[str, Any]:
    return {"state": "closed", "consecutive_failures": 0, "opened_at": None, "cooldown_until": None, "last_error_type": None, "last_error": None, "last_success_at": None}


def _requires_human_confirmation(request: dict[str, Any]) -> bool:
    policy = request.get("jury_policy")
    return bool((isinstance(policy, dict) and policy.get("requires_human_confirmation")) or request.get("requires_human_confirmation") or request.get("side_effect"))


def _read_json_from_fd(fd: int) -> dict[str, Any]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 8192).decode("utf-8", "replace").strip()
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _public_lock(raw: Any) -> dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    return {"owner_pid": _int_or_none(data.get("pid")) or 0, "started_at": str(data.get("started_at") or ""), "task_id": str(data.get("task_id") or "")}


def _worker_public_summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation": payload.get("operation"),
        "device": payload.get("device"),
        "rounds": payload.get("rounds"),
        "profile": payload.get("profile"),
        "one_model_at_a_time_verified": payload.get("one_model_at_a_time_verified"),
        "cuda_oom": payload.get("cuda_oom"),
        "memory_leak_detected": payload.get("memory_leak_detected"),
        "network_attempted": payload.get("network_attempted"),
        "download_attempted": payload.get("download_attempted"),
    }


def _circuit_public(data: dict[str, Any]) -> dict[str, Any]:
    return {key: data.get(key) for key in ("state", "consecutive_failures", "cooldown_until", "last_error_type")}


def _limit(value: Any, limit: int = 20000) -> str:
    text = value if isinstance(value, str) else "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _redact(text: str) -> str:
    out = text
    out = re.sub(r"(Authorization)\s*[:=]?\s*\S+", r"\1<redacted>", out, flags=re.IGNORECASE)
    out = re.sub(r"(Bearer)\s+\S+", r"\1<redacted>", out, flags=re.IGNORECASE)
    out = re.sub(r"(OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*[:=]?\s*\S+", r"\1<redacted>", out)
    for marker in SECRET_MARKERS:
        out = out.replace(marker, f"{marker[:4]}<redacted>")
    return out


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("health")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--goal", required=True)
    run_parser.add_argument("--rounds", type=int, default=None)
    run_parser.add_argument("--profile", default="deterministic_diagnostic")
    sub.add_parser("reset-circuit")
    args = parser.parse_args(argv)
    controller = RecursiveMASRuntimeController.from_env()
    if args.command == "status":
        print(json.dumps(controller.status().to_dict(), sort_keys=True))
        return 0
    if args.command == "health":
        print(json.dumps(controller.health(), sort_keys=True))
        return 0
    if args.command == "reset-circuit":
        print(json.dumps(controller.reset_circuit(), sort_keys=True))
        return 0
    request = {"goal": args.goal, "profile": args.profile}
    if args.rounds is not None:
        request["rounds"] = args.rounds
    result = controller.execute(request)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
