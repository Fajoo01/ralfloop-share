from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Mapping, Protocol


class PecReadGateway(Protocol):
    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]: ...


_TARI_ASSESSMENT_RE = re.compile(r"^ACCERTAMENTI.*_9R[0-9]+\.pdf$", re.I)
_TARI_NOTIFICATION_RE = re.compile(r"^PIPLCMILIMG_.*_AR_[0-9]+\.pdf$", re.I)
_DIFENSORE_SENDER = "difensore.regionale@pec.consiglio.regione.lombardia.it"
_DIFENSORE_FORM_MARKER = "modulo richiesta intervento"
_FORM_NAME_RE = re.compile(r"(?:modulo.*difensore|richiesta.*intervento)", re.I)
_IDENTITY_NAME_RE = re.compile(r"(?:identit|carta.*ident|passaport|patente)", re.I)


def _source_message(source_artifact: Mapping[str, Any]) -> Mapping[str, Any] | None:
    payload = source_artifact.get("payload")
    if not isinstance(payload, Mapping):
        return None
    return next(
        (item for item in payload.get("messages") or () if isinstance(item, Mapping)),
        None,
    )


def required_document_gate(
    source_artifact: Mapping[str, Any],
    explicit_attachment_paths: tuple[str, ...],
) -> dict[str, Any]:
    """Return mandatory documents inferred from the official source message.

    The rule is source-driven: it only activates when the official Difensore sender
    supplied its intervention form. A copied blank template never satisfies the form
    requirement because its content hash is compared with the source attachment.
    """
    message = _source_message(source_artifact)
    if message is None:
        return {"required": [], "missing": [], "gate": "none"}
    sender = str(message.get("sender") or "").casefold()
    attachments = [item for item in message.get("attachments") or () if isinstance(item, Mapping)]
    form_template = next(
        (
            item for item in attachments
            if _DIFENSORE_FORM_MARKER in str(item.get("filename") or "").casefold()
        ),
        None,
    )
    if _DIFENSORE_SENDER not in sender or form_template is None:
        return {"required": [], "missing": [], "gate": "none"}

    required = ["completed_difensore_form", "identity_document_or_digitally_signed_form"]
    missing = list(required)
    template_hash = str(form_template.get("content_hash") or "")
    form_satisfied = False
    identity_satisfied = False
    for raw in explicit_attachment_paths:
        path = Path(raw)
        name = path.name
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if _FORM_NAME_RE.search(name) and digest != template_hash:
            form_satisfied = True
            if path.suffix.casefold() == ".p7m":
                identity_satisfied = True
        if _IDENTITY_NAME_RE.search(name):
            identity_satisfied = True
    if form_satisfied:
        missing.remove("completed_difensore_form")
    if identity_satisfied:
        missing.remove("identity_document_or_digitally_signed_form")
    return {"required": required, "missing": missing, "gate": "difensore_official_form"}


def stage_tari_supporting_documents(
    gateway: PecReadGateway,
    objective: str,
    *,
    outbox_root: str | Path | None = None,
) -> dict[str, Any]:
    """Find, verify and idempotently stage a coherent TARI evidence packet.

    No mailbox mutation is performed. Candidate messages are ranked by verified
    attachment inventory; a packet is accepted only when it contains at least five
    assessment PDFs and five notification/proof PDFs.
    """
    if "tari" not in objective.casefold():
        return {"status": "not_applicable", "paths": [], "attachments": [], "local_staging_writes": 0}
    search = gateway.call("pec_search_messages", {"query": "tari", "limit": 100})
    rows = [item for item in search.get("messages") or () if isinstance(item, Mapping)]
    ranked: list[tuple[int, int, Mapping[str, Any], list[Mapping[str, Any]]]] = []
    for row in rows:
        pdfs = [item for item in row.get("attachments") or () if isinstance(item, Mapping)]
        assessments = [item for item in pdfs if _TARI_ASSESSMENT_RE.match(str(item.get("filename") or ""))]
        notifications = [item for item in pdfs if _TARI_NOTIFICATION_RE.match(str(item.get("filename") or ""))]
        if len(assessments) >= 5 and len(notifications) >= 5:
            ranked.append((len(assessments), len(notifications), row, assessments + notifications))
    if not ranked:
        return {"status": "supporting_documents_not_found", "paths": [], "attachments": [], "local_staging_writes": 0}
    ranked.sort(key=lambda item: (item[0], item[1], str(item[2].get("received_at") or "")), reverse=True)
    _, _, message, selected = ranked[0]
    message_id = str(message.get("native_id") or "")
    if not message_id:
        return {"status": "supporting_documents_not_found", "paths": [], "attachments": [], "local_staging_writes": 0}

    root = Path(outbox_root or os.getenv("BOTTAZZI_PEC_OUTBOX_ROOT", "/var/lib/ralfloop/pec-outbox")).resolve()
    packet_digest = hashlib.sha256(
        (message_id + "\0" + "\0".join(sorted(str(item.get("content_hash") or "") for item in selected))).encode("utf-8")
    ).hexdigest()[:16]
    packet_dir = root / "tari-support" / packet_digest
    packet_dir.mkdir(parents=True, exist_ok=True)
    packet_dir.chmod(0o750)
    paths: list[str] = []
    staged: list[dict[str, Any]] = []
    local_writes = 0
    for meta in selected:
        attachment_id = str(meta.get("attachment_id") or "")
        filename = str(meta.get("filename") or "")
        safe_name = Path(filename).name
        if not attachment_id or not safe_name or safe_name != filename or safe_name in {".", ".."}:
            raise ValueError("pec_support_attachment_name_invalid")
        payload = gateway.call(
            "pec_get_attachment",
            {"message_id": message_id, "attachment_id": attachment_id},
        ).get("attachment")
        if not isinstance(payload, Mapping):
            raise ValueError("pec_support_attachment_missing")
        data = base64.b64decode(str(payload.get("data_base64") or ""), validate=True)
        expected_size = int(meta.get("size") or 0)
        expected_hash = str(meta.get("content_hash") or "")
        digest = hashlib.sha256(data).hexdigest()
        if expected_size and len(data) != expected_size:
            raise ValueError("pec_support_attachment_size_mismatch")
        if expected_hash and digest != expected_hash:
            raise ValueError("pec_support_attachment_hash_mismatch")
        destination = packet_dir / safe_name
        if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() == digest:
            pass
        else:
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_bytes(data)
            temporary.chmod(0o640)
            os.replace(temporary, destination)
            local_writes += 1
        paths.append(str(destination))
        staged.append({
            "filename": safe_name,
            "path": str(destination),
            "sha256": digest,
            "bytes": len(data),
            "source_message_id": message_id,
            "source_attachment_id": attachment_id,
        })
    return {
        "status": "staged",
        "paths": paths,
        "attachments": staged,
        "source_message_id": message_id,
        "packet_digest": packet_digest,
        "local_staging_writes": local_writes,
        "writes": 0,
        "sends": 0,
    }


__all__ = ["required_document_gate", "stage_tari_supporting_documents"]
