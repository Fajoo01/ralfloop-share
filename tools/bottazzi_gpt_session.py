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
    EXTERNAL_UNVALIDATED_TTL_SECONDS,
    ExternalChatAdoptionStore,
    GptSessionError,
    Handoff,
    HandoffStore,
    MutationJournalStore,
    MutationLock,
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
        return None, {"source_target_id": stored_source, "source_chat_url": stored_url}, "stored_source_not_found"
    return None, {}, None


def _recover_stored_source_home_tab(cdp: ChromeCdp, tabs, store: HandoffStore, resolution: dict, error: str | None):
    if error != "stored_source_not_found" or len(tabs) != 1:
        return None, resolution, error
    stored_url = normalize_chatgpt_conversation_url(str(resolution.get("source_chat_url") or ""))
    candidate = tabs[0]
    if not stored_url or normalize_chatgpt_conversation_url(candidate.url):
        return None, resolution, error
    # Never hijack the visible Home tab after a browser restart. The persisted
    # worker may be old compared with conversations opened on another client.
    # Recovery continues in a dedicated background target instead.
    return None, {**resolution, "foreground_preserved": True}, error


def _recover_stored_source_new_tab(cdp: ChromeCdp, store: HandoffStore, resolution: dict, error: str | None):
    if error != "stored_source_not_found":
        return None, resolution, error
    stored_url = normalize_chatgpt_conversation_url(str(resolution.get("source_chat_url") or ""))
    if not stored_url:
        return None, resolution, error
    target_id = None
    try:
        target_id = cdp.create_chatgpt_target(clear_cache=False, background=True)
        ui = cdp.navigate_chatgpt_conversation(target_id, stored_url)
        if not bool(ui.get("authenticated")) or not bool(ui.get("ready")):
            raise CdpError("stored_source_recovery_target_not_ready")
        refreshed = next((tab for tab in cdp.targets() if tab.target_id == target_id), None)
        if refreshed is None or normalize_chatgpt_conversation_url(refreshed.url) != stored_url:
            raise CdpError("stored_source_recovery_url_mismatch")
        store.update_source_chat(target_id, stored_url)
    except (CdpError, GptSessionError, OSError) as exc:
        if target_id:
            try:
                cdp.close_target(target_id)
            except CdpError:
                pass
        return None, {**resolution, "source_recovery_error": str(exc)}, "stored_source_recovery_failed"
    return refreshed, {
        "source_recovered": True,
        "source_recovered_by_new_tab": True,
        "previous_source_target_id": resolution.get("source_target_id"),
        "source_target_id": target_id,
        "source_chat_url": stored_url,
    }, None


def _archive_source_conversation(
    cdp: ChromeCdp,
    source_target_id: str,
    source_url: str,
    *,
    allow_already_archived: bool = False,
    archive_started_hook=None,
) -> dict:
    normalized = normalize_chatgpt_conversation_url(source_url)
    if not normalized:
        raise GptSessionError("source_conversation_url_missing")
    tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    target = next(
        (
            tab
            for tab in tabs
            if tab.target_id == source_target_id and normalize_chatgpt_conversation_url(tab.url) == normalized
        ),
        None,
    )
    temporary_target_id = None
    try:
        if target is None:
            temporary_target_id = cdp.create_chatgpt_target(clear_cache=False, background=True)
            cdp.navigate_chatgpt_conversation(temporary_target_id, normalized)
            target_id = temporary_target_id
        else:
            target_id = target.target_id
        result = cdp.archive_chatgpt_conversation(
            target_id,
            normalized,
            allow_absent=allow_already_archived,
            archive_started_hook=archive_started_hook,
        )
        if not bool(result.get("archived")):
            raise CdpError("conversation_archive_not_confirmed")
        return result
    finally:
        if temporary_target_id:
            try:
                cdp.close_target(temporary_target_id)
            except CdpError:
                pass


