from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .gpt_browser_cdp import (
    CHATGPT_ORIGIN,
    ChromeCdp,
    CdpError,
    _canonical_chatgpt_conversation_url,
)
from .gpt_session_rollover import chatgpt_project_new_chat_url
from .gpt_work_queue import GptJobState, GptWorkJob, GptWorkQueue


ROOT = Path(__file__).resolve().parents[2]
UI_PATH = ROOT / "web" / "gpt_queue.html"
OCCUPYING_STATES = {GptJobState.STARTING, GptJobState.ACTIVE}


class ApiInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateJob(ApiInput):
    title: str = Field(min_length=1, max_length=300)
    prompt: str = Field(default="", max_length=32_000)
    project_name: str = Field(default="", max_length=300)
    project_url: str | None = Field(default=None, max_length=1200)
    auto_start: bool = False


class ImportChat(ApiInput):
    target_id: str = Field(min_length=1, max_length=160)


class ResumeChat(ApiInput):
    conversation_url: str = Field(min_length=1, max_length=1200)
    conversation_context_url: str | None = Field(default=None, max_length=1200)
    title: str = Field(default="Chat GPT", min_length=1, max_length=300)
    project_name: str = Field(default="", max_length=300)
    project_url: str | None = Field(default=None, max_length=1200)


class SendMessage(ApiInput):
    text: str = Field(min_length=1, max_length=32_000)


class MoveJob(ApiInput):
    rank: int = Field(ge=1, le=10_000)


class SetLimit(ApiInput):
    max_open_chats: int = Field(ge=1, le=12)


class SetJobState(ApiInput):
    state: GptJobState = Field(strict=False)


@dataclass(frozen=True)
class BrowserSnapshot:
    targets_by_id: dict[str, Any]
    targets_by_conversation: dict[str, Any]
    ambiguous_conversations: frozenset[str]
    open_conversations: int
    blank_chat_tabs: int


