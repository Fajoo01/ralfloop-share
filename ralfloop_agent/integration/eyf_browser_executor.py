from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore


CAPABILITY = "eyf_support4youth_browser"
APPROVAL_ACTION = "eyf_browser_apply"
SCHEMA_VERSION = "eyf_browser_v1"
ALLOWED_HOST = "support4youth.coe.int"

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
    ) -> None:
        self.store = store
        self.policy = policy
        self.browser = browser
        self.allowed_uploads = {
            str(Path(item).expanduser().resolve(strict=True))
            for item in allowed_uploads
        }

    def preview(
        self,
        operations: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        snapshot = self._snapshot()
        normalized, files = self._normalize_operations(operations)
        scope = _build_scope(snapshot, normalized, files)

        return {
            "status": "preview",
            "capability": CAPABILITY,
            "host": ALLOWED_HOST,
            "operation_count": len(normalized),
            "file_count": len(files),
            "has_submit": any(item["kind"] == "submit" for item in normalized),
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
            snapshot = self._snapshot()
            normalized, files = self._normalize_operations(operations)
        except ValueError as exc:
            return {
                "status": str(exc),
                "approval_required": True,
                "executed": False,
            }

        scope = _build_scope(snapshot, normalized, files)

        return self.store.create_request(
            action=APPROVAL_ACTION,
            bando_id="eyf.support4youth",
            version=SCHEMA_VERSION,
            scope=scope,
            requested_by=requested_by,
        )

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

        # Verifica integrità del batch memorizzato.
        expected_batch = str(scope.get("batch_sha256") or "")
        reconstructed = _batch_digest_from_scope(scope)
        if not expected_batch or reconstructed != expected_batch:
            return self._stale(request_id, "batch_changed")

        # Snapshot read-only PRIMA del claim.
        try:
            current_snapshot = self._snapshot()
        except ValueError as exc:
            return self._stale(request_id, str(exc))

        if current_snapshot["page_sha256"] != str(scope.get("page_sha256") or ""):
            return self._stale(request_id, "page_changed")

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

            result = {
                "status": "executed",
                "request_id": request_id,
                "capability": CAPABILITY,
                "batch_sha256": expected_batch,
                "executed": True,
                "verified": True,
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

    def _snapshot(self) -> dict[str, str]:
        raw = self.browser.snapshot()
        if not isinstance(raw, Mapping):
            raise ValueError("snapshot_invalid")

        url = str(raw.get("url") or "")
        parsed = urlparse(url)

        if parsed.scheme != "https" or parsed.hostname != ALLOWED_HOST:
            raise ValueError("host_forbidden")

        stable = raw.get("stable")
        if stable is None:
            stable = {
                str(key): value
                for key, value in raw.items()
                if str(key) not in _VOLATILE_SNAPSHOT_KEYS
            }

        canonical = _canonical_json(stable)

        return {
            "url": url,
            "page_sha256": hashlib.sha256(canonical).hexdigest(),
        }

    def _normalize_operations(
        self,
        operations: Sequence[Mapping[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        if not operations or len(operations) > 64:
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

        return normalized, files

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


def _build_scope(
    snapshot: Mapping[str, str],
    operations: list[dict[str, Any]],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "capability": CAPABILITY,
        "host": ALLOWED_HOST,
        "url": str(snapshot["url"]),
        "page_sha256": str(snapshot["page_sha256"]),
        "operations": operations,
        "files": files,
    }

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
        "page_sha256": scope.get("page_sha256"),
        "operations": scope.get("operations"),
        "files": scope.get("files"),
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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
    "APPROVAL_ACTION",
    "CAPABILITY",
    "EyfBrowserAdapter",
    "EyfBrowserApprovalService",
    "SCHEMA_VERSION",
]