def _archive_source_with_journal(
    cdp: ChromeCdp,
    journal: MutationJournalStore,
    phase: str,
    source_target_id: str,
    source_url: str,
) -> dict:
    archive_may_have_started = phase in {"archive_started", "source_archived", "source_ghosted", "source_closed", "committed"}
    started_hook = None if archive_may_have_started else lambda: journal.update(phase="archive_started")
    result = _archive_source_conversation(
        cdp,
        source_target_id,
        source_url,
        allow_already_archived=archive_may_have_started,
        archive_started_hook=started_hook,
    )
    journal.update(phase="source_archived")
    return result


def _install_rollover_ghost(
    cdp: ChromeCdp,
    journal: MutationJournalStore,
    *,
    source_target_id: str,
    successor_target_id: str,
    successor_url: str,
) -> dict:
    human_target = cdp.install_human_input_target(successor_target_id, successor_url)
    tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    source = next((tab for tab in tabs if tab.target_id == source_target_id), None)
    ghost = None
    if source is not None and source.target_id != successor_target_id:
        ghost = cdp.mark_chatgpt_ghost_tab(source.target_id, successor_url=successor_url)
    journal.update(phase="source_ghosted", source_ghosted=bool(ghost))
    return {"human_input_target": human_target, "ghost": ghost, "source_ghosted": bool(ghost)}


def _unlock_rollover_source(cdp: ChromeCdp, source_target_id: str) -> None:
    tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    if any(tab.target_id == source_target_id for tab in tabs):
        cdp.unlock_human_input_after_failed_handoff(source_target_id)


def _finalize_adoption_state(
    adoption: ExternalChatAdoptionStore,
    *,
    source_url: str | None,
    candidate_url: str,
    now: int,
) -> None:
    state = adoption.load()
    seen = list(state.get("seen_conversations") or [])
    for value in (source_url, candidate_url):
        if value and value not in seen:
            seen.append(value)
    remaining_unvalidated = []
    for value in state.get("unvalidated_candidates") or []:
        if not isinstance(value, dict):
            continue
        normalized = normalize_chatgpt_conversation_url(str(value.get("conversation_url") or ""))
        if normalized != candidate_url:
            remaining_unvalidated.append(value)
    state.update(
        {
            "seen_conversations": seen,
            "last_adopted_conversation": candidate_url,
            "pending_conversation": None,
            "pending_detected_epoch": 0,
            "unvalidated_candidates": remaining_unvalidated,
            "last_scan_epoch": max(int(state.get("last_scan_epoch") or 0), now),
        }
    )
    adoption.save(state)


