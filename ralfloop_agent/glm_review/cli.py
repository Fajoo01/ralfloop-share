from __future__ import annotations

import argparse
import json
from typing import Any

from .service import GlmReviewService


def add_glm_parser(sub: argparse._SubParsersAction) -> None:
    glm = sub.add_parser("glm", help="manage slow Colibri/GLM review jobs")
    actions = glm.add_subparsers(dest="glm_action", required=True)
    enqueue = actions.add_parser("enqueue", help="enqueue an immutable review packet")
    enqueue.add_argument("--type", dest="task_type", required=True, choices=("grant_review", "technical_review", "other"))
    enqueue.add_argument("--input", required=True)
    enqueue.add_argument("--task-id")
    enqueue.add_argument("--immediate", action="store_true")
    enqueue.add_argument("--action", choices=("send_email", "submit_application", "portal_upload", "sign", "publish", "modify_external_data", "delete_external_data"))
    status = actions.add_parser("status"); status.add_argument("task_id")
    listing = actions.add_parser("list"); listing.add_argument("--state"); listing.add_argument("--limit", type=int, default=100)
    retry = actions.add_parser("retry"); retry.add_argument("task_id")
    cancel = actions.add_parser("cancel"); cancel.add_argument("task_id")
    result = actions.add_parser("result"); result.add_argument("task_id")
    digest = actions.add_parser("digest")
    digest.add_argument("--dry-run", action="store_true")
    digest.add_argument("--emit-outbox", action="store_true")
    worker = actions.add_parser("worker", help="run bounded persistent-queue work")
    worker.add_argument("--force", action="store_true")
    worker.add_argument("--limit", type=int, default=1)
    worker.add_argument("--task-id", help="atomically claim only this exact task")
    actions.add_parser("metrics")
    actions.add_parser("pause")
    actions.add_parser("resume")


def add_approval_parsers(sub: argparse._SubParsersAction) -> None:
    approve = sub.add_parser("approve", help="approve the current immutable GLM artifact")
    approve.add_argument("task_id")
    approve.add_argument("--approver", default="local-user")
    approve.add_argument("--channel", default="cli")
    reject = sub.add_parser("reject", help="reject the current immutable GLM artifact")
    reject.add_argument("task_id")
    reject.add_argument("--approver", default="local-user")
    reject.add_argument("--channel", default="cli")
    reject.add_argument("--reason", default="")


def run(args: argparse.Namespace) -> int:
    service = GlmReviewService()
    try:
        if args.command == "approve":
            output = service.approve(args.task_id, approver=args.approver, channel=args.channel)
        elif args.command == "reject":
            output = service.reject(args.task_id, approver=args.approver, channel=args.channel, reason=args.reason)
        elif args.glm_action == "enqueue":
            output = service.enqueue_file(
                args.input,
                task_type=args.task_type,
                task_id=args.task_id,
                immediate=args.immediate,
                external_action=args.action,
            )
        elif args.glm_action == "status":
            output = service.status(args.task_id)
        elif args.glm_action == "list":
            output = service.list(state=args.state, limit=args.limit)
        elif args.glm_action == "retry":
            output = service.retry(args.task_id)
        elif args.glm_action == "cancel":
            output = service.queue.cancel(args.task_id)
        elif args.glm_action == "result":
            output = service.result(args.task_id)
        elif args.glm_action == "digest":
            # No network send. emit-outbox only appends to the existing protected local outbox.
            output = service.digest(dry_run=args.dry_run or not args.emit_outbox, emit_outbox=args.emit_outbox)
        elif args.glm_action == "worker":
            output = (
                service.process_task(args.task_id, force=args.force)
                if args.task_id
                else service.process(force=args.force, limit=args.limit)
            )
        elif args.glm_action == "metrics":
            output = service.queue.metrics()
        elif args.glm_action in {"pause", "resume"}:
            service.queue.set_paused(args.glm_action == "pause")
            output = {"status": "paused" if args.glm_action == "pause" else "resumed"}
        else:
            output = {"status": "unsupported_command"}
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        output = {"status": "input_error", "error": str(exc)}
        _print(output)
        return 2
    except Exception as exc:
        output = {"status": "failed", "error": f"{type(exc).__name__}:{str(exc)[:300]}"}
        _print(output)
        return 1
    _print(output)
    if isinstance(output, dict) and output.get("status") in {
        "not_found",
        "input_error",
        "max_attempts_reached",
        "active_run_not_retryable",
        "task_already_running",
        "task_not_claimable",
    }:
        return 2
    if isinstance(output, dict) and output.get("state") == "invalid_input":
        return 2
    return 0


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))
