from __future__ import annotations

import os

from .pec_browser_adapter import (
    PecAuthenticatedBrowserAdapter,
    PecAuthenticatedCdpTransport,
)
from .pec_imap_adapter import (
    PecImapAdapter,
    PecImapConfig,
    PecImapError,
)


class ManagedBrowserPecProvider:
    def __init__(self) -> None:
        self.transport = PecAuthenticatedCdpTransport()
        self.adapter = PecAuthenticatedBrowserAdapter(self.transport)

    def list_messages(self, *, limit: int):
        return self.adapter.list_messages(limit=limit)

    def get_message(self, native_id: str):
        return self.adapter.get_message(native_id)

    def find_by_runts_reference(
        self,
        reference: str,
        *,
        limit: int,
    ):
        return self.adapter.find_by_runts_reference(
            reference,
            limit=limit,
        )

    def close(self) -> None:
        self.transport.close()


class PecFallbackReadProvider:
    """
    IMAP-first PEC provider.

    Discovery/search:
        IMAP -> browser/CDP fallback.

    Exact native IDs:
        imap.* -> IMAP
        everything else -> browser

    No mutation operation is exposed.
    """

    def __init__(
        self,
        primary: PecImapAdapter,
        fallback: ManagedBrowserPecProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    def list_messages(self, *, limit: int):
        try:
            return self.primary.list_messages(limit=limit)
        except PecImapError:
            return self.fallback.list_messages(limit=limit)

    def get_message(self, native_id: str):
        if native_id.startswith("imap."):
            return self.primary.get_message(native_id)

        return self.fallback.get_message(native_id)

    def find_by_runts_reference(
        self,
        reference: str,
        *,
        limit: int,
    ):
        try:
            return self.primary.find_by_runts_reference(
                reference,
                limit=limit,
            )
        except PecImapError:
            return self.fallback.find_by_runts_reference(
                reference,
                limit=limit,
            )

    def download_attachment(
        self,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        if message_id.startswith("imap."):
            return self.primary.download_attachment(
                message_id,
                attachment_id,
            )

        method = getattr(
            self.fallback,
            "download_attachment",
            None,
        )

        if method is None:
            raise PecImapError(
                "pec_browser_attachment_download_unavailable"
            )

        return method(message_id, attachment_id)

    def close(self) -> None:
        self.fallback.close()


def imap_environment_present() -> bool:
    return bool(
        os.getenv("BOTTAZZI_PEC_IMAP_HOST", "").strip()
        and os.getenv(
            "BOTTAZZI_PEC_IMAP_USERNAME", ""
        ).strip()
        and (
            os.getenv(
                "BOTTAZZI_PEC_IMAP_PASSWORD_FILE", ""
            ).strip()
            or os.getenv(
                "BOTTAZZI_PEC_IMAP_PASSWORD", ""
            ).strip()
        )
    )


def build_default_pec_provider():
    """
    BOTTAZZI_PEC_READ_PROVIDER:

      auto    -> IMAP-first when configured, browser fallback
      imap    -> force IMAP, fail closed
      browser -> force authenticated browser/CDP

    Default is auto.
    """

    mode = os.getenv(
        "BOTTAZZI_PEC_READ_PROVIDER",
        "auto",
    ).strip().casefold() or "auto"

    if mode not in {"auto", "imap", "browser"}:
        raise ValueError("pec_read_provider_invalid")

    if mode == "browser":
        return ManagedBrowserPecProvider()

    if mode == "imap":
        return PecImapAdapter(
            PecImapConfig.from_environment()
        )

    # auto
    browser = ManagedBrowserPecProvider()

    if not imap_environment_present():
        return browser

    try:
        imap = PecImapAdapter(
            PecImapConfig.from_environment()
        )
    except (PecImapError, OSError):
        # Configuration itself is unusable: preserve the
        # existing authenticated browser read boundary.
        return browser

    return PecFallbackReadProvider(
        primary=imap,
        fallback=browser,
    )


__all__ = [
    "ManagedBrowserPecProvider",
    "PecFallbackReadProvider",
    "build_default_pec_provider",
    "imap_environment_present",
]
