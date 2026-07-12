from __future__ import annotations

import gc
import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


GIB = 1024**3


@dataclass
class SourceJuryVramPolicy:
    max_candidates: int = 8
    batch_size: int = 0
    batch_size_8gb: int = 3
    batch_size_12gb: int = 4
    batch_size_24gb: int = 8
    oom_backoff: bool = True
    min_batch_size: int = 1
    cleanup_between_batches: bool = True

    @classmethod
    def from_env(cls) -> "SourceJuryVramPolicy":
        return cls(
            max_candidates=_env_int("RALFLOOP_BANDO_SOURCE_JURY_MAX_CANDIDATES", 8),
            batch_size=_env_int("RALFLOOP_BANDO_SOURCE_JURY_BATCH_SIZE", 0),
            batch_size_8gb=_env_int("RALFLOOP_BANDO_SOURCE_JURY_8GB_BATCH_SIZE", 3),
            batch_size_12gb=_env_int("RALFLOOP_BANDO_SOURCE_JURY_12GB_BATCH_SIZE", 4),
            batch_size_24gb=_env_int("RALFLOOP_BANDO_SOURCE_JURY_24GB_BATCH_SIZE", 8),
            oom_backoff=os.getenv("RALFLOOP_BANDO_SOURCE_JURY_OOM_BACKOFF", "1") == "1",
            min_batch_size=max(1, _env_int("RALFLOOP_BANDO_SOURCE_JURY_MIN_BATCH_SIZE", 1)),
            cleanup_between_batches=os.getenv("RALFLOOP_BANDO_SOURCE_JURY_CLEANUP_BETWEEN_BATCHES", "1") == "1",
        )

    def selected_batch_size(self, memory: dict[str, Any]) -> int:
        if self.batch_size:
            return max(self.min_batch_size, min(self.batch_size, self.max_candidates))
        total = int(memory.get("total_vram_bytes") or 0)
        free = int(memory.get("free_vram_before_bytes") or total or 0)
        if total >= 24 * GIB:
            selected = self.batch_size_24gb
        elif total > 8 * GIB and total <= 12 * GIB:
            selected = self.batch_size_12gb
        else:
            selected = self.batch_size_8gb
        if free and free < total:
            free_ratio = free / max(total, 1)
            if free_ratio < 0.35:
                selected = min(selected, 1)
            elif free_ratio < 0.55:
                selected = min(selected, 2)
        return max(self.min_batch_size, min(selected, self.max_candidates))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceJuryBatchPlan:
    candidate_count: int
    selected_batch_size: int
    batches: list[dict[str, Any]]
    preflight: dict[str, Any]
    input_hash: str
    batch_plan_hash: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceJuryCandidateResult:
    candidate_id: str
    batch_id: str
    deterministic_assessment: dict[str, Any]
    jury_assessment: dict[str, Any]
    final_assessment: dict[str, Any]
    native_latent_verified: bool
    fallback: bool
    errors: list[dict[str, Any]] = field(default_factory=list)
    human_review_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceJuryBatchResult:
    batch_id: str
    candidate_ids: list[str]
    status: str
    native_latent_verified: bool
    fallback: bool
    duration_ms: int
    cleanup: dict[str, Any]
    candidates: list[SourceJuryCandidateResult] = field(default_factory=list)
    error_type: str | None = None
    error: str | None = None
    retry_of: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["candidates"] = [item.to_dict() for item in self.candidates]
        return data


