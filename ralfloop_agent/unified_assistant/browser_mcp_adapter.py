from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any, Callable, Mapping, Sequence

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from src.mcp_transport import MCPClientSession, MCPError, MCPProtocolError, UnixMCPTransport

from .contracts import PlanAssignment
from .conversation import PendingAction, approval_matches, payload_matches
from .executor import StructuredArtifact


DEFAULT_SOCKET = "/run/ralf-browser-playwright-mcp/mcp.sock"
READ_TOOLS = frozenset({"browser_snapshot", "browser_tabs"})
WRITE_TOOLS = frozenset({"browser_click", "browser_type", "browser_file_upload"})
BROWSER_INTERACT_ACTION = "browser_interact"
LOGICAL_ACTIONS = frozenset({"click", "type", "upload", "submit"})
TARGET_REF_RE = re.compile(r"^e[0-9]+$")
INTERACTION_WORDS = re.compile(
    r"\b(?:clicca|click|scrivi|digita|type|compila|fill|carica|upload|"
    r"invia|submit|seleziona|select|trascina|drag|premi|press)\b",
    re.I,
)


def _text_result(result: Mapping[str, Any], *, limit: int = 12000) -> str:
    rows = result.get("content") or ()
    text = "\n".join(
        str(item.get("text") or "")
        for item in rows
        if isinstance(item, Mapping) and item.get("type") == "text"
    ).strip()
    if not text:
        structured = result.get("structuredContent")
        if structured is not None:
            text = str(structured)
    return text[:limit]


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stable_snapshot_text(text: str) -> str:
    page_start = text.find("### Page")
    snapshot_start = text.find("### Snapshot")
    start = page_start if page_start >= 0 else snapshot_start
    if start < 0:
        return text.strip()
    events_start = text.find("\n### Events", max(start, snapshot_start))
    end = events_start if events_start >= 0 else len(text)
    return text[start:end].strip()


def _snapshot_hash(text: str) -> str:
    return hashlib.sha256(_stable_snapshot_text(text).encode()).hexdigest()


def _target_present(snapshot: str, target: str) -> bool:
    return bool(target and re.search(rf"\[ref={re.escape(target)}\]", snapshot))


class BrowserMCPReadOnly:
    """Strict Playwright MCP adapter for zero-side-effect browser inspection."""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.socket_path = socket_path or os.getenv(
            "RALF_BROWSER_PLAYWRIGHT_MCP_SOCKET", DEFAULT_SOCKET
        )
        self._session_factory = session_factory

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        return MCPClientSession(
            UnixMCPTransport(self.socket_path, connect_timeout=0.8),
            timeout=8.0,
            client_name="unified-browser-read",
        )

    def inspect(self, objective: str) -> dict[str, Any]:
        if INTERACTION_WORDS.search(objective):
            raise ValueError("browser_interaction_not_read_only")
        operation = (
            "tabs"
            if re.search(r"\b(?:tab|tabs|scheda|schede)\b", objective, re.I)
            else "snapshot"
        )
        tool = "browser_tabs" if operation == "tabs" else "browser_snapshot"
        arguments = {"action": "list"} if tool == "browser_tabs" else {}
        with self._session() as client:
            names = {item.name for item in client.list_tools()}
            if tool not in names:
                raise RuntimeError("browser_read_tool_not_discovered")
            result = client.call_tool(tool, arguments)
        if result.get("isError"):
            raise RuntimeError("browser_read_tool_failed")
        return {
            "ok": True,
            "operation": operation,
            "tool": tool,
            "arguments": arguments,
            "text": _text_result(result),
            "side_effects": 0,
            "writes": 0,
        }


