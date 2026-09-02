#!/usr/bin/env python3
import os

from ralfloop_agent.unified_assistant.service_identity_mcp import (
    JellyfinReadOnlyClient, JellyfinUserMCPServer, serve,
)


if __name__ == "__main__":
    client = JellyfinReadOnlyClient.from_config(os.getenv(
        "RALF_JELLYFIN_CONFIG", "/home/sibilla-cumana/jellyfin-novita-agent/config.json",
    ))
    raise SystemExit(serve(JellyfinUserMCPServer(client)))