@dataclass
class SourceJuryAggregateResult:
    status: str
    candidate_count: int
    completed_count: int
    failed_count: int
    batch_count: int
    native_latent_verified: bool
    native_latent_verified_all_batches: bool
    fallback: bool
    rounds: int
    partial: bool
    binding_count: int
    supporting_count: int
    discovery_only_count: int
    rejected_count: int
    human_review_count: int
    oom_count: int
    retry_count: int
    total_duration_ms: int
    peak_vram_bytes: int
    average_vram_bytes: int
    model_config_hash: str
    batch_plan: dict[str, Any]
    batches: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    preflight: dict[str, Any]
    aggregate_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SourceJuryBatchScheduler:
    def __init__(
        self,
        policy: SourceJuryVramPolicy | None = None,
        *,
        memory_probe: Callable[[], dict[str, Any]] | None = None,
        cleanup_probe: Callable[[], list[str]] | None = None,
    ) -> None:
        self.policy = policy or SourceJuryVramPolicy.from_env()
        self.memory_probe = memory_probe or probe_gpu_memory
        self.cleanup_probe = cleanup_probe or _worker_processes

    def plan(self, candidates: list[dict[str, Any]]) -> SourceJuryBatchPlan:
        limited = candidates[: self.policy.max_candidates]
        memory = self.memory_probe()
        selected = self.policy.selected_batch_size(memory)
        batches = []
        for index, start in enumerate(range(0, len(limited), selected), start=1):
            end = min(start + selected, len(limited))
            rows = limited[start:end]
            batches.append(
                {
                    "batch_id": f"batch-{index:02d}",
                    "candidate_start": start,
                    "candidate_end": end - 1,
                    "candidate_ids": [_candidate_id(row, start + offset) for offset, row in enumerate(rows)],
                    "size": len(rows),
                }
            )
        input_hash = _stable_hash(limited)
        plan_payload = {"candidate_count": len(limited), "selected_batch_size": selected, "batches": batches}
        return SourceJuryBatchPlan(
            candidate_count=len(limited),
            selected_batch_size=selected,
            batches=batches,
            preflight={
                **memory,
                "recommended_batch_size": self.policy.selected_batch_size(memory),
                "selected_batch_size": selected,
                "candidate_count": len(limited),
                "batch_count": len(batches),
            },
            input_hash=input_hash,
            batch_plan_hash=_stable_hash(plan_payload),
        )

    def run(
        self,
        candidates: list[dict[str, Any]],
        *,
        runner: Callable[[list[dict[str, Any]], str], dict[str, Any]],
        audit_dir: str | Path | None = None,
    ) -> SourceJuryAggregateResult:
        started = time.monotonic()
        limited = candidates[: self.policy.max_candidates]
        plan = self.plan(limited)
        audit = Path(audit_dir) if audit_dir else None
        if audit:
            audit.mkdir(parents=True, exist_ok=True)
            _write_json(audit / "jury-run.json", {"policy": self.policy.to_dict(), "candidate_count": len(limited)})
            _write_json(audit / "batch-plan.json", plan.to_dict())
        batch_results: list[SourceJuryBatchResult] = []
        oom_count = 0
        retry_count = 0
        index = 0
        batch_sequence = 1
        current_size = plan.selected_batch_size
        while index < len(limited):
            rows = limited[index : index + current_size]
            batch_id = f"batch-{batch_sequence:02d}"
            if audit:
                _write_json(audit / f"{batch_id}-input.json", rows)
            result, is_oom = self._run_batch(batch_id, rows, runner)
            if is_oom:
                oom_count += 1
                if self.policy.oom_backoff and current_size > self.policy.min_batch_size:
                    retry_count += 1
                    cleanup = self.cleanup(batch_id, audit)
                    result.cleanup = cleanup
                    if audit:
                        _write_json(audit / f"{batch_id}-result.json", result.to_dict())
                    batch_results.append(result)
                    current_size = self._backoff_size(current_size)
                    batch_sequence += 1
                    continue
                if len(rows) == 1:
                    result = self._single_candidate_oom(batch_id, rows[0], result)
            result.cleanup = self.cleanup(batch_id, audit)
            if audit:
                _write_json(audit / f"{batch_id}-result.json", result.to_dict())
            batch_results.append(result)
            index += len(rows)
            batch_sequence += 1
        aggregate = self._aggregate(plan, batch_results, started, oom_count, retry_count)
        if audit:
            _write_json(audit / "aggregate-result.json", aggregate.to_dict())
            (audit / "worker-check.txt").write_text("\n".join(self.cleanup_probe()) + "\n", encoding="utf-8")
        return aggregate

    def _run_batch(
        self,
        batch_id: str,
        rows: list[dict[str, Any]],
        runner: Callable[[list[dict[str, Any]], str], dict[str, Any]],
    ) -> tuple[SourceJuryBatchResult, bool]:
        started = time.monotonic()
        candidate_ids = [_candidate_id(row, offset) for offset, row in enumerate(rows)]
        try:
            out = runner(rows, batch_id)
        except Exception as exc:
            is_oom = _is_oom_error(type(exc).__name__, str(exc))
            return (
                SourceJuryBatchResult(
                    batch_id=batch_id,
                    candidate_ids=candidate_ids,
                    status="cuda_oom" if is_oom else "worker_error",
                    native_latent_verified=False,
                    fallback=False,
                    duration_ms=_duration_ms(started),
                    cleanup={},
                    error_type=type(exc).__name__,
                    error=str(exc),
                ),
                is_oom,
            )
        native = out.get("native_result") or {}
        status = str(native.get("status") or out.get("status") or "completed")
        err_type = str(native.get("error_type") or native.get("raw", {}).get("error_type") or "")
        err = str(native.get("error") or native.get("raw", {}).get("error") or native.get("answer") or "")
        is_oom = _is_oom_error(err_type, err) or "oom" in status.lower()
        candidate_results = []
        for row in out.get("deterministic_results") or []:
            final = _final_with_hard_gate(row)
            candidate_results.append(
                SourceJuryCandidateResult(
                    candidate_id=str(row.get("candidate_id") or ""),
                    batch_id=batch_id,
                    deterministic_assessment=row.get("deterministic_assessment") or {},
                    jury_assessment=row.get("jury_assessment") or {},
                    final_assessment=final,
                    native_latent_verified=bool(out.get("native_latent_verified")),
                    fallback=bool(out.get("fallback")),
                    errors=[],
                    human_review_required=bool(row.get("human_review_required") or final.get("human_review_required")),
                )
            )
        return (
            SourceJuryBatchResult(
                batch_id=batch_id,
                candidate_ids=candidate_ids,
                status="cuda_oom" if is_oom else status,
                native_latent_verified=bool(out.get("native_latent_verified")),
                fallback=bool(out.get("fallback")),
                duration_ms=_duration_ms(started),
                cleanup={},
                candidates=candidate_results,
                error_type=err_type or None,
                error=err or None,
            ),
            is_oom,
        )

    def _single_candidate_oom(
        self,
        batch_id: str,
        row: dict[str, Any],
        result: SourceJuryBatchResult,
    ) -> SourceJuryBatchResult:
        cid = _candidate_id(row, 0)
        result.status = "native_oom_single_candidate"
        result.candidates = [
            SourceJuryCandidateResult(
                candidate_id=cid,
                batch_id=batch_id,
                deterministic_assessment=row.get("deterministic_assessment") or {},
                jury_assessment={},
                final_assessment={"recommended_use": "human_review", "binding_eligible": False},
                native_latent_verified=False,
                fallback=False,
                errors=[{"error_type": result.error_type or "CUDAOutOfMemoryError", "error": result.error or "CUDA OOM"}],
                human_review_required=True,
            )
        ]
        return result

    def cleanup(self, batch_id: str, audit_dir: Path | None = None) -> dict[str, Any]:
        started = time.monotonic()
        before = self.cleanup_probe()
        cleanup = {
            "batch_id": batch_id,
            "cleanup_started": True,
            "workers_before": before,
            "cleanup_completed": False,
        }
        gc.collect()
        try:
            torch = sys.modules.get("torch")
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        after = self.cleanup_probe()
        memory = self.memory_probe()
        cleanup.update(
            {
                "cleanup_completed": True,
                "workers_after": after,
                "allocated_vram_after": memory.get("allocated_vram_before_bytes", 0),
                "reserved_vram_after": memory.get("reserved_vram_before_bytes", 0),
                "free_vram_after": memory.get("free_vram_before_bytes", 0),
                "cleanup_duration_ms": _duration_ms(started),
            }
        )
        if audit_dir:
            _write_json(audit_dir / f"{batch_id}-cleanup.json", cleanup)
            with (audit_dir / "gpu-memory.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"batch_id": batch_id, **memory}, sort_keys=True) + "\n")
        return cleanup

    def _aggregate(
        self,
        plan: SourceJuryBatchPlan,
        batches: list[SourceJuryBatchResult],
        started: float,
        oom_count: int,
        retry_count: int,
    ) -> SourceJuryAggregateResult:
        candidate_results = [item for batch in batches for item in batch.candidates]
        completed = [item for item in candidate_results if not item.errors]
        failed = [item for item in candidate_results if item.errors]
        uses = [str(item.final_assessment.get("recommended_use") or "") for item in candidate_results]
        all_success_batches = [batch for batch in batches if batch.candidates and batch.status not in {"cuda_oom", "worker_error"}]
        native_all = bool(all_success_batches) and all(batch.native_latent_verified for batch in all_success_batches) and not failed
        vram_samples = _vram_used_samples(plan, batches)
        aggregate = SourceJuryAggregateResult(
            status="completed" if len(completed) == plan.candidate_count else "partial",
            candidate_count=plan.candidate_count,
            completed_count=len(completed),
            failed_count=len(failed),
            batch_count=len([batch for batch in batches if batch.candidates]),
            native_latent_verified=native_all,
            native_latent_verified_all_batches=native_all,
            fallback=False,
            rounds=1,
            partial=bool(failed),
            binding_count=uses.count("binding"),
            supporting_count=uses.count("supporting"),
            discovery_only_count=uses.count("discovery_only"),
            rejected_count=uses.count("reject"),
            human_review_count=sum(1 for item in candidate_results if item.human_review_required),
            oom_count=oom_count,
            retry_count=retry_count,
            total_duration_ms=_duration_ms(started),
            peak_vram_bytes=max(vram_samples) if vram_samples else 0,
            average_vram_bytes=int(sum(vram_samples) / len(vram_samples)) if vram_samples else 0,
            model_config_hash=_stable_hash(_model_config()),
            batch_plan=plan.to_dict(),
            batches=[batch.to_dict() for batch in batches],
            candidates=[item.to_dict() for item in candidate_results],
            preflight=plan.preflight,
        )
        payload = aggregate.to_dict()
        payload.pop("aggregate_hash", None)
        aggregate.aggregate_hash = _stable_hash(payload)
        return aggregate

    def _backoff_size(self, size: int) -> int:
        if size <= 3:
            return self.policy.min_batch_size
        return max(self.policy.min_batch_size, size // 2)


def probe_gpu_memory() -> dict[str, Any]:
    base = {
        "cuda_available": False,
        "gpu_name": None,
        "total_vram_bytes": 0,
        "free_vram_before_bytes": 0,
        "reserved_vram_before_bytes": 0,
        "allocated_vram_before_bytes": 0,
    }
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            free, total = torch.cuda.mem_get_info()
            return {
                "cuda_available": True,
                "gpu_name": torch.cuda.get_device_name(0),
                "total_vram_bytes": int(total),
                "free_vram_before_bytes": int(free),
                "reserved_vram_before_bytes": int(torch.cuda.memory_reserved(0)),
                "allocated_vram_before_bytes": int(torch.cuda.memory_allocated(0)),
            }
    except Exception:
        pass
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            name, total_mb, free_mb = [part.strip() for part in proc.stdout.splitlines()[0].split(",")[:3]]
            return {
                **base,
                "cuda_available": True,
                "gpu_name": name,
                "total_vram_bytes": int(total_mb) * 1024 * 1024,
                "free_vram_before_bytes": int(free_mb) * 1024 * 1024,
            }
    except Exception:
        pass
    return base


def _final_with_hard_gate(row: dict[str, Any]) -> dict[str, Any]:
    final = dict(row.get("final_assessment") or {})
    deterministic = row.get("deterministic_assessment") or {}
    if deterministic.get("deterministic_gate") is False:
        final["binding_eligible"] = False
        final["recommended_use"] = "reject" if deterministic.get("authority_level") == "D" else "discovery_only"
    return final


def _candidate_id(row: dict[str, Any], offset: int) -> str:
    return str(row.get("candidate_id") or row.get("url") or f"candidate-{offset}")


def _is_oom_error(error_type: str, error: str) -> bool:
    text = f"{error_type} {error}".lower()
    return "outofmemory" in text or "out of memory" in text or "cuda oom" in text or "cudaoutofmemory" in text


def _worker_processes() -> list[str]:
    try:
        proc = subprocess.run(["pgrep", "-af", "recursive_mas_worker.py"], check=False, capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if proc.returncode not in {0, 1}:
        return []
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _vram_used_samples(plan: SourceJuryBatchPlan, batches: list[SourceJuryBatchResult]) -> list[int]:
    samples = []
    total = int(plan.preflight.get("total_vram_bytes") or 0)
    free = int(plan.preflight.get("free_vram_before_bytes") or 0)
    if total and free:
        samples.append(max(0, total - free))
    for batch in batches:
        cleanup = batch.cleanup or {}
        total = int(plan.preflight.get("total_vram_bytes") or 0)
        free = int(cleanup.get("free_vram_after") or 0)
        allocated = int(cleanup.get("allocated_vram_after") or 0)
        if total and free:
            samples.append(max(0, total - free))
        elif allocated:
            samples.append(allocated)
    return samples


def _model_config() -> dict[str, str]:
    keys = [
        "RALFLOOP_RECURSIVE_MAS_EXECUTION_MODE",
        "RALFLOOP_RECURSIVE_MAS_STYLE",
        "RALFLOOP_RECURSIVE_MAS_ROUNDS",
        "RALFLOOP_RECURSIVE_MAS_TIMEOUT_SEC",
    ]
    return {key: os.getenv(key, "") for key in keys}


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
