from __future__ import annotations

from datetime import datetime, timedelta
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy, effective_approval_status
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from .adapter import ColibriGlmAdapter, build_review_prompt
from .context import build_context_packet, canonical_json, redact_secrets
from .director import (
    Assignment,
    DirectorDecision,
    DirectorOrchestrator,
    director_prompt,
    parse_director_decision,
    preflight_grant_review,
)
from .models import AdapterStatus, ContextPacket, GlmReview
from .normalizer import normalize_grant_review, source_draft_present
from .parser import ReviewParseError, parse_glm_review
from .queue import GlmQueue, ROME, now_rome
from .validators import build_candidate_artifact, validate_review_artifact


TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,199}$")


class GlmReviewService:
    def __init__(
        self,
        *,
        state_dir: str | Path | None = None,
        adapter: ColibriGlmAdapter | None = None,
    ) -> None:
        default_state = Path.home() / ".local/state/ralfloop/glm-review"
        self.state_dir = Path(state_dir or os.getenv("RALFLOOP_GLM_STATE_DIR", str(default_state)))
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.artifacts_dir = self.state_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.artifacts_dir.chmod(0o700)
        self.queue = GlmQueue(self.state_dir)
        self._external_adapter = adapter is not None
        self._explicit_ngen = "GLM_NGEN" in os.environ
        self._explicit_timeout = "GLM_TIMEOUT_SECONDS" in os.environ
        self.adapter = adapter or ColibriGlmAdapter(
            launcher=os.getenv("RALFLOOP_COLIBRI_LAUNCHER", "/home/sibilla-cumana/Dati/ralfloop-colibri/bin/glm-run"),
            timeout_seconds=_env_int("GLM_TIMEOUT_SECONDS", "RALFLOOP_GLM_TIMEOUT_SECONDS", 3_600, 60, 14_400),
            max_output_bytes=_env_int("RALFLOOP_GLM_MAX_OUTPUT_BYTES", None, 131_072, 1_024, 2_000_000),
            max_error_bytes=_env_int("RALFLOOP_GLM_MAX_ERROR_BYTES", None, 131_072, 1_024, 2_000_000),
            ngen=_env_int("GLM_NGEN", "RALFLOOP_GLM_NGEN", 128, 1, 2_048),
        )

    def enqueue_file(
        self,
        input_path: str | Path,
        *,
        task_type: str,
        task_id: str | None = None,
        immediate: bool = False,
        external_action: str | None = None,
    ) -> dict[str, Any]:
        path = Path(input_path)
        if not path.is_file():
            raise ValueError("input_file_missing")
        if path.stat().st_size > 2_000_000:
            raise ValueError("input_file_too_large")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("input_json_invalid") from exc
        if not isinstance(payload, dict):
            raise ValueError("input_must_be_object")
        return self.enqueue_payload(
            payload,
            task_type=task_type,
            task_id=task_id,
            immediate=immediate,
            external_action=external_action,
            source_path=str(path.resolve()),
        )

    def enqueue_payload(
        self,
        payload: dict[str, Any],
        *,
        task_type: str,
        task_id: str | None = None,
        immediate: bool = False,
        external_action: str | None = None,
        source_path: str = "memory",
    ) -> dict[str, Any]:
        if task_type not in {"grant_review", "technical_review", "other"}:
            raise ValueError("task_type_invalid")
        original_payload = payload
        if task_type == "grant_review":
            payload, normalization = normalize_grant_review(payload, source_file=source_path)
        else:
            payload = dict(payload)
            normalization = {
                "schema_version": 1,
                "source_file": source_path,
                "input_keys": sorted(str(key) for key in original_payload),
                "selected_source_path": "$",
                "selected_keys": sorted(str(key) for key in original_payload),
                "normalized_keys": sorted(str(key) for key in payload),
                "fields_preserved": sorted(str(key) for key in payload),
                "fields_derived": {},
                "fields_missing": [],
                "warnings": [],
                "mapping_error": False,
                "source_hashes": {
                    "input_sha256": _hash_json(original_payload),
                    "selected_sha256": _hash_json(payload),
                    "normalized_sha256": _hash_json(payload),
                },
            }
        task_id = task_id or _derived_task_id(task_type, payload)
        if not TASK_ID_RE.fullmatch(task_id):
            raise ValueError("task_id_invalid")
        existing = self.queue.get(task_id)
        if existing:
            existing["idempotent"] = True
            return existing
        task_dir = self._task_dir(task_id)
        input_artifact = task_dir / "input.json"
        packet_artifact = task_dir / "context-packet.json"
        configured_limit = _env_int("RALFLOOP_GLM_CONTEXT_MAX_CHARS", None, 6_000, 2_000, 20_000)
        context_limit = configured_limit
        packet, metadata = build_context_packet(
            payload,
            task_id=task_id,
            task_type=task_type,
            max_chars=context_limit,
            top_k=50 if task_type == "grant_review" else 8,
        )
        mapping_error = bool(normalization.get("mapping_error")) or (
            source_draft_present(original_payload) and not packet.draft.strip()
        )
        if mapping_error and "input_mapping_error:source_draft_became_empty" not in normalization["warnings"]:
            normalization["warnings"].append("input_mapping_error:source_draft_became_empty")
        normalization["mapping_error"] = mapping_error
        normalization["source_hashes"]["context_packet_sha256"] = metadata["sha256"]
        source = {
            "schema_version": 1,
            "source": {
                "kind": "local_authorized",
                "path": source_path,
                "sha256": _hash_json(original_payload),
                "selected_source_path": normalization["selected_source_path"],
                "normalized_sha256": normalization["source_hashes"]["normalized_sha256"],
                "captured_at": now_rome(),
            },
            "payload": _sanitize_payload(payload),
        }
        _write_json(input_artifact, source)
        _write_json(packet_artifact, packet.model_dump(mode="json"))
        _write_json(task_dir / "context-metadata.json", metadata)
        _write_json(task_dir / "normalization-report.json", normalization)
        state = "invalid_input" if mapping_error else "queued" if immediate else "waiting_night_window"
        result = self.queue.enqueue(
            {
                "task_id": task_id,
                "task_type": task_type,
                "state": state,
                "priority": _priority(payload),
                "deadline": payload.get("deadline"),
                "input_artifact": str(input_artifact),
                "packet_artifact": str(packet_artifact),
                "packet_hash": metadata["sha256"],
                "external_action": external_action or payload.get("external_action"),
                "ngen_override": self.adapter.ngen if self._explicit_ngen and isinstance(self.adapter, ColibriGlmAdapter) else None,
                "timeout_override": self.adapter.timeout_seconds if self._explicit_timeout and isinstance(self.adapter, ColibriGlmAdapter) else None,
                "last_error": "input_mapping_error" if mapping_error else None,
            }
        )
        result["context"] = metadata
        result["normalization"] = normalization
        return result

    def process_one(self, *, force: bool = False) -> dict[str, Any]:
        with self.queue.worker_lock() as acquired:
            if not acquired:
                return {"status": "deferred_resource_busy", "reason": "worker_lock_busy"}
            return self._process_one_locked(force=force)

    def process(self, *, force: bool = False, limit: int = 1) -> list[dict[str, Any]]:
        with self.queue.worker_lock() as acquired:
            if not acquired:
                return [{"status": "deferred_resource_busy", "reason": "worker_lock_busy"}]
            output = []
            for _ in range(max(1, min(limit, 10))):
                result = self._process_one_locked(force=force)
                output.append(result)
                if result.get("status") in {"idle", "deferred_resource_busy"}:
                    break
            return output

    def process_task(self, task_id: str, *, force: bool = False) -> dict[str, Any]:
        if not TASK_ID_RE.fullmatch(task_id):
            raise ValueError("task_id_invalid")
        current = self.queue.get(task_id)
        if not current:
            return {"status": "not_found", "task_id": task_id}
        if current["state"] == "running":
            return {"status": "task_already_running", "task_id": task_id, "state": current["state"]}
        with self.queue.worker_lock() as acquired:
            if not acquired:
                return {"status": "deferred_resource_busy", "reason": "worker_lock_busy", "task_id": task_id}
            self.queue.recover_running()
            self.recover_orphan_artifacts()
            job = self.queue.claim_task(task_id, force=force)
            if not job:
                current = self.queue.get(task_id)
                if not current:
                    return {"status": "not_found", "task_id": task_id}
                if current["state"] == "running":
                    return {"status": "task_already_running", "task_id": task_id, "state": current["state"]}
                return {"status": "task_not_claimable", "task_id": task_id, "state": current["state"]}
            return self._run_job(job)

    def _process_one_locked(self, *, force: bool) -> dict[str, Any]:
        self.queue.recover_running()
        self.recover_orphan_artifacts()
        job = self.queue.claim_next(force=force)
        if not job:
            return {"status": "idle", "paused": self.queue.is_paused()}
        return self._run_job(job)

    def _run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        task_id = str(job["task_id"])
        run_token = str(job["run_token"])
        source_wrapper = json.loads(Path(job["input_artifact"]).read_text(encoding="utf-8"))
        source_payload = source_wrapper.get("payload") if isinstance(source_wrapper, dict) else {}
        source_payload = source_payload if isinstance(source_payload, dict) else {}
        packet_payload = json.loads(Path(job["packet_artifact"]).read_text(encoding="utf-8"))
        if source_draft_present(source_payload) and not str(packet_payload.get("draft") or "").strip():
            report_path = self._task_dir(task_id) / "normalization-report.json"
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _, report = normalize_grant_review(source_payload, source_file=str(job["input_artifact"]))
            report["mapping_error"] = True
            warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
            if "input_mapping_error:source_draft_became_empty" not in warnings:
                warnings.append("input_mapping_error:source_draft_became_empty")
            report["warnings"] = warnings
            _write_json(report_path, report)
            envelope = {
                "ok": False,
                "status": "invalid_input",
                "error": "input_mapping_error",
                "model_started": False,
                "result": {},
                "normalization_report": str(report_path),
            }
            return self.queue.finish(
                task_id,
                run_token=run_token,
                provider_status="invalid_input",
                envelope=envelope,
                generation_started=False,
                error="input_mapping_error",
            )
        packet = ContextPacket.model_validate(packet_payload)
        adapter = self._adapter_for_job(job)
        if (
            job.get("task_type") == "grant_review"
            and isinstance(adapter, ColibriGlmAdapter)
            and os.getenv("RALFLOOP_GLM_DIRECTOR_ENABLED", "1") == "1"
        ):
            return self._run_director_job(job, packet, adapter)
        attempt_dir = self._task_dir(task_id) / f"attempt-{int(job['attempts']) + 1:02d}-{run_token[:8]}"
        self.queue.attach_attempt_artifact(task_id, run_token, str(attempt_dir))
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(task_id, run_token, stop_heartbeat),
            name=f"glm-heartbeat-{task_id[:20]}",
            daemon=True,
        )
        heartbeat.start()
        try:
            if isinstance(adapter, ColibriGlmAdapter):
                envelope = adapter.review(
                    packet,
                    attempt_dir,
                    on_process_start=lambda pid: self.queue.register_process(task_id, run_token, pid),
                )
            else:
                envelope = adapter.review(packet, attempt_dir)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2)
        envelope_data = envelope.model_dump(mode="json")
        generation_started = bool(envelope.model_started) if isinstance(adapter, ColibriGlmAdapter) else envelope.status != AdapterStatus.DEFERRED_RESOURCE_BUSY
        self._ensure_attempt_artifacts(attempt_dir, packet, envelope_data)
        if envelope.status != AdapterStatus.COMPLETED:
            finished = self.queue.finish(
                task_id,
                run_token=run_token,
                provider_status=str(envelope.status),
                envelope=envelope_data,
                generation_started=generation_started,
                error=envelope.error,
            )
            if finished.get("status") == "stale_run_token":
                self._mark_stale_envelope(attempt_dir)
            return finished
        review = GlmReview.model_validate(envelope.result)
        return self._finalize_review(job, packet, review, envelope_data, attempt_dir=attempt_dir, generation_started=generation_started)

    def _run_director_job(
        self,
        job: dict[str, Any],
        packet: ContextPacket,
        adapter: ColibriGlmAdapter,
    ) -> dict[str, Any]:
        task_id = str(job["task_id"])
        run_token = str(job["run_token"])
        source_wrapper = json.loads(Path(job["input_artifact"]).read_text(encoding="utf-8"))
        payload = source_wrapper.get("payload") if isinstance(source_wrapper, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        generation_count = 0
        director_attempts: list[tuple[Path, dict[str, Any]]] = []
        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(task_id, run_token, stop_heartbeat),
            name=f"glm-director-heartbeat-{task_id[:20]}",
            daemon=True,
        )
        heartbeat.start()

        class DirectorAdapterFailure(RuntimeError):
            def __init__(self, envelope: Any, directory: Path) -> None:
                super().__init__(str(envelope.error or envelope.status))
                self.envelope = envelope
                self.directory = directory

        def call_director(strategic_packet: dict[str, Any]) -> DirectorDecision:
            nonlocal generation_count
            number = int(strategic_packet["round"])
            directory = self._task_dir(task_id) / f"attempt-{int(job['attempts']) + generation_count + 1:02d}-{run_token[:8]}-director-{number:02d}"
            self.queue.attach_attempt_artifact(task_id, run_token, str(directory))
            envelope = adapter.review(
                packet,
                directory,
                on_process_start=lambda pid: self.queue.register_process(task_id, run_token, pid),
                prompt_override=director_prompt(
                    strategic_packet,
                    max_assignments=_env_int("RALFLOOP_GLM_MAX_ASSIGNMENTS", None, 5, 1, 20),
                ),
                result_parser=parse_director_decision,
            )
            envelope_data = envelope.model_dump(mode="json")
            director_attempts.append((directory, envelope_data))
            if envelope.model_started:
                generation_count += 1
            if envelope.status != AdapterStatus.COMPLETED:
                raise DirectorAdapterFailure(envelope, directory)
            return DirectorDecision.model_validate(envelope.result)

        workers = self._director_workers(payload)
        orchestrator = DirectorOrchestrator(
            self.queue,
            call_director,
            workers,
            model_configuration=f"{adapter.model}:ngen={adapter.ngen}:timeout={adapter.timeout_seconds}",
        )
        try:
            result = orchestrator.run(
                task_id=task_id,
                task_type="grant_review",
                payload=payload,
                max_rounds=min(
                    _env_int("RALFLOOP_GLM_MAX_ROUNDS", None, 3, 1, 10),
                    max(1, self.queue.max_attempts - int(job["attempts"])),
                ),
                max_assignments=_env_int("RALFLOOP_GLM_MAX_ASSIGNMENTS", None, 5, 1, 20),
                time_budget_seconds=_env_int("RALFLOOP_GLM_DIRECTOR_TIME_BUDGET_SECONDS", None, 7_200, 60, 14_400),
                worker_call_budget=_env_int("RALFLOOP_GLM_WORKER_CALL_BUDGET", None, 15, 1, 100),
                retrospective=bool(payload.get("retrospective_review")),
                approval=False,
            )
        except DirectorAdapterFailure as exc:
            stop_heartbeat.set()
            heartbeat.join(timeout=2)
            finished = self.queue.finish(
                task_id,
                run_token=run_token,
                provider_status=str(exc.envelope.status),
                envelope=exc.envelope.model_dump(mode="json"),
                generation_started=generation_count,
                error=exc.envelope.error,
            )
            if finished.get("status") == "stale_run_token":
                self._mark_stale_envelope(exc.directory)
            return finished
        except (ValueError, OSError, json.JSONDecodeError) as exc:
            directory = director_attempts[-1][0] if director_attempts else self._task_dir(task_id) / f"claim-{run_token[:8]}-director-error"
            envelope = _director_envelope(
                directory,
                adapter,
                {"status": "director_validation_failed", "error": f"{type(exc).__name__}:{exc}"},
                generation_count,
            )
            self._ensure_attempt_artifacts(directory, packet, envelope)
            return self.queue.finish(
                task_id,
                run_token=run_token,
                provider_status="invalid_output",
                envelope=envelope,
                generation_started=generation_count,
                error=f"director_validation_failed:{type(exc).__name__}:{str(exc)[:300]}",
            )
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=2)

        _write_json(self._task_dir(task_id) / "director-result.json", result)
        if result["status"] not in {"conclude", "skipped"}:
            directory = director_attempts[-1][0] if director_attempts else self._task_dir(task_id) / f"claim-{run_token[:8]}-director"
            aggregate = director_attempts[-1][1] if director_attempts else _director_envelope(directory, adapter, result, generation_count)
            self._ensure_attempt_artifacts(directory, packet, aggregate)
            return self.queue.finish(
                task_id,
                run_token=run_token,
                provider_status="invalid_output",
                envelope=aggregate,
                generation_started=generation_count,
                error=f"director_{result['status']}",
            )
        review = _compose_director_review(task_id, result)
        directory = director_attempts[-1][0] if director_attempts else self._task_dir(task_id) / f"claim-{run_token[:8]}-deterministic"
        aggregate = _director_envelope(directory, adapter, result, generation_count, review=review)
        self._ensure_attempt_artifacts(directory, packet, aggregate)
        return self._finalize_review(
            job,
            packet,
            review,
            aggregate,
            attempt_dir=directory,
            generation_started=generation_count,
        )

    def _director_workers(self, payload: dict[str, Any]) -> dict[str, Any]:
        preflight = preflight_grant_review(payload).model_dump(mode="json")

        def rules(item: Assignment, context: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "deliverable": preflight, "artifacts": [], "verified_facts": preflight["verified_facts"], "metrics": {"tool_ms": 0}}

        def retrieval(item: Assignment, context: dict[str, Any]) -> dict[str, Any]:
            evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
            refs = [str(row.get("source_id")) for row in evidence if isinstance(row, dict) and row.get("source_id")]
            return {"ok": bool(refs), "deliverable": {"evidence_refs": refs}, "artifacts": refs, "metrics": {"tool_ms": 0}}

        def bounded(item: Assignment, context: dict[str, Any]) -> dict[str, Any]:
            return {"ok": True, "deliverable": item.deliverable, "artifacts": [], "metrics": {"tool_ms": 0}}

        def unavailable(item: Assignment, context: dict[str, Any]) -> dict[str, Any]:
            return {"ok": False, "status": "executor_requires_explicit_runtime", "worker": item.worker, "metrics": {"tool_ms": 0}}

        return {
            "rules_engine": rules,
            "retrieval_worker": retrieval,
            "reviewer": bounded,
            "writer": bounded,
            "judge": bounded,
            "web_tool_agent": unavailable,
            "document_worker": unavailable,
            "coder": unavailable,
        }

    def _finalize_review(
        self,
        job: dict[str, Any],
        packet: ContextPacket,
        review: GlmReview,
        envelope_data: dict[str, Any],
        *,
        attempt_dir: Path,
        generation_started: bool,
    ) -> dict[str, Any]:
        task_id = str(job["task_id"])
        source_wrapper = json.loads(Path(job["input_artifact"]).read_text(encoding="utf-8"))
        source_payload = source_wrapper.get("payload") if isinstance(source_wrapper, dict) else {}
        validation = validate_review_artifact(packet, review, source_payload, artifact_version=int(job["artifact_version"]))
        validation_path = attempt_dir / "validation.json"
        result_path = attempt_dir / f"candidate-v{int(job['artifact_version']):03d}.json"
        candidate = build_candidate_artifact(
            packet,
            review,
            validation,
            artifact_version=int(job["artifact_version"]),
            source_payload=source_payload if isinstance(source_payload, dict) else {},
        )
        _write_json(validation_path, validation.model_dump(mode="json"))
        _write_json(result_path, candidate)
        result_hash = _sha256_file(result_path)
        finished = self.queue.finish(
            task_id,
            run_token=str(job["run_token"]),
            provider_status="completed",
            envelope=envelope_data,
            generation_started=generation_started,
            result_artifact=str(result_path),
            result_hash=result_hash,
            validation_artifact=str(validation_path),
            error=None if validation.ok else "deterministic_validation_failed",
        )
        if finished.get("status") == "stale_run_token":
            self._mark_stale_envelope(attempt_dir)
            return finished
        if finished["state"] == "awaiting_user_approval":
            finished = self._request_telegram_approval(finished)
        return finished

    def retry(self, task_id: str) -> dict[str, Any]:
        job = self.queue.get(task_id)
        if not job:
            return {"status": "not_found", "task_id": task_id}
        if job.get("run_token"):
            return self._queue_retry(task_id)
        if job["state"] != "invalid_output":
            return self._queue_retry(task_id)
        with self.queue.worker_lock() as acquired:
            if not acquired:
                return {"status": "deferred_resource_busy", "reason": "worker_lock_busy", "task_id": task_id}
            job = self.queue.get(task_id) or job
            prior_artifact = str((job.get("envelope") or {}).get("prompt_artifact") or "")
            try:
                attempt_dir = Path(prior_artifact).parent if prior_artifact else self._latest_attempt_dir(task_id)
            except OSError:
                return self._queue_retry(task_id)
            stdout_path = attempt_dir / "stdout.txt"
            try:
                review = parse_glm_review(
                    stdout_path.read_bytes(),
                    max_bytes=int(os.getenv("RALFLOOP_GLM_MAX_OUTPUT_BYTES", "131072")),
                )
                if review.task_id != task_id:
                    raise ReviewParseError("task_id_mismatch")
            except (OSError, ReviewParseError):
                return self._queue_retry(task_id)
            running = self.queue.prepare_reparse(task_id)
            if not running:
                return {"status": "reparse_state_changed", "task_id": task_id}
            parsed_path = attempt_dir / "reparsed-result.json"
            _write_json(parsed_path, review.model_dump(mode="json"))
            envelope = dict(job.get("envelope") or {})
            envelope.update(
                {
                    "ok": True,
                    "provider": "colibri_glm",
                    "model": "glm-5.2-colibri",
                    "status": "completed",
                    "result": review.model_dump(mode="json"),
                    "result_artifact": str(parsed_path),
                    "error": None,
                }
            )
            self.queue.audit("invalid_output_reparsed", task_id, attempt=running["attempts"])
            return self._finalize_review(
                running,
                ContextPacket.model_validate_json(Path(running["packet_artifact"]).read_text(encoding="utf-8")),
                review,
                envelope,
                attempt_dir=attempt_dir,
                generation_started=False,
            )

    def status(self, task_id: str) -> dict[str, Any]:
        job = self.queue.get(task_id)
        if not job:
            return {"status": "not_found", "task_id": task_id}
        return self._sync_telegram_approval(job)

    def list(self, *, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return [self._sync_telegram_approval(job) for job in self.queue.list(state=state, limit=limit)]

    def result(self, task_id: str) -> dict[str, Any]:
        job = self.queue.get(task_id)
        if not job:
            return {"status": "not_found", "task_id": task_id}
        path = job.get("result_artifact")
        if not path or not Path(path).is_file():
            return {"status": "result_unavailable", "task_id": task_id, "state": job["state"]}
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def digest(self, *, dry_run: bool = True, emit_outbox: bool = False) -> dict[str, Any]:
        jobs = self.list(limit=500)
        cutoff = datetime.now(ROME) - timedelta(hours=24)
        recent = [job for job in jobs if _parse_time(job["updated_at"]) >= cutoff]
        awaiting = [job for job in jobs if job["state"] == "awaiting_user_approval"]
        upcoming = sorted(
            [job for job in jobs if job.get("deadline") and job["state"] not in {"cancelled", "rejected", "dispatched"}],
            key=lambda item: str(item["deadline"]),
        )[:10]
        critical = 0
        missing = 0
        suggestions = 0
        for job in recent:
            result = self.result(job["task_id"])
            review = result.get("glm_review") if isinstance(result, dict) else None
            if isinstance(review, dict):
                critical += sum(1 for issue in review.get("critical_issues") or [] if issue.get("severity") == "high")
                missing += len(review.get("missing_evidence") or [])
                suggestions += len(review.get("recommended_changes") or [])
        lines = [
            "# Ralf GLM digest",
            "",
            f"Generated: {now_rome()}",
            f"Completed/reviewed (24h): {sum(job['state'] in {'completed','awaiting_user_approval','approved','dispatched'} for job in recent)}",
            f"Critical issues: {critical}",
            f"Missing evidence: {missing}",
            f"Suggested changes: {suggestions}",
            f"Deferred/failed: {sum(job['state'] in {'deferred_resource_busy','timeout','invalid_output','failed'} for job in recent)}",
            f"Awaiting approval: {len(awaiting)}",
            "",
            "## Approval-bound drafts",
        ]
        for job in awaiting:
            request_id = job.get("approval_request_id") or "not-created"
            lines.append(
                f"- task={job['task_id']} hash={job['result_hash']} action={job['external_action']} "
                f"approval_request={request_id} command=rl:approval {request_id}"
            )
        lines.extend(["", "## Upcoming deadlines"])
        for job in upcoming:
            lines.append(f"- {job['deadline']} {job['task_id']}")
        text = "\n".join(lines).rstrip() + "\n"
        digest_dir = self.state_dir / "digests"
        digest_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = digest_dir / f"digest-{datetime.now(ROME).strftime('%Y%m%d-%H%M%S')}.md"
        _write_bytes(path, text.encode("utf-8"))
        outbox = None
        if emit_outbox and not dry_run:
            outbox_path = Path(os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", str(self.state_dir / "telegram-outbox.jsonl")))
            outbox_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with outbox_path.open("a", encoding="utf-8") as handle:
                os.chmod(handle.fileno(), 0o600)
                request_id = "glm_digest_" + datetime.now(ROME).strftime("%Y%m%d")
                handle.write(json.dumps({"status": "queued", "kind": "glm_digest", "request_id": request_id, "created_at": now_rome(), "message": text}, ensure_ascii=False, sort_keys=True) + "\n")
            outbox = str(outbox_path)
        return {"status": "dry_run" if dry_run else "created", "artifact": str(path), "outbox": outbox, "message": text, "metrics": self.queue.metrics()}

    def approve(self, task_id: str, *, approver: str = "local-user", channel: str = "cli") -> dict[str, Any]:
        return self.queue.decide(task_id, "approved", approver=approver, channel=channel)

    def reject(self, task_id: str, *, approver: str = "local-user", channel: str = "cli", reason: str = "") -> dict[str, Any]:
        return self.queue.decide(task_id, "rejected", approver=approver, channel=channel, reason=reason)

    def _task_dir(self, task_id: str) -> Path:
        path = self.artifacts_dir / task_id
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    def _latest_attempt_dir(self, task_id: str) -> Path:
        rows = sorted(path for path in self._task_dir(task_id).glob("attempt-*") if path.is_dir())
        if not rows:
            raise OSError("attempt_artifact_missing")
        return rows[-1]

    def _adapter_for_job(self, job: dict[str, Any]) -> Any:
        if self._external_adapter or not isinstance(self.adapter, ColibriGlmAdapter):
            return self.adapter
        ngen = int(job.get("ngen_override") or self.adapter.ngen)
        timeout = int(job.get("timeout_override") or self.adapter.timeout_seconds)
        if job.get("task_type") == "grant_review":
            if not self._explicit_ngen and not job.get("ngen_override"):
                ngen = max(256, ngen)
            if not self._explicit_timeout and not job.get("timeout_override"):
                timeout = max(7_200, timeout)
        return ColibriGlmAdapter(
            launcher=self.adapter.launcher,
            timeout_seconds=timeout,
            max_output_bytes=self.adapter.max_output_bytes,
            max_error_bytes=self.adapter.max_error_bytes,
            ngen=ngen,
            outer_grace_seconds=self.adapter.outer_grace_seconds,
        )

    def _queue_retry(self, task_id: str) -> dict[str, Any]:
        return self.queue.retry(
            task_id,
            ngen_override=self.adapter.ngen if self._explicit_ngen and isinstance(self.adapter, ColibriGlmAdapter) else None,
            timeout_override=self.adapter.timeout_seconds if self._explicit_timeout and isinstance(self.adapter, ColibriGlmAdapter) else None,
        )

    def _heartbeat_loop(self, task_id: str, run_token: str, stop: threading.Event) -> None:
        while not stop.wait(10):
            if not self.queue.heartbeat(task_id, run_token):
                return

    def _ensure_attempt_artifacts(
        self,
        attempt_dir: Path,
        packet: ContextPacket,
        envelope: dict[str, Any],
    ) -> None:
        attempt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        defaults = {
            "prompt.txt": build_review_prompt(packet).encode("utf-8"),
            "stdout.txt": (json.dumps(envelope.get("result") or {}, ensure_ascii=False, sort_keys=True) + "\n").encode(),
            "stderr.txt": ((str(envelope.get("error") or "")) + "\n").encode(),
            "result.json": (json.dumps(envelope.get("result") or {}, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
            "envelope.json": (json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
        }
        for name, data in defaults.items():
            if not (attempt_dir / name).exists():
                _write_bytes(attempt_dir / name, data)

    def _mark_stale_envelope(self, attempt_dir: Path) -> None:
        path = attempt_dir / "envelope.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {"status": "orphaned_attempt"}
        payload.update({"orphan": True, "stale": True, "finalization": "stale_run_token"})
        _write_json(path, payload)

    def recover_orphan_artifacts(self) -> int:
        recovered = 0
        with self.queue.connect() as conn:
            finalized_without_db = conn.execute(
                """select a.task_id,a.artifact_dir from glm_attempts a
                   join glm_jobs j on j.task_id=a.task_id
                   where a.status='orphaned_attempt' and j.state='orphaned_attempt'
                     and j.run_token is null and a.artifact_dir is not null
                   order by a.claimed_at desc"""
            ).fetchall()
        for row in finalized_without_db:
            attempt_dir = Path(row["artifact_dir"])
            envelope_path = attempt_dir / "envelope.json"
            if not envelope_path.is_file() or _attempt_process_alive(attempt_dir):
                continue
            try:
                envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            diagnostic = dict(envelope)
            diagnostic.update({"ok": False, "status": "invalid_output", "orphan": True, "error": "database_finalization_missing_reparse_available"})
            self.queue.record_orphan(
                str(row["task_id"]),
                artifact_dir=str(attempt_dir),
                status="invalid_output",
                envelope=diagnostic,
            )
            recovered_job = self.queue.get(str(row["task_id"]))
            if recovered_job and not recovered_job.get("external_action"):
                try:
                    review = parse_glm_review((attempt_dir / "stdout.txt").read_bytes())
                    if review.task_id != row["task_id"]:
                        raise ReviewParseError("task_id_mismatch")
                    running = self.queue.prepare_reparse(str(row["task_id"]))
                    if running:
                        recovered_envelope = dict(diagnostic)
                        recovered_envelope.update({"ok": True, "status": "completed", "result": review.model_dump(mode="json"), "error": None})
                        self._finalize_review(
                            running,
                            ContextPacket.model_validate_json(Path(running["packet_artifact"]).read_text(encoding="utf-8")),
                            review,
                            recovered_envelope,
                            attempt_dir=attempt_dir,
                            generation_started=False,
                        )
                except (OSError, ReviewParseError, ValueError):
                    pass
            recovered += 1
        for task_dir in sorted(path for path in self.artifacts_dir.iterdir() if path.is_dir()):
            for attempt_dir in sorted(path for path in task_dir.glob("attempt-*") if path.is_dir()):
                envelope_path = attempt_dir / "envelope.json"
                if envelope_path.exists():
                    continue
                prompt = attempt_dir / "prompt.txt"
                stdout = _promote_running(attempt_dir / "stdout.txt")
                stderr = _promote_running(attempt_dir / "stderr.txt")
                if not prompt.exists() or not stdout.exists() or not stderr.exists() or _attempt_process_alive(attempt_dir):
                    continue
                raw = stdout.read_bytes()
                try:
                    parse_glm_review(raw)
                    status = "orphaned_attempt"
                    error = "process_ended_before_database_finalization"
                except ReviewParseError as exc:
                    status = "invalid_output"
                    error = f"orphan_recovery:{exc}"
                result_path = attempt_dir / "result.json"
                if not result_path.exists():
                    _write_json(result_path, {})
                envelope = {
                    "ok": False,
                    "provider": "colibri_glm",
                    "model": "glm-5.2-colibri",
                    "status": status,
                    "exit_code": None,
                    "duration_ms": 0,
                    "result": {},
                    "prompt_artifact": str(prompt),
                    "result_artifact": str(result_path),
                    "launcher_log": "",
                    "error": error,
                    "ngen": None,
                    "timeout_seconds": None,
                    "model_started": True,
                    "orphan": True,
                    "stale": False,
                }
                _write_json(envelope_path, envelope)
                self.queue.record_orphan(
                    task_dir.name,
                    artifact_dir=str(attempt_dir),
                    status=status,
                    envelope=envelope,
                )
                recovered += 1
        return recovered

    def _request_telegram_approval(self, job: dict[str, Any]) -> dict[str, Any]:
        policy = _glm_approval_policy()
        if not policy.enabled or job.get("approval_request_id"):
            return job
        scope = {
            "schema_version": 1,
            "task_id": job["task_id"],
            "artifact_hash": job["result_hash"],
            "artifact_version": job["artifact_version"],
            "external_action": job["external_action"],
            "result_artifact": job["result_artifact"],
            "packet_hash": job["packet_hash"],
        }
        try:
            store = DomainApprovalStore(policy=policy)
            created = store.create_request(
                action="dispatch_glm_artifact",
                bando_id=job["task_id"],
                version=str(job["artifact_version"]),
                scope=scope,
                requested_by="glm_review_queue",
            )
            request = created.get("request")
            if not isinstance(request, dict):
                self.queue.audit("telegram_approval_not_created", job["task_id"], status=created.get("status"))
                return job
            outbox = Path(os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", "/var/lib/ralfloop/domain-approval-outbox.jsonl"))
            outbox.parent.mkdir(parents=True, exist_ok=True)
            with outbox.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"status": "queued", "request_id": request["request_id"], "message": request["telegram_message"]}, ensure_ascii=False, sort_keys=True) + "\n")
            return self.queue.attach_approval_request(job["task_id"], str(request["request_id"]))
        except (OSError, ValueError) as exc:
            self.queue.audit("telegram_approval_not_created", job["task_id"], status=type(exc).__name__)
            return job

    def _sync_telegram_approval(self, job: dict[str, Any]) -> dict[str, Any]:
        request_id = str(job.get("approval_request_id") or "")
        if job.get("state") != "awaiting_user_approval" or not request_id:
            return job
        policy = _glm_approval_policy()
        if not policy.enabled:
            return job
        row = DomainApprovalStore(policy=policy).get_request(request_id)
        if not row:
            return job
        scope = row.get("scope") or {}
        if (
            scope.get("artifact_hash") != job.get("result_hash")
            or int(scope.get("artifact_version") or 0) != int(job.get("artifact_version") or 0)
            or scope.get("external_action") != job.get("external_action")
        ):
            self.queue.audit("telegram_approval_scope_stale", job["task_id"], approval_request_id=request_id)
            return job
        status = effective_approval_status(row)
        if status == "approved":
            return self.queue.decide(job["task_id"], "approved", approver=f"telegram:{request_id}", channel="existing-domain-approval-gate")
        if status == "rejected":
            return self.queue.decide(job["task_id"], "rejected", approver=f"telegram:{request_id}", channel="existing-domain-approval-gate")
        return job


def _derived_task_id(task_type: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(f"{task_type}\0{canonical_json(payload)}".encode("utf-8")).hexdigest()[:20]
    return f"glm-{task_type.replace('_', '-')}-{digest}"


def _hash_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_payload(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)[0]
    return value


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"))


def _write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("wb") as handle:
        os.chmod(handle.fileno(), 0o600)
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    path.chmod(0o600)
    try:
        descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _env_int(primary: str, secondary: str | None, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(primary)
    if raw is None and secondary:
        raw = os.getenv(secondary)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{primary}_invalid") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{primary}_out_of_range:{minimum}..{maximum}")
    return value


def _promote_running(path: Path) -> Path:
    if path.exists():
        return path
    running = path.with_name(path.name + ".running")
    if running.exists():
        os.replace(running, path)
        path.chmod(0o600)
    return path


def _attempt_process_alive(attempt_dir: Path) -> bool:
    path = attempt_dir / "process.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _compose_director_review(task_id: str, result: dict[str, Any]) -> GlmReview:
    blackboard = result.get("blackboard") if isinstance(result.get("blackboard"), dict) else {}
    decision = result.get("decision") if isinstance(result.get("decision"), dict) else {}
    contradictions = [str(value)[:500] for value in blackboard.get("contradictions") or []][:3]
    failed = [row for row in blackboard.get("failed_assignments") or [] if isinstance(row, dict)][:3]
    missing = [str(value)[:300] for value in blackboard.get("open_questions") or []][:5]
    summary = str(decision.get("strategic_summary") or decision.get("strategy") or result.get("reason") or result.get("status") or "Review completed")[:300]
    if not summary.strip():
        summary = "Review completed"
    recommended = []
    for row in failed:
        recommended.append({
            "target": str(row.get("task_id") or "assignment")[:500],
            "change": "Resolve failed bounded assignment",
            "reason": str((row.get("result") or {}).get("status") or "acceptance failed")[:500],
        })
    return GlmReview.model_validate({
        "schema_version": 1,
        "task_id": task_id,
        "verdict": "insufficient_evidence" if missing else "revise" if contradictions or failed else "accept",
        "summary": summary,
        "critical_issues": [
            {"severity": "high", "issue": value, "evidence_refs": []}
            for value in contradictions
        ],
        "recommended_changes": recommended,
        "missing_evidence": missing,
        "risk_flags": ["director_assignment_failed"] if failed else [],
        "confidence": 0.4 if missing else 0.75,
        "requires_human_approval": False,
    })


def _director_envelope(
    directory: Path,
    adapter: ColibriGlmAdapter,
    result: dict[str, Any],
    generation_count: int,
    *,
    review: GlmReview | None = None,
) -> dict[str, Any]:
    return {
        "ok": review is not None,
        "provider": "colibri_glm",
        "model": "glm-5.2-colibri",
        "status": "completed" if review is not None else "invalid_output",
        "exit_code": 0 if generation_count else None,
        "duration_ms": int((result.get("metrics") or {}).get("total_ms", 0)),
        "result": review.model_dump(mode="json") if review else {},
        "director_result": result,
        "prompt_artifact": str(directory / "prompt.txt"),
        "result_artifact": str(directory / "result.json"),
        "launcher_log": "",
        "error": None if review is not None else str(result.get("error") or result.get("status") or "director_failed"),
        "ngen": adapter.ngen,
        "timeout_seconds": adapter.timeout_seconds,
        "model_started": bool(generation_count),
        "orphan": False,
        "stale": False,
    }


def _priority(payload: dict[str, Any]) -> int:
    explicit = payload.get("priority")
    if isinstance(explicit, int):
        return max(-100, min(explicit, 100))
    deadline = payload.get("deadline")
    if not deadline:
        return 0
    try:
        days = (datetime.fromisoformat(str(deadline)[:10]).date() - datetime.now(ROME).date()).days
    except ValueError:
        return 0
    if days <= 7:
        return 100
    if days <= 30:
        return 50
    return 10


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value).astimezone(ROME)
    except ValueError:
        return datetime.min.replace(tzinfo=ROME)


def _glm_approval_policy() -> DomainApprovalPolicy:
    policy = DomainApprovalPolicy.from_env()
    ttl = max(3_600, min(int(os.getenv("RALFLOOP_GLM_APPROVAL_TTL_SEC", "86400")), 604_800))
    return replace(policy, ttl_sec=ttl)