def _recover_incomplete_mutation(
    cdp: ChromeCdp,
    store: HandoffStore,
    adoption: ExternalChatAdoptionStore,
    journal: MutationJournalStore,
) -> dict | None:
    entry = journal.load()
    if entry is None:
        return None
    kind = str(entry.get("kind") or "")
    phase = str(entry.get("phase") or "")
    tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    current = store.load_current()
    current_url = normalize_chatgpt_conversation_url(str(current.get("source_chat_url") or ""))

    if kind == "adopt_external":
        source_target_id = str(entry.get("source_target_id") or "")
        source_url = normalize_chatgpt_conversation_url(str(entry.get("source_url") or ""))
        candidate_url = normalize_chatgpt_conversation_url(str(entry.get("candidate_url") or ""))
        if not source_target_id or not source_url or not candidate_url:
            raise GptSessionError("mutation_recovery_required:adopt_external:invalid_journal")
        target = next((tab for tab in tabs if tab.target_id == source_target_id), None)
        target_url = normalize_chatgpt_conversation_url(target.url) if target is not None else None
        if current_url == candidate_url:
            resolved, _, error = _resolve_stored_source(tabs, store)
            if error:
                raise GptSessionError(f"mutation_recovery_required:adopt_external:{error}")
            if resolved is None or normalize_chatgpt_conversation_url(resolved.url) != candidate_url:
                raise GptSessionError("mutation_recovery_required:adopt_external:candidate_tab_missing")
            _archive_source_with_journal(cdp, journal, phase, source_target_id, source_url)
            _finalize_adoption_state(adoption, source_url=source_url, candidate_url=candidate_url, now=int(time.time()))
            journal.clear()
            return {"kind": kind, "outcome": "committed", "phase": phase, "conversation_url": candidate_url, "source_chat_archived": True}
        if target is not None and target_url == candidate_url:
            store.update_source_chat(target.target_id, candidate_url)
            journal.update(phase="source_state_done")
            _archive_source_with_journal(cdp, journal, phase, source_target_id, source_url)
            _finalize_adoption_state(adoption, source_url=source_url, candidate_url=candidate_url, now=int(time.time()))
            journal.clear()
            return {"kind": kind, "outcome": "committed", "phase": phase, "conversation_url": candidate_url, "source_chat_archived": True}
        if current_url == source_url:
            resolved, _, error = _resolve_stored_source(tabs, store)
            if error:
                raise GptSessionError(f"mutation_recovery_required:adopt_external:{error}")
            if resolved is not None and normalize_chatgpt_conversation_url(resolved.url) == source_url:
                journal.clear()
                return {"kind": kind, "outcome": "rolled_back", "phase": phase}
        raise GptSessionError("mutation_recovery_required:adopt_external:ambiguous_state")

    if kind == "rollover":
        source_target_id = str(entry.get("source_target_id") or "")
        source_url = normalize_chatgpt_conversation_url(str(entry.get("source_url") or ""))
        successor_target_id = str(entry.get("successor_target_id") or "")
        if not source_target_id or not source_url:
            raise GptSessionError("mutation_recovery_required:rollover:invalid_journal")
        source_tab = next((tab for tab in tabs if tab.target_id == source_target_id), None)
        successor = next((tab for tab in tabs if tab.target_id == successor_target_id), None) if successor_target_id else None
        if successor is not None:
            successor_url = normalize_chatgpt_conversation_url(successor.url)
            if successor_url:
                try:
                    successor_ui = cdp.chatgpt_ui_state(successor.target_id)
                except CdpError as exc:
                    raise GptSessionError(f"mutation_recovery_pending:rollover:{exc}") from exc
                if int(successor_ui.get("user_turns") or 0) >= 1:
                    store.update_source_chat(successor.target_id, successor_url)
                    journal.update(phase="source_state_done", successor_url=successor_url)
                    _archive_source_with_journal(cdp, journal, phase, source_target_id, source_url)
                    ghost_state = _install_rollover_ghost(
                        cdp,
                        journal,
                        source_target_id=source_target_id,
                        successor_target_id=successor.target_id,
                        successor_url=successor_url,
                    )
                    journal.update(phase="committed")
                    journal.clear()
                    return {
                        "kind": kind,
                        "outcome": "committed",
                        "phase": phase,
                        "source_chat_url": successor_url,
                        "source_chat_archived": True,
                        "source_chat_ghosted": bool(ghost_state.get("source_ghosted")),
                    }
            cdp.close_target(successor.target_id)
            current = store.load_current()
            current_url = normalize_chatgpt_conversation_url(str(current.get("source_chat_url") or ""))
            if current_url == source_url:
                _unlock_rollover_source(cdp, source_target_id)
                journal.clear()
                return {"kind": kind, "outcome": "rolled_back", "phase": phase}
            raise GptSessionError("mutation_recovery_required:rollover:unconfirmed_successor")
        if phase in {"source_state_done", "archive_started", "source_archived", "source_ghosted", "source_closed", "committed"} and current_url and current_url != source_url:
            resolved, _, error = _resolve_stored_source(tabs, store)
            if error:
                raise GptSessionError(f"mutation_recovery_required:rollover:{error}")
            if resolved is not None and normalize_chatgpt_conversation_url(resolved.url) == current_url:
                _archive_source_with_journal(cdp, journal, phase, source_target_id, source_url)
                ghost_state = _install_rollover_ghost(
                    cdp,
                    journal,
                    source_target_id=source_target_id,
                    successor_target_id=resolved.target_id,
                    successor_url=current_url,
                )
                journal.update(phase="committed")
                journal.clear()
                return {
                    "kind": kind,
                    "outcome": "committed",
                    "phase": phase,
                    "source_chat_url": current_url,
                    "source_chat_archived": True,
                    "source_chat_ghosted": bool(ghost_state.get("source_ghosted")),
                }
        if current_url == source_url:
            resolved, _, error = _resolve_stored_source(tabs, store)
            if error:
                raise GptSessionError(f"mutation_recovery_required:rollover:{error}")
            if resolved is not None and normalize_chatgpt_conversation_url(resolved.url) == source_url:
                _unlock_rollover_source(cdp, source_target_id)
                journal.clear()
                return {"kind": kind, "outcome": "rolled_back", "phase": phase}
        raise GptSessionError("mutation_recovery_required:rollover:ambiguous_state")

    raise GptSessionError(f"mutation_recovery_required:unknown_kind:{kind or 'missing'}")


