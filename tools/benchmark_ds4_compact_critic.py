#!/usr/bin/env python3
"""Compare verbose and compact DS4 critic protocols in one resident server session."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.providers.gpu_engine_scheduler import (  # noqa: E402
    GpuEngineTransitionError,
    TransactionalGpuScheduler,
)
from ralfloop_agent.semantic_judge.compact import (  # noqa: E402
    compact_critic_context,
    expand_compact_patch,
    parse_compact_patch,
)
from ralfloop_agent.semantic_judge.core import parse_semantic_review, semantic_prompt  # noqa: E402
from ralfloop_agent.semantic_judge.ds4_server import (  # noqa: E402
    Ds4ServerProfile,
    Ds4ServerRequestError,
    Ds4ServerSession,
)
from tools.benchmark_ds4_email_critic import (  # noqa: E402
    DEFAULT_CORPUS,
    DEFAULT_MODEL,
    DEFAULT_SERVER,
    atomic_write,
    evaluate_review,
    load_corpus,
    metrics,
    percentile,
    prepare_case,
    review_error,
    run_repairs,
    snapshot,
)


PROFILES = (("BASE", "verbose", 128), ("COMPACT64", "compact", 64), ("COMPACT32", "compact", 32))
PROFILE_BY_NAME = {item[0]: item for item in PROFILES}


def _run_case(
    server: Ds4ServerSession,
    case: Mapping[str, Any],
    *,
    profile_name: str,
    protocol: str,
    budget: int,
) -> dict[str, Any]:
    compact = compact_critic_context(case["context"], case["draft"]) if protocol == "compact" else None
    prompt = compact.prompt if compact else semantic_prompt(case["context"], case["draft"])
    reply = None
    started = time.perf_counter()
    patch_value: dict[str, Any] | None = None
    try:
        reply = server.review_prompt(prompt, max_tokens=budget)
        if compact is None:
            review = parse_semantic_review(reply.content)
        else:
            patch = parse_compact_patch(reply.content, compact)
            patch_value = patch.model_dump(mode="json", exclude_none=True)
            review = expand_compact_patch(patch, compact)
        row = evaluate_review(case, {
            "review": review,
            "raw": reply.content,
            "runtime_ms": reply.request_ms,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "usage": reply.usage,
        }, budget=budget)
    except Ds4ServerRequestError:
        raise
    except Exception as exc:
        row = review_error(
            exc, budget=budget,
            runtime_ms=int((time.perf_counter() - started) * 1000), reply=reply,
        )
    row.update({
        "profile": profile_name,
        "protocol": protocol,
        "case": case["case"],
        "problem": case["problem"],
        "expected_verdict": case["expected_verdict"],
        "acceptable_issue_types": case["acceptable_issue_types"],
        "domain_build_ms": case["domain_build_ms"],
        "qwen_draft_ms": case["qwen_draft_ms"],
        "hard_guard_result": case["hard_guard_result"],
        "prompt_chars": len(prompt),
        "prefill_ms": getattr(reply, "prefill_ms", None),
        "decode_ms": getattr(reply, "decode_ms", None),
        "compact_patch": patch_value,
    })
    return row


def _timing_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def values(key: str) -> list[int]:
        return [int(row[key]) for row in rows if row.get(key) is not None and row["ds4_verdict"] != "error"]

    totals = values("ds4_runtime_ms")
    prefills = values("prefill_ms")
    decodes = values("decode_ms")
    inputs = values("ds4_input_tokens")
    outputs = values("ds4_output_tokens")
    return {
        "input_tokens_min": min(inputs) if inputs else None,
        "input_tokens_max": max(inputs) if inputs else None,
        "output_tokens_min": min(outputs) if outputs else None,
        "output_tokens_max": max(outputs) if outputs else None,
        "prefill_p50_ms": int(statistics.median(prefills)) if prefills else None,
        "prefill_p95_ms": percentile(prefills, 0.95),
        "decode_p50_ms": int(statistics.median(decodes)) if decodes else None,
        "decode_p95_ms": percentile(decodes, 0.95),
        "total_p50_ms": int(statistics.median(totals)) if totals else None,
        "total_p95_ms": percentile(totals, 0.95),
    }


def run_matrix(
    prepared: list[dict[str, Any]],
    *,
    args: argparse.Namespace,
    artifact_dir: Path,
) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    scheduler = TransactionalGpuScheduler()
    rows_by_profile: dict[str, list[dict[str, Any]]] = {}
    warm: list[dict[str, Any]] = []
    transition: dict[str, Any] = {}
    profile = Ds4ServerProfile(
        executable=args.server, model_path=args.model, host="127.0.0.1", port=args.port,
        context_tokens=1024, prefill_chunk=128, threads=8, max_output_tokens=128,
        stage_mb=1280, reserve_mb=384, weight_cache_verbose=True, weight_cache_limit_gb=3,
        startup_timeout_sec=args.startup_timeout_sec, request_timeout_sec=args.timeout_sec,
    )
    with scheduler.engine_session("deepseek", task_id="ds4-compact-critic-benchmark") as transition:
        with Ds4ServerSession(profile, artifact_dir=artifact_dir) as server:
            selected_profiles = [PROFILE_BY_NAME[name] for name in args.profiles]
            for name, protocol, budget in selected_profiles:
                rows_by_profile[name] = [
                    _run_case(server, case, profile_name=name, protocol=protocol, budget=budget)
                    for case in prepared
                ]
            case = prepared[0]
            if "COMPACT32" in rows_by_profile and args.warm_repeats:
                cold = dict(rows_by_profile["COMPACT32"][0])
                cold["warm_label"] = "cold"
                warm.append(cold)
                for index in range(1, args.warm_repeats + 1):
                    row = _run_case(
                        server, case, profile_name="COMPACT32", protocol="compact", budget=32,
                    )
                    row["warm_label"] = f"warm{index}"
                    warm.append(row)
            runtime = {
                **transition,
                "server_startup_ms": server.startup_ms,
                "server_log": str(server.log_path),
                "server_diagnostics": server.diagnostics().as_dict(),
                "server_command": profile.command(),
                "server_environment": profile.environment_overrides(),
                "single_resident_session": True,
            }
    return rows_by_profile, warm, runtime


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--server", type=Path, default=DEFAULT_SERVER)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--port", type=int, default=19194)
    parser.add_argument("--timeout-sec", type=float, default=1800)
    parser.add_argument("--startup-timeout-sec", type=float, default=180)
    parser.add_argument("--repair-mode", choices=("none", "fixture", "qwen"), default="qwen")
    parser.add_argument("--profiles", nargs="+", choices=tuple(PROFILE_BY_NAME), default=list(PROFILE_BY_NAME))
    parser.add_argument("--warm-repeats", type=int, choices=range(0, 4), default=3)
    parser.add_argument("--cases", nargs="+", default=[])
    parser.add_argument("--fast-chat-env", type=Path, default=Path("/etc/ralfloop/fast-chat.env"))
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
    if args.cases:
        requested = set(args.cases)
        known = {case["case"] for case in prepared}
        unknown = requested - known
        if unknown:
            parser.error("unknown --cases: " + ",".join(sorted(unknown)))
        prepared = [case for case in prepared if case["case"] in requested]
    status, error = "completed", ""
    rows_by_profile: dict[str, list[dict[str, Any]]] = {}
    warm: list[dict[str, Any]] = []
    runtime: dict[str, Any] = {}
    repair_transitions: dict[str, Any] = {}
    try:
        rows_by_profile, warm, runtime = run_matrix(prepared, args=args, artifact_dir=artifact_dir)
        for name, rows in rows_by_profile.items():
            repair_transitions[name] = run_repairs(
                prepared, rows, mode=args.repair_mode, fast_chat_env=args.fast_chat_env,
            )
    except GpuEngineTransitionError as exc:
        status = "gpu_busy" if "busy" in str(exc) or "insufficient_gpu" in str(exc) else "gpu_transition_failed"
        error = f"{type(exc).__name__}:{exc}"
    except Ds4ServerRequestError as exc:
        status, error = "gpu_runtime_failed", f"{type(exc).__name__}:{exc}"
    except Exception as exc:
        status, error = "failed", f"{type(exc).__name__}:{exc}"

    after = snapshot(side_effect_paths)
    side_effect_check = {
        "paths_unchanged": before == after,
        "isolated_approval_created": any(path.exists() for path in side_effect_paths[:3]),
        "before": before, "after": after,
    }
    profile_reports = {
        name: {
            "rows": rows,
            "quality": metrics(rows, []) if rows else {},
            "timing": _timing_metrics(rows),
            "repair_transition": repair_transitions.get(name, {}),
        }
        for name, rows in rows_by_profile.items()
    }
    report = {
        "schema_version": "ds4_compact_critic_benchmark_v1",
        "status": status, "error": error,
        "corpus": str(args.corpus),
        "corpus_sha256": hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        "profiles": profile_reports,
        "warm_repeats": warm,
        "warm_timing": _timing_metrics(warm),
        "runtime": runtime,
        "side_effect_check": side_effect_check,
        "ds4_calls_after_repair": 0,
    }
    atomic_write(args.output, report)
    print(json.dumps({
        "status": status, "error": error, "output": str(args.output),
        "profiles": {name: {"quality": value["quality"], "timing": value["timing"]}
                     for name, value in profile_reports.items()},
        "warm_timing": report["warm_timing"],
        "side_effect_check": {
            "paths_unchanged": side_effect_check["paths_unchanged"],
            "isolated_approval_created": side_effect_check["isolated_approval_created"],
        },
    }, ensure_ascii=False, indent=2))
    return 0 if status == "completed" and side_effect_check["paths_unchanged"] and not side_effect_check["isolated_approval_created"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
