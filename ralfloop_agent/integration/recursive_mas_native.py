from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


DEFAULT_RECURSIVE_MAS_ROOT = Path("/home/sibilla-cumana/RecursiveMAS")
DEFAULT_RECURSIVE_MAS_PYTHON = DEFAULT_RECURSIVE_MAS_ROOT / ".venv-recursivemas" / "bin" / "python"
WORKER_PATH = Path(__file__).with_name("recursive_mas_worker.py")
RALFLOOP_REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN_ENV_NAMES = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_TOKEN")
SEQUENTIAL_LIGHT_PARAMS = {
    "planner_qwen3_1_7b": 1_700_000_000,
    "critic_llama3_2_1b": 1_000_000_000,
    "solver_qwen2_5_math_1_5b": 1_500_000_000,
}
SEQUENTIAL_LIGHT_REPOS = [
    "RecursiveMAS/Sequential-Light-Planner-Qwen3-1.7B",
    "RecursiveMAS/Sequential-Light-Critic-Llama3.2-1B",
    "RecursiveMAS/Sequential-Light-Solver-Qwen2.5-Math-1.5B",
    "RecursiveMAS/Sequential-Light-Outerlinks",
]
REQUIRED_UPSTREAM_FILES = [
    "README.md",
    "requirements.txt",
    "inference/README.md",
    "inference/run.py",
    "inference/system_loader.py",
    "inference/modeling.py",
    "inference/hf_resolver.py",
    "inference/load_from_repo.py",
    "inference/inference_utils/inference_mas.py",
    "inference/inference_utils/inference_mas_mixture.py",
    "inference/inference_utils/inference_mas_distill.py",
    "inference/inference_utils/inference_mas_deliberation.py",
    "inference/inference_utils/llm_judge.py",
]