def cmd_adopt_external(args: argparse.Namespace) -> int:
    cdp = ChromeCdp(args.endpoint)
    store = HandoffStore(args.state_dir)
    adoption = ExternalChatAdoptionStore(args.state_dir)
    journal = MutationJournalStore(args.state_dir)
    if args.apply:
        try:
            recovered = _recover_incomplete_mutation(cdp, store, adoption, journal)
        except (CdpError, GptSessionError, OSError) as exc:
            _json({"ok": False, "action": "noop", "reason": str(exc), "recovery_required": True})
            return 1
        if recovered is not None:
            _json({"ok": True, "action": "recovered", "recovery": recovered})
            return 0
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
        if source_error:
            recovered_source, source_resolution, source_error = _recover_stored_source_home_tab(
                cdp, tabs, store, source_resolution, source_error
            )
            if recovered_source is not None:
                source = recovered_source
        if source_error:
            recovered_source, source_resolution, source_error = _recover_stored_source_new_tab(
                cdp, store, source_resolution, source_error
            )
            if recovered_source is not None:
                source = recovered_source
                tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    except (CdpError, GptSessionError) as exc:
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

    # A human-selected conversation in the headed browser is authoritative for
    # interactive input. Background watcher/recovery tabs never have focus, and
    # ghost tabs are explicitly excluded so clicking an old ghost cannot make it
    # the worker again.
    foreground_tabs = []
    if hasattr(cdp, "chatgpt_focus_state"):
        try:
            for tab in tabs:
                if not normalize_chatgpt_conversation_url(tab.url):
                    continue
                focus_state = cdp.chatgpt_focus_state(tab.target_id)
                if focus_state.get("focused") and not focus_state.get("ghost") and not focus_state.get("handoff_locked"):
                    foreground_tabs.append(tab)
        except CdpError as exc:
            _json({"ok": False, "action": "noop", "reason": str(exc)})
            return 0
    if len(foreground_tabs) == 1 and foreground_tabs[0].target_id != source.target_id:
        foreground = foreground_tabs[0]
        foreground_url = normalize_chatgpt_conversation_url(foreground.url)
        try:
            foreground_ui = cdp.chatgpt_ui_state(foreground.target_id)
        except CdpError as exc:
            _json({"ok": False, "action": "noop", "reason": str(exc)})
            return 0
        if foreground_url and foreground_ui.get("authenticated"):
            if not args.apply:
                _json({"ok": True, "action": "candidate", "reason": "foreground_chat", "conversation_url": foreground_url, "source_target_id": source.target_id})
                return 0
            old_source = source
            old_source_url = normalize_chatgpt_conversation_url(old_source.url)
            try:
                cdp.install_human_input_target(foreground.target_id, foreground_url)
                store.update_source_chat(foreground.target_id, foreground_url)
                ghosted = False
                if old_source.target_id != foreground.target_id and old_source_url:
                    ghosted = bool(cdp.mark_chatgpt_ghost_tab(old_source.target_id, successor_url=foreground_url).get("ghost"))
                source = foreground
                seen = list(adoption_state.get("seen_conversations") or [])
                if foreground_url not in seen:
                    seen.append(foreground_url)
                adoption_state.update({
                    "seen_conversations": seen,
                    "last_adopted_conversation": foreground_url,
                    "watcher_target_id": None if str(adoption_state.get("watcher_target_id") or "") == foreground.target_id else adoption_state.get("watcher_target_id"),
                    "pending_conversation": None if normalize_chatgpt_conversation_url(str(adoption_state.get("pending_conversation") or "")) == foreground_url else adoption_state.get("pending_conversation"),
                    "pending_detected_epoch": 0 if normalize_chatgpt_conversation_url(str(adoption_state.get("pending_conversation") or "")) == foreground_url else int(adoption_state.get("pending_detected_epoch") or 0),
                })
                adoption.save(adoption_state)
            except (CdpError, GptSessionError, OSError) as exc:
                _json({"ok": False, "action": "deferred", "reason": f"foreground_adoption_failed:{exc}", "conversation_url": foreground_url})
                return 1
            _json({"ok": True, "action": "adopted", "reason": "foreground_chat", "conversation_url": foreground_url, "source_target_id": foreground.target_id, "previous_source_target_id": old_source.target_id, "previous_source_ghosted": ghosted})
            return 0

    now = int(time.time())
    watcher_id = str(adoption_state.get("watcher_target_id") or "")
    if watcher_id and watcher_id == source.target_id:
        adoption_state["watcher_target_id"] = None
        watcher_id = ""
    watcher = next((tab for tab in tabs if tab.target_id == watcher_id), None) if watcher_id else None
    last_scan = int(adoption_state.get("last_scan_epoch") or 0)
    if had_state and watcher is not None and now - last_scan < max(0, args.scan_interval_seconds):
        _json({"ok": True, "action": "noop", "reason": "scan_interval", "next_scan_in_seconds": max(0, args.scan_interval_seconds - (now - last_scan))})
        return 0

    watcher_created = False
    try:
        if watcher is None:
            watcher_id = cdp.create_chatgpt_target(clear_cache=False, background=True)
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
    normalized_history = [
        normalized
        for value in conversation_urls
        if (normalized := normalize_chatgpt_conversation_url(value))
    ]
    normalized_open = {
        normalized
        for value in open_urls
        if (normalized := normalize_chatgpt_conversation_url(value))
    }
    seen_set = {
        normalized
        for value in seen_urls
        if (normalized := normalize_chatgpt_conversation_url(value))
    }
    history_index: dict[str, int] = {}
    for index, url in enumerate(normalized_history):
        history_index.setdefault(url, index)
    pending = normalize_chatgpt_conversation_url(str(adoption_state.get("pending_conversation") or ""))
    candidate = None
    if pending:
        if pending in normalized_history and pending not in seen_set and pending not in normalized_open:
            candidate = pending
        else:
            adoption_state["pending_conversation"] = None
            adoption_state["pending_detected_epoch"] = 0
            pending = None

    unvalidated: list[dict[str, object]] = []
    validated_unvalidated = None
    for value in adoption_state.get("unvalidated_candidates") or []:
        if not isinstance(value, dict):
            continue
        unvalidated_url = normalize_chatgpt_conversation_url(str(value.get("conversation_url") or ""))
        anchor_url = normalize_chatgpt_conversation_url(str(value.get("source_url") or ""))
        detected_epoch = max(0, int(value.get("detected_epoch") or 0))
        if not unvalidated_url or not anchor_url or unvalidated_url == anchor_url:
            continue
        if detected_epoch and now - detected_epoch > EXTERNAL_UNVALIDATED_TTL_SECONDS:
            if unvalidated_url not in seen_set:
                seen_urls.append(unvalidated_url)
                seen_set.add(unvalidated_url)
            continue
        if unvalidated_url in seen_set:
            continue
        if unvalidated_url in normalized_open:
            seen_urls.append(unvalidated_url)
            seen_set.add(unvalidated_url)
            continue
        candidate_index = history_index.get(unvalidated_url)
        anchor_index = history_index.get(anchor_url)
        if candidate_index is not None and anchor_index is not None:
            if candidate_index < anchor_index:
                if candidate is None and validated_unvalidated is None:
                    validated_unvalidated = unvalidated_url
                    continue
                unvalidated.append(
                    {"conversation_url": unvalidated_url, "source_url": anchor_url, "detected_epoch": detected_epoch}
                )
                continue
            if candidate_index > anchor_index:
                seen_urls.append(unvalidated_url)
                seen_set.add(unvalidated_url)
                continue
        unvalidated.append(
            {"conversation_url": unvalidated_url, "source_url": anchor_url, "detected_epoch": detected_epoch}
        )
    adoption_state["unvalidated_candidates"] = unvalidated
    if candidate is None and validated_unvalidated:
        candidate = validated_unvalidated

    if candidate is None:
        if source_url and source_url not in normalized_history:
            tracked = {
                str(value.get("conversation_url"))
                for value in unvalidated
                if isinstance(value, dict) and value.get("conversation_url")
            }
            for conversation_url in normalized_history:
                if conversation_url in seen_set or conversation_url in normalized_open or conversation_url == source_url or conversation_url in tracked:
                    continue
                unvalidated.append(
                    {"conversation_url": conversation_url, "source_url": source_url, "detected_epoch": now}
                )
                tracked.add(conversation_url)
            adoption_state.update(
                {
                    "seen_conversations": seen_urls,
                    "unvalidated_candidates": unvalidated,
                    "watcher_target_id": watcher_id,
                    "last_scan_epoch": now,
                }
            )
            adoption.save(adoption_state)
            _json(
                {
                    "ok": True,
                    "action": "deferred",
                    "reason": "source_not_in_history",
                    "source_chat_url": source_url,
                    "unvalidated_count": len(unvalidated),
                    "watcher_target_id": watcher_id,
                }
            )
            return 0
        candidate = select_external_conversation(
            conversation_urls,
            seen_urls=seen_urls,
            open_urls=open_urls,
            source_url=source.url,
        )
    if candidate is None:
        merged = list(seen_urls)
        protected_unvalidated = {
            str(value.get("conversation_url"))
            for value in unvalidated
            if isinstance(value, dict) and value.get("conversation_url")
        }
        for value in conversation_urls:
            normalized = normalize_chatgpt_conversation_url(value)
            if normalized and normalized not in protected_unvalidated and normalized not in merged:
                merged.append(normalized)
        adoption_state.update(
            {
                "seen_conversations": merged,
                "unvalidated_candidates": unvalidated,
                "watcher_target_id": watcher_id,
                "last_scan_epoch": now,
            }
        )
        adoption.save(adoption_state)
        _json({"ok": True, "action": "noop", "reason": "no_external_conversation", "watcher_target_id": watcher_id})
        return 0

    previous_pending = normalize_chatgpt_conversation_url(str(adoption_state.get("pending_conversation") or ""))
    adoption_state.update(
        {
            "pending_conversation": candidate,
            "pending_detected_epoch": int(adoption_state.get("pending_detected_epoch") or 0) if previous_pending == candidate else now,
            "watcher_target_id": watcher_id,
            "last_scan_epoch": now,
        }
    )
    adoption.save(adoption_state)

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

    if not source_url:
        _json({"ok": False, "action": "deferred", "reason": "source_conversation_url_missing", "conversation_url": candidate})
        return 1
    try:
        journal.begin(
            "adopt_external",
            source_target_id=source.target_id,
            source_url=source_url,
            candidate_url=candidate,
        )
        adopted_ui = cdp.navigate_chatgpt_conversation(source.target_id, candidate)
        journal.update(phase="browser_done")
        store.update_source_chat(source.target_id, candidate)
        journal.update(phase="source_state_done")
        _archive_source_with_journal(cdp, journal, "source_state_done", source.target_id, source_url)
        _finalize_adoption_state(adoption, source_url=source_url, candidate_url=candidate, now=now)
        journal.update(phase="committed")
        journal.clear()
    except (CdpError, GptSessionError, OSError) as exc:
        _json(
            {
                "ok": False,
                "action": "deferred",
                "reason": f"mutation_incomplete:{exc}",
                "conversation_url": candidate,
                "source_target_id": source.target_id,
                "recovery_required": True,
                "server_chat_deleted": False,
            }
        )
        return 1
    _json(
        {
            "ok": True,
            "action": "adopted",
            "conversation_url": candidate,
            "source_target_id": source.target_id,
            "user_turns": int(adopted_ui.get("user_turns") or 0),
            "source_chat_archived": True,
            "server_chat_deleted": False,
        }
    )
    return 0