class GptWorkController:
    """Thin controller joining the durable queue to the dedicated GPT browser.

    The queue database is the canonical work list. Browser target IDs are treated
    as ephemeral witnesses and are rebound by canonical conversation URL after a
    browser restart. No chat is deleted or archived by this controller.
    """

    def __init__(self, queue: GptWorkQueue, cdp: ChromeCdp) -> None:
        self.queue = queue
        self.cdp = cdp
        self._project_url_cache: dict[str, str] = {}

    def browser_snapshot(self) -> BrowserSnapshot:
        by_id: dict[str, Any] = {}
        grouped: dict[str, list[Any]] = {}
        blank = 0
        for target in self.cdp.targets():
            if target.target_type != "page" or not target.is_chatgpt:
                continue
            by_id[target.target_id] = target
            conversation = _canonical_chatgpt_conversation_url(target.url)
            if conversation:
                grouped.setdefault(conversation, []).append(target)
            else:
                blank += 1
        ambiguous = frozenset(url for url, targets in grouped.items() if len(targets) != 1)
        by_conversation = {
            url: targets[0]
            for url, targets in grouped.items()
            if len(targets) == 1
        }
        return BrowserSnapshot(
            targets_by_id=by_id,
            targets_by_conversation=by_conversation,
            ambiguous_conversations=ambiguous,
            open_conversations=len(grouped),
            blank_chat_tabs=blank,
        )

    def reconcile(self, browser: BrowserSnapshot | None = None) -> BrowserSnapshot:
        browser = browser or self.browser_snapshot()
        for job in self.queue.list_jobs():
            if job.state in {GptJobState.DONE, GptJobState.CANCELLED}:
                continue
            target = browser.targets_by_id.get(job.target_id or "")
            if target is not None and job.conversation_url:
                target_conversation = _canonical_chatgpt_conversation_url(target.url)
                if target_conversation != job.conversation_url:
                    target = None
            if target is None and job.conversation_url:
                target = browser.targets_by_conversation.get(job.conversation_url)
            if target is not None:
                conversation = _canonical_chatgpt_conversation_url(target.url) or job.conversation_url
                if conversation and (not job.conversation_url or conversation == job.conversation_url):
                    context_url = target.url if _canonical_chatgpt_conversation_url(target.url) else job.conversation_context_url
                    if (
                        job.target_id != target.target_id
                        or job.conversation_url != conversation
                        or job.conversation_context_url != context_url
                        or job.state in {GptJobState.STARTING, GptJobState.REVIEW, GptJobState.BLOCKED, GptJobState.FAILED}
                    ):
                        self.queue.bind_chat(
                            job.job_id,
                            conversation_url=conversation,
                            conversation_context_url=context_url,
                            target_id=target.target_id,
                            state=GptJobState.ACTIVE,
                        )
                    continue
            if job.state is GptJobState.STARTING:
                self.queue.set_state(job.job_id, GptJobState.FAILED, last_error="start_target_missing")
            elif job.state is GptJobState.ACTIVE:
                reason = "chat_target_ambiguous" if job.conversation_url in browser.ambiguous_conversations else "chat_not_open_locally"
                self.queue.set_state(job.job_id, GptJobState.REVIEW, last_error=reason)
        return browser

    def _occupied_job_ids(self, browser: BrowserSnapshot) -> set[str]:
        occupied: set[str] = set()
        for job in self.queue.list_jobs():
            if job.state not in OCCUPYING_STATES:
                continue
            if job.target_id and job.target_id in browser.targets_by_id:
                target = browser.targets_by_id[job.target_id]
                target_conversation = _canonical_chatgpt_conversation_url(target.url)
                if not job.conversation_url or target_conversation == job.conversation_url:
                    occupied.add(job.job_id)
                    continue
            if job.conversation_url and job.conversation_url in browser.targets_by_conversation:
                occupied.add(job.job_id)
        return occupied

    def _exact_job_target(self, job: GptWorkJob, browser: BrowserSnapshot | None = None):
        if not job.target_id or not job.conversation_url:
            raise CdpError("job_has_no_open_target")
        browser = browser or self.browser_snapshot()
        target = browser.targets_by_id.get(job.target_id)
        if target is None:
            raise CdpError("job_target_not_open")
        if _canonical_chatgpt_conversation_url(target.url) != job.conversation_url:
            raise CdpError("job_target_assignment_mismatch")
        return target

    def pump(self, *, max_to_start: int | None = None) -> dict[str, Any]:
        browser = self.reconcile()
        settings = self.queue.settings()
        occupied = self._occupied_job_ids(browser)
        available = max(0, settings.max_open_chats - len(occupied))
        if max_to_start is not None:
            available = min(available, max(0, int(max_to_start)))
        started: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        while available > 0:
            job = self.queue.next_queued()
            if job is None:
                break
            if not job.prompt.strip():
                self.queue.set_state(job.job_id, GptJobState.BLOCKED, last_error="prompt_required")
                errors.append({"job_id": job.job_id, "error": "prompt_required"})
                continue
            self.queue.set_state(job.job_id, GptJobState.STARTING)
            target_id: str | None = None
            try:
                result = self.cdp.start_chatgpt_job(
                    job.prompt,
                    new_chat_url=job.project_url or CHATGPT_ORIGIN,
                    background=True,
                    submit=True,
                )
                target_id = str(result.get("new_target_id") or "") or None
                conversation_url = _canonical_chatgpt_conversation_url(str(result.get("conversation_url") or ""))
                context_url = str(result.get("conversation_context_url") or "") or conversation_url
                if not target_id or not conversation_url:
                    raise CdpError("job_start_binding_missing")
                self.cdp.install_human_input_target(target_id, context_url or conversation_url)
                bound = self.queue.bind_chat(
                    job.job_id,
                    conversation_url=conversation_url,
                    conversation_context_url=context_url,
                    target_id=target_id,
                    state=GptJobState.ACTIVE,
                )
                started.append(bound.model_dump(mode="json"))
                available -= 1
            except (CdpError, OSError, RuntimeError, ValueError) as exc:
                if target_id:
                    try:
                        self.cdp.close_target(target_id)
                    except (AttributeError, CdpError):
                        pass
                self.queue.set_state(job.job_id, GptJobState.FAILED, last_error=str(exc)[:2000])
                errors.append({"job_id": job.job_id, "error": str(exc)[:500]})
                break
        return {"started": started, "errors": errors, **self.runtime_state(reconcile=False)}

    def start_job(self, job_id: str) -> dict[str, Any]:
        browser = self.reconcile()
        job = self.queue.get_job(job_id)
        if job.state is GptJobState.ACTIVE:
            try:
                self._exact_job_target(job, browser)
                return {"started": [job.model_dump(mode="json")], "errors": [], **self.runtime_state(reconcile=False)}
            except CdpError:
                job = self.queue.get_job(job_id)
        if job.conversation_url:
            occupied = self._occupied_job_ids(browser)
            if job.job_id not in occupied and len(occupied) >= self.queue.settings().max_open_chats:
                raise ValueError("chat_slot_limit_reached")
            target = browser.targets_by_conversation.get(job.conversation_url)
            created_target_id: str | None = None
            try:
                if target is None:
                    if job.conversation_url in browser.ambiguous_conversations:
                        raise CdpError("chat_target_ambiguous")
                    created_target_id = self.cdp.create_chatgpt_target(clear_cache=False, background=True)
                    context_url = job.conversation_context_url or job.conversation_url
                    self.cdp.navigate_chatgpt_conversation(created_target_id, context_url)
                    refreshed = self.browser_snapshot()
                    target = refreshed.targets_by_id.get(created_target_id)
                    if target is None or _canonical_chatgpt_conversation_url(target.url) != job.conversation_url:
                        raise CdpError("job_target_assignment_mismatch")
                context_url = target.url if _canonical_chatgpt_conversation_url(target.url) else (job.conversation_context_url or job.conversation_url)
                self.cdp.install_human_input_target(target.target_id, context_url)
                bound = self.queue.bind_chat(
                    job.job_id,
                    conversation_url=job.conversation_url,
                    conversation_context_url=context_url,
                    target_id=target.target_id,
                    state=GptJobState.ACTIVE,
                )
                return {"started": [bound.model_dump(mode="json")], "errors": [], **self.runtime_state(reconcile=False)}
            except (CdpError, OSError, RuntimeError, ValueError) as exc:
                if created_target_id:
                    try:
                        self.cdp.close_target(created_target_id)
                    except CdpError:
                        pass
                self.queue.set_state(job.job_id, GptJobState.FAILED, last_error=str(exc)[:2000])
                return {"started": [], "errors": [{"job_id": job.job_id, "error": str(exc)[:500]}], **self.runtime_state(reconcile=False)}
        if not job.prompt.strip():
            self.queue.set_state(job.job_id, GptJobState.BLOCKED, last_error="prompt_required")
            return {"started": [], "errors": [{"job_id": job.job_id, "error": "prompt_required"}], **self.runtime_state(reconcile=False)}
        if job.state is not GptJobState.QUEUED:
            self.queue.set_state(job.job_id, GptJobState.QUEUED)
        self.queue.reorder(job_id, 1)
        return self.pump(max_to_start=1)

    def import_target(self, target_id: str) -> GptWorkJob:
        browser = self.reconcile()
        target = browser.targets_by_id.get(str(target_id))
        if target is None:
            raise ValueError("chat_not_found")
        conversation_url = _canonical_chatgpt_conversation_url(target.url)
        if not conversation_url:
            raise ValueError("conversation_url_invalid")
        for job in self.queue.list_jobs():
            if job.conversation_url == conversation_url:
                if job.target_id == target.target_id and job.state is GptJobState.ACTIVE:
                    return job
                raise ValueError("conversation_already_queued")
        if len(self._occupied_job_ids(browser)) >= self.queue.settings().max_open_chats:
            raise ValueError("chat_slot_limit_reached")
        self.cdp.install_human_input_target(target.target_id, target.url)
        project_url = chatgpt_project_new_chat_url(target.url)
        return self.queue.create_job(
            target.title or "Chat GPT",
            project_name="Progetto ChatGPT" if project_url else "",
            project_url=project_url,
            conversation_url=conversation_url,
            conversation_context_url=target.url,
            target_id=target.target_id,
            state=GptJobState.ACTIVE,
        )

    def resolve_project_url_by_name(self, project_name: str) -> str:
        name = str(project_name or "").strip()
        if not name:
            raise ValueError("project_name_required")
        cached = self._project_url_cache.get(name.casefold())
        if cached:
            return cached
        if not hasattr(self.cdp, "resolve_project_url"):
            raise CdpError("project_resolver_unavailable")
        target_id = self.cdp.create_target("https://chatgpt.com/projects", background=True)
        try:
            url = self.cdp.resolve_project_url(target_id, name, wait_timeout_s=12.0)
            self._project_url_cache[name.casefold()] = url
            return url
        finally:
            try:
                self.cdp.close_target(target_id)
            except CdpError:
                pass

    def account_history(self) -> dict[str, Any]:
        jobs = self.queue.list_jobs()
        jobs_by_conversation: dict[str, GptWorkJob] = {}
        terminal = {GptJobState.DONE, GptJobState.CANCELLED}
        for job in jobs:
            if not job.conversation_url:
                continue
            previous = jobs_by_conversation.get(job.conversation_url)
            if previous is None or (previous.state in terminal and job.state not in terminal):
                jobs_by_conversation[job.conversation_url] = job

        open_rows = self.browser_rows()
        open_by_conversation = {row["conversation_url"]: row for row in open_rows}
        records: list[dict[str, str]] = []
        projects: list[dict[str, str]] = []
        scan_error: str | None = None
        targets = [
            target
            for target in self.cdp.targets()
            if target.target_type == "page" and target.is_chatgpt
        ]
        scored_targets: list[tuple[bool, Any]] = []
        for target in targets:
            focused = False
            try:
                focused = bool(self.cdp.chatgpt_companion_state(target.target_id).get("focused"))
            except (AttributeError, CdpError):
                pass
            scored_targets.append((focused, target))
        scored_targets.sort(key=lambda item: item[0])
        for _, target in scored_targets[:4]:
            try:
                if hasattr(self.cdp, "sidebar_catalog"):
                    catalog = self.cdp.sidebar_catalog(target.target_id, wait_timeout_s=20.0, max_records=240)
                    records = list(catalog.get("chats") or [])
                    projects = list(catalog.get("projects") or [])
                else:
                    records = self.cdp.conversation_records(target.target_id, reload=False, wait_timeout_s=1.0)
                    projects = []
            except (AttributeError, CdpError) as exc:
                scan_error = str(exc)
                continue
            if records or projects:
                scan_error = None
                break

        project_scan_error: str | None = None
        temporary_project_target: str | None = None
        if hasattr(self.cdp, "project_records"):
            try:
                temporary_project_target = self.cdp.create_target("https://chatgpt.com/projects", background=True)
                page_projects = self.cdp.project_records(temporary_project_target, wait_timeout_s=12.0)
                merged_projects: dict[str, dict[str, str]] = {}
                for project in [*projects, *page_projects]:
                    url = str(project.get("url") or "").strip()
                    title = str(project.get("title") or "").strip()
                    key = url or (f"name:{title.casefold()}" if title else "")
                    if key:
                        merged_projects[key] = project
                projects = list(merged_projects.values())
            except (AttributeError, CdpError, OSError) as exc:
                project_scan_error = str(exc)
            finally:
                if temporary_project_target:
                    try:
                        self.cdp.close_target(temporary_project_target)
                    except CdpError:
                        pass

        projects_by_id = {
            str(project.get("project_id") or ""): project
            for project in projects
            if str(project.get("project_id") or "")
        }
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for rank, record in enumerate(records):
            conversation_url = _canonical_chatgpt_conversation_url(str(record.get("url") or ""))
            if not conversation_url or conversation_url in seen:
                continue
            seen.add(conversation_url)
            opened = open_by_conversation.get(conversation_url)
            job = jobs_by_conversation.get(conversation_url)
            project_id = str(record.get("project_id") or "")
            project = projects_by_id.get(project_id) or {}
            context_url = str(record.get("context_url") or (opened or {}).get("url") or conversation_url)
            rows.append({
                "rank": rank,
                "title": str(record.get("title") or "").strip() or (opened or {}).get("title") or "Chat GPT",
                "conversation_url": conversation_url,
                "conversation_context_url": context_url,
                "project_id": project_id or None,
                "project_name": project.get("title") or "",
                "project_url": project.get("url") or None,
                "open": bool(opened),
                "managed": bool(job and job.state not in terminal),
                "job_id": job.job_id if job and job.state not in terminal else None,
                "job_state": job.state.value if job and job.state not in terminal else None,
            })
        for opened in open_rows:
            conversation_url = opened["conversation_url"]
            if conversation_url in seen:
                continue
            seen.add(conversation_url)
            job = jobs_by_conversation.get(conversation_url)
            rows.append({
                "rank": len(rows),
                "title": opened.get("title") or "Chat GPT",
                "conversation_url": conversation_url,
                "conversation_context_url": opened.get("url") or conversation_url,
                "project_id": None,
                "project_name": opened.get("project_name") or "",
                "project_url": opened.get("project_url") or None,
                "open": True,
                "managed": bool(job and job.state not in terminal),
                "job_id": job.job_id if job and job.state not in terminal else None,
                "job_state": job.state.value if job and job.state not in terminal else None,
            })
        return {
            "chats": rows,
            "count": len(rows),
            "projects": projects,
            "project_count": len(projects),
            "project_error": project_scan_error,
            "error": scan_error if not rows and not projects else None,
        }

    def resume_history_chat(
        self,
        conversation_url: str,
        title: str = "Chat GPT",
        *,
        conversation_context_url: str | None = None,
        project_name: str = "",
        project_url: str | None = None,
    ) -> dict[str, Any]:
        canonical = _canonical_chatgpt_conversation_url(conversation_url)
        if not canonical:
            raise ValueError("conversation_url_invalid")
        context_url = conversation_context_url or canonical
        existing = next((job for job in self.queue.list_jobs() if job.conversation_url == canonical and job.state not in {GptJobState.DONE, GptJobState.CANCELLED}), None)
        if existing is None:
            existing = self.queue.create_job(
                str(title or "Chat GPT").strip() or "Chat GPT",
                project_name=project_name,
                project_url=project_url,
                conversation_url=canonical,
                conversation_context_url=context_url,
                state=GptJobState.REVIEW,
            )
        result = self.start_job(existing.job_id)
        return {"job": self.queue.get_job(existing.job_id).model_dump(mode="json"), "start": result, "server_chat_created": False}

    def send_message(self, job_id: str, text: str) -> dict[str, Any]:
        job = self.queue.get_job(job_id)
        if job.state is not GptJobState.ACTIVE:
            raise ValueError("job_not_active")
        target = self._exact_job_target(job)
        context_url = job.conversation_context_url or target.url
        self.cdp.install_human_input_target(target.target_id, context_url)
        result = self.cdp.queue_human_message(target.target_id, context_url, text)
        if not bool(result.get("queued")):
            raise CdpError(str(result.get("reason") or "message_not_queued"))
        return {"action": "queued", "job_id": job.job_id, "conversation_url": job.conversation_url}

    def activate_job(self, job_id: str) -> dict[str, Any]:
        job = self.queue.get_job(job_id)
        target = self._exact_job_target(job)
        self.cdp.activate_target(target.target_id)
        return {"action": "activated", "target_id": target.target_id}

    def release_job(self, job_id: str) -> GptWorkJob:
        job = self.queue.get_job(job_id)
        if job.target_id:
            target = self._exact_job_target(job)
            self.cdp.close_target(target.target_id)
        return self.queue.bind_chat(
            job.job_id,
            conversation_url=job.conversation_url,
            conversation_context_url=job.conversation_context_url,
            target_id=None,
            state=GptJobState.REVIEW,
        )

    def finish_job(self, job_id: str, state: GptJobState) -> GptWorkJob:
        if state not in {GptJobState.DONE, GptJobState.CANCELLED}:
            raise ValueError("state_not_terminal")
        job = self.queue.get_job(job_id)
        if job.target_id:
            try:
                target = self._exact_job_target(job)
                self.cdp.close_target(target.target_id)
            except CdpError:
                pass
        return self.queue.set_state(job.job_id, state)

    def runtime_state(self, *, reconcile: bool = True) -> dict[str, Any]:
        browser = self.reconcile() if reconcile else self.browser_snapshot()
        occupied = self._occupied_job_ids(browser)
        settings = self.queue.settings()
        jobs = self.queue.list_jobs()
        return {
            "browser": {
                "ok": True,
                "endpoint": self.cdp.endpoint,
                "open_conversations": browser.open_conversations,
                "blank_chat_tabs": browser.blank_chat_tabs,
            },
            "slots": {
                "used": len(occupied),
                "limit": settings.max_open_chats,
                "free": max(0, settings.max_open_chats - len(occupied)),
            },
            "counts": {
                state.value: sum(1 for job in jobs if job.state is state)
                for state in GptJobState
            },
        }

    def snapshot(self) -> dict[str, Any]:
        runtime = self.runtime_state()
        queue = self.queue.snapshot()
        jobs_by_id = {job["job_id"]: job for job in queue["jobs"]}
        for section in queue["projects"]:
            section["jobs"] = [jobs_by_id[job_id] for job_id in section.pop("job_ids", []) if job_id in jobs_by_id]
        return {"queue": queue, "runtime": runtime}

    def browser_rows(self) -> list[dict[str, Any]]:
        jobs = self.queue.list_jobs()
        binding_to_job = {
            (job.target_id, job.conversation_url): job.job_id
            for job in jobs
            if job.target_id and job.conversation_url
        }
        rows: list[dict[str, Any]] = []
        for target in self.cdp.targets():
            if target.target_type != "page" or not target.is_chatgpt:
                continue
            conversation_url = _canonical_chatgpt_conversation_url(target.url)
            if not conversation_url:
                continue
            try:
                companion = self.cdp.chatgpt_companion_state(target.target_id)
            except (AttributeError, CdpError):
                companion = {}
            if bool(companion.get("ghost")):
                continue
            key = (target.target_id, conversation_url)
            job_id = binding_to_job.get(key)
            project_url = chatgpt_project_new_chat_url(target.url)
            rows.append(
                {
                    "target_id": target.target_id,
                    "title": target.title,
                    "url": target.url,
                    "conversation_url": conversation_url,
                    "project_url": project_url,
                    "project_name": "Progetto ChatGPT" if project_url else "",
                    "focused": bool(companion.get("focused")),
                    "busy": bool(companion.get("busy")),
                    "composer_chars": int(companion.get("composer_chars") or 0),
                    "managed": bool(job_id),
                    "queued": bool(job_id),
                    "job_id": job_id,
                }
            )
        rows.sort(key=lambda row: (not row["managed"], not row["focused"], not row["busy"], str(row["title"]).casefold()))
        return rows

    def dashboard_snapshot(self) -> dict[str, Any]:
        base = self.queue.snapshot()
        browser_rows = self.browser_rows()
        limit = int(base["settings"]["max_open_chats"])
        managed = sum(1 for row in browser_rows if row["managed"])
        base["browser"] = {
            "ok": True,
            "endpoint": self.cdp.endpoint,
            "open_chats": browser_rows,
            "open_chat_count": len(browser_rows),
            "managed_open_count": managed,
            "unmanaged_open_count": max(0, len(browser_rows) - managed),
            "max_open_chats": limit,
            "slots_free": max(0, limit - managed),
            "over_limit": managed > limit,
        }
        return base


