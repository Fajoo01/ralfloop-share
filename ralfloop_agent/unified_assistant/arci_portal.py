from __future__ import annotations

from datetime import date
import os
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .browser_read_only import (
    BrowserReadOnlyError,
    CdpReadOnlySnapshotClient,
    FixedScriptResult,
)


ARCI_HOST = "portale.arci.it"
ARCI_ORIGIN = "https://portale.arci.it"
ARCI_MEMBERS_PATH = "/admin/office/circolosoci/"
ARCI_OPERATION = "arci.organization_profile.read"


ARCI_ORGANIZATION_PROFILE_FUNCTION = r"""
async function() {
  const expectedOrigin = 'https://portale.arci.it';
  const expectedPath = '/admin/office/circolosoci/';

  if (
    location.origin !== expectedOrigin ||
    location.pathname !== expectedPath
  ) {
    return {status: 'PAGE_UNAVAILABLE'};
  }

  const body = document.body?.innerText || '';
  const authenticated =
    /Benvenuto/i.test(body) &&
    Array.from(document.querySelectorAll('a[href]')).some((link) => {
      try {
        const url = new URL(link.href, location.href);
        return url.origin === expectedOrigin &&
          url.pathname === '/admin/password_change/';
      } catch (_) {
        return false;
      }
    });

  if (!authenticated) {
    return {status: 'AUTH_REQUIRED'};
  }

  const organizationName = Array.from(
    document.querySelectorAll('a[href]')
  ).map((link) => {
    try {
      const url = new URL(link.href, location.href);
      if (url.origin !== expectedOrigin || url.pathname !== '/') return '';
      return (link.innerText || link.textContent || '').trim();
    } catch (_) {
      return '';
    }
  }).find((text) => /\bAPS\b/i.test(text)) || '';

  const countFrom = (root) => {
    const text = root.querySelector('.paginator')?.innerText || '';
    const match = text.match(/\b(\d+)\s+Soci\b/i);
    return match ? Number(match[1]) : null;
  };

  const memberCount = countFrom(document);
  const query = new URL(expectedPath, expectedOrigin);
  query.searchParams.set('organismidirigenti', '1');

  const response = await fetch(query.href, {
    method: 'GET',
    credentials: 'same-origin',
    headers: {'Accept': 'text/html'}
  });

  let responseUrl;
  try {
    responseUrl = new URL(response.url);
  } catch (_) {
    return {status: 'SOURCE_UNAVAILABLE'};
  }

  if (
    !response.ok ||
    responseUrl.origin !== expectedOrigin ||
    responseUrl.pathname !== expectedPath
  ) {
    return {status: 'SOURCE_UNAVAILABLE'};
  }

  const html = await response.text();
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  const rows = Array.from(
    parsed.querySelectorAll('#result_list tbody tr')
  );
  const total = countFrom(parsed);

  if (total === null || total !== rows.length) {
    return {status: 'SOURCE_INCOMPLETE'};
  }

  const now = new Date();
  const asOf = now.toISOString().slice(0, 10);
  const ages = rows.map((row) => {
    const value = (
      row.querySelector('.field-datanascita')?.textContent || ''
    ).trim();
    const match = value.match(/^(\d{2})[-/](\d{2})[-/](\d{4})$/);
    if (!match) return null;
    const day = Number(match[1]);
    const month = Number(match[2]);
    const year = Number(match[3]);
    let age = now.getUTCFullYear() - year;
    const currentMonth = now.getUTCMonth() + 1;
    if (
      currentMonth < month ||
      (currentMonth === month && now.getUTCDate() < day)
    ) age -= 1;
    return age >= 0 && age <= 130 ? age : null;
  }).filter((age) => Number.isInteger(age));

  return {
    status: ages.length === total ? 'FOUND' : 'PARTIAL',
    session_authenticated: true,
    organization_name: organizationName.slice(0, 160),
    member_count: memberCount,
    as_of: asOf,
    governance_total: total,
    governance_dated: ages.length,
    governance_under_15: ages.filter((age) => age < 15).length,
    governance_age_15_30: ages.filter(
      (age) => age >= 15 && age <= 30
    ).length,
    governance_over_30: ages.filter((age) => age > 30).length
  };
}
""".strip()


class FixedFunctionReader(Protocol):
    def call_fixed_function(
        self,
        *,
        expected_host: str,
        expected_path_prefix: str,
        expected_path: str | None,
        function: str,
        arguments: tuple[Any, ...],
        operation: str,
    ) -> FixedScriptResult: ...


class ArciOrganizationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["FOUND", "PARTIAL", "AUTH_REQUIRED", "UNAVAILABLE"]
    session_authenticated: bool
    organization_name: str | None = Field(default=None, max_length=160)
    member_count: int | None = Field(default=None, ge=0, le=1_000_000)
    as_of: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    governance_total: int | None = Field(default=None, ge=0, le=10_000)
    governance_dated: int | None = Field(default=None, ge=0, le=10_000)
    governance_under_15: int | None = Field(default=None, ge=0, le=10_000)
    governance_age_15_30: int | None = Field(default=None, ge=0, le=10_000)
    governance_over_30: int | None = Field(default=None, ge=0, le=10_000)
    provenance: tuple[str, ...] = ()
    read_operations: tuple[str, ...] = ()
    write_operations: int = 0
    side_effects: int = 0
    content_role: Literal["data"] = "data"


class ArciPortalReadOnly:
    def __init__(self, reader: FixedFunctionReader) -> None:
        self.reader = reader

    @classmethod
    def from_environment(cls) -> "ArciPortalReadOnly":
        endpoint = os.getenv(
            "RALFLOOP_ARCI_CDP_ENDPOINT",
            "http://127.0.0.1:9236",
        )
        return cls(CdpReadOnlySnapshotClient(
            endpoint,
            fixed_functions=(ARCI_ORGANIZATION_PROFILE_FUNCTION,),
        ))

    def read_organization_profile(self) -> ArciOrganizationProfile:
        try:
            result = self.reader.call_fixed_function(
                expected_host=ARCI_HOST,
                expected_path_prefix=ARCI_MEMBERS_PATH,
                expected_path=ARCI_MEMBERS_PATH,
                function=ARCI_ORGANIZATION_PROFILE_FUNCTION,
                arguments=(),
                operation=ARCI_OPERATION,
            )
        except BrowserReadOnlyError:
            return _unavailable()

        value = result.value
        if not isinstance(value, Mapping):
            return _unavailable()

        status = str(value.get("status") or "")
        if status == "AUTH_REQUIRED":
            return ArciOrganizationProfile(
                status="AUTH_REQUIRED",
                session_authenticated=False,
                read_operations=(result.operation,),
            )
        if status not in {"FOUND", "PARTIAL"}:
            return _unavailable((result.operation,))

        organization_name = " ".join(
            str(value.get("organization_name") or "").split()
        )
        if (
            not organization_name
            or len(organization_name) > 160
            or "@" in organization_name
        ):
            return _unavailable((result.operation,))

        keys = (
            "member_count",
            "governance_total",
            "governance_dated",
            "governance_under_15",
            "governance_age_15_30",
            "governance_over_30",
        )
        numbers = {key: _bounded_int(value.get(key)) for key in keys}
        if any(number is None for number in numbers.values()):
            return _unavailable((result.operation,))

        total = int(numbers["governance_total"] or 0)
        dated = int(numbers["governance_dated"] or 0)
        buckets = sum(int(numbers[key] or 0) for key in (
            "governance_under_15",
            "governance_age_15_30",
            "governance_over_30",
        ))
        if (
            dated > total
            or buckets != dated
            or total > 10_000
            or dated > 10_000
            or int(numbers["member_count"] or 0) < total
            or (status == "FOUND" and dated != total)
        ):
            return _unavailable((result.operation,))

        as_of = str(value.get("as_of") or "")
        if not _iso_date(as_of):
            return _unavailable((result.operation,))

        return ArciOrganizationProfile(
            status=status,
            session_authenticated=True,
            organization_name=organization_name,
            member_count=numbers["member_count"],
            as_of=as_of,
            governance_total=total,
            governance_dated=dated,
            governance_under_15=numbers["governance_under_15"],
            governance_age_15_30=numbers["governance_age_15_30"],
            governance_over_30=numbers["governance_over_30"],
            provenance=(
                "arci_portal:organization",
                f"arci_portal:governance_age_buckets:{as_of}",
            ),
            read_operations=(result.operation,),
        )


def _bounded_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= 1_000_000 else None


def _iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return len(value) == 10


def _unavailable(
    operations: tuple[str, ...] = (),
) -> ArciOrganizationProfile:
    return ArciOrganizationProfile(
        status="UNAVAILABLE",
        session_authenticated=False,
        read_operations=operations,
    )


__all__ = [
    "ARCI_HOST",
    "ARCI_MEMBERS_PATH",
    "ARCI_OPERATION",
    "ARCI_ORGANIZATION_PROFILE_FUNCTION",
    "ARCI_ORIGIN",
    "ArciOrganizationProfile",
    "ArciPortalReadOnly",
]
