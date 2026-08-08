from __future__ import annotations

from collections.abc import Iterable
import re
import unicodedata

from pydantic import BaseModel, Field

from src.models import Evidence


FAST_CHAT_NO_TOOLS_MESSAGE = (
    "Fast-path chat: nessuno strumento è stato eseguito. "
    "Usa 'ralf agent' per ottenere risultati fondati su comandi reali."
)

EXECUTION_CLAIM_PATTERNS = (
    re.compile(r"\b(?:ho|abbiamo)\s+(?:eseguito|lanciato|controllato|scansionato)\b"),
    re.compile(r"\becco\s+(?:i|gli)\s+risultati\b"),
    re.compile(r"\bla\s+scansione\s+(?:mostra|indica|ha\s+mostrato)\b"),
    re.compile(r"\bnon\s+ho\s+(?:modificato|cancellato|spostato|scritto)\b"),
    re.compile(r"\b(?:i|we)\s+(?:ran|executed|scanned|checked)\b"),
    re.compile(r"\b(?:the\s+)?scan\s+(?:shows|found|indicates)\b"),
)


class ExecutionProvenance(BaseModel):
    tools_executed: bool = False
    execution_evidence: list[Evidence] = Field(default_factory=list)
    command_count: int = Field(default=0, ge=0)
    result_ids: list[str] = Field(default_factory=list)
    provider: str | None = None
    endpoint: str | None = None
    model_id: str | None = None


def empty_execution_provenance(
    *,
    provider: str | None = None,
    endpoint: str | None = None,
    model_id: str | None = None,
) -> ExecutionProvenance:
    return ExecutionProvenance(provider=provider, endpoint=endpoint, model_id=model_id)


def provenance_from_results(
    results: Iterable[tuple[str, Evidence]],
    *,
    provider: str | None = None,
    endpoint: str | None = None,
    model_id: str | None = None,
) -> ExecutionProvenance:
    pairs = list(results)
    return ExecutionProvenance(
        tools_executed=bool(pairs),
        execution_evidence=[evidence for _, evidence in pairs],
        command_count=len(pairs),
        result_ids=[result_id for result_id, _ in pairs],
        provider=provider,
        endpoint=endpoint,
        model_id=model_id,
    )


def _normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def has_execution_claim(text: str) -> bool:
    normalized = _normalized(text)
    return any(pattern.search(normalized) for pattern in EXECUTION_CLAIM_PATTERNS)


def guard_execution_claims(
    text: str,
    provenance: ExecutionProvenance,
) -> tuple[str, bool]:
    if provenance.command_count > 0 and provenance.tools_executed:
        return text, False
    if has_execution_claim(text):
        return FAST_CHAT_NO_TOOLS_MESSAGE, True
    return text, False


def metadata_with_provenance(
    metadata: dict | None,
    provenance: ExecutionProvenance,
    *,
    execution_claim_blocked: bool = False,
) -> dict:
    return {
        **dict(metadata or {}),
        "tools_executed": provenance.tools_executed,
        "execution_evidence": [
            evidence.model_dump(mode="json") for evidence in provenance.execution_evidence
        ],
        "command_count": provenance.command_count,
        "result_ids": list(provenance.result_ids),
        "provider": provenance.provider,
        "endpoint": provenance.endpoint,
        "model_id": provenance.model_id,
        "execution_claim_blocked": execution_claim_blocked,
    }
