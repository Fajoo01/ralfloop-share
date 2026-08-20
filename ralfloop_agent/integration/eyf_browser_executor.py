from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.storage import append_jsonl


CAPABILITY = "eyf_support4youth_browser"
APPROVAL_ACTION = "eyf_browser_apply"
SCHEMA_VERSION = "eyf_browser_v2"
ALLOWED_HOST = "support4youth.coe.int"
ALLOWED_PATH = "/organization/profile"
FINAL_SUBMIT_TARGET = "workflow:send-updates"
FINAL_DECLARATIONS = {
    "accept_terms": True,
    "accept_data_processing": True,
}

_ALLOWED_KINDS = {"fill", "upload", "click", "submit"}
_VOLATILE_SNAPSHOT_KEYS = {
    "timestamp",
    "captured_at",
    "duration_ms",
    "elapsed_ms",
}


class EyfBrowserAdapter(Protocol):
    """Injected browser boundary. No browser side effect exists outside execute()."""

    def snapshot(self) -> Mapping[str, Any]:
        ...

    def fill(self, target: str, value: str) -> Mapping[str, Any] | None:
        ...

    def upload(self, target: str, path: str) -> Mapping[str, Any] | None:
        ...

    def click(self, target: str) -> Mapping[str, Any] | None:
        ...

    def submit(self, target: str) -> Mapping[str, Any] | None:
        ...


