from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlparse

from ralfloop_agent.tools.contracts import PolicyDecision


class PolicyLayer:
    def __init__(
        self,
        readable_paths: list[str] | None = None,
        writable_paths: list[str] | None = None,
        network_allowlist: list[str] | None = None,
        destructive_patterns: list[str] | None = None,
    ) -> None:
        self.readable_paths = readable_paths or ["/workspace"]
        self.writable_paths = writable_paths or ["/workspace/tmp", "/workspace/out"]
        self.network_allowlist = network_allowlist or [
            "127.0.0.1:11434",
            "mediasetinfinity.mediaset.it",
            "www.mediasetinfinity.mediaset.it",
            "live03-col.msf.cdn.mediaset.net",
            "live02-col.msf.cdn.mediaset.net",
            "live01-col.msf.cdn.mediaset.net",
        ]
        self.destructive_patterns = destructive_patterns or ["rm -rf", "dd", "mkfs", "shutdown", "reboot"]

    def check_command(self, command: str) -> PolicyDecision:
        lowered = command.lower()
        for pattern in self.destructive_patterns:
            if pattern in lowered:
                return PolicyDecision(allowed=False, reason=f"destructive_command:{pattern}")
        return PolicyDecision(allowed=True, reason="allowed")

    def check_read_path(self, path: str) -> PolicyDecision:
        return self._check_path(path, self.readable_paths, "read")

    def check_write_path(self, path: str) -> PolicyDecision:
        return self._check_path(path, self.writable_paths, "write")

    def _check_path(self, path: str, allowed_roots: list[str], mode: str) -> PolicyDecision:
        p = PurePosixPath(path)
        for root in allowed_roots:
            rp = PurePosixPath(root)
            if p == rp or rp in p.parents:
                return PolicyDecision(allowed=True, reason="allowed")
        return PolicyDecision(allowed=False, reason=f"path_not_allowed:{mode}:{path}")

    def check_url(self, url: str) -> PolicyDecision:
        parsed = urlparse(url)
        host = parsed.netloc
        if host in self.network_allowlist:
            return PolicyDecision(allowed=True, reason="allowed")
        return PolicyDecision(allowed=False, reason=f"host_not_allowed:{host}")
