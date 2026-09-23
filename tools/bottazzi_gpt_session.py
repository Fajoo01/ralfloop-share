#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ralfloop_agent.integration.gpt_browser_cdp import ChromeCdp, CdpError
from ralfloop_agent.integration.gpt_session_rollover import (
    GptSessionError,
    Handoff,
    HandoffStore,
    RolloverPolicy,
    SessionMetrics,
    evaluate_rollover,
    session_metrics_from_ui,
    should_defer_latency_rollover,
)


def _json(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_status(args: argparse.Namespace) -> int:
    cdp = ChromeCdp(args.endpoint)
    try:
        health = cdp.health()
        targets = cdp.targets()
        tabs = [t for t in targets if t.target_type == "page" and t.is_chatgpt]
        tab_rows = []
        for t in tabs:
            row = {"id": t.target_id, "title": t.title, "url": t.url}
            try:
                row["ui"] = cdp.chatgpt_ui_state(t.target_id)
            except CdpError as exc:
                row["ui"] = {
                    "ready": False,
                    "target_id": t.target_id,
                    "reason": "cdp_probe_failed",
                    "error": str(exc),
                }
            tab_rows.append(row)
        browser = {
            "ok": True,
            "browser": health.get("Browser"),
            "endpoint": args.endpoint,
            "chatgpt_tabs": tab_rows,
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



def cmd_shepherd(args: argparse.Namespace) -> int:
    cdp = ChromeCdp(args.endpoint)
    tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    if not tabs:
        _json({"ok": False, "action": "noop", "reason": "chatgpt_tab_not_found"})
        return 0
    store = HandoffStore(args.state_dir)
    source = None
    if args.source_target_id:
        source = next((tab for tab in tabs if tab.target_id == args.source_target_id), None)
        if source is None:
            _json({"ok": False, "action": "noop", "reason": "source_target_not_found", "source_target_id": args.source_target_id})
            return 0
    else:
        stored_source = ""
        if store.current_path.exists():
            stored_source = str(store.load_current().get("source_chat") or "")
        if stored_source:
            source = next((tab for tab in tabs if tab.target_id == stored_source), None)
            if source is None and len(tabs) > 1:
                _json({"ok": False, "action": "noop", "reason": "stored_source_not_found", "source_target_id": stored_source, "chatgpt_tab_count": len(tabs)})
                return 0
        if source is None:
            if len(tabs) != 1:
                _json({"ok": False, "action": "noop", "reason": "chatgpt_source_ambiguous", "chatgpt_tab_count": len(tabs)})
                return 0
            source = tabs[0]
    ui = cdp.chatgpt_ui_state(source.target_id)
    metrics = session_metrics_from_ui(ui)
    policy = RolloverPolicy(
        max_turns=args.max_turns,
        max_age_minutes=args.max_age_minutes,
        max_consecutive_errors=args.max_errors,
        max_response_latency_ms=args.max_latency_ms,
    )
    decision = evaluate_rollover(metrics, policy)
    defer_latency_rollover = should_defer_latency_rollover(
        decision.reasons,
        response_pending=bool(ui.get("response_pending")),
        response_in_progress=bool(ui.get("response_in_progress")),
        response_idle_ms=int(ui.get("response_idle_ms") or 0),
        max_idle_ms=args.max_stall_ms,
        max_active_idle_ms=args.max_active_stall_ms,
    )
    report = {
        "ok": True,
        "ui": ui,
        "rollover": decision.rollover,
        "reasons": list(decision.reasons),
        "defer_latency_rollover": defer_latency_rollover,
        "applied": False,
    }
    if not decision.rollover:
        _json(report)
        return 0
    if defer_latency_rollover:
        report["blocked"] = "latency_deferred"
        _json(report)
        return 0
    if not ui.get("ready"):
        report["blocked"] = "interaction_required" if ui.get("interaction_required") else "composer_not_ready"
        _json(report)
        return 0
    if not args.apply:
        report["dry_run"] = True
        _json(report)
        return 0
    prompt = store.render_prompt()
    try:
        handoff = cdp.handoff_to_new_chat(
            prompt,
            source_target_id=source.target_id,
            submit=args.submit,
            close_source=False,
        )
    except CdpError as exc:
        report["ok"] = False
        report["blocked"] = str(exc)
        _json(report)
        return 0
    new_target_id = str(handoff.get("new_target_id") or "")
    if not new_target_id:
        report["ok"] = False
        report["blocked"] = "handoff_target_missing"
        _json(report)
        return 0
    try:
        store.update_source_chat(new_target_id)
    except (GptSessionError, OSError) as exc:
        try:
            cdp.close_target(new_target_id)
        except CdpError:
            pass
        report["ok"] = False
        report["blocked"] = f"handoff_persist_failed:{type(exc).__name__}"
        report["source_preserved"] = True
        _json(report)
        return 0
    closed: list[str] = []
    if source.target_id != new_target_id:
        try:
            cdp.close_target(source.target_id)
        except CdpError as exc:
            report["source_close_error"] = str(exc)
        else:
            closed.append(source.target_id)
    handoff["closed_target_ids"] = closed
    report["applied"] = True
    report["handoff"] = handoff
    report["worker_target_id"] = new_target_id
    _json(report)
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bot-tazzi dedicated GPT browser/session controller")
    parser.add_argument("--endpoint", default="http://127.0.0.1:9238")
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

    shepherd = sub.add_parser("shepherd")
    shepherd.add_argument("--apply", action="store_true")
    shepherd.add_argument("--submit", action="store_true")
    shepherd.add_argument("--source-target-id", default=None)
    shepherd.add_argument("--max-turns", type=int, default=36)
    shepherd.add_argument("--max-age-minutes", type=int, default=120)
    shepherd.add_argument("--max-errors", type=int, default=2)
    shepherd.add_argument("--max-latency-ms", type=int, default=30000)
    shepherd.add_argument("--max-stall-ms", type=int, default=60000)
    shepherd.add_argument("--max-active-stall-ms", type=int, default=600000)
    shepherd.set_defaults(func=cmd_shepherd)

    rotate = sub.add_parser("rotate")
    rotate.add_argument("--apply", action="store_true", help="actually close local ChatGPT tabs and rotate")
    rotate.set_defaults(func=cmd_rotate)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