INDEX_HTML = r'''<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bot-tazzi · GPT lavori</title>
<style>
:root{font-family:Inter,ui-sans-serif,system-ui,sans-serif;color-scheme:dark;background:#0f1115;color:#eceff4}
*{box-sizing:border-box} body{margin:0;background:#0f1115} button,input,textarea{font:inherit}
.shell{max-width:1120px;margin:0 auto;padding:20px}.top{display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}
h1{font-size:24px;margin:0}.muted{color:#9aa4b2}.cards{display:grid;grid-template-columns:repeat(3,minmax(130px,1fr));gap:10px;margin:16px 0}
.card,.panel,.project{background:#171a21;border:1px solid #2b303b;border-radius:14px}.card{padding:12px}.metric{font-size:24px;font-weight:800}
.panel{padding:14px;margin-bottom:16px}.formgrid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.wide{grid-column:1/-1}
input,textarea{width:100%;background:#0f1115;color:#eceff4;border:1px solid #343b48;border-radius:9px;padding:9px}textarea{min-height:110px;resize:vertical}
button{border:0;border-radius:9px;padding:8px 10px;background:#e6e9ef;color:#11151b;font-weight:750;cursor:pointer}button.secondary{background:#2a303b;color:#eef2f7}button.danger{background:#54262b;color:#ffdfe2}
.actions{display:flex;gap:6px;flex-wrap:wrap}.projects{display:grid;gap:14px}.project{padding:14px}.project h2{font-size:17px;margin:0 0 10px}.job{display:grid;grid-template-columns:44px minmax(0,1fr) auto;gap:10px;align-items:center;padding:10px 0;border-top:1px solid #272c35}.job:first-of-type{border-top:0}
.rank{text-align:center;font-weight:800;color:#aab3c0}.title{font-weight:750;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.meta{font-size:12px;color:#97a1af;margin-top:3px}.badge{display:inline-block;border-radius:999px;padding:2px 7px;background:#29313d;color:#dce4ef}.error{color:#ff9ea7}.empty{padding:18px;color:#929cab;text-align:center}
a{color:#9cc7ff;text-decoration:none}@media(max-width:700px){.cards{grid-template-columns:1fr}.formgrid{grid-template-columns:1fr}.job{grid-template-columns:36px 1fr}.job>.actions{grid-column:2}}
</style>
</head>
<body><main class="shell">
<div class="top"><div><h1>Bot-tazzi · GPT lavori</h1><div class="muted">Coda unica, chat separate, Progetti preservati.</div></div><div class="actions"><button id="pump">Avvia coda</button><button class="secondary" id="refresh">Aggiorna</button></div></div>
<div class="cards"><div class="card"><div class="muted">Chat lavoro</div><div class="metric" id="slots">—</div></div><div class="card"><div class="muted">In coda</div><div class="metric" id="queued">—</div></div><div class="card"><div class="muted">Da rivedere</div><div class="metric" id="review">—</div></div></div>
<section class="panel"><div class="formgrid"><label>Massimo chat aperte<input id="limit" type="number" min="1" max="12"></label><div class="actions" style="align-self:end"><button class="secondary" id="save-limit">Salva limite</button></div></div></section>
<section class="panel"><div class="formgrid"><label>Titolo<input id="title" maxlength="300" placeholder="es. Testa BaffoFlix sul P30"></label><label>Nome progetto<input id="project-name" maxlength="300" placeholder="es. Indipendentemenza dai colletti bianchi"></label><label class="wide">URL progetto ChatGPT<input id="project-url" maxlength="1200" placeholder="https://chatgpt.com/g/.../project"></label><label class="wide">Istruzione<textarea id="prompt" maxlength="32000" placeholder="Cosa deve fare questa chat"></textarea></label><div class="wide actions"><button id="add">Aggiungi e avvia se c'è posto</button></div></div><div id="form-error" class="error"></div></section>
<div id="projects" class="projects"></div>
</main>
<script>
const req = async (path, method='GET', body=null) => {
  const opt={method,headers:{'X-Bottazzi-Frontend':'1'}};
  if(body!==null){opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body)}
  const r=await fetch(path,opt); const data=await r.json(); if(!r.ok) throw new Error(data.detail||data.error||'request_failed'); return data;
};
const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n};
const action=async(path,body=null)=>{await req(path,'POST',body);await load()};
function jobRow(job){
  const row=el('div','job'); row.append(el('div','rank',String(job.rank)));
  const main=el('div'); const title=el('div','title',job.title); main.append(title);
  const meta=el('div','meta'); const badge=el('span','badge',job.state); meta.append(badge);
  if(job.last_error){meta.append(document.createTextNode(' · '+job.last_error))} main.append(meta); row.append(main);
  const actions=el('div','actions');
  const up=el('button','secondary','↑'); up.onclick=()=>action(`/api/jobs/${job.job_id}/move`,{rank:Math.max(1,job.rank-1)}); actions.append(up);
  const down=el('button','secondary','↓'); down.onclick=()=>action(`/api/jobs/${job.job_id}/move`,{rank:job.rank+1}); actions.append(down);
  if(job.state==='queued'){const start=el('button','secondary','Avvia');start.onclick=()=>action(`/api/jobs/${job.job_id}/start`);actions.append(start)}
  const href=job.conversation_context_url||job.conversation_url;
  if(href){const open=el('a','','Apri');open.href=href;open.target='_blank';open.rel='noreferrer';actions.append(open)}
  if(!['done','cancelled'].includes(job.state)){const done=el('button','secondary','Fatto');done.onclick=()=>action(`/api/jobs/${job.job_id}/state`,{state:'done'});actions.append(done);const cancel=el('button','danger','Annulla');cancel.onclick=()=>action(`/api/jobs/${job.job_id}/state`,{state:'cancelled'});actions.append(cancel)}
  row.append(actions); return row;
}
async function load(){
  try{
    const state=await req('/api/state'); const rt=state.runtime; const q=state.queue;
    document.getElementById('slots').textContent=`${rt.slots.used} / ${rt.slots.limit}`;
    document.getElementById('queued').textContent=rt.counts.queued||0; document.getElementById('review').textContent=(rt.counts.review||0)+(rt.counts.failed||0)+(rt.counts.blocked||0);
    document.getElementById('limit').value=q.settings.max_open_chats;
    const root=document.getElementById('projects'); root.replaceChildren();
    if(!q.projects.length){root.append(el('div','panel empty','Nessun lavoro in coda.'));return}
    for(const p of q.projects){const sec=el('section','project');const h=el('h2','',p.project_name||'Progetto ChatGPT');if(p.project_url){const a=el('a');a.href=p.project_url;a.target='_blank';a.rel='noreferrer';a.append(h);sec.append(a)}else sec.append(h);for(const job of p.jobs)sec.append(jobRow(job));root.append(sec)}
  }catch(e){document.getElementById('projects').replaceChildren(el('div','panel error',String(e.message||e)))}
}
document.getElementById('refresh').onclick=load; document.getElementById('pump').onclick=()=>action('/api/pump');
document.getElementById('save-limit').onclick=()=>action('/api/settings',{max_open_chats:Number(document.getElementById('limit').value)});
document.getElementById('add').onclick=async()=>{const err=document.getElementById('form-error');err.textContent='';try{await action('/api/jobs',{title:document.getElementById('title').value,prompt:document.getElementById('prompt').value,project_name:document.getElementById('project-name').value,project_url:document.getElementById('project-url').value||null,auto_start:true});document.getElementById('title').value='';document.getElementById('prompt').value=''}catch(e){err.textContent=String(e.message||e)}};
load(); setInterval(load,3000);
</script></body></html>'''


