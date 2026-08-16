#!/usr/bin/env python3
"""Local DS4 email semantic-critic benchmark. No mail, Telegram, approval, or deploy side effects."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
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
from ralfloop_agent.semantic_judge import parse_semantic_review  # noqa: E402
from ralfloop_agent.semantic_judge.core import semantic_prompt  # noqa: E402
from ralfloop_agent.semantic_judge.ds4_server import (  # noqa: E402
    Ds4ServerProfile,
    Ds4ServerRequestError,
    Ds4ServerSession,
)


DEFAULT_CORPUS = ROOT / "tests/fixtures/email_semantic_critic_corpus_v1.json"
DEFAULT_SERVER = Path("/home/sibilla-cumana/src/ds4-cuda-stream-pr739/ds4-server")
DEFAULT_MODEL = Path(
    "/home/sibilla-cumana/Dati/ralfloop-models/deepseek-v4-flash-pr739/gguf/"
    "DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf"
)
DEFAULT_SWEEP_CASES = (
    "A_safe_paraphrase",
    "B_unsupported_commitment",
    "D_missing_required_meaning",
)


def load_corpus(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "email_semantic_critic_corpus_v1":
        raise ValueError("critic_corpus_schema_invalid")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or len(raw_cases) < 10:
        raise ValueError("critic_corpus_cases_invalid")
    contexts: dict[str, Mapping[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for raw in raw_cases:
        if not isinstance(raw, dict) or not isinstance(raw.get("case"), str):
            raise ValueError("critic_corpus_case_invalid")
        item = copy.deepcopy(raw)
        case_id = item["case"]
        if "context" in item:
            context = item["context"]
        else:
            context = contexts.get(str(item.get("context_ref") or ""))
        if not isinstance(context, Mapping):
            raise ValueError(f"critic_corpus_context_invalid:{case_id}")
        item["context"] = copy.deepcopy(dict(context))
        contexts[case_id] = item["context"]
        result.append(item)
    if len({item["case"] for item in result}) != len(result):
        raise ValueError("critic_corpus_duplicate_case")
    return result


def prepare_case(case: Mapping[str, Any]) -> dict[str, Any]:
    context = copy.deepcopy(dict(case["context"]))
    started = time.perf_counter()
    domain = build_email_reply_domain(context)
    domain_build_ms = round((time.perf_counter() - started) * 1000, 3)
    context["email_reply_domain_v1"] = domain.model_dump(mode="json")
    context["email_reply_domain_sha256"] = domain_digest(domain)
    draft = str(case["draft"])
    try:
        validate_draft_against_domain(draft, domain)
        hard_guard = "passed"
    except EmailReplyDomainValidationError as exc:
        hard_guard = f"blocked:{exc.reason_code}:{exc.domain_ref}"
    return {
        "case": case["case"],
        "problem": case["problem"],
        "expected_verdict": case["expected_verdict"],
        "acceptable_issue_types": list(case["acceptable_issue_types"]),
        "context": context,
        "domain": domain,
        "draft": draft,
        "expected_repair": str(case["expected_repair"]),
        "oracle_required_patterns": list(case.get("oracle_required_patterns") or []),
        "oracle_forbidden_patterns": list(case.get("oracle_forbidden_patterns") or []),
        "domain_build_ms": domain_build_ms,
        "qwen_draft_ms": None,
        "hard_guard_result": hard_guard,
    }


def oracle_passes(case: Mapping[str, Any], text: str) -> bool:
    return all(re.search(pattern, text, re.I | re.S) for pattern in case["oracle_required_patterns"]) and not any(
        re.search(pattern, text, re.I | re.S) for pattern in case["oracle_forbidden_patterns"]
    )


def _domain_reference(case: Mapping[str, Any], issue_type: str) -> str:
    domain = case["domain"]
    if issue_type == "missing_required_meaning" and domain.required_meanings:
        return domain.required_meanings[-1].key
    if domain.forbidden_claims_without_evidence:
        return domain.forbidden_claims_without_evidence[0]
    if domain.supported_facts:
        return domain.supported_facts[0].key
    return domain.evidence[0].ref


def fake_review(case: Mapping[str, Any]) -> dict[str, Any]:
    if case["expected_verdict"] == "pass":
        value = {"verdict": "pass", "issues": [], "summary": "No semantic issue."}
    else:
        issue_type = case["acceptable_issue_types"][0]
        value = {
            "verdict": "repair",
            "issues": [{
                "type": issue_type,
                "severity": "high",
                "draft_text": "" if issue_type == "missing_required_meaning" else case["draft"][:160],
                "reason": f"Draft violates authoritative domain: {case['problem']}.",
                "domain_refs": [_domain_reference(case, issue_type)],
            }],
            "summary": "Concrete semantic repair required.",
        }
    review = parse_semantic_review(json.dumps(value, ensure_ascii=False))
    return {
        "review": review,
        "raw": json.dumps(value, ensure_ascii=False),
        "runtime_ms": 0,
        "input_tokens": None,
        "output_tokens": None,
        "usage": {},
    }


def evaluate_review(case: Mapping[str, Any], result: Mapping[str, Any], *, budget: int) -> dict[str, Any]:
    review = result["review"]
    types = [item.type for item in review.issues]
    matched = review.verdict == case["expected_verdict"] and (
        review.verdict == "pass" or bool(set(types) & set(case["acceptable_issue_types"]))
    )
    return {
        "max_output_tokens": budget,
        "ds4_runtime_ms": result["runtime_ms"],
        "ds4_verdict": review.verdict,
        "ds4_issue_count": len(review.issues),
        "ds4_issue_types": types,
        "ds4_criticism": [item.model_dump(mode="json") for item in review.issues],
        "ds4_summary": review.summary,
        "ds4_raw": result["raw"],
        "ds4_input_tokens": result["input_tokens"],
        "ds4_output_tokens": result["output_tokens"],
        "ds4_usage": result["usage"],
        "ds4_parser_result": "passed",
        "semantic_expected_match": matched,
    }


def review_error(
    exc: BaseException, *, budget: int, runtime_ms: int, reply: Any | None = None,
) -> dict[str, Any]:
    return {
        "max_output_tokens": budget,
        "ds4_runtime_ms": runtime_ms,
        "ds4_verdict": "error",
        "ds4_issue_count": 0,
        "ds4_issue_types": [],
        "ds4_criticism": [],
        "ds4_summary": "",
        "ds4_raw": reply.content if reply is not None else "",
        "ds4_input_tokens": reply.input_tokens if reply is not None else None,
        "ds4_output_tokens": reply.output_tokens if reply is not None else None,
        "ds4_usage": dict(reply.usage) if reply is not None else {},
        "ds4_parser_result": f"error:{type(exc).__name__}:{exc}",
        "semantic_expected_match": False,
        "ds4_error": f"{type(exc).__name__}:{exc}",
    }


def run_real_reviews(
    prepared: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    artifact_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, dict[str, Any]]:
    by_id = {item["case"]: item for item in prepared}
    sweep: list[dict[str, Any]] = []
    cached: dict[tuple[str, int], dict[str, Any]] = {}
    transition: dict[str, Any] = {}
    scheduler = TransactionalGpuScheduler()
    with scheduler.engine_session("deepseek", task_id="ds4-email-critic-benchmark") as transition:
        profile = Ds4ServerProfile(
            executable=args.server,
            model_path=args.model,
            host="127.0.0.1",
            port=args.port,
            context_tokens=args.context_tokens,
            prefill_chunk=args.prefill_chunk,
            threads=args.threads,
            max_output_tokens=max(args.sweep_tokens),
            stage_mb=args.stage_mb,
            reserve_mb=args.reserve_mb,
            weight_cache_verbose=True,
            weight_cache_limit_gb=args.weight_cache_limit_gb,
            startup_timeout_sec=args.startup_timeout_sec,
            request_timeout_sec=args.timeout_sec,
        )
        with Ds4ServerSession(profile, artifact_dir=artifact_dir) as server:
            for budget in args.sweep_tokens:
                for case_id in args.sweep_cases:
                    case = by_id[case_id]
                    started = time.perf_counter()
                    reply = None
                    try:
                        reply = server.review_prompt(
                            semantic_prompt(case["context"], case["draft"]), max_tokens=budget,
                        )
                        result = {
                            "review": parse_semantic_review(reply.content),
                            "raw": reply.content,
                            "runtime_ms": reply.request_ms,
                            "input_tokens": reply.input_tokens,
                            "output_tokens": reply.output_tokens,
                            "usage": reply.usage,
                        }
                        row = evaluate_review(case, result, budget=budget)
                    except Ds4ServerRequestError:
                        raise
                    except Exception as exc:
                        row = review_error(
                            exc, budget=budget,
                            runtime_ms=int((time.perf_counter() - started) * 1000), reply=reply,
                        )
                    row["case"] = case_id
                    sweep.append(row)
                    cached[(case_id, budget)] = row
                if all(cached[(case_id, budget)]["semantic_expected_match"] for case_id in args.sweep_cases):
                    break
            tested_budgets = tuple(dict.fromkeys(row["max_output_tokens"] for row in sweep))
            eligible = [budget for budget in tested_budgets if all(
                cached[(case_id, budget)]["semantic_expected_match"] for case_id in args.sweep_cases
            )]
            selected_budget = min(eligible) if eligible else tested_budgets[-1]
            rows: list[dict[str, Any]] = []
            for case in prepared:
                key = (case["case"], selected_budget)
                if key in cached:
                    review_row = dict(cached[key])
                else:
                    started = time.perf_counter()
                    reply = None
                    try:
                        reply = server.review_prompt(
                            semantic_prompt(case["context"], case["draft"]), max_tokens=selected_budget,
                        )
                        result = {
                            "review": parse_semantic_review(reply.content),
                            "raw": reply.content,
                            "runtime_ms": reply.request_ms,
                            "input_tokens": reply.input_tokens,
                            "output_tokens": reply.output_tokens,
                            "usage": reply.usage,
                        }
                        review_row = evaluate_review(case, result, budget=selected_budget)
                    except Ds4ServerRequestError:
                        raise
                    except Exception as exc:
                        review_row = review_error(
                            exc, budget=selected_budget,
                            runtime_ms=int((time.perf_counter() - started) * 1000),
                            reply=reply,
                        )
                rows.append({
                    "case": case["case"],
                    "problem": case["problem"],
                    "domain_build_ms": case["domain_build_ms"],
                    "qwen_draft_ms": case["qwen_draft_ms"],
                    "hard_guard_result": case["hard_guard_result"],
                    **review_row,
                })
            startup_ms = int(server.startup_ms or 0)
            server_log = str(server.log_path)
            diagnostics = server.diagnostics().as_dict()
    return rows, sweep, selected_budget, {
        **transition,
        "server_startup_ms": startup_ms,
        "server_log": server_log,
        "server_diagnostics": diagnostics,
        "server_command": profile.command(),
        "server_environment": profile.environment_overrides(),
        "request_sampling": {
            key: value for key, value in profile.request_payload("<prompt>").items()
            if key not in {"messages"}
        },
        "single_resident_session": True,
    }


def run_fake_reviews(prepared: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, dict[str, Any]]:
    rows = []
    for case in prepared:
        evaluated = evaluate_review(case, fake_review(case), budget=budget)
        rows.append({
            "case": case["case"], "problem": case["problem"],
            "domain_build_ms": case["domain_build_ms"], "qwen_draft_ms": None,
            "hard_guard_result": case["hard_guard_result"], **evaluated,
        })
    return rows, [], budget, {"engine": "fake", "external_stopped": False, "external_restored": False, "server_startup_ms": 0}


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def run_repairs(
    prepared: list[dict[str, Any]], rows: list[dict[str, Any]], *, mode: str, fast_chat_env: Path,
) -> dict[str, Any]:
    by_id = {item["case"]: item for item in prepared}
    qwen_transition: dict[str, Any] = {"engine": "none"}

    def apply_repair(row: dict[str, Any], text: str, elapsed_ms: int, provider: str, model: str) -> None:
        case = by_id[row["case"]]
        row["qwen_repair_ms"] = elapsed_ms
        row["qwen_repair"] = text
        row["qwen_repair_provider"] = provider
        row["qwen_repair_model"] = model
        row["original_problem_removed"] = oracle_passes(case, text)
        try:
            validate_draft_against_domain(text, case["domain"])
            row["final_validator_result"] = "passed"
        except EmailReplyDomainValidationError as exc:
            row["final_validator_result"] = f"blocked:{exc.reason_code}:{exc.domain_ref}"
        row["overall_result"] = (
            "passed" if row["original_problem_removed"] and row["final_validator_result"] == "passed"
            else "blocked_or_incorrect"
        )

    pending = [row for row in rows if row["ds4_verdict"] == "repair"]
    if mode == "qwen" and pending:
        load_env(fast_chat_env)
        from ralfloop_agent.providers.chat import FallbackChatProvider, build_chat_provider
        from src.google_workspace import RalfReplyGenerator

        scheduler = TransactionalGpuScheduler()
        try:
            with scheduler.engine_session("qwen_chat", task_id="ds4-email-critic-repair-benchmark") as qwen_transition:
                provider = build_chat_provider()
                primary = provider.primary if isinstance(provider, FallbackChatProvider) else provider
                generator = RalfReplyGenerator(provider=provider)
                for row in pending:
                    case = by_id[row["case"]]
                    started = time.perf_counter()
                    try:
                        generated = generator.generate_repair(
                            case["context"], case["draft"], "semantic_judge_repair",
                            semantic_issues=row["ds4_criticism"],
                        )
                        elapsed_ms = int((time.perf_counter() - started) * 1000)
                        if generated.fallback_used:
                            raise RuntimeError("qwen_repair_fallback_used")
                        apply_repair(row, generated.text, elapsed_ms, generated.provider, generated.model)
                    except Exception as exc:
                        row.update({
                            "qwen_repair_ms": int((time.perf_counter() - started) * 1000),
                            "qwen_repair": "", "qwen_repair_provider": getattr(primary, "name", ""),
                            "qwen_repair_model": getattr(primary, "default_model", ""),
                            "original_problem_removed": False,
                            "final_validator_result": "blocked:repair_error",
                            "overall_result": "blocked_or_incorrect",
                            "qwen_repair_error": f"{type(exc).__name__}:{exc}",
                        })
        except GpuEngineTransitionError as exc:
            qwen_transition = {"engine": "qwen_chat", "error": f"{type(exc).__name__}:{exc}"}
            for row in pending:
                row.update({
                    "qwen_repair_ms": None, "qwen_repair": "", "qwen_repair_provider": None,
                    "qwen_repair_model": None, "original_problem_removed": False,
                    "final_validator_result": "blocked:qwen_gpu_unavailable",
                    "overall_result": "fail_closed",
                })
    else:
        for row in pending:
            case = by_id[row["case"]]
            if mode == "fixture":
                apply_repair(row, case["expected_repair"], 0, "fixture_qwen", "scripted")
            else:
                row.update({
                    "qwen_repair_ms": None, "qwen_repair": "", "qwen_repair_provider": None,
                    "qwen_repair_model": None, "original_problem_removed": False,
                    "final_validator_result": "blocked:repair_not_run", "overall_result": "blocked_or_incorrect",
                })

    for row in rows:
        if row["ds4_verdict"] == "repair":
            continue
        case = by_id[row["case"]]
        row.update({
            "qwen_repair_ms": None, "qwen_repair": "", "qwen_repair_provider": None,
            "qwen_repair_model": None, "original_problem_removed": oracle_passes(case, case["draft"]),
        })
        if row["ds4_verdict"] == "error":
            row["final_validator_result"] = "blocked:semantic_critic_error"
            row["overall_result"] = "fail_closed"
            continue
        try:
            validate_draft_against_domain(case["draft"], case["domain"])
            row["final_validator_result"] = "passed"
        except EmailReplyDomainValidationError as exc:
            row["final_validator_result"] = f"blocked:{exc.reason_code}:{exc.domain_ref}"
        row["overall_result"] = (
            "passed" if row["original_problem_removed"] and row["final_validator_result"] == "passed"
            else "blocked_or_incorrect"
        )
    return dict(qwen_transition)


def percentile(values: list[int], fraction: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999)))
    return ordered[index]


def metrics(rows: list[dict[str, Any]], sweep: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [row for row in rows if row["case"] != "A_safe_paraphrase"]
    matched_issues = 0
    predicted_issues = 0
    for row in rows:
        expected = set(row.get("acceptable_issue_types") or [])
        predicted_issues += len(row["ds4_issue_types"])
        matched_issues += sum(issue in expected for issue in row["ds4_issue_types"])
    matched_positive_cases = sum(bool(row["semantic_expected_match"]) for row in positives)
    repair_rows = [row for row in rows if row["ds4_verdict"] == "repair"]
    repair_success = sum(
        bool(row["original_problem_removed"]) and row["final_validator_result"] == "passed"
        for row in repair_rows
    )
    errors = [row for row in rows if row["ds4_verdict"] == "error"]
    latencies = [int(row["ds4_runtime_ms"]) for row in rows if row["ds4_verdict"] != "error"]
    predicted_repairs = sum(row["ds4_verdict"] == "repair" for row in rows)
    return {
        "critic_case_precision": round(matched_positive_cases / predicted_repairs, 4) if predicted_repairs else None,
        "critic_case_recall": round(matched_positive_cases / max(1, len(positives)), 4),
        "critic_issue_precision": round(matched_issues / predicted_issues, 4) if predicted_issues else None,
        "safe_false_positive_rate": (
            round(float(rows[0]["ds4_verdict"] == "repair"), 4)
            if rows[0]["ds4_verdict"] != "error" else None
        ),
        "repair_success_rate": round(repair_success / len(repair_rows), 4) if repair_rows else None,
        "fail_closed_rate": round(sum(row["overall_result"] == "fail_closed" for row in errors) / max(1, len(errors)), 4),
        "ds4_latency_p50_ms": int(statistics.median(latencies)) if latencies else None,
        "ds4_latency_p95_ms": percentile(latencies, 0.95),
        "ds4_runs": len(latencies),
        "ds4_errors": len(errors),
        "sweep_runs": len(sweep),
    }


def snapshot(paths: list[Path]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path in paths:
        try:
            stat = path.stat()
            result[str(path)] = {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        except FileNotFoundError:
            result[str(path)] = {"exists": False}
        except PermissionError:
            result[str(path)] = {"exists": True, "permission": "metadata_denied"}
    return result


def atomic_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, ensure_ascii=False, indent=2, default=str).encode("utf-8") + b"\n"
    with tempfile.NamedTemporaryFile(prefix=path.name + ".", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def parse_csv_ints(value: str) -> tuple[int, ...]:
    values = tuple(dict.fromkeys(int(item.strip()) for item in value.split(",") if item.strip()))
    if not values or any(item not in {32, 64, 128, 256} for item in values):
        raise argparse.ArgumentTypeError("tokens must be subset of 32,64,128,256")
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="Real local DS4 semantic critic benchmark")
    parser.add_argument("--runtime", choices=("fake", "ds4-server"), default="fake")
    parser.add_argument("--repair-mode", choices=("none", "fixture", "qwen"), default="fixture")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--server", type=Path, default=DEFAULT_SERVER)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--port", type=int, default=19194)
    parser.add_argument("--context-tokens", type=int, default=1024)
    parser.add_argument("--prefill-chunk", type=int, default=128)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--stage-mb", type=int, default=1280)
    parser.add_argument("--reserve-mb", type=int, default=384)
    parser.add_argument("--weight-cache-limit-gb", type=int, default=3)
    parser.add_argument("--timeout-sec", type=float, default=1800)
    parser.add_argument("--startup-timeout-sec", type=float, default=180)
    parser.add_argument("--sweep-tokens", type=parse_csv_ints, default=(64, 128))
    parser.add_argument("--sweep-cases", nargs="+", default=list(DEFAULT_SWEEP_CASES))
    parser.add_argument("--fake-budget", type=int, default=64)
    parser.add_argument("--fast-chat-env", type=Path, default=Path("/etc/ralfloop/fast-chat.env"))
    parser.add_argument("--print-report", action="store_true")
    args = parser.parse_args()

    artifact_dir = args.output.parent
    isolated = artifact_dir / "forbidden-side-effects"
    os.environ["RALFLOOP_TELEGRAM_APPROVAL_DB"] = str(isolated / "approval.sqlite")
    os.environ["RALFLOOP_TELEGRAM_APPROVAL_OUTBOX"] = str(isolated / "outbox.jsonl")
    os.environ["RALFLOOP_TELEGRAM_APPROVAL_OUTBOX_STATE"] = str(isolated / "outbox.state.json")
    side_effect_paths = [
        isolated / "approval.sqlite", isolated / "outbox.jsonl", isolated / "outbox.state.json",
        Path("/var/lib/ralfloop/domain-approval-outbox.jsonl"),
        Path("/var/lib/ralfloop/domain-approval-outbox.jsonl.state.json"),
        Path(tempfile.gettempdir()) / f"ralfloop_domain_approvals_{os.getuid()}.sqlite",
    ]
    before = snapshot(side_effect_paths)
    prepared = [prepare_case(case) for case in load_corpus(args.corpus)]
    case_ids = {item["case"] for item in prepared}
    if any(case not in case_ids for case in args.sweep_cases):
        parser.error("unknown --sweep-cases")

    status = "completed"
    error = ""
    try:
        if args.runtime == "ds4-server":
            rows, sweep, selected_budget, ds4_transition = run_real_reviews(prepared, args=args, artifact_dir=artifact_dir)
        else:
            rows, sweep, selected_budget, ds4_transition = run_fake_reviews(prepared, args.fake_budget)
        for row, case in zip(rows, prepared, strict=True):
            row["expected_verdict"] = case["expected_verdict"]
            row["acceptable_issue_types"] = case["acceptable_issue_types"]
        qwen_transition = run_repairs(prepared, rows, mode=args.repair_mode, fast_chat_env=args.fast_chat_env)
    except GpuEngineTransitionError as exc:
        status = "gpu_busy" if "busy" in str(exc) or "insufficient_gpu" in str(exc) else "gpu_transition_failed"
        error = f"{type(exc).__name__}:{exc}"
        rows, sweep, selected_budget = [], [], None
        ds4_transition, qwen_transition = {}, {}
    except Ds4ServerRequestError as exc:
        status = "gpu_runtime_failed"
        error = f"{type(exc).__name__}:{exc}"
        rows, sweep, selected_budget = [], [], None
        ds4_transition, qwen_transition = {}, {}
    except Exception as exc:
        status = "failed"
        error = f"{type(exc).__name__}:{exc}"
        rows, sweep, selected_budget = [], [], None
        ds4_transition, qwen_transition = {}, {}

    after = snapshot(side_effect_paths)
    side_effect_check = {
        "paths_unchanged": before == after,
        "isolated_approval_created": any(path.exists() for path in side_effect_paths[:3]),
        "before": before,
        "after": after,
    }
    report = {
        "schema_version": "ds4_email_critic_benchmark_v1",
        "status": status,
        "error": error,
        "runtime": args.runtime,
        "repair_mode": args.repair_mode,
        "corpus": str(args.corpus),
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "sampling": {
            "model": "deepseek-chat", "temperature": 0, "top_p": 1,
            "thinking": False, "greedy": True, "seed": None,
        },
        "timeout_safety_ceiling_sec": args.timeout_sec,
        "selected_output_tokens": selected_budget,
        "sweep": sweep,
        "rows": rows,
        "metrics": metrics(rows, sweep) if rows else {},
        "ds4_transition": ds4_transition,
        "qwen_transition": qwen_transition,
        "side_effect_check": side_effect_check,
        "ds4_calls_after_repair": 0,
    }
    atomic_write(args.output, report)
    printable = report if args.print_report else {
        "status": status,
        "error": error,
        "output": str(args.output),
        "selected_output_tokens": selected_budget,
        "metrics": report["metrics"],
        "side_effect_check": {
            "paths_unchanged": side_effect_check["paths_unchanged"],
            "isolated_approval_created": side_effect_check["isolated_approval_created"],
        },
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    if status != "completed" or not side_effect_check["paths_unchanged"] or side_effect_check["isolated_approval_created"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
