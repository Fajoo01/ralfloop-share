#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ralfloop_agent.integration.gpt_browser_cdp import ChromeCdp, CdpError
from ralfloop_agent.integration.gpt_session_rollover import (
    Handoff,
    HandoffStore,
    RolloverPolicy,
    SessionMetrics,
    evaluate_rollover,
)


def _json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_status(args: argparse.Namespace) -> int:
    cdp = ChromeCdp(args.endpoint)
    try:
        health = cdp.health()
        targets = cdp.targets()
        browser = {
            "ok": True,
            "browser": health.get("Browser"),
            "endpoint": args.endpoint,
            "chatgpt_tabs": [
                {"id": t.target_id, "title": t.title, "url": t.url}
                for t in targets
                if t.target_type == "page" and t.is_chatgpt
            ],
        }
    except CdpError as exc:
        browser = {"ok": False, "endpoint": args.endpoint, "error": str(exc)}

    store = HandoffStore(args.state_dir)
    handoff = {"present": store.current_path.exists(), "path": str(store.current_path)}
    if handoff["present"]:
        current = store.load_current()
        handoff.update(
            {
                "schema_version": current.get("schema_version"),
                "created_at": current.get("created_at"),
                "goal": current.get("goal"),
                "next_action": current.get("next_action"),
            }
        )
    _json({"browser": browser, "handoff": handoff})
    return 0


def cmd_decide(args: argparse.Namespace) -> int:
    metrics = SessionMetrics(
        turns=args.turns,
        age_minutes=args.age_minutes,
        consecutive_errors=args.errors,
        last_response_latency_ms=args.latency_ms,
        phase_boundary=args.phase_boundary,
        manual=args.manual,
    )
    policy = RolloverPolicy(
        max_turns=args.max_turns,
        max_age_minutes=args.max_age_minutes,
        max_consecutive_errors=args.max_errors,
        max_response_latency_ms=args.max_latency_ms,
    )
    decision = evaluate_rollover(metrics, policy)
    _json({"rollover": decision.rollover, "reasons": list(decision.reasons)})
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("checkpoint input must be a JSON object")
    handoff = Handoff(**payload)
    store = HandoffStore(args.state_dir)
    archive = store.save(handoff)
    _json({"current": str(store.current_path), "archive": str(archive)})
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    sys.stdout.write(HandoffStore(args.state_dir).render_prompt())
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    cdp = ChromeCdp(args.endpoint)
    if not args.apply:
        old = [
            {"id": t.target_id, "title": t.title, "url": t.url}
            for t in cdp.targets()
            if t.target_type == "page" and t.is_chatgpt
        ]
        _json(
            {
                "dry_run": True,
                "would_close_local_chatgpt_tabs": old,
                "would_clear_browser_cache": True,
                "would_delete_server_chat": False,
            }
        )
        return 0
    result = cdp.rotate_chatgpt_tab(close_old=True)
    result["dry_run"] = False
    _json(result)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bot-tazzi dedicated GPT browser/session controller")
    parser.add_argument("--endpoint", default="http://127.0.0.1:9237")
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    decide = sub.add_parser("decide")
    decide.add_argument("--turns", type=int, default=0)
    decide.add_argument("--age-minutes", type=int, default=0)
    decide.add_argument("--errors", type=int, default=0)
    decide.add_argument("--latency-ms", type=int, default=0)
    decide.add_argument("--phase-boundary", action="store_true")
    decide.add_argument("--manual", action="store_true")
    decide.add_argument("--max-turns", type=int, default=36)
    decide.add_argument("--max-age-minutes", type=int, default=120)
    decide.add_argument("--max-errors", type=int, default=2)
    decide.add_argument("--max-latency-ms", type=int, default=30000)
    decide.set_defaults(func=cmd_decide)

    checkpoint = sub.add_parser("checkpoint")
    checkpoint.add_argument("--input", required=True)
    checkpoint.set_defaults(func=cmd_checkpoint)

    render = sub.add_parser("render")
    render.set_defaults(func=cmd_render)

    rotate = sub.add_parser("rotate")
    rotate.add_argument("--apply", action="store_true", help="actually close local ChatGPT tabs and rotate")
    rotate.set_defaults(func=cmd_rotate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
