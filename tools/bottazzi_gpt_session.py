#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ralfloop_agent.integration.gpt_browser_cdp import CHATGPT_ORIGIN, ChromeCdp, CdpError
from ralfloop_agent.integration.gpt_session_rollover import (
    ExternalChatAdoptionStore,
    GptSessionError,
    Handoff,
    HandoffStore,
    RolloverPolicy,
    SessionMetrics,
    evaluate_rollover,
    normalize_chatgpt_conversation_url,
    select_external_conversation,
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



def _resolve_stored_source(tabs, store: HandoffStore):
    if not store.current_path.exists():
        return None, {}, None
    data = store.load_current()
    stored_source = str(data.get("source_chat") or "")
    stored_url = normalize_chatgpt_conversation_url(str(data.get("source_chat_url") or ""))
    if stored_source:
        source = next((tab for tab in tabs if tab.target_id == stored_source), None)
        if source is not None:
            current_url = normalize_chatgpt_conversation_url(source.url)
            if current_url != stored_url:
                store.update_source_chat(source.target_id, current_url)
                stored_url = current_url
            return source, {"source_recovered": False, "source_chat_url": stored_url}, None
    if stored_url:
        matches = [tab for tab in tabs if normalize_chatgpt_conversation_url(tab.url) == stored_url]
        if len(matches) == 1:
            source = matches[0]
            store.update_source_chat(source.target_id, stored_url)
            return source, {"source_recovered": True, "previous_source_target_id": stored_source or None, "source_target_id": source.target_id, "source_chat_url": stored_url}, None
        if len(matches) > 1:
            return None, {"source_chat_url": stored_url, "match_count": len(matches)}, "stored_source_url_ambiguous"
    if stored_source:
        return None, {"source_target_id": stored_source}, "stored_source_not_found"
    return None, {}, None



def cmd_adopt_external(args: argparse.Namespace) -> int:
    cdp = ChromeCdp(args.endpoint)
    store = HandoffStore(args.state_dir)
    adoption = ExternalChatAdoptionStore(args.state_dir)
    had_state = adoption.path.exists()
    try:
        adoption_state = adoption.load()
        tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    except (CdpError, GptSessionError) as exc:
        _json({"ok": False, "action": "noop", "reason": str(exc)})
        return 0
    if not tabs:
        _json({"ok": False, "action": "noop", "reason": "chatgpt_tab_not_found"})
        return 0

    try:
        source, source_resolution, source_error = _resolve_stored_source(tabs, store)
    except GptSessionError as exc:
        _json({"ok": False, "action": "noop", "reason": str(exc)})
        return 0
    if source_error:
        _json({"ok": False, "action": "noop", "reason": source_error, "chatgpt_tab_count": len(tabs), **source_resolution})
        return 0
    if source is None:
        if len(tabs) != 1:
            _json({"ok": False, "action": "noop", "reason": "chatgpt_source_ambiguous", "chatgpt_tab_count": len(tabs)})
            return 0
        source = tabs[0]

    now = int(time.time())
    watcher_id = str(adoption_state.get("watcher_target_id") or "")
    watcher = next((tab for tab in tabs if tab.target_id == watcher_id), None) if watcher_id else None
    last_scan = int(adoption_state.get("last_scan_epoch") or 0)
    if had_state and watcher is not None and now - last_scan < max(0, args.scan_interval_seconds):
        _json({"ok": True, "action": "noop", "reason": "scan_interval", "next_scan_in_seconds": max(0, args.scan_interval_seconds - (now - last_scan))})
        return 0

    watcher_created = False
    try:
        if watcher is None:
            watcher_id = cdp.create_chatgpt_target(clear_cache=False)
            watcher_created = True
        conversation_urls = cdp.conversation_urls(watcher_id, reload=not watcher_created)
        tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
        source = next((tab for tab in tabs if tab.target_id == source.target_id), source)
    except CdpError as exc:
        if watcher_created and watcher_id:
            try:
                cdp.close_target(watcher_id)
            except CdpError:
                pass
        _json({"ok": False, "action": "noop", "reason": str(exc), "watcher_target_id": watcher_id or None})
        return 0

    source_url = normalize_chatgpt_conversation_url(source.url)
    seen_urls = list(adoption_state.get("seen_conversations") or [])
    if not had_state:
        baseline = list(conversation_urls)
        if source_url and source_url not in baseline:
            baseline.append(source_url)
        adoption_state.update(
            {
                "seen_conversations": baseline,
                "watcher_target_id": watcher_id,
                "last_adopted_conversation": None,
                "last_scan_epoch": now,
            }
        )
        adoption.save(adoption_state)
        _json({"ok": True, "action": "baseline", "conversation_count": len(baseline), "watcher_target_id": watcher_id})
        return 0

    open_urls = [tab.url for tab in tabs if tab.target_id != watcher_id]
    candidate = select_external_conversation(
        conversation_urls,
        seen_urls=seen_urls,
        open_urls=open_urls,
        source_url=source.url,
    )
    if candidate is None:
        merged = list(seen_urls)
        for value in conversation_urls:
            normalized = normalize_chatgpt_conversation_url(value)
            if normalized and normalized not in merged:
                merged.append(normalized)
        adoption_state.update({"seen_conversations": merged, "watcher_target_id": watcher_id, "last_scan_epoch": now})
        adoption.save(adoption_state)
        _json({"ok": True, "action": "noop", "reason": "no_external_conversation", "watcher_target_id": watcher_id})
        return 0

    try:
        ui = cdp.chatgpt_ui_state(source.target_id)
    except CdpError as exc:
        _json({"ok": False, "action": "deferred", "reason": str(exc), "conversation_url": candidate})
        return 0
    busy_reason = None
    if bool(ui.get("response_pending")) or bool(ui.get("response_in_progress")):
        busy_reason = "worker_response_active"
    elif int(ui.get("composer_chars") or 0) > 0:
        busy_reason = "unsent_composer_text"
    elif not bool(ui.get("ready")):
        busy_reason = "worker_not_ready"
    if busy_reason:
        adoption_state.update({"watcher_target_id": watcher_id, "last_scan_epoch": now})
        adoption.save(adoption_state)
        _json({"ok": True, "action": "deferred", "reason": busy_reason, "conversation_url": candidate})
        return 0
    if not args.apply:
        adoption_state.update({"watcher_target_id": watcher_id, "last_scan_epoch": now})
        adoption.save(adoption_state)
        _json({"ok": True, "action": "candidate", "conversation_url": candidate, "source_target_id": source.target_id})
        return 0

    try:
        adopted_ui = cdp.navigate_chatgpt_conversation(source.target_id, candidate)
    except CdpError as exc:
        _json({"ok": False, "action": "deferred", "reason": str(exc), "conversation_url": candidate})
        return 0
    try:
        store.update_source_chat(source.target_id, candidate)
    except (GptSessionError, OSError) as exc:
        _json({"ok": False, "action": "adopted", "reason": f"source_identity_persist_failed:{type(exc).__name__}", "conversation_url": candidate, "source_target_id": source.target_id, "server_chat_deleted": False})
        return 0
    updated_seen = list(seen_urls)
    for value in (source_url, candidate):
        if value and value not in updated_seen:
            updated_seen.append(value)
    adoption_state.update(
        {
            "seen_conversations": updated_seen,
            "watcher_target_id": watcher_id,
            "last_adopted_conversation": candidate,
            "last_scan_epoch": now,
        }
    )
    adoption.save(adoption_state)
    _json(
        {
            "ok": True,
            "action": "adopted",
            "conversation_url": candidate,
            "source_target_id": source.target_id,
            "user_turns": int(adopted_ui.get("user_turns") or 0),
            "server_chat_deleted": False,
        }
    )
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
        try:
            source, source_resolution, source_error = _resolve_stored_source(tabs, store)
        except GptSessionError as exc:
            _json({"ok": False, "action": "noop", "reason": str(exc)})
            return 0
        if source_error:
            _json({"ok": False, "action": "noop", "reason": source_error, "chatgpt_tab_count": len(tabs), **source_resolution})
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
    new_source_url = None
    try:
        current_target = next((tab for tab in cdp.targets() if tab.target_id == new_target_id), None)
        if current_target is not None:
            new_source_url = normalize_chatgpt_conversation_url(current_target.url)
    except CdpError:
        pass
    try:
        store.update_source_chat(new_target_id, new_source_url)
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

    adopt = sub.add_parser("adopt-external")
    adopt.add_argument("--apply", action="store_true")
    adopt.add_argument("--scan-interval-seconds", type=int, default=30)
    adopt.set_defaults(func=cmd_adopt_external)

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
