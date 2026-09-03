#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ralfloop_agent.unified_assistant.operational_runtime import BottazziOperationalRuntime
from ralfloop_agent.unified_assistant.pec_browser_adapter import PecAuthenticatedBrowserAdapter, PecAuthenticatedCdpTransport
from ralfloop_agent.unified_assistant.pec_runts import RuntsAuthBoundaryProvider


def main() -> None:
    parser = argparse.ArgumentParser(description="Sanitized Bottazzi PEC/RUNTS shadow smoke")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9236")
    parser.add_argument("--memory", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()
    transport = PecAuthenticatedCdpTransport(args.cdp_endpoint)
    try:
        with BottazziOperationalRuntime(args.memory, pec_provider=PecAuthenticatedBrowserAdapter(transport), runts_provider=RuntsAuthBoundaryProvider()) as runtime:
            result = runtime.invoke_pec_runts("scopri messaggi PEC", {"limit": args.limit})
            messages = result.get("structuredContent", {}).get("messages", [])
            print(json.dumps({
                "selectedCapability": result.get("selectedCapability"),
                "isError": result.get("isError"),
                "count": len(messages),
                "writes": result.get("structuredContent", {}).get("writes", 0),
                "allProvenanceHasHash": all(bool(row.get("source", {}).get("content_hash")) for row in messages),
                "persistedEntities": len(runtime.memory.list_entities(domain="pec")),
            }, sort_keys=True))
    finally:
        transport.close()


if __name__ == "__main__":
    main()
