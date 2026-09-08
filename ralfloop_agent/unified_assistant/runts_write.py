from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping, Protocol

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from .conversation import PendingAction, approval_matches, payload_matches


RUNTS_REPLY_ACTION = "runts_practice_reply"
RUNTS_DOCUMENT_TYPE = "B00"


class RuntsReplyReader(Protocol):
    def get_practice(self, native_id: str): ...
    def list_messages(self, practice_id: str, *, limit: int): ...
    def get_message(self, native_id: str): ...


class RuntsReplyWriter(Protocol):
    def preflight(self, scope: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def execute(self, scope: Mapping[str, Any]) -> Mapping[str, Any]: ...


def _sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()

    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)

    return h.hexdigest()


def _canonical_sha(value: Mapping[str, Any]) -> str:
    raw = json.dumps(
        dict(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    return hashlib.sha256(raw).hexdigest()


def _message_hash_from_proposal(
    proposal: Mapping[str, Any],
) -> str:
    message_id = str(
        proposal.get("authoritative_message_id") or ""
    )

    matches = []

    for item in proposal.get("provenance") or ():
        if not isinstance(item, Mapping):
            continue

        if (
            str(item.get("system") or "") == "runts"
            and str(item.get("native_id") or "") == message_id
            and str(item.get("content_hash") or "")
        ):
            matches.append(str(item["content_hash"]))

    matches = list(dict.fromkeys(matches))

    if len(matches) != 1:
        raise ValueError(
            "runts_authoritative_message_hash_unresolved"
        )

    return matches[0]


def build_runts_pending_payload(
    proposal: Mapping[str, Any],
    *,
    expected_practice_status: str,
) -> dict[str, Any]:
    if str(proposal.get("status") or "") != (
        "READY_FOR_HUMAN_APPROVAL"
    ):
        raise ValueError("runts_proposal_not_ready")

    if proposal.get("blockers"):
        raise ValueError("runts_proposal_blocked")

    review = proposal.get("review_context")

    if not isinstance(review, Mapping):
        raise ValueError("runts_review_context_required")

    validation = proposal.get("final_validation")

    if not isinstance(validation, Mapping):
        raise ValueError("runts_final_validation_required")

    practice_id = str(proposal.get("practice_id") or "")
    message_id = str(
        proposal.get("authoritative_message_id") or ""
    )
    proposal_id = str(proposal.get("proposal_id") or "")
    pdf_path = str(review.get("pdf_path") or "")
    pdf_sha256 = str(proposal.get("sha256") or "")
    exercise = int(validation.get("exercise") or 0)

    if not (
        practice_id
        and message_id
        and proposal_id
        and pdf_path
        and len(pdf_sha256) == 64
        and exercise
    ):
        raise ValueError("runts_proposal_shape_invalid")

    path = Path(pdf_path)

    if not path.is_file():
        raise ValueError("runts_pdf_missing")

    actual = _sha256_file(path)

    if actual != pdf_sha256:
        raise ValueError("runts_pdf_hash_mismatch")

    authoritative_hash = _message_hash_from_proposal(
        proposal
    )

    subject = (
        f"Integrazione pratica {practice_id} - "
        f"Rendiconto per cassa {exercise}"
    )

    body = (
        "In riscontro alla comunicazione dell'Ufficio, "
        f"si allega il rendiconto per cassa relativo "
        f"all'esercizio {exercise}, redatto conformemente "
        "al Modello D di cui al D.M. n. 39/2020 e completo "
        "di tutte le sezioni richieste.\n\n"
        "La rettifica riguarda esclusivamente la corretta "
        "rappresentazione nel modello ministeriale, "
        "mantenendo invariati i valori del rendiconto "
        "approvato.\n\n"
        "Si trasmette pertanto il Modello D corretto tramite "
        f"la messaggistica della pratica n. {practice_id}, "
        "come richiesto dall'Ufficio."
    )

    return {
        "practice_id": practice_id,
        "authoritative_message_id": message_id,
        "authoritative_message_hash": authoritative_hash,
        "proposal_id": proposal_id,
        "expected_practice_status": str(
            expected_practice_status
        ),
        "exercise": exercise,
        "subject": subject,
        "body": body,
        "body_sha256": hashlib.sha256(
            body.encode()
        ).hexdigest(),
        "pdf_path": pdf_path,
        "pdf_name": path.name,
        "pdf_sha256": pdf_sha256,
        "document_type_code": RUNTS_DOCUMENT_TYPE,
        "channel": "RUNTS MESSAGGISTICA",
        "no_new_deposit": True,
    }


def build_runts_reply_approval_scope(
    pending: PendingAction,
) -> dict[str, Any]:
    if (
        pending.domain != "runts"
        or pending.action != RUNTS_REPLY_ACTION
    ):
        raise ValueError("runts_pending_required")

    payload = pending.payload

    artifact = {
        "action": pending.action,
        "version": 1,
        "pending_id": pending.pending_id,
        "pending_version": pending.version,
        "payload_digest": pending.payload_digest,
        "practice_id": str(
            payload.get("practice_id") or ""
        ),
        "authoritative_message_id": str(
            payload.get("authoritative_message_id") or ""
        ),
        "authoritative_message_hash": str(
            payload.get(
                "authoritative_message_hash"
            ) or ""
        ),
        "proposal_id": str(
            payload.get("proposal_id") or ""
        ),
        "expected_practice_status": str(
            payload.get("expected_practice_status") or ""
        ),
        "exercise": int(
            payload.get("exercise") or 0
        ),
        "subject": str(
            payload.get("subject") or ""
        ),
        "body": str(
            payload.get("body") or ""
        ),
        "body_sha256": str(
            payload.get("body_sha256") or ""
        ),
        "pdf_path": str(
            payload.get("pdf_path") or ""
        ),
        "pdf_name": str(
            payload.get("pdf_name") or ""
        ),
        "pdf_sha256": str(
            payload.get("pdf_sha256") or ""
        ),
        "document_type_code": str(
            payload.get("document_type_code") or ""
        ),
        "channel": str(
            payload.get("channel") or ""
        ),
        "no_new_deposit": bool(
            payload.get("no_new_deposit")
        ),
    }

    artifact_sha256 = _canonical_sha(artifact)

    return {
        **artifact,
        "artifact_sha256": artifact_sha256,
        "idempotency_key": (
            "runts:"
            + hashlib.sha256(
                (
                    artifact["practice_id"]
                    + "\x00"
                    + artifact_sha256
                ).encode()
            ).hexdigest()
        ),
    }


class UnifiedRuntsApprovalCoordinator:
    def __init__(
        self,
        store: DomainApprovalStore,
        *,
        policy: DomainApprovalPolicy,
    ) -> None:
        self.store = store
        self.policy = policy

    def request(
        self,
        pending: PendingAction,
        *,
        requested_by: str,
    ) -> dict[str, Any]:
        if not self.policy.enabled:
            return {
                "status": "approval_gate_disabled"
            }

        if (
            not self.policy.allowed_user_ids
            or not self.policy.allowed_chat_ids
        ):
            return {
                "status":
                "approval_allowlist_unconfigured"
            }

        scope = build_runts_reply_approval_scope(
            pending
        )

        created = self.store.create_request(
            action=RUNTS_REPLY_ACTION,
            bando_id="runts.messaggistica",
            version=str(pending.version),
            scope=scope,
            requested_by=requested_by,
        )

        request = (
            created.get("request")
            if isinstance(created, Mapping)
            else None
        )

        if not isinstance(request, Mapping):
            return {
                "status": str(
                    created.get("status")
                    or "approval_request_failed"
                )
            }

        return {
            "status": "pending",
            "request_id": str(
                request["request_id"]
            ),
            "created_at": int(
                request["created_at"]
            ),
            "expires_at": int(
                request["expires_at"]
            ),
            "scope_digest_short": str(
                request["scope_digest_short"]
            ),
            "telegram_message": str(
                request.get("telegram_message") or ""
            ),
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
            return {
                "status": "approval_request_missing"
            }

        row = self.store.get_request(
            pending.approval_ref
        )

        if not row:
            return {
                "status": "approval_request_missing"
            }

        scope = build_runts_reply_approval_scope(
            pending
        )

        if str(
            row.get("scope_digest") or ""
        ) != scope_digest(scope):
            self.store.mark_stale(
                pending.approval_ref,
                ["runts_artifact_changed"],
            )
            return {
                "status": "scope_digest_mismatch"
            }

        decision = DomainApprovalDecision(
            request_id=pending.approval_ref,
            decision="approve",
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            chat_type=chat_type,
            idempotency_key=(
                f"unified-runts:"
                f"{pending.pending_id}:"
                f"{pending.version}:"
                f"{telegram_message_id}"
            ),
        )

        return self.store.decide(
            decision,
            scope_digest_short=str(
                row.get("scope_digest_short") or ""
            ),
        )

    def cancel(
        self,
        pending: PendingAction,
    ) -> dict[str, Any]:
        if not pending.approval_ref:
            return {
                "status": "no_approval_request"
            }

        return self.store.cancel(
            pending.approval_ref
        )


class RuntsApprovedReplyExecutor:
    """
    Approval-bound RUNTS executor.

    The writer is deliberately injected.  The default production
    writer will be added separately and must expose only
    preflight(scope) and execute(scope): no generic URL/method API.
    """

    def __init__(
        self,
        *,
        store: DomainApprovalStore,
        reader_factory: Callable[
            [], RuntsReplyReader
        ],
        writer_factory: Callable[
            [], RuntsReplyWriter
        ],
        write_enabled: bool | None = None,
    ) -> None:
        self.store = store
        self.reader_factory = reader_factory
        self.writer_factory = writer_factory
        self.write_enabled = (
            os.getenv(
                "BOTTAZZI_RUNTS_WRITE_ENABLED",
                "0",
            ) == "1"
            if write_enabled is None
            else bool(write_enabled)
        )

    def execute(
        self,
        pending: PendingAction,
    ) -> dict[str, Any]:
        if (
            not payload_matches(pending)
            or not approval_matches(pending)
        ):
            return {
                "status": "approval_required",
                "executed": False,
                "writes": 0,
            }

        if pending.action != RUNTS_REPLY_ACTION:
            return {
                "status": "denied",
                "executed": False,
                "writes": 0,
            }

        request_id = str(
            pending.approval_ref or ""
        )
        row = self.store.get_request(request_id)

        stored_status = str(
            (row or {}).get("status") or ""
        )

        if stored_status == "consumed":
            return {
                "status": "already_executed",
                "executed": True,
                "writes": 0,
                "retry_allowed": False,
            }

        if stored_status == "executing":
            return {
                "status": "EXECUTION_UNCERTAIN",
                "executed": False,
                "writes": 0,
                "retry_allowed": False,
            }

        if stored_status == "execution_failed":
            return {
                "status":
                "FAILED_AFTER_PARTIAL_WRITE",
                "executed": False,
                "writes": 0,
                "retry_allowed": False,
            }

        if (
            row is None
            or effective_approval_status(row)
            != "approved"
        ):
            return {
                "status": (
                    "approval_expired"
                    if row
                    and int(
                        row.get("expires_at") or 0
                    ) <= int(time.time())
                    else "approval_not_executable"
                ),
                "executed": False,
                "writes": 0,
                "retry_allowed": False,
            }

        scope = build_runts_reply_approval_scope(
            pending
        )

        if str(
            row.get("scope_digest") or ""
        ) != scope_digest(scope):
            self.store.mark_stale(
                request_id,
                ["runts_scope_changed"],
            )
            return {
                "status": "FAILED_BEFORE_WRITE",
                "reason": "scope_digest_mismatch",
                "executed": False,
                "writes": 0,
            }

        pdf_path = Path(scope["pdf_path"])

        if not pdf_path.is_file():
            self.store.mark_stale(
                request_id,
                ["runts_pdf_missing"],
            )
            return {
                "status": "FAILED_BEFORE_WRITE",
                "reason": "pdf_missing",
                "executed": False,
                "writes": 0,
            }

        if _sha256_file(pdf_path) != (
            scope["pdf_sha256"]
        ):
            self.store.mark_stale(
                request_id,
                ["runts_pdf_hash_changed"],
            )
            return {
                "status": "FAILED_BEFORE_WRITE",
                "reason": "pdf_hash_mismatch",
                "executed": False,
                "writes": 0,
            }

        reader = self.reader_factory()

        try:
            practice = reader.get_practice(
                scope["practice_id"]
            )

            if (
                str(practice.native_id)
                != scope["practice_id"]
            ):
                raise ValueError(
                    "practice_identity_mismatch"
                )

            if (
                str(practice.status_raw)
                != scope[
                    "expected_practice_status"
                ]
            ):
                self.store.mark_stale(
                    request_id,
                    ["runts_practice_status_changed"],
                )
                return {
                    "status": "FAILED_BEFORE_WRITE",
                    "reason":
                    "practice_status_changed",
                    "executed": False,
                    "writes": 0,
                }

            messages = reader.list_messages(
                scope["practice_id"],
                limit=100,
            )

            authoritative = [
                item
                for item in messages
                if str(item.native_id)
                == scope[
                    "authoritative_message_id"
                ]
            ]

            if len(authoritative) != 1:
                self.store.mark_stale(
                    request_id,
                    [
                        "runts_authoritative_"
                        "message_missing"
                    ],
                )
                return {
                    "status": "FAILED_BEFORE_WRITE",
                    "reason":
                    "authoritative_message_missing",
                    "executed": False,
                    "writes": 0,
                }

            auth = authoritative[0]

            if str(
                auth.source.content_hash or ""
            ) != scope[
                "authoritative_message_hash"
            ]:
                self.store.mark_stale(
                    request_id,
                    [
                        "runts_authoritative_"
                        "message_changed"
                    ],
                )
                return {
                    "status": "FAILED_BEFORE_WRITE",
                    "reason":
                    "authoritative_message_changed",
                    "executed": False,
                    "writes": 0,
                }

            duplicate = _find_matching_message(
                messages,
                scope,
            )

            if duplicate is not None:
                claim = self.store.claim_execution(
                    request_id,
                    action=RUNTS_REPLY_ACTION,
                )

                if not claim.get("claimed"):
                    return {
                        "status": "already_executed",
                        "executed": True,
                        "writes": 0,
                        "retry_allowed": False,
                    }

                result = {
                    "status": "already_executed",
                    "executed": True,
                    "writes": 0,
                    "reconciled": True,
                    "provider_message_id":
                    str(duplicate.native_id),
                }

                self.store.finish_claimed_execution(
                    request_id,
                    action=RUNTS_REPLY_ACTION,
                    success=True,
                    result=result,
                )

                return result

            if not self.write_enabled:
                return {
                    "status":
                    "runts_write_disabled",
                    "executed": False,
                    "writes": 0,
                    "retry_allowed": True,
                    "provider_call_attempted":
                    False,
                }

            writer = self.writer_factory()

            preflight = dict(
                writer.preflight(scope)
            )

            if not preflight.get("ok"):
                return {
                    "status": "FAILED_BEFORE_WRITE",
                    "reason": str(
                        preflight.get("reason")
                        or "writer_preflight_failed"
                    ),
                    "executed": False,
                    "writes": 0,
                }

            if str(
                preflight.get(
                    "document_type_code"
                ) or ""
            ) != RUNTS_DOCUMENT_TYPE:
                return {
                    "status": "FAILED_BEFORE_WRITE",
                    "reason":
                    "runts_document_type_unresolved",
                    "executed": False,
                    "writes": 0,
                }

            claim = self.store.claim_execution(
                request_id,
                action=RUNTS_REPLY_ACTION,
            )

            if not claim.get("claimed"):
                return {
                    "status": str(
                        claim.get("status")
                        or "execution_claim_failed"
                    ),
                    "executed": False,
                    "writes": 0,
                    "retry_allowed": False,
                }

            try:
                provider_result = dict(
                    writer.execute(scope)
                )
            except Exception as exc:
                uncertain = {
                    "status": "EXECUTION_UNCERTAIN",
                    "executed": False,
                    "writes": int(
                        getattr(
                            exc,
                            "writes",
                            0,
                        )
                        or 0
                    ),
                    "retry_allowed": False,
                    "reason": (
                        str(
                            getattr(
                                exc,
                                "reason",
                                "",
                            )
                        ).strip()
                        or type(exc).__name__
                    )[:240],
                    "error_type":
                        type(exc).__name__,
                    "phase":
                        getattr(
                            exc,
                            "phase",
                            None,
                        ),
                }

                self.store.finish_claimed_execution(
                    request_id,
                    action=RUNTS_REPLY_ACTION,
                    success=False,
                    result=uncertain,
                )

                return uncertain

            # Provider verification must be independent
            # from the submit response.
            verify_reader = self.reader_factory()

            verified_messages = (
                verify_reader.list_messages(
                    scope["practice_id"],
                    limit=100,
                )
            )

            match = _find_matching_message(
                verified_messages,
                scope,
            )

            if match is None:
                uncertain = {
                    "status": "EXECUTION_UNCERTAIN",
                    "executed": False,
                    "writes": int(
                        provider_result.get(
                            "writes"
                        ) or 1
                    ),
                    "retry_allowed": False,
                    "provider_result":
                    provider_result,
                }

                self.store.finish_claimed_execution(
                    request_id,
                    action=RUNTS_REPLY_ACTION,
                    success=False,
                    result=uncertain,
                )

                return uncertain

            result = {
                "status": "EXECUTED_VERIFIED",
                "executed": True,
                "writes": int(
                    provider_result.get(
                        "writes"
                    ) or 1
                ),
                "retry_allowed": False,
                "provider_message_id":
                str(match.native_id),
                "provider_result":
                provider_result,
            }

            self.store.finish_claimed_execution(
                request_id,
                action=RUNTS_REPLY_ACTION,
                success=True,
                result=result,
            )

            return result

        except Exception as exc:
            current = self.store.get_request(
                request_id
            )

            if (
                current
                and current.get("status")
                == "executing"
            ):
                uncertain = {
                    "status": "EXECUTION_UNCERTAIN",
                    "executed": False,
                    "writes": int(
                        getattr(
                            exc,
                            "writes",
                            0,
                        )
                        or 0
                    ),
                    "retry_allowed": False,
                    "reason": (
                        str(
                            getattr(
                                exc,
                                "reason",
                                "",
                            )
                        ).strip()
                        or type(exc).__name__
                    )[:240],
                    "error_type":
                        type(exc).__name__,
                    "phase":
                        getattr(
                            exc,
                            "phase",
                            None,
                        ),
                }

                self.store.finish_claimed_execution(
                    request_id,
                    action=RUNTS_REPLY_ACTION,
                    success=False,
                    result=uncertain,
                )

                return uncertain

            self.store.mark_stale(
                request_id,
                ["runts_preflight_exception"],
            )

            return {
                "status": "FAILED_BEFORE_WRITE",
                "executed": False,
                "writes": 0,
                "reason": type(exc).__name__,
            }


def _normal(value: str) -> str:
    return " ".join(str(value).split())


def _find_matching_message(
    messages,
    scope: Mapping[str, Any],
):
    expected_subject = _normal(
        str(scope["subject"])
    )
    expected_body = _normal(
        str(scope["body"])
    )

    for message in messages:
        if (
            _normal(str(message.subject))
            != expected_subject
            or _normal(str(message.body))
            != expected_body
        ):
            continue

        attachments = tuple(
            getattr(message, "attachments", ())
            or ()
        )

        if any(
            str(
                getattr(
                    attachment,
                    "document_type",
                    "",
                )
                or ""
            )
            == RUNTS_DOCUMENT_TYPE
            for attachment in attachments
        ):
            return message

    return None


__all__ = [
    "RUNTS_DOCUMENT_TYPE",
    "RUNTS_REPLY_ACTION",
    "RuntsApprovedReplyExecutor",
    "RuntsReplyReader",
    "RuntsReplyWriter",
    "UnifiedRuntsApprovalCoordinator",
    "build_runts_pending_payload",
    "build_runts_reply_approval_scope",
]