@dataclass
class RecursiveMASProbeResult:
    available: bool
    native_latent: bool
    repository: str
    upstream_commit: str | None
    python_executable: str = sys.executable
    python_version: str = ""
    virtualenv_active: bool = False
    dependencies: dict[str, dict[str, Any]] = field(default_factory=dict)
    dependency_versions: dict[str, str | None] = field(default_factory=dict)
    cuda: dict[str, Any] = field(default_factory=dict)
    cuda_probe_status: str = "not_tested"
    ram: dict[str, Any] = field(default_factory=dict)
    style: str = "sequential_light"
    checkpoints_available: bool = False
    checkpoint_search_paths: list[str] = field(default_factory=list)
    checkpoint_files_found: list[str] = field(default_factory=list)
    checkpoint_manifest: dict[str, dict[str, Any]] = field(default_factory=dict)
    checkpoint_status: str = "not_searched"
    repository_status: str = "missing"
    repository_path: str = ""
    repository_commit: str | None = None
    repository_dirty: bool | None = None
    upstream_files_found: list[str] = field(default_factory=list)
    upstream_files_missing: list[str] = field(default_factory=list)
    system_loader_present: bool = False
    inner_recursive_link_present: bool = False
    outer_recursive_link_present: bool = False
    adapter_implemented: bool = True
    repository_available: bool = False
    dependencies_available: bool = False
    dedicated_environment_present: bool = False
    dedicated_python_available: bool = False
    recursive_mas_python: str = ""
    dedicated_dependencies_available: bool = False
    process_bridge_available: bool = False
    worker_probe: dict[str, Any] = field(default_factory=dict)
    worker_error: str | None = None
    hardware_compatible: bool = False
    native_enabled: bool = False
    runtime_ready: bool = False
    checkpoints_downloaded: bool = False
    checkpoint_manifest_complete: bool = False
    offline_ready: bool = False
    load_check_passed: bool = False
    native_canary_passed: bool = False
    native_execution_verified: bool = False
    native_latent_verified: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecursiveMASExecutionResult:
    executed: bool
    available: bool
    backend_name: str = "RecursiveMAS"
    implementation_level: str = "native_latent"
    style: str = "sequential_light"
    native_latent: bool = False
    answer: str | None = None
    recursion_rounds: int | None = None
    duration_ms: int = 0
    device: str = "cpu"
    dtype: str = "auto"
    peak_vram_bytes: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    upstream_commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RecursiveMASNativeAdapter:
    def __init__(self, root: str | Path | None = None, python: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("RALFLOOP_RECURSIVE_MAS_ROOT") or DEFAULT_RECURSIVE_MAS_ROOT)
        self.python = Path(python or os.getenv("RALFLOOP_RECURSIVE_MAS_PYTHON") or (self.root / ".venv-recursivemas" / "bin" / "python"))
        self._system: Any | None = None
        self._system_loader: Any | None = None
        self._native_runner = None
        self._inner_outer_verified = False

    def probe(self, style: str = "sequential_light", device: str | None = None) -> RecursiveMASProbeResult:
        try:
            repository = str(self.root)
            repo = _repository_info(self.root)
            modeling = self.root / "inference" / "modeling.py"
            modeling_text = _read_text(modeling)
            inner_present = "class Adapter" in modeling_text and "forward(self, x:" in modeling_text
            outer_present = "class CrossModelAdapter" in modeling_text and "residual_proj" in modeling_text
            dedicated_python_available = self.python.is_file() and os.access(self.python, os.X_OK)
            worker_probe: dict[str, Any] = {}
            worker_error = None
            process_bridge_available = False
            if dedicated_python_available:
                worker_call = self._call_worker("probe", {"style": style, "device": device})
                process_bridge_available = bool(worker_call.get("ok") and worker_call.get("exit_code") == 0)
                worker_probe = worker_call.get("json") if isinstance(worker_call.get("json"), dict) else {}
                if not process_bridge_available:
                    worker_error = str(worker_call.get("error") or worker_call.get("stderr") or "worker_unavailable")
            manifest_call = self._call_worker("checkpoint-manifest", {"style": style}) if process_bridge_available else {}
            worker_manifest = manifest_call.get("json") if isinstance(manifest_call.get("json"), dict) else {}
            dependencies = _dependencies_from_worker(worker_probe) if worker_probe else _dependency_info()
            dependency_versions = {name: data.get("version") for name, data in dependencies.items()}
            cuda = _cuda_info(device=device)
            ram = _ram_info()
            checkpoint = _checkpoint_info(style)
            estimate = self.estimate_resources(style=style, dtype="float16", device=device)
            native_enabled = os.getenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "0") == "1"
            dependencies_available = all(
                dependencies.get(dep, {}).get("importable")
                for dep in ("torch", "transformers", "huggingface_hub")
            )
            if worker_probe:
                dependencies_available = bool(worker_probe.get("dependencies_available"))
            hardware_compatible = bool(estimate["fits_cuda"] if str(device or "").startswith("cuda") else estimate["fits_cpu"])
            repository_available = repo["repository_status"] == "complete" and inner_present and outer_present
            checkpoints_available = bool(worker_manifest.get("complete")) if worker_manifest else checkpoint["checkpoint_status"] == "complete"
            dedicated_environment_present = (self.root / ".venv-recursivemas").is_dir()
            dedicated_dependencies_available = bool(dependencies_available and worker_probe)
            bridge_ok = process_bridge_available if dedicated_python_available else True
            runtime_ready = all(
                (
                    repository_available,
                    dependencies_available,
                    checkpoints_available,
                    hardware_compatible,
                    native_enabled,
                    bridge_ok,
                )
            )
            cuda_status = str(cuda.get("status") or ("available" if cuda.get("available") else "unavailable"))
            reasons = []
            if not repository_available:
                reasons.append(f"repository_{repo['repository_status']}")
            if not (self.root / "inference" / "system_loader.py").is_file():
                reasons.append("system_loader_missing")
            if not inner_present:
                reasons.append("inner_recursive_link_missing")
            if not outer_present:
                reasons.append("outer_recursive_link_missing")
            for dep in ("torch", "transformers"):
                if not dependencies.get(dep, {}).get("importable"):
                    reasons.append(f"{dep}_missing")
            if not dependencies.get("huggingface_hub", {}).get("importable"):
                reasons.append("huggingface_hub_missing")
            if not checkpoints_available:
                reasons.append(f"checkpoints_{checkpoint['checkpoint_status']}")
            if dedicated_environment_present and not dedicated_python_available:
                reasons.append("dedicated_python_missing")
            if dedicated_python_available and not process_bridge_available:
                reasons.append("worker_unavailable")
            if not hardware_compatible:
                reasons.append("hardware_incompatible")
            if not native_enabled:
                reasons.append("native_disabled")
            return RecursiveMASProbeResult(
                available=runtime_ready,
                native_latent=runtime_ready,
                repository=repository,
                upstream_commit=repo["repository_commit"],
                python_executable=sys.executable,
                python_version=sys.version.split()[0],
                virtualenv_active=_virtualenv_active(),
                dependencies=dependencies,
                dependency_versions=dependency_versions,
                cuda=cuda,
                cuda_probe_status=cuda_status,
                ram=ram,
                style=style,
                checkpoints_available=checkpoints_available,
                checkpoint_search_paths=checkpoint["checkpoint_search_paths"],
                checkpoint_files_found=checkpoint["checkpoint_files_found"],
                checkpoint_manifest=checkpoint["checkpoint_manifest"],
                checkpoint_status=checkpoint["checkpoint_status"],
                repository_status=repo["repository_status"],
                repository_path=repository,
                repository_commit=repo["repository_commit"],
                repository_dirty=repo["repository_dirty"],
                upstream_files_found=repo["upstream_files_found"],
                upstream_files_missing=repo["upstream_files_missing"],
                system_loader_present=(self.root / "inference" / "system_loader.py").is_file(),
                inner_recursive_link_present=inner_present,
                outer_recursive_link_present=outer_present,
                repository_available=repository_available,
                dependencies_available=dependencies_available,
                dedicated_environment_present=dedicated_environment_present,
                dedicated_python_available=dedicated_python_available,
                recursive_mas_python=str(self.python),
                dedicated_dependencies_available=dedicated_dependencies_available,
                process_bridge_available=process_bridge_available,
                worker_probe=worker_probe,
                worker_error=worker_error,
                hardware_compatible=hardware_compatible,
                native_enabled=native_enabled,
                runtime_ready=runtime_ready,
                checkpoints_downloaded=checkpoints_available,
                checkpoint_manifest_complete=checkpoints_available,
                offline_ready=bool(worker_manifest.get("offline_ready")) if worker_manifest else checkpoints_available,
                load_check_passed=False,
                native_canary_passed=False,
                native_execution_verified=False,
                native_latent_verified=False,
                reason="runtime_ready" if runtime_ready else ",".join(reasons),
            )
        except Exception as exc:
            return RecursiveMASProbeResult(
                available=False,
                native_latent=False,
                repository=str(self.root),
                upstream_commit=None,
                python_executable=sys.executable,
                python_version=sys.version.split()[0],
                virtualenv_active=_virtualenv_active(),
                reason=f"probe_error:{type(exc).__name__}:{exc}",
            )

    def estimate_resources(self, style: str, dtype: str = "float16", device: str | None = None) -> dict[str, Any]:
        normalized_dtype = _normalize_dtype_name(dtype)
        dtype_bytes = {"float32": 4, "float16": 2, "bfloat16": 2}[normalized_dtype]
        if style != "sequential_light":
            param_total = 9_000_000_000
            style_reason = "generic_conservative_non_light_style"
        else:
            param_total = sum(SEQUENTIAL_LIGHT_PARAMS.values())
            style_reason = "sequential_light_planner_critic_solver"
        weights = int(param_total * dtype_bytes)
        adapter_params = 700_000_000
        adapters = int(adapter_params * dtype_bytes)
        kv_cache = int(1_200_000_000 if dtype_bytes == 2 else 2_400_000_000)
        activations = int(900_000_000 if dtype_bytes == 2 else 1_800_000_000)
        framework_overhead = int(1_200_000_000)
        safety_margin = int(1_500_000_000)
        estimated_runtime = weights + adapters + kv_cache + activations + framework_overhead + safety_margin
        cuda = _cuda_info(device=device)
        ram = _ram_info()
        available_vram = int(cuda.get("free_bytes") or 0)
        available_ram = int(ram.get("available_bytes") or 0)
        cuda_status = str(cuda.get("status") or "not_tested")
        return {
            "dtype": normalized_dtype,
            "device": device or "auto",
            "parameter_count": int(param_total),
            "weights_bytes": weights,
            "adapter_bytes": adapters,
            "kv_cache_bytes": kv_cache,
            "activation_bytes": activations,
            "framework_overhead_bytes": framework_overhead,
            "safety_margin_bytes": safety_margin,
            "estimated_weights_bytes": weights,
            "estimated_runtime_bytes": estimated_runtime,
            "available_vram_bytes": available_vram,
            "available_ram_bytes": available_ram,
            "fits_cuda": bool(cuda_status == "available" and available_vram >= estimated_runtime),
            "fits_cpu": bool(available_ram >= estimated_runtime),
            "cuda_probe_status": cuda_status,
            "confidence": "medium",
            "assumptions": [
                "local checkpoint inference only",
                "no tensor parallelism assumed",
                "KV cache, activations, CUDA overhead and safety margin included",
                "fits_cpu uses MemAvailable, not MemFree",
                "fits_cuda uses free VRAM, not total VRAM",
            ],
            "reason": style_reason,
        }

    def load(
        self,
        style: str,
        dataset: str = "math500",
        device: str = "cpu",
        dtype: str = "auto",
    ) -> RecursiveMASExecutionResult:
        started = time.monotonic()
        if os.getenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "0") != "1":
            return self._load_error(started, style, device, dtype, "native_disabled", "native backend disabled")
        probe = self.probe(style=style, device=device)
        if not probe.available:
            return self._load_error(started, style, device, dtype, "probe_failed", probe.reason, probe.upstream_commit)
        estimate = self.estimate_resources(style=style, dtype=dtype, device=device)
        if str(device).startswith("cuda") and not estimate["fits_cuda"]:
            return self._load_error(started, style, device, dtype, "insufficient_vram", "resource estimate exceeds VRAM", probe.upstream_commit)
        if str(device) == "cpu" and not estimate["fits_cpu"]:
            return self._load_error(started, style, device, dtype, "insufficient_ram", "resource estimate exceeds RAM", probe.upstream_commit)
        if os.getenv("RALFLOOP_RECURSIVE_MAS_ALLOW_DOWNLOAD", "0") != "1":
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
        worker = self._call_worker(
            "run",
            {"style": style, "dataset": dataset, "device": device, "dtype": dtype},
        )
        payload = worker.get("json") if isinstance(worker.get("json"), dict) else {}
        return self._load_error(
            started,
            style,
            device,
            dtype,
            str(payload.get("reason") or worker.get("error") or "checkpoint_execution_not_enabled"),
            "native subprocess execution is disabled until checkpoints are explicitly enabled",
            probe.upstream_commit,
        )

    def _call_worker(self, operation: str, request: dict[str, Any] | None = None, timeout_sec: int = 30) -> dict[str, Any]:
        if operation not in {
            "probe",
            "import-check",
            "checkpoint-manifest",
            "load-check",
            "native-canary",
            "native-diagnostic-matrix",
            "gpu-stagewise-probe",
            "gpu-stagewise-canary",
            "gpu-stagewise-matrix",
            "run",
        }:
            return {"ok": False, "error": "unsupported_worker_operation"}
        python = self.python.expanduser()
        root = self.root.resolve()
        if not python.is_file():
            return {"ok": False, "error": "recursive_mas_python_missing", "python": str(python)}
        if not root.is_dir():
            return {"ok": False, "error": "recursive_mas_root_missing", "root": str(root)}
        argv = [str(python), str(WORKER_PATH), f"--{operation}"]
        env = _worker_env(root)
        payload = dict(request or {})
        payload["upstream_root"] = str(root)
        try:
            completed = subprocess.run(
                argv,
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=timeout_sec,
                env=env,
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return {"ok": False, "error": "worker_timeout", "stdout": _limit_text(exc.stdout), "stderr": _limit_text(exc.stderr)}
        stdout = _limit_text(completed.stdout)
        stderr = _limit_text(completed.stderr)
        parsed = None
        parse_error = None
        try:
            parsed = json.loads(stdout)
        except Exception as exc:
            parse_error = f"{type(exc).__name__}: {exc}"
        return {
            "ok": completed.returncode == 0 and isinstance(parsed, dict),
            "exit_code": completed.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "json": parsed,
            "error": parse_error,
            "argv": argv,
            "env_keys": sorted(env),
        }

    def run(self, prompt: str, style: str = "sequential_light", recursion_rounds: int | None = None) -> RecursiveMASExecutionResult:
        started = time.monotonic()
        commit = _git_commit(self.root) if self.root.is_dir() else None
        if self._system is None or not self._inner_outer_verified:
            return RecursiveMASExecutionResult(
                executed=False,
                available=False,
                style=style,
                native_latent=False,
                answer=None,
                recursion_rounds=recursion_rounds,
                duration_ms=_elapsed_ms(started),
                error_type="not_loaded",
                error_message="native RecursiveMAS system is not loaded",
                upstream_commit=commit,
            )
        if self._native_runner is None:
            return RecursiveMASExecutionResult(
                executed=False,
                available=True,
                style=style,
                native_latent=False,
                answer=None,
                recursion_rounds=recursion_rounds,
                duration_ms=_elapsed_ms(started),
                error_type="generic_prompt_runner_unavailable",
                error_message="official inference entrypoints are dataset runners; generic prompt runner not configured",
                upstream_commit=commit,
            )
        try:
            answer = self._native_runner(self._system, prompt, recursion_rounds)
            return RecursiveMASExecutionResult(
                executed=True,
                available=True,
                style=style,
                native_latent=True,
                answer=str(answer),
                recursion_rounds=recursion_rounds,
                duration_ms=_elapsed_ms(started),
                device="native_loaded",
                dtype="native_loaded",
                peak_vram_bytes=_peak_vram_bytes(),
                upstream_commit=commit,
            )
        except Exception as exc:
            return RecursiveMASExecutionResult(
                executed=False,
                available=True,
                style=style,
                native_latent=False,
                answer=None,
                recursion_rounds=recursion_rounds,
                duration_ms=_elapsed_ms(started),
                error_type=type(exc).__name__,
                error_message=str(exc),
                upstream_commit=commit,
            )

    def unload(self) -> None:
        system = self._system
        self._system = None
        self._inner_outer_verified = False
        try:
            if system is not None and self._system_loader is not None:
                self._system_loader.unload_mas_system(system)
        finally:
            _empty_cuda_cache()

    def _load_error(
        self,
        started: float,
        style: str,
        device: str,
        dtype: str,
        error_type: str,
        message: str,
        commit: str | None = None,
    ) -> RecursiveMASExecutionResult:
        return RecursiveMASExecutionResult(
            executed=False,
            available=False,
            style=style,
            native_latent=False,
            duration_ms=_elapsed_ms(started),
            device=device,
            dtype=dtype,
            error_type=error_type,
            error_message=message,
            upstream_commit=commit,
        )


def _dependency_info() -> dict[str, dict[str, Any]]:
    out = {}
    for name in ("torch", "transformers", "huggingface_hub"):
        importable = importlib.util.find_spec(name) is not None
        version = None
        if importable:
            try:
                version = importlib.metadata.version(name)
            except Exception:
                version = None
        out[name] = {"importable": importable, "version": version}
    return out


def _dependencies_from_worker(worker_probe: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = worker_probe.get("dependencies")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for name, data in raw.items():
        if not isinstance(data, dict):
            continue
        out[str(name)] = {
            "importable": bool(data.get("import_ok")),
            "version": data.get("version"),
            "error": data.get("error"),
        }
    return out


def _worker_env(root: Path) -> dict[str, str]:
    cache = root / ".hf-recursivemas"
    env = {
        "PATH": os.getenv("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(RALFLOOP_REPO_ROOT),
        "RALFLOOP_RECURSIVE_MAS_ROOT": str(root),
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HOME": str(cache),
        "HUGGINGFACE_HUB_CACHE": str(cache / "hub"),
        "TRANSFORMERS_CACHE": str(cache / "transformers"),
    }
    if os.getenv("RALFLOOP_RECURSIVE_MAS_NATIVE_CANARY") == "1":
        env["RALFLOOP_RECURSIVE_MAS_NATIVE_CANARY"] = "1"
    if os.getenv("RALFLOOP_RECURSIVE_MAS_NATIVE_DIAGNOSTIC") == "1":
        env["RALFLOOP_RECURSIVE_MAS_NATIVE_DIAGNOSTIC"] = "1"
    if os.getenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE") == "1":
        env["RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE"] = "1"
    return env


def _limit_text(value: Any, max_chars: int = 200_000) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n[truncated]"


def _repository_info(root: Path) -> dict[str, Any]:
    if not root.is_dir():
        return {
            "repository_status": "missing",
            "repository_commit": None,
            "repository_dirty": None,
            "upstream_files_found": [],
            "upstream_files_missing": list(REQUIRED_UPSTREAM_FILES),
        }
    found = [rel for rel in REQUIRED_UPSTREAM_FILES if (root / rel).is_file()]
    missing = [rel for rel in REQUIRED_UPSTREAM_FILES if rel not in found]
    status = "complete" if not missing else "partial"
    return {
        "repository_status": status,
        "repository_commit": _git_commit(root),
        "repository_dirty": _git_dirty(root),
        "upstream_files_found": found,
        "upstream_files_missing": missing,
    }


def _cuda_info(device: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {"available": False, "status": "not_tested", "device": device or "auto"}
    try:
        import torch  # type: ignore

        info["torch_version"] = getattr(torch, "__version__", None)
        info["available"] = bool(torch.cuda.is_available())
        if info["available"]:
            info["status"] = "available"
            index = 0
            if device and ":" in str(device):
                index = int(str(device).split(":", 1)[1])
            free_bytes, total_bytes = torch.cuda.mem_get_info(index)
            props = torch.cuda.get_device_properties(index)
            info.update(
                {
                    "index": index,
                    "name": props.name,
                    "total_bytes": int(total_bytes),
                    "free_bytes": int(free_bytes),
                }
            )
        else:
            info["status"] = "unavailable"
    except ModuleNotFoundError as exc:
        info["status"] = "dependency_missing"
        info["error"] = f"{type(exc).__name__}:{exc}"
    except Exception as exc:
        info["status"] = "unavailable"
        info["error"] = f"{type(exc).__name__}:{exc}"
    return info


def _ram_info() -> dict[str, Any]:
    meminfo = _meminfo()
    if meminfo:
        return {
            "total_bytes": meminfo.get("MemTotal", 0),
            "free_bytes": meminfo.get("MemFree", 0),
            "available_bytes": meminfo.get("MemAvailable", meminfo.get("MemFree", 0)),
            "cached_bytes": meminfo.get("Cached", 0),
            "buffers_bytes": meminfo.get("Buffers", 0),
            "swap_total_bytes": meminfo.get("SwapTotal", 0),
            "swap_free_bytes": meminfo.get("SwapFree", 0),
            "source": "/proc/meminfo",
            "note": "MemFree is immediately unused RAM; MemAvailable estimates RAM usable without swapping by reclaiming cache/buffers.",
        }
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        avail = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        free_bytes = int(avail * page_size)
        return {
            "total_bytes": int(pages * page_size),
            "free_bytes": free_bytes,
            "available_bytes": free_bytes,
            "cached_bytes": 0,
            "swap_total_bytes": 0,
            "swap_free_bytes": 0,
            "source": "sysconf_fallback",
            "note": "Fallback cannot distinguish MemFree from MemAvailable.",
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}:{exc}", "available_bytes": 0, "source": "error"}


def _checkpoints_available(style: str) -> bool:
    return _checkpoint_info(style)["checkpoint_status"] == "complete"


def _checkpoint_info(style: str) -> dict[str, Any]:
    paths = _checkpoint_search_paths()
    manifest: dict[str, dict[str, Any]] = {}
    files_found: list[str] = []
    if style != "sequential_light":
        return {
            "checkpoint_search_paths": [str(path) for path in paths],
            "checkpoint_files_found": [],
            "checkpoint_manifest": {},
            "checkpoint_status": "not_searched",
        }
    for repo in SEQUENTIAL_LIGHT_REPOS:
        owner, name = repo.split("/", 1)
        rel = f"models--{owner}--{name}"
        matches = [str(path / rel) for path in paths if (path / rel).exists()]
        manifest[repo] = {"status": "found" if matches else "missing", "paths": matches}
        files_found.extend(matches)
    found_count = sum(1 for item in manifest.values() if item["status"] == "found")
    if found_count == 0:
        status = "missing"
    elif found_count == len(SEQUENTIAL_LIGHT_REPOS):
        status = "complete"
    else:
        status = "partial"
    return {
        "checkpoint_search_paths": [str(path) for path in paths],
        "checkpoint_files_found": files_found,
        "checkpoint_manifest": manifest,
        "checkpoint_status": status,
    }


def _checkpoint_search_paths() -> list[Path]:
    paths: list[Path] = []
    for env_name in ("HF_HOME", "HUGGINGFACE_HUB_CACHE"):
        value = os.getenv(env_name)
        if not value:
            continue
        path = Path(value).expanduser()
        if env_name == "HF_HOME":
            path = path / "hub"
        paths.append(path)
    paths.append(Path.home() / ".cache" / "huggingface" / "hub")
    paths.append(DEFAULT_RECURSIVE_MAS_ROOT / "checkpoints")
    deduped = []
    seen = set()
    for path in paths:
        text = str(path)
        if text in seen:
            continue
        seen.add(text)
        deduped.append(path)
    return deduped


def _meminfo(path: str = "/proc/meminfo") -> dict[str, int]:
    try:
        out = {}
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if ":" not in line:
                continue
            key, rest = line.split(":", 1)
            parts = rest.strip().split()
            if not parts:
                continue
            out[key] = int(parts[0]) * 1024
        return out
    except Exception:
        return {}


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _git_commit(path: Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-c", f"safe.directory={path}", "-C", str(path), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None


def _git_dirty(path: Path) -> bool | None:
    try:
        output = subprocess.check_output(
            ["git", "-c", f"safe.directory={path}", "-C", str(path), "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return bool(output.strip())
    except Exception:
        return None


def _normalize_dtype_name(dtype: str) -> str:
    value = str(dtype or "float16").lower()
    aliases = {
        "fp32": "float32",
        "torch.float32": "float32",
        "fp16": "float16",
        "half": "float16",
        "torch.float16": "float16",
        "bf16": "bfloat16",
        "torch.bfloat16": "bfloat16",
        "auto": "float16",
    }
    normalized = aliases.get(value, value)
    if normalized not in {"float32", "float16", "bfloat16"}:
        raise ValueError(f"unsupported dtype for estimate: {dtype}")
    return normalized


def _virtualenv_active() -> bool:
    return bool(os.getenv("VIRTUAL_ENV")) or sys.prefix != getattr(sys, "base_prefix", sys.prefix)


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _peak_vram_bytes() -> int | None:
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return int(torch.cuda.max_memory_allocated())
    except Exception:
        return None
    return None


def _empty_cuda_cache() -> None:
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--checkpoint-manifest", action="store_true")
    parser.add_argument("--load-check", action="store_true")
    parser.add_argument("--native-canary", action="store_true")
    parser.add_argument("--native-diagnostic-matrix", action="store_true")
    parser.add_argument("--gpu-stagewise-probe", action="store_true")
    parser.add_argument("--gpu-stagewise-canary", action="store_true")
    parser.add_argument("--gpu-stagewise-matrix", action="store_true")
    parser.add_argument("--style", default="sequential_light")
    parser.add_argument("--device", default=os.getenv("RALFLOOP_RECURSIVE_MAS_DEVICE", "cpu"))
    parser.add_argument("--dtype", default="float16")
    parser.add_argument("--recursion-rounds", type=int, default=1)
    parser.add_argument("--rounds", type=int, default=None)
    parser.add_argument("--profile", default="deterministic_diagnostic")
    parser.add_argument("--prompt", default="Solve and return only the final integer: 2 + 3")
    parser.add_argument("--question", default="What is 2 + 3?")
    parser.add_argument("--include-release-like", default="0")
    args = parser.parse_args(argv)
    adapter = RecursiveMASNativeAdapter()
    if args.probe:
        probe = adapter.probe(style=args.style, device=args.device).to_dict()
        probe["resource_estimate"] = adapter.estimate_resources(args.style, dtype=args.dtype, device=args.device)
        print(json.dumps(probe, ensure_ascii=False, sort_keys=True))
        return 0
    if args.checkpoint_manifest:
        result = adapter._call_worker("checkpoint-manifest", {"style": args.style})
        print(json.dumps(result.get("json") or result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.load_check:
        result = adapter._call_worker("load-check", {"style": args.style, "device": args.device, "dtype": args.dtype}, timeout_sec=1800)
        print(json.dumps(result.get("json") or result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.native_canary:
        result = adapter._call_worker(
            "native-canary",
            {"style": args.style, "device": args.device, "recursion_rounds": args.recursion_rounds, "prompt": args.prompt},
            timeout_sec=2700,
        )
        print(json.dumps(result.get("json") or result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.native_diagnostic_matrix:
        result = adapter._call_worker(
            "native-diagnostic-matrix",
            {
                "style": args.style,
                "device": args.device,
                "question": args.question,
                "include_release_like": args.include_release_like,
            },
            timeout_sec=2700,
        )
        print(json.dumps(result.get("json") or result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.gpu_stagewise_probe:
        result = adapter._call_worker(
            "gpu-stagewise-probe",
            {"style": args.style, "device": args.device, "dtype": args.dtype},
            timeout_sec=1800,
        )
        print(json.dumps(result.get("json") or result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.gpu_stagewise_canary:
        result = adapter._call_worker(
            "gpu-stagewise-canary",
            {
                "style": args.style,
                "device": args.device,
                "rounds": args.rounds or args.recursion_rounds,
                "question": args.question,
                "profile": args.profile,
            },
            timeout_sec=2700,
        )
        print(json.dumps(result.get("json") or result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    if args.gpu_stagewise_matrix:
        result = adapter._call_worker(
            "gpu-stagewise-matrix",
            {
                "style": args.style,
                "device": args.device,
                "question": args.question,
                "profile": args.profile,
            },
            timeout_sec=2700,
        )
        print(json.dumps(result.get("json") or result, ensure_ascii=False, sort_keys=True))
        return 0 if result.get("ok") else 1
    parser.error("safe CLI requires --probe, --checkpoint-manifest, --load-check, --native-canary, --native-diagnostic-matrix, --gpu-stagewise-probe, --gpu-stagewise-canary, or --gpu-stagewise-matrix")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
