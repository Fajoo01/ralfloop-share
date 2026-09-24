#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ralfloop_agent.integration.gpt_frontend import create_server


def main() -> int:
    parser = argparse.ArgumentParser(description="Bot-tazzi local GPT work queue frontend")
    parser.add_argument(
        "--origin",
        default=os.getenv("BOTTAZZI_GPT_FRONTEND_ORIGIN", "http://127.0.0.1:19201"),
        help="Local HTTP origin to bind, default http://127.0.0.1:19201",
    )
    args = parser.parse_args()
    server = create_server(origin=args.origin)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
