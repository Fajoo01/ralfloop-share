from __future__ import annotations

"""Base portal adapter contract and audit-aware approval service.

Gli adapter concreti devono implementare snapshot/preview/request/execute.
Il service avvolge ogni chiamata con un audit JSONL unificato e non aggiunge
politiche di approvazione: il fail-closed resta responsabilità dell'adapter.
"""

from pathlib import Path
import os
from typing import Any, Mapping, Protocol, Sequence

from src.audit import log_operation


class PortalAdapter(Protocol):
    def snapshot(self) -> Mapping[str, Any]: ...

    def preview(
        self,
        operations: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]: ...

    def request(
        self,
        operations: Sequence[Mapping[str, Any]],
        requested_by: str = "ralf",
    ) -> Mapping[str, Any]: ...

    def execute(self, request_id: str) -> Mapping[str, Any]: ...


class ApprovalPortalService:
    """Wrapper comune per audit JSONL di operazioni portali."""

    def __init__(
        self,
        adapter: PortalAdapter,
        *,
        audit_path: str | Path | None = None,
    ) -> None:
        self.adapter = adapter
        configured_audit = audit_path or os.getenv(
            "RALF_PORTAL_AUDIT",
            "logs/portal_adapter_audit.jsonl",
        )
        self.audit_path = Path(configured_audit)

    def _audit(self, method: str, payload: Mapping[str, Any]) -> None:
        try:
            log_operation(
                f"portal.{method}",
                {"method": method, **dict(payload)},
                audit_path=self.audit_path,
            )
        except Exception:
            # L'audit non deve alterare il risultato operativo.
            pass

    def snapshot(self) -> Mapping[str, Any]:
        result = dict(self.adapter.snapshot())
        self._audit("snapshot", result)
        return result

    def preview(
        self,
        operations: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        result = dict(self.adapter.preview(operations))
        self._audit("preview", result)
        return result

    def request(
        self,
        operations: Sequence[Mapping[str, Any]],
        requested_by: str = "ralf",
    ) -> Mapping[str, Any]:
        result = dict(
            self.adapter.request(operations, requested_by=requested_by)
        )
        self._audit("request", result)
        return result

    def execute(self, request_id: str) -> Mapping[str, Any]:
        result = dict(self.adapter.execute(request_id))
        self._audit("execute", result)
        return result


__all__ = [
    "ApprovalPortalService",
    "PortalAdapter",
]