def cmd_shepherd(args: argparse.Namespace) -> int:
    cdp = ChromeCdp(args.endpoint)
    store = HandoffStore(args.state_dir)
    if args.apply:
        adoption = ExternalChatAdoptionStore(args.state_dir)
        journal = MutationJournalStore(args.state_dir)
        try:
            recovered = _recover_incomplete_mutation(cdp, store, adoption, journal)
        except (CdpError, GptSessionError, OSError) as exc:
            _json({"ok": False, "action": "noop", "reason": str(exc), "recovery_required": True})
            return 1
        if recovered is not None:
            _json({"ok": True, "action": "recovered", "recovery": recovered})
            return 0
    tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
    if not tabs:
        _json({"ok": False, "action": "noop", "reason": "chatgpt_tab_not_found"})
        return 0
    source = None
    if args.source_target_id:
        source = next((tab for tab in tabs if tab.target_id == args.source_target_id), None)
        if source is None:
            _json({"ok": False, "action": "noop", "reason": "source_target_not_found", "source_target_id": args.source_target_id})
            return 0
    else:
        try:
            source, source_resolution, source_error = _resolve_stored_source(tabs, store)
            if source_error:
                recovered_source, source_resolution, source_error = _recover_stored_source_home_tab(
                    cdp, tabs, store, source_resolution, source_error
                )
                if recovered_source is not None:
                    source = recovered_source
            if source_error:
                recovered_source, source_resolution, source_error = _recover_stored_source_new_tab(
                    cdp, store, source_resolution, source_error
                )
                if recovered_source is not None:
                    source = recovered_source
                    tabs = [t for t in cdp.targets() if t.target_type == "page" and t.is_chatgpt]
        except (CdpError, GptSessionError) as exc:
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
    source_url = normalize_chatgpt_conversation_url(source.url)
    if not source_url:
        report["ok"] = False
        report["blocked"] = "source_conversation_url_missing"
        _json(report)
        return 1
    journal = MutationJournalStore(args.state_dir)
    try:
        journal.begin("rollover", source_target_id=source.target_id, source_url=source_url)
        cdp.lock_human_input_during_handoff(source.target_id)
        journal.update(phase="source_human_locked")

        def record_successor(target_id: str) -> None:
            journal.update(phase="target_created", successor_target_id=target_id)

        handoff = cdp.handoff_to_new_chat(
            prompt,
            source_target_id=source.target_id,
            submit=args.submit,
            close_source=False,
            target_created_hook=record_successor,
        )
    except (CdpError, GptSessionError, OSError) as exc:
        report["ok"] = False
        report["blocked"] = str(exc)
        try:
            recovery = _recover_incomplete_mutation(
                cdp,
                store,
                ExternalChatAdoptionStore(args.state_dir),
                journal,
            )
        except (CdpError, GptSessionError, OSError) as recovery_exc:
            report["recovery_required"] = True
            report["recovery_error"] = str(recovery_exc)
        else:
            if recovery is not None:
                report["recovery"] = recovery
        _json(report)
        if str(exc) == "temporary_access_limited" and (report.get("recovery") or {}).get("outcome") == "rolled_back":
            return 0
        return 1
    new_target_id = str(handoff.get("new_target_id") or "")
    if not new_target_id:
        report["ok"] = False
        report["blocked"] = "handoff_target_missing"
        report["recovery_required"] = True
        _json(report)
        return 1
    new_source_url = None
    try:
        current_target = next((tab for tab in cdp.targets() if tab.target_id == new_target_id), None)
        if current_target is not None:
            new_source_url = normalize_chatgpt_conversation_url(current_target.url)
        journal.update(phase="browser_done", successor_target_id=new_target_id, successor_url=new_source_url)
        if not new_source_url:
            report["ok"] = False
            report["blocked"] = "successor_conversation_url_missing"
            report["recovery_required"] = True
            report["worker_target_id"] = new_target_id
            _json(report)
            return 1
        store.update_source_chat(new_target_id, new_source_url)
        journal.update(phase="source_state_done", successor_url=new_source_url)
    except (CdpError, GptSessionError, OSError) as exc:
        report["ok"] = False
        report["blocked"] = f"handoff_persist_incomplete:{exc}"
        report["recovery_required"] = True
        _json(report)
        return 1
    try:
        _archive_source_with_journal(cdp, journal, "source_state_done", source.target_id, source_url)
    except (CdpError, GptSessionError, OSError) as exc:
        report["ok"] = False
        report["blocked"] = f"handoff_archive_incomplete:{exc}"
        report["recovery_required"] = True
        report["worker_target_id"] = new_target_id
        _json(report)
        return 1
    try:
        ghost_state = _install_rollover_ghost(
            cdp,
            journal,
            source_target_id=source.target_id,
            successor_target_id=new_target_id,
            successor_url=new_source_url,
        )
    except (CdpError, GptSessionError, OSError) as exc:
        report["ok"] = False
        report["blocked"] = f"handoff_ghost_incomplete:{exc}"
        report["recovery_required"] = True
        report["worker_target_id"] = new_target_id
        _json(report)
        return 1
    journal.update(phase="committed")
    journal.clear()
    handoff["closed_target_ids"] = []
    handoff["source_chat_archived"] = True
    handoff["source_chat_ghosted"] = bool(ghost_state.get("source_ghosted"))
    handoff["human_input_target"] = bool(ghost_state.get("human_input_target"))
    report["applied"] = True
    report["handoff"] = handoff
    report["source_chat_archived"] = True
    report["source_chat_ghosted"] = bool(ghost_state.get("source_ghosted"))
    report["human_input_target"] = bool(ghost_state.get("human_input_target"))
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


def _requires_mutation_lock(args: argparse.Namespace) -> bool:
    if args.command in {"checkpoint", "adopt-external", "shepherd"}:
        return True
    if args.command == "rotate":
        return bool(getattr(args, "apply", False))
    return False


def main() -> int:
    args = build_parser().parse_args()
    if not _requires_mutation_lock(args):
        return int(args.func(args))
    try:
        with MutationLock(args.state_dir):
            return int(args.func(args))
    except GptSessionError as exc:
        reason = str(exc)
        if reason == "mutation_locked":
            _json({"ok": True, "action": "deferred", "reason": reason})
            return 0
        _json({"ok": False, "action": "noop", "reason": reason})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