class BrowserMCPApprovalProvider:
    """Narrow Playwright write adapter. No arbitrary tool names or free-form code."""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        session_factory: Callable[[], Any] | None = None,
        upload_roots: Sequence[str | Path] | None = None,
        upload_staging_dir: str | Path | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.socket_path = socket_path or os.getenv(
            "RALF_BROWSER_PLAYWRIGHT_MCP_SOCKET", DEFAULT_SOCKET
        )
        self._session_factory = session_factory
        self.timeout = timeout
        self._request_scope_depth = 0
        self._request_session_cm = None
        self._request_transaction = None
        self.upload_roots = tuple(
            Path(item).expanduser().resolve()
            for item in (upload_roots if upload_roots is not None else self._upload_roots_from_env())
        )
        self.upload_staging_dir = Path(
            upload_staging_dir if upload_staging_dir is not None else self._upload_staging_dir_from_env()
        ).expanduser().resolve()

    @staticmethod
    def _upload_roots_from_env() -> tuple[str, ...]:
        raw = os.getenv("RALFLOOP_BROWSER_UPLOAD_ROOTS", "")
        return tuple(item.strip() for item in raw.split(os.pathsep) if item.strip())

    @staticmethod
    def _upload_staging_dir_from_env() -> str:
        configured = os.getenv("RALFLOOP_BROWSER_UPLOAD_STAGING_DIR", "").strip()
        if configured:
            return configured
        output_root = os.getenv("RALF_PLAYWRIGHT_MCP_OUTPUT_DIR", "/tmp/ralf-playwright-mcp")
        return str(Path(output_root) / "approved-uploads")

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        return MCPClientSession(
            UnixMCPTransport(self.socket_path, connect_timeout=0.8),
            timeout=self.timeout,
            client_name="unified-browser-write",
        )

    @staticmethod
    def _tool_names(client: Any) -> set[str]:
        return {item.name for item in client.list_tools()}

    @contextmanager
    def _fresh_transaction(self):
        with self._session() as client:
            yield client, self._tool_names(client)

    @contextmanager
    def request_scope(self):
        self._request_scope_depth += 1
        try:
            yield self
        finally:
            self._request_scope_depth -= 1
            if self._request_scope_depth == 0 and self._request_session_cm is not None:
                cm = self._request_session_cm
                self._request_session_cm = None
                self._request_transaction = None
                cm.__exit__(None, None, None)

    @contextmanager
    def _borrow_transaction(self):
        if self._request_scope_depth > 0:
            if self._request_transaction is None:
                cm = self._fresh_transaction()
                self._request_session_cm = cm
                self._request_transaction = cm.__enter__()
            yield self._request_transaction
            return
        with self._fresh_transaction() as transaction:
            yield transaction

    @staticmethod
    def _checked_call(client: Any, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if tool not in READ_TOOLS | WRITE_TOOLS:
            raise MCPProtocolError("browser_tool_not_allowlisted")
        result = client.call_tool(tool, dict(arguments))
        if result.get("isError"):
            raise MCPProtocolError(f"browser_tool_failed:{tool}")
        return dict(result)

    def snapshot(self) -> dict[str, Any]:
        with self._borrow_transaction() as transaction:
            return self._snapshot_in_transaction(transaction)

    def _snapshot_in_transaction(
        self, transaction: tuple[Any, set[str]],
    ) -> dict[str, Any]:
        client, names = transaction
        if "browser_snapshot" not in names:
            raise MCPProtocolError("browser_snapshot_not_discovered")
        result = self._checked_call(client, "browser_snapshot", {})
        text = _text_result(result)
        if not text:
            raise MCPProtocolError("browser_snapshot_empty")
        return {"text": text, "sha256": _snapshot_hash(text)}

    def _validated_upload_paths(self, values: Sequence[Any]) -> list[str]:
        if not self.upload_roots:
            raise ValueError("browser_upload_roots_unconfigured")
        paths: list[str] = []
        for value in values:
            path = Path(str(value)).expanduser()
            if not path.is_absolute():
                raise ValueError("browser_upload_path_must_be_absolute")
            resolved = path.resolve()
            if not any(resolved == root or root in resolved.parents for root in self.upload_roots):
                raise ValueError("browser_upload_path_outside_allowed_roots")
            if not resolved.is_file():
                raise ValueError("browser_upload_file_missing")
            paths.append(str(resolved))
        if not paths:
            raise ValueError("browser_upload_paths_required")
        return paths

    @staticmethod
    def _upload_file_metadata(paths: Sequence[str]) -> list[dict[str, Any]]:
        return [
            {
                "path": path,
                "size": Path(path).stat().st_size,
                "sha256": _file_sha256(Path(path)),
            }
            for path in paths
        ]

    def _stage_upload_paths(self, paths: Sequence[str]) -> tuple[list[str], Path]:
        self.upload_staging_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.upload_staging_dir, 0o700)
        stage_dir = Path(tempfile.mkdtemp(prefix="approved-", dir=self.upload_staging_dir))
        os.chmod(stage_dir, 0o700)
        staged: list[str] = []
        try:
            for index, value in enumerate(paths):
                source = Path(value)
                slot = stage_dir / str(index)
                slot.mkdir(mode=0o700)
                destination = slot / source.name
                shutil.copyfile(source, destination)
                os.chmod(destination, 0o600)
                staged.append(str(destination))
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise
        return staged, stage_dir

    def normalize_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = str(payload.get("logical_action") or payload.get("action") or "").strip().casefold()
        if action not in LOGICAL_ACTIONS:
            raise ValueError("browser_action_not_allowed")
        target = str(payload.get("target") or "").strip()
        if not TARGET_REF_RE.fullmatch(target):
            raise ValueError("browser_target_must_be_snapshot_ref")
        normalized: dict[str, Any] = {
            "logical_action": action,
            "target": target,
            "element": str(payload.get("element") or "").strip()[:240],
        }
        if action == "type":
            text = payload.get("text")
            if text is None:
                raise ValueError("browser_type_text_required")
            normalized["text"] = str(text)
        elif action == "upload":
            values = payload.get("paths")
            if not isinstance(values, (list, tuple)):
                raise ValueError("browser_upload_paths_required")
            paths = self._validated_upload_paths(values)
            upload_files = self._upload_file_metadata(paths)
            expected = payload.get("upload_files")
            if expected is not None:
                if not isinstance(expected, (list, tuple)) or list(expected) != upload_files:
                    raise ValueError("browser_upload_file_changed")
            normalized["paths"] = paths
            normalized["upload_files"] = upload_files
        return normalized

    def apply(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        with self._borrow_transaction() as transaction:
            return self._apply_in_transaction(scope, transaction)

    def _apply_in_transaction(
        self, scope: Mapping[str, Any], transaction: tuple[Any, set[str]],
    ) -> dict[str, Any]:
        payload = self.normalize_payload(scope)
        action = str(payload["logical_action"])
        target = str(payload["target"])
        element = str(payload.get("element") or "")
        calls: list[dict[str, Any]] = []
        client, names = transaction
        required = {"browser_click"} if action in {"click", "submit", "upload"} else {"browser_type"}
        if action == "upload":
            required.add("browser_file_upload")
        if not required.issubset(names):
            raise MCPProtocolError("browser_write_tool_not_discovered")

        if action in {"click", "submit"}:
            arguments = {"target": target, **({"element": element} if element else {})}
            result = self._checked_call(client, "browser_click", arguments)
            calls.append({"tool": "browser_click", "arguments": arguments, "result": _text_result(result)})
        elif action == "type":
            arguments = {
                "target": target,
                "text": str(payload["text"]),
                "submit": False,
                **({"element": element} if element else {}),
            }
            result = self._checked_call(client, "browser_type", arguments)
            calls.append({"tool": "browser_type", "arguments": arguments, "result": _text_result(result)})
        else:
            staged_paths, stage_dir = self._stage_upload_paths(payload["paths"])
            try:
                click_args = {"target": target, **({"element": element} if element else {})}
                click_result = self._checked_call(client, "browser_click", click_args)
                calls.append({"tool": "browser_click", "arguments": click_args, "result": _text_result(click_result)})
                upload_args = {"paths": staged_paths}
                upload_result = self._checked_call(client, "browser_file_upload", upload_args)
                calls.append({
                    "tool": "browser_file_upload",
                    "arguments": {"paths": list(payload["paths"]), "staged": True},
                    "result": _text_result(upload_result),
                })
            finally:
                shutil.rmtree(stage_dir, ignore_errors=True)
        return {
            "ok": True,
            "logical_action": action,
            "calls": calls,
            "writes": len(calls),
        }


def prepare_browser_interaction_payload(
    provider: BrowserMCPApprovalProvider,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = provider.normalize_payload(arguments)
    before = provider.snapshot()
    target = str(normalized["target"])
    if not _target_present(str(before["text"]), target):
        raise ValueError("browser_target_not_in_snapshot")
    summary = [
        f"azione={normalized['logical_action']}",
        f"target={target}",
    ]
    if normalized.get("element"):
        summary.append(f"elemento={normalized['element']}")
    if normalized["logical_action"] == "type":
        summary.append(f"testo={normalized['text']}")
    if normalized["logical_action"] == "upload":
        for item in normalized["upload_files"]:
            summary.append(
                f"file={item['path']} sha256={item['sha256'][:12]} size={item['size']}"
            )
    return {
        **normalized,
        "pre_snapshot_sha256": str(before["sha256"]),
        "pre_snapshot_excerpt": _stable_snapshot_text(str(before["text"]))[:4000],
        "approval_summary": summary,
    }


def build_browser_interaction_scope(pending: PendingAction) -> dict[str, Any]:
    if pending.domain != "browser" or pending.action != BROWSER_INTERACT_ACTION:
        raise ValueError("browser_pending_required")
    payload = dict(pending.payload)
    logical_action = str(payload.get("logical_action") or "")
    target = str(payload.get("target") or "")
    pre_hash = str(payload.get("pre_snapshot_sha256") or "")
    if logical_action not in LOGICAL_ACTIONS:
        raise ValueError("browser_scope_action_invalid")
    if not TARGET_REF_RE.fullmatch(target) or not re.fullmatch(r"[0-9a-f]{64}", pre_hash):
        raise ValueError("browser_scope_incomplete")
    scope = {
        "action": BROWSER_INTERACT_ACTION,
        "version": 1,
        "pending_id": pending.pending_id,
        "pending_version": pending.version,
        "payload_digest": pending.payload_digest,
        "logical_action": logical_action,
        "target": target,
        "element": str(payload.get("element") or ""),
        "pre_snapshot_sha256": pre_hash,
        "approval_summary": list(payload.get("approval_summary") or ()),
    }
    if logical_action == "type":
        scope["text"] = str(payload.get("text") or "")
    if logical_action == "upload":
        scope["paths"] = [str(item) for item in payload.get("paths") or ()]
        scope["upload_files"] = [dict(item) for item in payload.get("upload_files") or ()]
    scope["artifact_sha256"] = _canonical_hash(scope)
    return scope


class UnifiedBrowserApprovalCoordinator:
    def __init__(self, store: DomainApprovalStore, *, policy: DomainApprovalPolicy) -> None:
        self.store = store
        self.policy = policy

    def request(self, pending: PendingAction, *, requested_by: str) -> dict[str, Any]:
        if not self.policy.enabled or not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {"status": "approval_gate_unavailable"}
        scope = build_browser_interaction_scope(pending)
        created = self.store.create_request(
            action=BROWSER_INTERACT_ACTION,
            bando_id="browser.playwright",
            version=str(pending.version),
            scope=scope,
            requested_by=requested_by,
        )
        request = created.get("request") if isinstance(created, Mapping) else None
        if not isinstance(request, Mapping):
            return {"status": str(created.get("status") or "approval_request_failed")}
        return {
            "status": "pending",
            "request_id": str(request["request_id"]),
            "created_at": int(request["created_at"]),
            "expires_at": int(request["expires_at"]),
            "scope_digest_short": str(request["scope_digest_short"]),
        }

    def approve(
        self,
        pending: PendingAction,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        chat_type: str = "private",
    ) -> dict[str, Any]:
        if not pending.approval_ref:
            return {"status": "approval_request_missing"}
        row = self.store.get_request(pending.approval_ref)
        if not row:
            return {"status": "approval_request_missing"}
        scope = build_browser_interaction_scope(pending)
        if str(row.get("scope_digest") or "") != scope_digest(scope):
            self.store.mark_stale(pending.approval_ref, ["browser_scope_changed"])
            return {"status": "scope_digest_mismatch"}
        return self.store.decide(
            DomainApprovalDecision(
                request_id=pending.approval_ref,
                decision="approve",
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id,
                chat_type=chat_type,
                idempotency_key=(
                    f"browser:{pending.pending_id}:{pending.version}:{telegram_message_id}"
                ),
            ),
            scope_digest_short=str(row.get("scope_digest_short") or ""),
        )

    def cancel(self, pending: PendingAction) -> dict[str, Any]:
        if not pending.approval_ref:
            return {"status": "no_approval_request"}
        return self.store.cancel(pending.approval_ref)


class UnifiedBrowserApprovalExecutor:
    def __init__(
        self,
        store: DomainApprovalStore,
        provider: BrowserMCPApprovalProvider,
        *,
        write_enabled: bool = True,
    ) -> None:
        self.store = store
        self.provider = provider
        self.write_enabled = bool(write_enabled)

    def execute(self, pending: PendingAction) -> dict[str, Any]:
        if not payload_matches(pending) or not approval_matches(pending):
            return {"status": "APPROVAL_REQUIRED", "executed": False, "writes": 0}
        if pending.action != BROWSER_INTERACT_ACTION:
            return {"status": "POLICY_DENIED", "executed": False, "writes": 0}

        request_id = str(pending.approval_ref or "")
        row = self.store.get_request(request_id)
        stored_status = str((row or {}).get("status") or "")
        if stored_status == "consumed":
            return {
                "status": "already_executed",
                "executed": True,
                "writes": 0,
                "retry_allowed": False,
            }
        if stored_status in {"executing", "execution_failed"}:
            return {
                "status": "EXECUTION_UNCERTAIN",
                "executed": False,
                "writes": 0,
                "retry_allowed": False,
            }
        if row is None or effective_approval_status(row) != "approved":
            return {"status": "APPROVAL_INVALID", "executed": False, "writes": 0}

        scope = build_browser_interaction_scope(pending)
        if str(row.get("scope_digest") or "") != scope_digest(scope):
            self.store.mark_stale(request_id, ["browser_scope_changed"])
            return {"status": "DRAFT_CHANGED", "executed": False, "writes": 0}

        try:
            before = self.provider.snapshot()
        except Exception:
            return {
                "status": "SOURCE_UNAVAILABLE",
                "executed": False,
                "writes": 0,
                "retry_allowed": True,
            }
        if str(before["sha256"]) != str(scope["pre_snapshot_sha256"]):
            self.store.mark_stale(request_id, ["browser_snapshot_changed_before_action"])
            return {
                "status": "DRAFT_CHANGED",
                "executed": False,
                "writes": 0,
                "retry_allowed": False,
            }
        if not _target_present(str(before["text"]), str(scope["target"])):
            self.store.mark_stale(request_id, ["browser_target_missing_before_action"])
            return {
                "status": "DRAFT_CHANGED",
                "executed": False,
                "writes": 0,
                "retry_allowed": False,
            }
        if not self.write_enabled:
            return {
                "status": "browser_write_disabled",
                "executed": False,
                "writes": 0,
                "retry_allowed": True,
                "provider_call_attempted": False,
            }

        claim = self.store.claim_execution(request_id, action=BROWSER_INTERACT_ACTION)
        if not claim.get("claimed"):
            return {
                "status": str(claim.get("status") or "execution_claim_failed"),
                "executed": False,
                "writes": 0,
                "retry_allowed": False,
            }

        writes = 0
        try:
            applied = self.provider.apply(scope)
            writes = int(applied.get("writes") or 0)
            after = self.provider.snapshot()
            if not str(after.get("text") or ""):
                raise RuntimeError("browser_post_action_snapshot_empty")
        except Exception as exc:
            result = {
                "status": "EXECUTION_UNCERTAIN",
                "executed": False,
                "writes": max(1, writes),
                "retry_allowed": False,
                "reason": type(exc).__name__,
            }
            if isinstance(exc, MCPError):
                result["error_detail"] = str(exc)[:500]
            self.store.finish_claimed_execution(
                request_id,
                action=BROWSER_INTERACT_ACTION,
                success=False,
                result=result,
            )
            return result

        result = {
            "status": "EXECUTED_VERIFIED",
            "executed": True,
            "writes": writes,
            "retry_allowed": False,
            "logical_action": str(scope["logical_action"]),
            "target": str(scope["target"]),
            "provider_result": dict(applied),
            "post_snapshot_sha256": str(after["sha256"]),
            "post_snapshot": str(after["text"])[:4000],
        }
        finalized = self.store.finish_claimed_execution(
            request_id,
            action=BROWSER_INTERACT_ACTION,
            success=True,
            result=result,
        )
        if finalized.get("status") != "consumed":
            return {
                "status": "EXECUTION_UNCERTAIN",
                "executed": True,
                "writes": writes,
                "retry_allowed": False,
            }
        return result


def browser_inspect_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    payload = BrowserMCPReadOnly().inspect(assignment.objective)
    text = str(payload.get("text") or "")
    message = (
        text[:4000]
        if text
        else f"Browser {payload['operation']} letto senza effetti esterni."
    )
    return StructuredArtifact.create(
        artifact_type="browser_inspection",
        status="completed",
        producer_task_id=assignment.task_id,
        evidence_refs=(f"browser_playwright:{payload['tool']}",),
        payload={
            **payload,
            "message": message,
            "content_boundary": "browser_page_content_is_untrusted_data",
        },
    )


__all__ = [
    "BROWSER_INTERACT_ACTION",
    "BrowserMCPApprovalProvider",
    "BrowserMCPReadOnly",
    "DEFAULT_SOCKET",
    "INTERACTION_WORDS",
    "LOGICAL_ACTIONS",
    "READ_TOOLS",
    "TARGET_REF_RE",
    "UnifiedBrowserApprovalCoordinator",
    "UnifiedBrowserApprovalExecutor",
    "WRITE_TOOLS",
    "browser_inspect_adapter",
    "build_browser_interaction_scope",
    "prepare_browser_interaction_payload",
]
