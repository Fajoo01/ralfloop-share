from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping

import requests


class Ds4ServerError(RuntimeError):
    pass


class Ds4ServerRequestError(Ds4ServerError):
    pass


@dataclass(frozen=True)
class Ds4ServerProfile:
    executable: Path
    model_path: Path
    host: str = "127.0.0.1"
    port: int = 19194
    context_tokens: int = 1024
    prefill_chunk: int = 128
    threads: int = 8
    max_output_tokens: int = 128
    stage_mb: int = 1280
    reserve_mb: int = 384
    weight_cache_verbose: bool = True
    weight_cache_limit_gb: int = 3
    startup_timeout_sec: float = 180.0
    request_timeout_sec: float = 1800.0

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("ds4_server_host_must_be_loopback")
        if not 1024 <= self.context_tokens <= 16384:
            raise ValueError("ds4_context_tokens_out_of_range")
        if not 32 <= self.prefill_chunk <= self.context_tokens:
            raise ValueError("ds4_prefill_chunk_out_of_range")
        if not 64 <= self.max_output_tokens <= 256:
            raise ValueError("ds4_output_tokens_out_of_range")
        if not 512 <= self.stage_mb <= 4096:
            raise ValueError("ds4_stage_mb_out_of_range")
        if not 256 <= self.reserve_mb <= 4096:
            raise ValueError("ds4_reserve_mb_out_of_range")
        if not 1 <= self.weight_cache_limit_gb <= 8:
            raise ValueError("ds4_weight_cache_limit_gb_out_of_range")
        if not 1024 <= self.port <= 65535:
            raise ValueError("ds4_server_port_out_of_range")

    def command(self) -> list[str]:
        return [
            str(self.executable), "-m", str(self.model_path),
            "--backend", "cuda",
            "--ssd-streaming", "--cuda-low-vram-stream", "--ssd-streaming-cold",
            "--ctx", str(self.context_tokens),
            "--prefill-chunk", str(self.prefill_chunk),
            "--threads", str(self.threads),
            "--tokens", str(self.max_output_tokens),
            "--host", self.host, "--port", str(self.port),
        ]

    def environment_overrides(self) -> dict[str, str]:
        return {
            "DS4_CUDA_LOW_VRAM_STAGE_MB": str(self.stage_mb),
            "DS4_CUDA_LOW_VRAM_RESERVE_MB": str(self.reserve_mb),
            "DS4_CUDA_WEIGHT_CACHE_VERBOSE": "1" if self.weight_cache_verbose else "0",
            "DS4_CUDA_WEIGHT_CACHE_LIMIT_GB": str(self.weight_cache_limit_gb),
        }

    def environment(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        value = dict(os.environ if base is None else base)
        value.update(self.environment_overrides())
        return value

    def request_payload(self, prompt: str, *, max_tokens: int | None = None) -> dict[str, Any]:
        bounded = self.max_output_tokens if max_tokens is None else int(max_tokens)
        if not 1 <= bounded <= 256:
            raise ValueError("ds4_request_output_tokens_out_of_range")
        return {
            "model": "deepseek-chat",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": bounded,
            "temperature": 0,
            "top_p": 1,
            "stream": False,
            "thinking": {"type": "disabled"},
            "think": False,
        }


@dataclass(frozen=True)
class Ds4ServerDiagnostics:
    early_prealloc: int
    lazy_alloc: int
    fences: int
    up_expert_oom: int
    failed: int
    host_registration_skipped: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class Ds4ServerReply:
    content: str
    request_ms: int
    input_tokens: int | None
    output_tokens: int | None
    usage: Mapping[str, Any]
    diagnostics: Ds4ServerDiagnostics
    prefill_ms: int | None = None
    decode_ms: int | None = None


_FATAL_FAILURE_RE = re.compile(
    r"(?:allocation failed|CUDA error:\s*out of memory|encode failed|decode failed|prompt (?:processing|decode) failed|"
    r"staging refused|stage preallocation refused)",
    re.I,
)
_PREFILL_DONE_RE = re.compile(r"prompt done\s+([0-9]+(?:\.[0-9]+)?)s", re.I)


def diagnostics_from_log(text: str) -> Ds4ServerDiagnostics:
    fatal_lines = [line for line in text.splitlines() if _FATAL_FAILURE_RE.search(line)]
    return Ds4ServerDiagnostics(
        early_prealloc=text.count("CUDA low-VRAM early stage preallocation:"),
        lazy_alloc=text.count("CUDA low-VRAM dense staging buffer allocated:"),
        fences=text.count("CUDA low-VRAM prefill fence after layer "),
        up_expert_oom=text.count("CUDA streaming up experts allocation failed"),
        failed=len(fatal_lines),
        host_registration_skipped=text.count("CUDA host registration skipped: out of memory"),
    )


def classify_runtime_failure(text: str) -> str:
    folded = text.casefold()
    if "cuda error: out of memory" in folded or "allocation failed" in folded and "out of memory" in folded:
        return "gpu_out_of_memory"
    if "encode failed" in folded or "decode failed" in folded or "prompt processing failed" in folded:
        return "gpu_processing_failed"
    if "invalid value" in folded or "requires --" in folded or "not compatible" in folded:
        return "invalid_runtime_argument"
    if "cannot open model" in folded:
        return "model_open_failed"
    return "runtime_error"


class Ds4ServerSession:
    """Owns one loopback DS4 process and one sequential resident model session."""

    def __init__(
        self,
        profile: Ds4ServerProfile,
        *,
        artifact_dir: Path | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        http: requests.Session | None = None,
    ) -> None:
        self.profile = profile
        self.artifact_dir = artifact_dir
        self.popen_factory = popen_factory
        self.http = http or requests.Session()
        self.process: subprocess.Popen[bytes] | None = None
        self.log_handle: Any = None
        self.log_path: Path | None = None
        self.startup_ms: int | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.profile.host}:{self.profile.port}"

    def __enter__(self) -> "Ds4ServerSession":
        if not self.profile.executable.is_file() or not os.access(self.profile.executable, os.X_OK):
            raise Ds4ServerError("deepseek_v4_flash_server_unavailable")
        if not self.profile.model_path.is_file():
            raise Ds4ServerError("deepseek_v4_flash_model_unavailable")
        if self.artifact_dir is None:
            self._temporary = tempfile.TemporaryDirectory(prefix="ralf-ds4-server-")
            directory = Path(self._temporary.name)
        else:
            directory = self.artifact_dir
            directory.mkdir(parents=True, exist_ok=True)
        self.log_path = directory / f"ds4-server-{time.time_ns()}.log"
        self.log_handle = self.log_path.open("wb")
        started = time.perf_counter()
        self.process = self.popen_factory(
            self.profile.command(),
            stdin=subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=subprocess.STDOUT,
            env=self.profile.environment(),
        )
        try:
            deadline = time.monotonic() + self.profile.startup_timeout_sec
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise Ds4ServerError(
                        "deepseek_v4_flash_server_startup_failed:" + classify_runtime_failure(self._log_text())
                    )
                response = None
                try:
                    response = self.http.get(f"{self.base_url}/v1/models", timeout=(1, 3))
                    if response.status_code == 200:
                        self.startup_ms = int((time.perf_counter() - started) * 1000)
                        return self
                except requests.RequestException:
                    pass
                finally:
                    if response is not None:
                        response.close()
                time.sleep(1)
            raise TimeoutError("deepseek_v4_flash_server_startup_timeout")
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def review_prompt(self, prompt: str, *, max_tokens: int | None = None) -> Ds4ServerReply:
        log_before = self._log_text()
        started = time.perf_counter()
        try:
            response = self.http.post(
                f"{self.base_url}/v1/chat/completions",
                json=self.profile.request_payload(prompt, max_tokens=max_tokens),
                timeout=(3, self.profile.request_timeout_sec),
            )
        except requests.Timeout as exc:
            raise TimeoutError("semantic_judge_timeout") from exc
        except requests.RequestException as exc:
            reason = classify_runtime_failure(self._log_text())
            if reason != "runtime_error" or self.process is not None and self.process.poll() is not None:
                raise Ds4ServerRequestError(f"deepseek_v4_flash_request_failed:{reason}") from exc
            raise Ds4ServerRequestError("deepseek_v4_flash_server_unreachable") from exc
        request_ms = int((time.perf_counter() - started) * 1000)
        try:
            if response.status_code >= 400:
                reason = classify_runtime_failure(self._log_text())
                raise Ds4ServerRequestError(f"deepseek_v4_flash_http_{response.status_code}:{reason}")
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise ValueError("deepseek_v4_flash_response_invalid_json") from exc
        finally:
            response.close()
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("deepseek_v4_flash_response_missing_content") from exc
        if not isinstance(content, str):
            raise ValueError("deepseek_v4_flash_response_content_not_text")
        usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
        log_after = self._log_text()
        log_delta = log_after[len(log_before):] if log_after.startswith(log_before) else log_after
        prefill_values = _PREFILL_DONE_RE.findall(log_delta)
        prefill_ms = round(float(prefill_values[-1]) * 1000) if prefill_values else None
        return Ds4ServerReply(
            content=content,
            request_ms=request_ms,
            input_tokens=usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else None,
            output_tokens=usage.get("completion_tokens") if isinstance(usage.get("completion_tokens"), int) else None,
            usage=dict(usage),
            diagnostics=diagnostics_from_log(log_after),
            prefill_ms=prefill_ms,
            decode_ms=max(0, request_ms - prefill_ms) if prefill_ms is not None else None,
        )

    def diagnostics(self) -> Ds4ServerDiagnostics:
        return diagnostics_from_log(self._log_text())

    def _log_text(self) -> str:
        if self.log_handle is not None:
            self.log_handle.flush()
        if self.log_path is None:
            return ""
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def __exit__(self, *_exc: object) -> None:
        self.http.close()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
        if self.log_handle is not None and not self.log_handle.closed:
            self.log_handle.flush()
            os.fsync(self.log_handle.fileno())
            self.log_handle.close()
        if self._temporary is not None:
            self._temporary.cleanup()


__all__ = [
    "Ds4ServerDiagnostics", "Ds4ServerError", "Ds4ServerProfile", "Ds4ServerReply",
    "Ds4ServerRequestError", "Ds4ServerSession", "classify_runtime_failure", "diagnostics_from_log",
]