class EyfBrowserApprovalService:
    def __init__(
        self,
        store: DomainApprovalStore,
        *,
        policy: DomainApprovalPolicy,
        browser: EyfBrowserAdapter,
        allowed_uploads: Sequence[str | Path] = (),
        approval_outbox: str | Path | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.browser = browser
        self.allowed_uploads = {
            str(Path(item).expanduser().resolve(strict=True))
            for item in allowed_uploads
        }
        configured_outbox = approval_outbox or os.getenv(
            "RALFLOOP_TELEGRAM_APPROVAL_OUTBOX"
        )
        self.approval_outbox = (
            Path(configured_outbox).expanduser()
            if configured_outbox
            else None
        )

    def preview(
        self,
        operations: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized, files = self._normalize_operations(operations)
        phase = _approval_phase(normalized)
        snapshot = self._snapshot()
        self._validate_snapshot_operations(normalized, snapshot)
        scope = _build_scope(snapshot, normalized, files, phase)

        return {
            "status": "preview",
            "capability": CAPABILITY,
            "host": ALLOWED_HOST,
            "operation_count": len(normalized),
            "file_count": len(files),
            "has_submit": any(item["kind"] == "submit" for item in normalized),
            "approval_phase": phase,
            "target_id": scope["target_id"],
            "page_sha256": scope["page_sha256"],
            "batch_sha256": scope["batch_sha256"],
            "scope_digest": scope_digest(scope),
            "approval_required": True,
            "arbitrary_shell": False,
            "executed": False,
        }

    def request(
        self,
        operations: Sequence[Mapping[str, Any]],
        *,
        requested_by: str,
    ) -> dict[str, Any]:
        if not self.policy.enabled:
            return {
                "status": "approval_gate_disabled",
                "approval_required": True,
                "executed": False,
            }

        if not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {
                "status": "approval_allowlist_unconfigured",
                "approval_required": True,
                "executed": False,
            }

        try:
            normalized, files = self._normalize_operations(operations)
            phase = _approval_phase(normalized)
            snapshot = self._snapshot()
            self._validate_snapshot_operations(normalized, snapshot)
        except ValueError as exc:
            return {
                "status": str(exc),
                "approval_required": True,
                "executed": False,
            }

        scope = _build_scope(snapshot, normalized, files, phase)

        out = self.store.create_request(
            action=APPROVAL_ACTION,
            bando_id="eyf.support4youth",
            version=SCHEMA_VERSION,
            scope=scope,
            requested_by=requested_by,
        )
        request = out.get("request") if isinstance(out, Mapping) else None
        request_id = str(
            request.get("request_id")
            if isinstance(request, Mapping)
            else ""
        )
        if not request_id:
            return out
        if self.approval_outbox is None:
            self.store.cancel(request_id)
            return {
                "status": "approval_notification_unconfigured",
                "request_id": request_id,
                "approval_required": True,
                "notification_queued": False,
                "executed": False,
            }
        try:
            append_jsonl(
                self.approval_outbox,
                {
                    "status": "queued",
                    "request_id": request_id,
                    "api_url": self.policy.api_url,
                    "message": str(request.get("telegram_message") or ""),
                },
            )
        except OSError:
            self.store.cancel(request_id)
            return {
                "status": "approval_notification_failed",
                "request_id": request_id,
                "approval_required": True,
                "notification_queued": False,
                "executed": False,
            }
        return {**out, "notification_queued": True}

    def execute(self, request_id: str) -> dict[str, Any]:
        if not self.policy.enabled:
            return {
                "status": "approval_gate_disabled",
                "request_id": request_id,
                "executed": False,
            }

        row = self.store.get_request(request_id)
        if not row:
            return {
                "status": "not_found",
                "request_id": request_id,
                "executed": False,
            }

        effective = effective_approval_status(row)

        if effective == "consumed":
            return {
                "status": "already_executed",
                "request_id": request_id,
                "executed": False,
            }

        if effective == "executing":
            return {
                "status": "execution_outcome_pending",
                "request_id": request_id,
                "executed": False,
                "retry_allowed": False,
            }

        if effective == "execution_failed":
            return {
                "status": "execution_failed",
                "request_id": request_id,
                "executed": False,
                "retry_allowed": False,
            }

        if effective != "approved":
            return {
                "status": effective,
                "request_id": request_id,
                "executed": False,
            }

        if row.get("action") != APPROVAL_ACTION:
            return self._stale(request_id, "approval_action_changed")

        scope = row.get("scope")
        if not isinstance(scope, Mapping):
            return self._stale(request_id, "scope_missing")

        approved_scope_digest = str(row.get("scope_digest") or "")
        current_scope_digest = scope_digest(dict(scope))
        if not approved_scope_digest or not hmac.compare_digest(
            approved_scope_digest,
            current_scope_digest,
        ):
            return self._stale(request_id, "scope_digest_changed")

        # Verifica integrità del batch memorizzato.
        expected_batch = str(scope.get("batch_sha256") or "")
        reconstructed = _batch_digest_from_scope(scope)
        if not expected_batch or reconstructed != expected_batch:
            return self._stale(request_id, "batch_changed")

        try:
            stored_phase = _approval_phase(list(scope.get("operations") or []))
        except ValueError as exc:
            return self._stale(request_id, str(exc))
        if stored_phase != str(scope.get("approval_phase") or ""):
            return self._stale(request_id, "approval_phase_changed")
        if (
            stored_phase == "final_submission"
            and scope.get("final_declarations") != FINAL_DECLARATIONS
        ):
            return self._stale(request_id, "final_declarations_changed")

        # Snapshot read-only PRIMA del claim.
        try:
            current_snapshot = self._snapshot()
        except ValueError as exc:
            return self._stale(request_id, str(exc))

        if current_snapshot["page_sha256"] != str(scope.get("page_sha256") or ""):
            return self._stale(request_id, "page_changed")

        state_validator = getattr(
            self.browser,
            "validate_snapshot_for_operations",
            None,
        )
        if callable(state_validator):
            try:
                state_validator(
                    list(scope.get("operations") or []),
                    current_snapshot.get("verification_state") or {},
                )
            except ValueError as exc:
                return self._stale(request_id, str(exc))

        # File re-hash PRIMA del claim.
        try:
            current_files = self._rehash_scope_files(scope)
        except ValueError as exc:
            return self._stale(request_id, str(exc))

        if current_files != list(scope.get("files") or []):
            return self._stale(request_id, "file_changed")

        claim = self.store.claim_execution(
            request_id,
            action=APPROVAL_ACTION,
        )
        if not claim.get("claimed"):
            return {
                "status": str(claim.get("status") or "execution_not_claimed"),
                "request_id": request_id,
                "executed": False,
            }

        # Da qui in poi qualunque incertezza è terminale/no-retry.
        try:
            executed = []
            for operation in list(scope.get("operations") or []):
                kind = str(operation["kind"])
                target = str(operation["target"])

                if kind == "fill":
                    result = self.browser.fill(target, str(operation["value"]))
                elif kind == "upload":
                    result = self.browser.upload(target, str(operation["path"]))
                elif kind == "click":
                    result = self.browser.click(target)
                elif kind == "submit":
                    result = self.browser.submit(target)
                else:
                    raise RuntimeError("stored_operation_invalid")

                if isinstance(result, Mapping) and result.get("ok") is False:
                    raise RuntimeError(f"browser_{kind}_failed")

                executed.append(kind)

            post = self._snapshot()
            verifier = getattr(self.browser, "verify_postconditions", None)
            verification: Mapping[str, Any] = {
                "ok": False,
                "reason": "adapter_verifier_unavailable",
            }
            if not callable(verifier):
                raise RuntimeError("adapter_verifier_unavailable")
            candidate = verifier(
                list(scope.get("operations") or []),
                scope.get("verification_state") or {},
                post.get("verification_state") or {},
            )
            if not isinstance(candidate, Mapping):
                raise RuntimeError("browser_postcondition_invalid")
            verification = candidate
            if verification.get("ok") is not True:
                raise RuntimeError("browser_postcondition_unverified")

            result = {
                "status": "executed",
                "request_id": request_id,
                "capability": CAPABILITY,
                "batch_sha256": expected_batch,
                "approval_phase": stored_phase,
                "executed": True,
                "verified": verification.get("ok") is True,
                "verification": dict(verification),
                "operation_count": len(executed),
                "post_page_sha256": post["page_sha256"],
                "retry_allowed": False,
            }

            finalized = self.store.finish_claimed_execution(
                request_id,
                action=APPROVAL_ACTION,
                success=True,
                result=result,
            )

            if finalized.get("status") != "consumed":
                return {
                    "status": "execution_finalization_failed",
                    "request_id": request_id,
                    "executed": False,
                    "retry_allowed": False,
                }

            return result

        except Exception as exc:
            result = {
                "status": "execution_failed",
                "request_id": request_id,
                "capability": CAPABILITY,
                "executed": False,
                "retry_allowed": False,
                "reason": "browser_outcome_failed_or_uncertain",
                "detail": type(exc).__name__,
            }

            self.store.finish_claimed_execution(
                request_id,
                action=APPROVAL_ACTION,
                success=False,
                result=result,
            )
            return result

    def _snapshot(self) -> dict[str, Any]:
        raw = self.browser.snapshot()
        if not isinstance(raw, Mapping):
            raise ValueError("snapshot_invalid")

        url = str(raw.get("url") or "")
        parsed = urlsplit(url)

        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("host_forbidden") from exc
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").casefold() != ALLOWED_HOST
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("host_forbidden")
        if (
            parsed.path.rstrip("/") != ALLOWED_PATH
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("path_forbidden")

        stable = raw.get("stable")
        if stable is None:
            stable = {
                str(key): value
                for key, value in raw.items()
                if str(key) not in _VOLATILE_SNAPSHOT_KEYS
            }

        canonical = _canonical_json(stable)

        snapshot: dict[str, Any] = {
            "url": url,
            "target_id": str(raw.get("target_id") or ""),
            "page_sha256": hashlib.sha256(canonical).hexdigest(),
        }
        if callable(getattr(self.browser, "verify_postconditions", None)):
            snapshot["verification_state"] = stable
        return snapshot

    def _normalize_operations(
        self,
        operations: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not operations or len(operations) > 128:
            raise ValueError("batch_size_invalid")

        normalized: list[dict[str, Any]] = []
        files: list[dict[str, Any]] = []
        submit_indexes: list[int] = []

        for index, raw in enumerate(operations):
            if not isinstance(raw, Mapping):
                raise ValueError("operation_invalid")

            kind = str(raw.get("kind") or "")
            target = str(raw.get("target") or "").strip()

            if kind not in _ALLOWED_KINDS:
                raise ValueError("operation_kind_forbidden")
            if not target or len(target) > 256:
                raise ValueError("operation_target_invalid")

            if kind == "fill":
                if set(raw) - {"kind", "target", "value"}:
                    raise ValueError("operation_fields_invalid")
                value = raw.get("value")
                if not isinstance(value, str) or len(value) > 20000:
                    raise ValueError("fill_value_invalid")
                normalized.append(
                    {"kind": kind, "target": target, "value": value}
                )

            elif kind == "upload":
                if set(raw) - {"kind", "target", "path"}:
                    raise ValueError("operation_fields_invalid")
                path = self._file_binding(str(raw.get("path") or ""))
                normalized.append(
                    {
                        "kind": kind,
                        "target": target,
                        "path": path["path"],
                        "sha256": path["sha256"],
                        "size": path["size"],
                    }
                )
                files.append(path)

            else:
                if set(raw) - {"kind", "target"}:
                    raise ValueError("operation_fields_invalid")
                normalized.append({"kind": kind, "target": target})
                if kind == "submit":
                    submit_indexes.append(index)

        if len(submit_indexes) > 1:
            raise ValueError("multiple_submit_forbidden")

        if submit_indexes and submit_indexes[0] != len(normalized) - 1:
            raise ValueError("submit_must_be_final")

        validator = getattr(self.browser, "validate_operation", None)
        if callable(validator):
            for operation in normalized:
                validator(operation)
        batch_validator = getattr(self.browser, "validate_batch", None)
        if callable(batch_validator):
            batch_validator(normalized)

        if len("\n".join(_approval_summary(normalized))) > 2400:
            raise ValueError("approval_summary_too_large")

        return normalized, files

    def _validate_snapshot_operations(
        self,
        operations: Sequence[Mapping[str, Any]],
        snapshot: Mapping[str, Any],
    ) -> None:
        validator = getattr(
            self.browser,
            "validate_snapshot_for_operations",
            None,
        )
        if callable(validator):
            validator(
                operations,
                snapshot.get("verification_state") or {},
            )

    def _file_binding(self, raw: str) -> dict[str, Any]:
        try:
            candidate = Path(raw).expanduser()
            if candidate.is_symlink():
                raise ValueError("upload_symlink_forbidden")
            path = candidate.resolve(strict=True)
        except OSError as exc:
            raise ValueError("upload_missing") from exc

        if not path.is_file():
            raise ValueError("upload_not_file")

        canonical = str(path)
        if canonical not in self.allowed_uploads:
            raise ValueError("upload_not_allowlisted")

        stat = path.stat()
        if stat.st_size > 25 * 1024 * 1024:
            raise ValueError("upload_too_large")

        return {
            "path": canonical,
            "size": stat.st_size,
            "sha256": _sha256_file(path),
        }

    def _rehash_scope_files(
        self,
        scope: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        current = []
        for stored in list(scope.get("files") or []):
            if not isinstance(stored, Mapping):
                raise ValueError("file_binding_invalid")
            current.append(self._file_binding(str(stored.get("path") or "")))
        return current

    def _stale(self, request_id: str, reason: str) -> dict[str, Any]:
        self.store.mark_stale(request_id, [reason])
        return {
            "status": "stale",
            "request_id": request_id,
            "executed": False,
            "reason": reason,
        }


def _approval_phase(operations: Sequence[Mapping[str, Any]]) -> str:
    submit_indexes = [
        index
        for index, operation in enumerate(operations)
        if str(operation.get("kind") or "") == "submit"
    ]
    if submit_indexes:
        if (
            len(submit_indexes) != 1
            or len(operations) != 1
            or str(operations[0].get("target") or "")
            != FINAL_SUBMIT_TARGET
        ):
            raise ValueError("final_submit_requires_separate_approval")
        return "final_submission"
    return "save_batch"


def _build_scope(
    snapshot: Mapping[str, Any],
    operations: list[dict[str, Any]],
    files: list[dict[str, Any]],
    approval_phase: str,
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "capability": CAPABILITY,
        "host": ALLOWED_HOST,
        "url": str(snapshot["url"]),
        "target_id": str(snapshot.get("target_id") or ""),
        "page_sha256": str(snapshot["page_sha256"]),
        "approval_phase": approval_phase,
        "approval_summary": _approval_summary(operations),
        "verification_state": snapshot.get("verification_state") or {},
        "operations": operations,
        "files": files,
    }
    if approval_phase == "final_submission":
        payload["final_declarations"] = dict(FINAL_DECLARATIONS)

    return {
        **payload,
        "batch_sha256": hashlib.sha256(_canonical_json(payload)).hexdigest(),
    }


def _batch_digest_from_scope(scope: Mapping[str, Any]) -> str:
    payload = {
        "schema_version": scope.get("schema_version"),
        "capability": scope.get("capability"),
        "host": scope.get("host"),
        "url": scope.get("url"),
        "target_id": scope.get("target_id"),
        "page_sha256": scope.get("page_sha256"),
        "approval_phase": scope.get("approval_phase"),
        "approval_summary": scope.get("approval_summary"),
        "verification_state": scope.get("verification_state"),
        "operations": scope.get("operations"),
        "files": scope.get("files"),
    }
    if scope.get("approval_phase") == "final_submission":
        payload["final_declarations"] = scope.get("final_declarations")
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _approval_summary(
    operations: Sequence[Mapping[str, Any]],
) -> list[str]:
    lines: list[str] = []
    for operation in operations:
        kind = str(operation.get("kind") or "")
        target = str(operation.get("target") or "")
        if kind == "fill":
            value = json.dumps(
                str(operation.get("value") or ""),
                ensure_ascii=False,
            )
            lines.append(f"fill {target} = {value}")
        elif kind == "upload":
            lines.append(
                "upload "
                f"{target} sha256={operation.get('sha256')} "
                f"size={operation.get('size')}"
            )
        else:
            lines.append(f"{kind} {target}")
    return lines


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ALLOWED_HOST",
    "ALLOWED_PATH",
    "APPROVAL_ACTION",
    "CAPABILITY",
    "EyfBrowserAdapter",
    "EyfBrowserApprovalService",
    "FINAL_SUBMIT_TARGET",
    "SCHEMA_VERSION",
]
