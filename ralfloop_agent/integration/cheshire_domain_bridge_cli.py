from __future__ import annotations

import argparse
import json
from typing import Any

from .cheshire_domain_bridge import CheshireDomainBridge, CheshireDomainBridgeRequest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("health")
    sim = sub.add_parser("simulate")
    sim.add_argument("--message", required=True)
    sim.add_argument("--domain")
    sim.add_argument("--side-effect", action="store_true")
    args = parser.parse_args(argv)
    bridge = CheshireDomainBridge.from_env()
    if args.command == "status":
        out: dict[str, Any] = bridge.status()
    elif args.command == "health":
        out = bridge.health()
    elif args.command == "simulate":
        req = CheshireDomainBridgeRequest.from_message(
            args.message,
            {"requested_domain": args.domain, "side_effect_intent": args.side_effect},
        )
        result = bridge.execute(req)
        out = {"result": result.to_dict(), "cheshire_response": bridge.to_cheshire_response(result)}
    else:
        raise AssertionError(args.command)
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
