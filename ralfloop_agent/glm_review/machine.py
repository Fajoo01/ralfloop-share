from __future__ import annotations

import argparse
import json
from pathlib import Path

from .adapter import ColibriGlmAdapter
from .models import ContextPacket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="glm-run-machine")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--artifact-dir", required=True)
    parser.add_argument("--launcher", default="/home/sibilla-cumana/Dati/ralfloop-colibri/bin/glm-run")
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--ngen", type=int, default=128)
    args = parser.parse_args(argv)
    packet = ContextPacket.model_validate_json(Path(args.packet).read_text(encoding="utf-8"))
    envelope = ColibriGlmAdapter(
        launcher=args.launcher,
        timeout_seconds=args.timeout,
        ngen=args.ngen,
    ).review(packet, args.artifact_dir)
    print(json.dumps(envelope.model_dump(mode="json"), sort_keys=True))
    return 0 if envelope.ok else 75 if envelope.status == "deferred_resource_busy" else 1


if __name__ == "__main__":
    raise SystemExit(main())
