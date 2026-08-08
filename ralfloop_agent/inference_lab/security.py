from __future__ import annotations

from collections.abc import Mapping, Sequence
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse


SECRET_KEY_RE = re.compile(r"secret|token|password|authorization|cookie|hmac|private.?key", re.I)
CREDENTIAL_VALUE_RE = re.compile(
    r"bearer\s+[A-Za-z0-9._~+/=-]+|(?:api[_-]?key|token|password|secret)\s*[:=]\s*\S+|-----BEGIN [A-Z ]+PRIVATE KEY-----",
    re.I,
)
NON_BINDING_FORBIDDEN_RE = re.compile(
    r"auto.?approve|auto.?execute|execute.?approved|promote.?domain|run.?domain.?canary|apply.?domain.?source.?update",
    re.I,
)
EXECUTION_KEYS = {"command", "shell", "execute", "approve", "authorization", "capability"}


class LabSecurityError(ValueError):
    pass


def validate_local_endpoint(url: str) -> str:
    parsed = _parsed_http_url(url)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise LabSecurityError("local_endpoint_not_loopback")
    return url.rstrip("/")


def validate_remote_endpoint(url: str, allowed_hosts: Sequence[str]) -> str:
    parsed = _parsed_http_url(url)
    allowed = {host.strip().lower() for host in allowed_hosts if host.strip()}
    if not allowed or (parsed.hostname or "").lower() not in allowed:
        raise LabSecurityError("remote_host_not_allowlisted")
    if parsed.hostname:
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_private:
            raise LabSecurityError("remote_host_not_private")
    return url.rstrip("/")


def _parsed_http_url(url: str):
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LabSecurityError("invalid_endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise LabSecurityError("endpoint_contains_credentials_or_parameters")
    return parsed


def redact_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if SECRET_KEY_RE.search(str(key)) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str) and CREDENTIAL_VALUE_RE.search(value):
        return "[REDACTED]"
    return value


def reject_secrets(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                raise LabSecurityError("secret_field_forbidden")
            reject_secrets(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            reject_secrets(item)
    elif isinstance(value, str) and CREDENTIAL_VALUE_RE.search(value):
        raise LabSecurityError("secret_value_forbidden")


def validate_non_binding_result(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in EXECUTION_KEYS or NON_BINDING_FORBIDDEN_RE.search(key_text):
                raise LabSecurityError("remote_result_attempts_binding_action")
            validate_non_binding_result(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            validate_non_binding_result(item)
    elif isinstance(value, str) and NON_BINDING_FORBIDDEN_RE.search(value):
        raise LabSecurityError("remote_result_attempts_protected_action")
