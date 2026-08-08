from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any, Callable

from .context import redact_secrets
from .models import AdapterEnvelope, AdapterStatus, ContextPacket
from .parser import ReviewParseError, parse_glm_review


DEFAULT_LAUNCHER = Path("/home/sibilla-cumana/Dati/ralfloop-colibri/bin/glm-run")
ENV_ALLOWLIST = {"PATH", "LANG", "LC_ALL", "TZ", "HOME", "USER", "LOGNAME"}
LAUNCHER_LOG_RE = re.compile(r"GLM log:\s*(/[^\r\n]+)")


class ColibriGlmAdapter:
    provider = "colibri_glm"
    model = "glm-5.2-colibri"

    def __init__(
        self,
        *,
        launcher: str | Path = DEFAULT_LAUNCHER,
        timeout_seconds: int = 3_600,
        max_output_bytes: int = 131_072,
        max_error_bytes: int = 131_072,
        ngen: int = 128,
        outer_grace_seconds: int = 45,
    ) -> None:
        self.launcher = Path(launcher)
        self.timeout_seconds = int(timeout_seconds)
        self.max_output_bytes = int(max_output_bytes)
        self.max_error_bytes = int(max_error_bytes)
        self.ngen = int(ngen)
        self.outer_grace_seconds = max(0, int(outer_grace_seconds))
        if self.timeout_seconds < 1 or self.max_output_bytes < 1024 or self.max_error_bytes < 1024:
            raise ValueError("invalid_adapter_limits")
        if not 1 <= self.ngen <= 2_048:
            raise ValueError("invalid_ngen")

    def review(
        self,
        packet: ContextPacket,
        artifact_dir: str | Path,
        *,
        on_process_start: Callable[[int], None] | None = None,
        prompt_override: str | None = None,
        result_parser: Callable[[bytes], Any] | None = None,
    ) -> AdapterEnvelope:
        directory = _protected_directory(Path(artifact_dir))
        prompt_path = directory / "prompt.txt"
        stdout_path = directory / "stdout.txt"
        stderr_path = directory / "stderr.txt"
        result_path = directory / "result.json"
        envelope_path = directory / "envelope.json"
        process_path = directory / "process.json"
        stdout_running = directory / "stdout.txt.running"
        stderr_running = directory / "stderr.txt.running"
        prompt, redactions = redact_secrets(prompt_override or build_review_prompt(packet))
        _protected_write(prompt_path, prompt.encode("utf-8"))
        started = time.monotonic()
        exit_code: int | None = None
        timed_out = False
        process_error: str | None = None
        if not self.launcher.is_file() or not os.access(self.launcher, os.X_OK):
            process_error = "launcher_unavailable"
        else:
            env = {key: value for key, value in os.environ.items() if key in ENV_ALLOWLIST}
            env.update(
                {
                    "GLM_NGEN": str(self.ngen),
                    "GLM_TIMEOUT_SECONDS": str(self.timeout_seconds),
                    "PYTHONIOENCODING": "utf-8",
                }
            )
            try:
                with stdout_running.open("wb") as stdout_handle, stderr_running.open("wb") as stderr_handle:
                    os.chmod(stdout_handle.fileno(), 0o600)
                    os.chmod(stderr_handle.fileno(), 0o600)
                    process = subprocess.Popen(
                        [str(self.launcher)],
                        stdin=subprocess.PIPE,
                        stdout=stdout_handle,
                        stderr=stderr_handle,
                        cwd=str(directory),
                        env=env,
                        shell=False,
                        start_new_session=True,
                        umask=0o077,
                    )
                    _protected_write(
                        process_path,
                        (json.dumps({
                            "pid": process.pid,
                            "started_epoch": int(time.time()),
                            "ngen": self.ngen,
                            "timeout_seconds": self.timeout_seconds,
                            "launcher": str(self.launcher),
                            "state": "running",
                        }, sort_keys=True) + "\n").encode(),
                    )
                    if on_process_start:
                        on_process_start(process.pid)
                    try:
                        process.communicate(
                            input=prompt.encode("utf-8"),
                            timeout=self.timeout_seconds + self.outer_grace_seconds,
                        )
                    except subprocess.TimeoutExpired:
                        timed_out = True
                        os.killpg(process.pid, signal.SIGTERM)
                        try:
                            process.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=10)
                    exit_code = process.returncode
                    stdout_handle.flush()
                    stderr_handle.flush()
                    os.fsync(stdout_handle.fileno())
                    os.fsync(stderr_handle.fileno())
                os.replace(stdout_running, stdout_path)
                os.replace(stderr_running, stderr_path)
                _fsync_directory(directory)
            except OSError as exc:
                process_error = f"launcher_os_error:{type(exc).__name__}"
        for running, final in ((stdout_running, stdout_path), (stderr_running, stderr_path)):
            if running.exists() and not final.exists():
                os.replace(running, final)
        _fsync_directory(directory)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        for path in (stdout_path, stderr_path):
            if not path.exists():
                _protected_write(path, b"")
            else:
                path.chmod(0o600)
        stdout = _read_limited(stdout_path, self.max_output_bytes)
        stderr = _read_limited(stderr_path, self.max_error_bytes)
        launcher_log = _launcher_log(stdout, stderr)
        _protect_launcher_log(launcher_log)
        model_started = bool(launcher_log) or exit_code == 0 or timed_out or exit_code in {124, 137, 143}

        result: dict[str, Any] = {}
        if process_error:
            status = AdapterStatus.FAILED
            error = process_error
        elif timed_out or exit_code in {124, 137, 143}:
            status = AdapterStatus.TIMEOUT
            error = "launcher_timeout"
        elif exit_code == 75:
            status = AdapterStatus.DEFERRED_RESOURCE_BUSY
            error = _safe_error(stderr or stdout, "resources_unavailable")
        elif exit_code != 0:
            status = AdapterStatus.FAILED
            error = _safe_error(stderr or stdout, f"launcher_exit_{exit_code}")
        elif stdout_path.stat().st_size > self.max_output_bytes:
            status = AdapterStatus.INVALID_OUTPUT
            error = "output_limit_exceeded"
        elif stderr_path.stat().st_size > self.max_error_bytes:
            status = AdapterStatus.FAILED
            error = "diagnostic_limit_exceeded"
        else:
            try:
                parsed = result_parser(stdout) if result_parser else parse_glm_review(stdout, max_bytes=self.max_output_bytes)
                if not result_parser and parsed.task_id != packet.task_id:
                    raise ReviewParseError("task_id_mismatch")
                result = parsed.model_dump(mode="json") if hasattr(parsed, "model_dump") else dict(parsed)
                _protected_write(result_path, (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
                status = AdapterStatus.COMPLETED
                error = None
            except (ReviewParseError, ValueError, TypeError) as exc:
                status = AdapterStatus.INVALID_OUTPUT
                error = str(exc)[:500]
        if not result_path.exists():
            _protected_write(result_path, b"{}\n")
        envelope = AdapterEnvelope(
            ok=status == AdapterStatus.COMPLETED,
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            result=result,
            prompt_artifact=str(prompt_path),
            result_artifact=str(result_path),
            launcher_log=launcher_log,
            error=error,
            ngen=self.ngen,
            timeout_seconds=self.timeout_seconds,
            model_started=model_started,
        )
        payload = envelope.model_dump(mode="json")
        payload["prompt_redactions"] = redactions
        _protected_write(envelope_path, (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode())
        _protected_write(
            process_path,
            (json.dumps({
                "pid": None,
                "finished_epoch": int(time.time()),
                "ngen": self.ngen,
                "timeout_seconds": self.timeout_seconds,
                "launcher": str(self.launcher),
                "state": "finished",
                "exit_code": exit_code,
                "model_started": model_started,
            }, sort_keys=True) + "\n").encode(),
        )
        return envelope


def build_review_prompt(packet: ContextPacket) -> str:
    schema = {
        "schema_version": 1,
        "task_id": packet.task_id,
        "verdict": "accept|revise|reject|insufficient_evidence",
        "summary": "string",
        "critical_issues": [],
        "recommended_changes": [],
        "missing_evidence": [],
        "risk_flags": [],
        "confidence": 0.0,
        "requires_human_approval": False,
    }
    return "\n".join(
        (
            "ROLE: consultative slow reviewer. Do not call tools, execute commands, send data, or authorize actions.",
            "Return exactly one short JSON object. No Markdown, prose, code fences, or extra keys.",
            "Close JSON before secondary detail. Summary <=20 words. Strings concise.",
            "Limits: critical_issues<=3; recommended_changes<=3; missing_evidence<=5. Use empty arrays when possible.",
            "Nonempty critical_issues items use severity,issue,evidence_refs; recommended_changes items use target,change,reason.",
            "Ground issues in source_id values from the packet. Mark missing evidence; never invent facts.",
            "known_gaps are already known: do not rediscover or restate them as strategic work.",
            "OUTPUT_SCHEMA=" + json.dumps(schema, ensure_ascii=False, separators=(",", ":")),
            "CONTEXT_PACKET=" + json.dumps(packet.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
        )
    )


def _protected_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _protected_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("wb") as handle:
        os.chmod(handle.fileno(), 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    path.chmod(0o600)
    _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_limited(path: Path, limit: int) -> bytes:
    with path.open("rb") as handle:
        return handle.read(limit + 1)


def _launcher_log(stdout: bytes, stderr: bytes) -> str:
    text = (stderr + b"\n" + stdout).decode("utf-8", errors="replace")
    match = LAUNCHER_LOG_RE.search(text)
    return match.group(1).strip() if match else ""


def _protect_launcher_log(value: str) -> None:
    if not value:
        return
    path = Path(value)
    allowed = Path("/home/sibilla-cumana/Dati/ralfloop-colibri/logs")
    try:
        resolved = path.resolve(strict=True)
        if resolved.parent == allowed.resolve() and resolved.is_file():
            resolved.chmod(0o600)
    except OSError:
        return


def _safe_error(value: bytes, fallback: str) -> str:
    text = value.decode("utf-8", errors="replace")
    text, _ = redact_secrets(text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[-1] if lines else fallback)[:500]
