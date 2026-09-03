from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

from .arci_datatables import ArciListQuery
from .arci_point_reads import ArciPointReadError
from .browser_read_only import BrowserReadOnlyError, CdpReadOnlySnapshotClient
from .platform import ErrorCode


_ARCI_READ_FUNCTION = r"""
async function(operation, payload) {
  const base = 'https://api.arci-torino.weconstudio.it/api/backoffice';
  const token = localStorage.getItem('auth._token.local');
  if (!token) return {transport_error: 'UNAUTHORIZED'};
  const ids = [payload.user_id, payload.card_id, payload.club_id].filter(Boolean);
  if (ids.some((value) => !/^[A-Za-z0-9_.:-]{1,240}$/.test(String(value)))) {
    return {transport_error: 'MALFORMED_RESPONSE'};
  }
  const points = {
    get_member: payload.user_id ? `/users/${payload.user_id}` : null,
    get_card: payload.card_id ? `/cards/${payload.card_id}` : null,
    get_club: payload.club_id ? `/clubs/${payload.club_id}` : null,
  };
  let path = points[operation];
  let method = 'GET';
  let body;
  if (operation === 'list_members_page') {
    path = '/list_users/datatables'; method = 'POST';
    body = {
      XDEBUG_SESSION_START: 'eclipse', action: 'read', conf: payload.page === 1,
      page: payload.page, limit: payload.page_size, filters: [], sorts: [],
      search: payload.search, club_id: payload.club_id,
      committee_id: payload.committee_id, regional_id: payload.regional_id,
      card_validity: payload.validity,
    };
  } else if (operation === 'list_cards_page') {
    path = '/cards/datatables'; method = 'POST';
    body = {
      XDEBUG_SESSION_START: 'eclipse', action: 'read', conf: payload.page === 1,
      page: payload.page, limit: payload.page_size, filters: [], sorts: [],
      search: payload.search, club_id: payload.club_id,
      committee_id: payload.committee_id, regional_id: payload.regional_id,
      has_consumer_movement: false, need_hydra_sync: false,
    };
  }
  if (!path) return {transport_error: 'FORBIDDEN'};
  try {
    const response = await fetch(base + path, {
      method,
      headers: {Accept: 'application/json', Authorization: token, 'Content-Type': 'application/json'},
      body: body ? JSON.stringify(body) : undefined,
      credentials: 'omit',
    });
    if (!response.ok) {
      const status = ({401: 'UNAUTHORIZED', 403: 'FORBIDDEN', 404: 'NOT_FOUND', 429: 'RATE_LIMITED'})[response.status] || 'SOURCE_UNAVAILABLE';
      return {transport_error: status};
    }
    return {data: await response.json()};
  } catch (_) {
    return {transport_error: 'SOURCE_UNAVAILABLE'};
  }
}
""".strip()


class ArciAuthenticatedCdpTransport:
    """Authenticated fixed-route reads through existing ARCI browser session."""

    def __init__(self, client: CdpReadOnlySnapshotClient) -> None:
        self.client = client

    @classmethod
    def from_environment(cls) -> "ArciAuthenticatedCdpTransport":
        endpoint = os.getenv("RALFLOOP_ARCI_CDP_ENDPOINT", "http://127.0.0.1:9236")
        return cls(CdpReadOnlySnapshotClient(endpoint, fixed_functions=(_ARCI_READ_FUNCTION,)))

    def get_member(self, user_id: str) -> Mapping[str, Any]:
        return self._read("get_member", {"user_id": user_id})

    def get_card(self, card_id: str) -> Mapping[str, Any]:
        return self._read("get_card", {"card_id": card_id})

    def get_club(self, club_id: str) -> Mapping[str, Any]:
        return self._read("get_club", {"club_id": club_id})

    def list_member_cards(self, user_id: str) -> Sequence[Mapping[str, Any]]:
        member = self.get_member(user_id)
        rows = member.get("cards")
        if not isinstance(rows, list):
            raise ArciPointReadError(ErrorCode.INCOMPLETE_SOURCE, "member_cards_relation_missing")
        return rows

    def list_members_page(self, query: ArciListQuery, page: int) -> Mapping[str, Any]:
        return self._read("list_members_page", {**query.model_dump(mode="json"), "page": page})

    def list_cards_page(self, query: ArciListQuery, page: int) -> Mapping[str, Any]:
        return self._read("list_cards_page", {**query.model_dump(mode="json"), "page": page})

    def _read(self, operation: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            result = self.client.call_fixed_function(
                expected_host="webapp.tessera-arci.it",
                expected_path_prefix="/club/",
                function=_ARCI_READ_FUNCTION,
                arguments=(operation, dict(payload)),
                operation=f"arci.{operation}.read",
            ).value
        except BrowserReadOnlyError as exc:
            raise ArciPointReadError(ErrorCode.SOURCE_UNAVAILABLE, "arci_browser_unavailable") from exc
        if not isinstance(result, Mapping):
            raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "arci_transport_envelope_invalid")
        if result.get("transport_error"):
            try:
                code = ErrorCode(str(result["transport_error"]))
            except ValueError:
                code = ErrorCode.SOURCE_UNAVAILABLE
            raise ArciPointReadError(code, "arci_read_failed")
        data = result.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("data"), Mapping):
            data = data["data"]
        if not isinstance(data, Mapping):
            raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "arci_response_not_object")
        return data


__all__ = ["ArciAuthenticatedCdpTransport"]
