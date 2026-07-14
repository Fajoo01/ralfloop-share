from __future__ import annotations

import json
import time

import requests

from .remote_mini_protocol import DraftRequest, DraftResponse
from .security import validate_remote_endpoint
from .speculative_metrics import require_compatible_tokenizer


class RemoteDraftError(RuntimeError):
    pass


class RemoteDraftClient:
    """Token protocol only. Target-side verification remains outside this client."""

    def __init__(
        self,
        *,
        base_url: str,
        allowed_hosts: tuple[str, ...],
        timeout_ms: int = 1_000,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = validate_remote_endpoint(base_url, allowed_hosts)
        self.timeout_sec = timeout_ms / 1000.0
        self.session = session or requests.Session()

    def draft(self, request: DraftRequest) -> tuple[DraftResponse, int, float]:
        started = time.monotonic()
        response = None
        try:
            response = self.session.post(
                f"{self.base_url}/v1/draft",
                json=request.model_dump(mode="json"),
                headers={"Accept": "application/json"},
                timeout=(min(1.0, self.timeout_sec), self.timeout_sec),
            )
            if response.status_code >= 400:
                raise RemoteDraftError(f"remote_draft_http_{response.status_code}")
            raw = response.content
            if len(raw) > 16_384:
                raise RemoteDraftError("remote_draft_output_too_large")
            try:
                result = DraftResponse.model_validate(json.loads(raw))
            except (ValueError, json.JSONDecodeError) as exc:
                raise RemoteDraftError("remote_draft_invalid_response") from exc
            if result.request_id != request.request_id:
                raise RemoteDraftError("remote_draft_request_id_mismatch")
            require_compatible_tokenizer(
                target_tokenizer_hash=request.tokenizer_hash,
                draft_tokenizer_hash=result.tokenizer_hash,
                target_vocabulary_hash=request.vocabulary_hash,
                draft_vocabulary_hash=result.vocabulary_hash,
            )
            if len(result.draft_token_ids) > request.max_draft_tokens:
                raise RemoteDraftError("remote_draft_too_many_tokens")
            return result, len(raw), (time.monotonic() - started) * 1000
        except requests.RequestException as exc:
            raise RemoteDraftError("remote_draft_unavailable") from exc
        finally:
            if response is not None:
                response.close()
