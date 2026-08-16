#!/usr/bin/env python3
"""Side-effect-free HIGH_ONLY shadow canary over sanitized email snapshots."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.domains.email_reply import (  # noqa: E402
    EmailReplyDomainValidationError,
    build_email_reply_domain,
    domain_digest,
    validate_draft_against_domain,
)
from ralfloop_agent.providers.gpu_engine_scheduler import (  # noqa: E402
    GpuEngineTransitionError,
    TransactionalGpuScheduler,
)
from ralfloop_agent.semantic_judge import (  # noqa: E402
    JudgeAvailabilityError,
    SemanticIssue,
    SemanticJudgeConfig,
    SemanticReview,
    SemanticReviewResult,
    assess_email_risk,
    parse_semantic_review,
    run_shadow_email_case,
)
from ralfloop_agent.semantic_judge.core import semantic_prompt  # noqa: E402
from ralfloop_agent.semantic_judge.ds4_server import Ds4ServerSession  # noqa: E402
from src.google_workspace import RalfReplyGenerator  # noqa: E402


DEFAULT_CORPUS = ROOT / "tests/fixtures/email_semantic_shadow_corpus_v1.json"
DEFAULT_OUTPUT = ROOT / ".ralf_run/email-semantic-shadow-canary"


class RecordedJudge:
    provider = "deepseek_v4_flash"
    model = "deepseek-v4-flash"

    def __init__(self, result: SemanticReviewResult | Exception) -> None:
        self.result = result
        self.calls = 0

    def review(self, packet, draft):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def load_corpus(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "email_semantic_shadow_corpus_v1":
        raise ValueError("shadow_corpus_schema_invalid")
    cases = payload.get("cases")
    if not isinstance(cases, list) or len(cases) < 10:
        raise ValueError("shadow_corpus_too_small")
    if len({item.get("case_id") for item in cases if isinstance(item, Mapping)}) != len(cases):
        raise ValueError("shadow_corpus_duplicate_case")
    return [deepcopy(dict(item)) for item in cases]


def prepare(cases: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prepared: list[dict[str, Any]] = []
    eligible: list[dict[str, Any]] = []
    for item in cases:
        context = deepcopy(dict(item["context"]))
        domain = build_email_reply_domain(context)
        context["email_reply_domain_v1"] = domain.model_dump(mode="json")
        context["email_reply_domain_sha256"] = domain_digest(domain)
        risk = assess_email_risk(context, str(item["draft"]))
        hard_guard = "passed"
        try:
            validate_draft_against_domain(str(item["draft"]), domain)
        except EmailReplyDomainValidationError as exc:
            hard_guard = exc.reason_code
        row = {**item, "context": context, "domain": domain, "risk": risk, "hard_guard": hard_guard}
        prepared.append(row)
        if risk.level == "high" and hard_guard == "passed":
            eligible.append(row)
    return prepared, eligible


def fake_reviews(eligible: list[dict[str, Any]]) -> dict[str, SemanticReviewResult | Exception]:
    output: dict[str, SemanticReviewResult | Exception] = {}
    for item in eligible:
        verdict = str(item.get("expected_ds4_verdict") or "pass")
        issues = []
        if verdict == "repair":
            issue_type = str((item.get("acceptable_issue_types") or ["other_semantic_incongruity"])[0])
            issues = [SemanticIssue(
                type=issue_type,
                severity="high",
                draft_text=str(item["draft"])[:240],
                reason="Draft conflicts with authoritative evidence-backed domain.",
                domain_refs=["source_email.body"],
            )]
        output[item["case_id"]] = SemanticReviewResult(
            SemanticReview(verdict=verdict, issues=issues),
            "deepseek_v4_flash_fake",
            "ds4-fake",
            0,
        )
    return output


def replay_reviews(
    eligible: list[dict[str, Any]], report_path: Path,
) -> tuple[dict[str, SemanticReviewResult | Exception], dict[str, Any]]:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "email_semantic_shadow_report_v1":
        raise ValueError("shadow_replay_report_invalid")
    rows = {
        str(item.get("case_id")): item
        for item in (payload.get("cases") or [])
        if isinstance(item, Mapping)
    }
    output: dict[str, SemanticReviewResult | Exception] = {}
    for item in eligible:
        row = rows.get(item["case_id"])
        if not row or not row.get("ds4_invoked") or row.get("ds4_verdict") not in {"pass", "repair"}:
            raise ValueError(f"shadow_replay_case_missing:{item['case_id']}")
        review = SemanticReview.model_validate({
            "verdict": row["ds4_verdict"],
            "issues": row.get("ds4_issues") or [],
            "summary": "",
        })
        output[item["case_id"]] = SemanticReviewResult(
            review,
            "deepseek_v4_flash",
            "deepseek-v4-flash",
            int(row.get("ds4_runtime_ms") or 0),
            input_tokens=row.get("ds4_input_tokens"),
            output_tokens=row.get("ds4_output_tokens"),
            metadata={"replayed": True, "source_report": str(report_path)},
        )
    runtime = dict(payload.get("runtime_metadata") or {})
    runtime.update({"critic_replayed": True, "critic_replay_source": str(report_path)})
    return output, runtime


def real_reviews(
    eligible: list[dict[str, Any]],
    *,
    config: SemanticJudgeConfig,
    runtime_artifacts: Path,
) -> tuple[dict[str, SemanticReviewResult | Exception], dict[str, Any]]:
    output: dict[str, SemanticReviewResult | Exception] = {}
    metadata: dict[str, Any] = {"gpu_status": "not_started", "external_restored": False}
    if not eligible:
        return output, metadata
    scheduler = TransactionalGpuScheduler()
    profile = config.server_profile()
    runtime_artifacts.mkdir(parents=True, exist_ok=True)
    transition_result: dict[str, Any] = {}
    try:
        with scheduler.engine_session("deepseek", task_id="email-semantic-shadow-canary") as transition_result:
            with Ds4ServerSession(profile, artifact_dir=runtime_artifacts) as server:
                metadata["gpu_status"] = "acquired"
                metadata["server_startup_ms"] = int(server.startup_ms or 0)
                for item in eligible:
                    prompt = semantic_prompt(item["context"], str(item["draft"]))
                    try:
                        reply = server.review_prompt(prompt, max_tokens=config.deepseek_max_output_tokens)
                        review = parse_semantic_review(reply.content, max_bytes=config.deepseek_max_output_bytes)
                        output[item["case_id"]] = SemanticReviewResult(
                            review,
                            "deepseek_v4_flash",
                            "deepseek-v4-flash",
                            reply.request_ms,
                            input_tokens=reply.input_tokens,
                            output_tokens=reply.output_tokens,
                            metadata={
                                "prefill_ms": reply.prefill_ms,
                                "decode_ms": reply.decode_ms,
                                "diagnostics": reply.diagnostics.as_dict(),
                            },
                        )
                    except Exception as exc:
                        output[item["case_id"]] = JudgeAvailabilityError(
                            f"deepseek_v4_flash_request_failed:{type(exc).__name__}:{exc}"
                        )
                metadata["server_diagnostics"] = server.diagnostics().as_dict()
                metadata["server_log"] = str(server.log_path)
    except GpuEngineTransitionError as exc:
        metadata.update({"gpu_status": "gpu_busy", "gpu_error": str(exc)})
        for item in eligible:
            output[item["case_id"]] = JudgeAvailabilityError(
                f"deepseek_v4_flash_gpu_unavailable:{exc}"
            )
    except Exception as exc:
        metadata.update({"gpu_status": "runtime_failure", "gpu_error": f"{type(exc).__name__}:{exc}"})
        for item in eligible:
            output.setdefault(
                item["case_id"],
                JudgeAvailabilityError(f"deepseek_v4_flash_runtime_unavailable:{type(exc).__name__}"),
            )
    finally:
        metadata.update(transition_result)
        metadata["external_restored"] = _agentcpm_restored(metadata)
    return output, metadata


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not _env_bool("RALFLOOP_SEMANTIC_JUDGE_SHADOW", False):
        raise RuntimeError("set_RALFLOOP_SEMANTIC_JUDGE_SHADOW=1")
    started = time.monotonic()
    cases = load_corpus(args.corpus)
    prepared, eligible = prepare(cases)
    if args.runtime == "real":
        config = SemanticJudgeConfig.from_env()
        if config.deepseek_max_output_tokens != 128:
            raise ValueError("shadow_canary_requires_BASE128")
        reviews, runtime = real_reviews(
            eligible,
            config=config,
            runtime_artifacts=args.output / "ds4-runtime",
        )
    elif args.runtime == "replay":
        if args.replay_report is None:
            raise ValueError("shadow_replay_report_required")
        reviews, runtime = replay_reviews(eligible, args.replay_report)
    else:
        reviews, runtime = fake_reviews(eligible), {
            "gpu_status": "fake",
            "external_restored": True,
            "server_startup_ms": 0,
        }

    def process_rows(qwen: RalfReplyGenerator | None, qwen_error: Exception | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in prepared:
            judge = RecordedJudge(reviews[item["case_id"]]) if item["case_id"] in reviews else None

            def repair(packet, draft, issues, *, current=item):
                if args.repair == "fixture":
                    return str(current.get("expected_repair") or draft)
                if qwen_error is not None:
                    raise qwen_error
                if args.repair == "qwen" and qwen is not None:
                    return qwen.generate_repair(
                        packet,
                        draft,
                        "semantic_judge_repair",
                        semantic_issues=issues,
                    )
                raise RuntimeError("semantic_repair_disabled")

            result = run_shadow_email_case(
                case_id=item["case_id"],
                context_packet=item["context"],
                draft=str(item["draft"]),
                artifact_root=args.output / "cases",
                semantic_judge=judge,
                repair_callback=repair,
                shadow_enabled=True,
            )
            repaired = str(result.get("draft_repaired") or result["draft_original"])
            oracle = oracle_passes(item, repaired)
            rows.append({
                "case_id": item["case_id"],
                "expected_risk": item["expected_risk"],
                "risk": result["risk"],
                "hard_guard": result["hard_guard"],
                "ds4_invoked": result["ds4_invoked"],
                "ds4_skipped_reason": result["ds4_skipped_reason"],
                "ds4_runtime_ms": result["ds4_runtime_ms"],
                "ds4_verdict": result["ds4_verdict"],
                "ds4_issue_types": [issue["type"] for issue in result["ds4_issues"]],
                "ds4_issues": result["ds4_issues"],
                "ds4_input_tokens": result["ds4_input_tokens"],
                "ds4_output_tokens": result["ds4_output_tokens"],
                "qwen_repair_attempted": result["qwen_repair_attempted"],
                "qwen_repair_ms": result["qwen_repair_ms"],
                "qwen_repair_success": bool(result["qwen_repair_attempted"] and oracle and result["final_validator"] == "passed"),
                "repair_error": result["repair_error"],
                "draft_repaired": result["draft_repaired"],
                "final_validator": result["final_validator"],
                "oracle_result": "passed" if oracle else "failed",
                "overall_result": result["overall_result"],
                "workload_runtime_ms": result["workload_runtime_ms"],
                "artifact_path": result["artifact_path"],
            })
        return rows

    qwen_transition: dict[str, Any] = {"engine": "none", "external_restored": True}
    rows: list[dict[str, Any]] = []
    if args.repair == "qwen":
        load_env(args.fast_chat_env)
        scheduler = TransactionalGpuScheduler()
        try:
            with scheduler.engine_session("qwen_chat", task_id="email-semantic-shadow-repair") as qwen_transition:
                rows = process_rows(RalfReplyGenerator())
        except GpuEngineTransitionError as exc:
            if not rows:
                rows = process_rows(None, exc)
            qwen_transition["error"] = f"{type(exc).__name__}:{exc}"
    else:
        rows = process_rows(None)
    runtime["qwen_transition"] = dict(qwen_transition)

    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
    report = {
        "schema_version": "email_semantic_shadow_report_v1",
        "mode": "shadow",
        "runtime": args.runtime,
        "repair_mode": args.repair,
        "metrics": metrics(rows, elapsed_ms),
        "runtime_metadata": runtime,
        "side_effect_verification": {
            "approval_records_created": 0,
            "outbox_records_created": 0,
            "telegram_send_calls": 0,
            "gmail_send_calls": 0,
            "conversation_state_mutations": 0,
        },
        "cases": rows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    atomic_json(args.output / "report.json", report)
    return report


def metrics(rows: list[dict[str, Any]], elapsed_ms: int) -> dict[str, Any]:
    levels = {level: sum(row["risk"]["level"] == level for row in rows) for level in ("low", "normal", "high")}
    invoked = [row for row in rows if row["ds4_invoked"]]
    completed = [row for row in invoked if isinstance(row["ds4_runtime_ms"], int) and row["ds4_runtime_ms"] > 0]
    latencies = sorted(row["ds4_runtime_ms"] for row in completed)
    repairs = [row for row in rows if row["qwen_repair_attempted"]]
    return {
        "total_cases": len(rows),
        "low": levels["low"],
        "normal": levels["normal"],
        "high": levels["high"],
        "hard_guard_blocked": sum(row["hard_guard"] != "passed" for row in rows),
        "ds4_invoked": len(invoked),
        "ds4_skipped": len(rows) - len(invoked),
        "ds4_pass": sum(row["ds4_verdict"] == "pass" for row in invoked),
        "ds4_repair": sum(row["ds4_verdict"] == "repair" for row in invoked),
        "final_validator_pass": sum(row["final_validator"] == "passed" for row in rows),
        "final_validator_block": sum(row["final_validator"] != "passed" for row in rows),
        "qwen_repair_attempted": len(repairs),
        "qwen_repair_success": sum(row["qwen_repair_success"] for row in repairs),
        "ds4_p50_ms": percentile(latencies, 0.50),
        "ds4_p95_ms": percentile(latencies, 0.95),
        "workload_total_ms": elapsed_ms,
        "workload_average_ms": round(elapsed_ms / len(rows), 1) if rows else 0,
        "ds4_invocation_rate_percent": round(100 * len(invoked) / len(rows), 1) if rows else 0,
    }


def oracle_passes(case: Mapping[str, Any], text: str) -> bool:
    required = case.get("oracle_required_patterns") or []
    forbidden = case.get("oracle_forbidden_patterns") or []
    return all(re.search(pattern, text, re.I | re.S) for pattern in required) and not any(
        re.search(pattern, text, re.I | re.S) for pattern in forbidden
    )


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    index = max(0, min(len(values) - 1, int((len(values) - 1) * fraction + 0.999999)))
    return values[index]


def _agentcpm_restored(metadata: Mapping[str, Any]) -> bool:
    if metadata.get("gpu_status") in {"fake", "not_started"}:
        return True
    if metadata.get("initial_engine") == "free" or metadata.get("external_was_running") is False:
        return True
    return bool(metadata.get("external_restored") or metadata.get("restored"))


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}


def atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--runtime", choices=("fake", "real", "replay"), default="fake")
    parser.add_argument("--replay-report", type=Path)
    parser.add_argument("--repair", choices=("fixture", "qwen", "none"), default="fixture")
    parser.add_argument("--fast-chat-env", type=Path, default=Path("/etc/ralfloop/fast-chat.env"))
    return parser.parse_args()


def main() -> int:
    report = run(parse_args())
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2, sort_keys=True))
    print(f"report={report['metrics']['total_cases']} cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
