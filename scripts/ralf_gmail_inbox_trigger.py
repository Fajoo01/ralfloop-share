from __future__ import annotations

import json
import os
from pathlib import Path

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.unified_assistant.email_inbox_trigger import GmailInboxTrigger
from ralfloop_agent.unified_assistant.email_send import GoogleWorkspaceEmailContext
from ralfloop_agent.unified_assistant.event_router import EventRouter
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.runtime import run_unified_telegram


def main() -> int:
    policy = DomainApprovalPolicy.from_env()
    if not policy.enabled or len(policy.allowed_user_ids) != 1 or len(policy.allowed_chat_ids) != 1:
        raise SystemExit("gmail trigger requires exactly one approved Telegram user/chat")
    account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
    socket = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
    timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))
    memory_path = Path(os.getenv(
        "RALFLOOP_OPERATIONAL_MEMORY_PATH",
        str(Path.home() / ".local/share/bottazzi/runtime-production/operational-memory.sqlite3"),
    ))
    session_root = Path(os.getenv(
        "RALFLOOP_UNIFIED_SESSION_DIR",
        str(Path.home() / ".local/state/ralf/unified-sessions"),
    ))
    state_path = Path(os.getenv(
        "RALFLOOP_GMAIL_TRIGGER_STATE",
        str(Path.home() / ".local/state/ralf/gmail-inbox-trigger.json"),
    ))
    factory = lambda: GoogleWorkspaceEmailContext(socket, account, timeout)
    with MemoryService(memory_path) as memory:
        trigger = GmailInboxTrigger(
            gateway_factory=factory, memory=memory, router=EventRouter(memory),
            response_runner=run_unified_telegram, account=account,
            telegram_user_id=next(iter(policy.allowed_user_ids)),
            telegram_chat_id=next(iter(policy.allowed_chat_ids)),
            session_root=session_root, state_path=state_path,
        )
        print(json.dumps(trigger.poll().__dict__, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