def _host_from_origin(origin: str) -> str:
    return urlparse(origin).netloc


class GptFrontendServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        controller: GptWorkController,
        origins: tuple[str, ...],
        allowed_networks: tuple[str, ...],
    ) -> None:
        self.controller = controller
        self.queue = controller.queue
        self.allowed_origins = frozenset(origin.rstrip("/") for origin in origins)
        self.allowed_hosts = frozenset(_host_from_origin(origin) for origin in self.allowed_origins)
        self.allowed_networks = tuple(ipaddress.ip_network(value, strict=False) for value in allowed_networks)
        super().__init__(address, GptFrontendHandler)


class GptFrontendHandler(BaseHTTPRequestHandler):
    server: GptFrontendServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:
        return

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'",
        )

    def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message, "detail": message})

    def _client_allowed(self) -> bool:
        try:
            address = ipaddress.ip_address(self.client_address[0])
        except ValueError:
            return False
        return any(address in network for network in self.server.allowed_networks)

    def _host_allowed(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts

    def _mutation_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is not None and origin.rstrip("/") not in self.server.allowed_origins:
            self._error(403, "origin_not_allowed")
            return False
        if self.headers.get("X-Bottazzi-Frontend") != "1":
            self._error(403, "frontend_header_required")
            return False
        if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self._error(415, "json_required")
            return False
        return True

    def _read_json(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            self._error(400, "invalid_content_length")
            return None
        if length < 0 or length > 40_000:
            self._error(413, "request_too_large")
            return None
        try:
            raw = self.rfile.read(length) if length else b"{}"
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(400, "invalid_json")
            return None
        if not isinstance(value, dict):
            self._error(400, "json_object_required")
            return None
        return value

    def _validated(self, model: type[ApiInput]) -> ApiInput | None:
        payload = self._read_json()
        if payload is None:
            return None
        try:
            return model.model_validate(payload)
        except ValidationError as exc:
            self._send_json(422, {"error": "validation_error", "detail": exc.errors(include_input=False)})
            return None

    def do_GET(self) -> None:
        if not self._client_allowed():
            self._error(403, "client_not_allowed")
            return
        if not self._host_allowed():
            self._error(403, "host_not_allowed")
            return
        path = urlparse(self.path).path
        if path == "/":
            body = UI_PATH.read_text(encoding="utf-8") if UI_PATH.is_file() else INDEX_HTML
            self._send_bytes(200, body.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/healthz":
            try:
                self._send_json(200, {"ok": True, "runtime": self.server.controller.runtime_state()})
            except (CdpError, OSError) as exc:
                self._send_json(503, {"ok": False, "error": str(exc)})
            return
        if path == "/api/state":
            try:
                self._send_json(200, self.server.controller.snapshot())
            except (CdpError, OSError) as exc:
                self._error(503, str(exc))
            return
        if path == "/api/snapshot":
            try:
                self._send_json(200, self.server.controller.dashboard_snapshot())
            except (CdpError, OSError) as exc:
                self._error(503, str(exc))
            return
        if path == "/api/history":
            try:
                self._send_json(200, {"ok": True, **self.server.controller.account_history()})
            except (CdpError, OSError) as exc:
                self._error(503, str(exc))
            return
        self._error(404, "not_found")

    def do_POST(self) -> None:
        if not self._client_allowed():
            self._error(403, "client_not_allowed")
            return
        if not self._host_allowed():
            self._error(403, "host_not_allowed")
            return
        if not self._mutation_allowed():
            return
        path = urlparse(self.path).path
        queue = self.server.queue
        controller = self.server.controller
        try:
            if path == "/api/history/resume":
                payload = self._validated(ResumeChat)
                if payload is None:
                    return
                assert isinstance(payload, ResumeChat)
                self._send_json(200, {"ok": True, **controller.resume_history_chat(payload.conversation_url, payload.title, conversation_context_url=payload.conversation_context_url, project_name=payload.project_name, project_url=payload.project_url)})
                return
            if path == "/api/jobs":
                payload = self._validated(CreateJob)
                if payload is None:
                    return
                assert isinstance(payload, CreateJob)
                project_url = payload.project_url
                if payload.project_name and not project_url:
                    project_url = controller.resolve_project_url_by_name(payload.project_name)
                job = queue.create_job(
                    payload.title,
                    prompt=payload.prompt,
                    project_name=payload.project_name,
                    project_url=project_url,
                )
                result: dict[str, Any] = {"ok": True, "job": job.model_dump(mode="json")}
                if payload.auto_start:
                    result["pump"] = controller.pump()
                self._send_json(200, result)
                return
            if path == "/api/jobs/import":
                payload = self._validated(ImportChat)
                if payload is None:
                    return
                assert isinstance(payload, ImportChat)
                job = controller.import_target(payload.target_id)
                self._send_json(200, {"ok": True, "job": job.model_dump(mode="json")})
                return
            if path == "/api/settings":
                payload = self._validated(SetLimit)
                if payload is None:
                    return
                assert isinstance(payload, SetLimit)
                settings = queue.set_max_open_chats(payload.max_open_chats)
                self._send_json(200, {"ok": True, "settings": settings.model_dump(mode="json")})
                return
            if path in {"/api/pump", "/api/start-next"}:
                payload = self._read_json()
                if payload is None:
                    return
                if payload:
                    self._error(422, "pump_body_must_be_empty")
                    return
                result = controller.pump(max_to_start=1 if path == "/api/start-next" else None)
                self._send_json(200, {"ok": True, **result})
                return

            match = re.fullmatch(r"/api/jobs/([A-Za-z0-9-]+)/(move|rank|start|state|activate|release|message)", path)
            if not match:
                self._error(404, "not_found")
                return
            job_id, action = match.groups()
            if action in {"move", "rank"}:
                payload = self._validated(MoveJob)
                if payload is None:
                    return
                assert isinstance(payload, MoveJob)
                job = queue.reorder(job_id, payload.rank)
                self._send_json(200, {"ok": True, "job": job.model_dump(mode="json")})
                return
            if action == "start":
                payload = self._read_json()
                if payload is None:
                    return
                if payload:
                    self._error(422, "start_body_must_be_empty")
                    return
                self._send_json(200, {"ok": True, **controller.start_job(job_id)})
                return
            if action == "activate":
                payload = self._read_json()
                if payload is None:
                    return
                if payload:
                    self._error(422, "activate_body_must_be_empty")
                    return
                self._send_json(200, {"ok": True, **controller.activate_job(job_id)})
                return
            if action == "release":
                payload = self._read_json()
                if payload is None:
                    return
                if payload:
                    self._error(422, "release_body_must_be_empty")
                    return
                job = controller.release_job(job_id)
                self._send_json(200, {"ok": True, "action": "released", "job": job.model_dump(mode="json"), "server_chat_deleted": False})
                return
            if action == "message":
                payload = self._validated(SendMessage)
                if payload is None:
                    return
                assert isinstance(payload, SendMessage)
                self._send_json(200, {"ok": True, **controller.send_message(job_id, payload.text)})
                return
            payload = self._validated(SetJobState)
            if payload is None:
                return
            assert isinstance(payload, SetJobState)
            if payload.state in {GptJobState.DONE, GptJobState.CANCELLED}:
                job = controller.finish_job(job_id, payload.state)
            elif payload.state is GptJobState.QUEUED:
                current = queue.get_job(job_id)
                if current.target_id:
                    controller.release_job(job_id)
                job = queue.set_state(job_id, GptJobState.QUEUED)
            else:
                self._error(422, "state_not_user_settable")
                return
            self._send_json(200, {"ok": True, "job": job.model_dump(mode="json")})
        except KeyError:
            self._error(404, "job_not_found")
        except ValueError as exc:
            self._error(409, str(exc))
        except CdpError as exc:
            self._error(409, str(exc))
        except (OSError, RuntimeError) as exc:
            self._error(503, str(exc))


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def create_server(
    queue: GptWorkQueue | None = None,
    cdp: ChromeCdp | None = None,
    *,
    origin: str = "http://127.0.0.1:19201",
    bind_host: str | None = None,
    allowed_origins: tuple[str, ...] | None = None,
    allowed_networks: tuple[str, ...] | None = None,
) -> GptFrontendServer:
    parsed = urlparse(origin)
    if parsed.scheme != "http" or parsed.port is None:
        raise ValueError("frontend_origin_must_be_http_with_port")
    bind_host = bind_host or os.getenv("BOTTAZZI_GPT_FRONTEND_BIND", parsed.hostname or "127.0.0.1")
    if bind_host not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        raise ValueError("frontend_bind_host_not_allowed")
    allowed_origins = allowed_origins or _csv_env("BOTTAZZI_GPT_FRONTEND_ALLOWED_ORIGINS", origin)
    if not allowed_origins:
        raise ValueError("frontend_allowed_origins_required")
    for value in allowed_origins:
        allowed = urlparse(value)
        if allowed.scheme != "http" or allowed.port != parsed.port or not allowed.hostname:
            raise ValueError("frontend_allowed_origin_invalid")
    allowed_networks = allowed_networks or _csv_env(
        "BOTTAZZI_GPT_FRONTEND_ALLOWED_NETWORKS",
        "127.0.0.0/8,::1/128",
    )
    if not allowed_networks:
        raise ValueError("frontend_allowed_networks_required")
    queue = queue or GptWorkQueue.from_env()
    cdp = cdp or ChromeCdp(os.getenv("BOTTAZZI_GPT_CDP_ENDPOINT", "http://127.0.0.1:9238"))
    controller = GptWorkController(queue, cdp)
    return GptFrontendServer(
        (bind_host, parsed.port),
        controller=controller,
        origins=tuple(allowed_origins),
        allowed_networks=tuple(allowed_networks),
    )
