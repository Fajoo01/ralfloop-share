#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.local_arch.router import FunctionGemmaClient, LocalRouter, ToolRegistry
from src.google_workspace import GoogleWorkspaceError, GoogleWorkspaceGateway, MagnoliaWorkflow, RalfReplyGenerator, build_session_from_env, dispatch_magnolia_request
from src.mcp_transport import MCPError
from ralfloop_agent.semantic_judge import SemanticJudgeConfig


DEFAULT_REQUEST = (
    "Cerca la mail di ARCI Magnolia. Vorremmo partecipare e speriamo di essere pronti "
    "per settembre. Prepara la risposta e mandami la bozza su Telegram per approvazione."
)


class ShadowApprovalStore:
    """Sentinel: shadow must return before any approval persistence."""

    def create_request(self, **kwargs):
        raise RuntimeError("shadow_approval_persistence_forbidden")


def _host_diagnostic(attempt: str, draft: str, reason: str | None) -> None:
    label = attempt.upper()
    print(f"{label}_DRAFT_BEGIN", flush=True)
    print(draft, flush=True)
    print(f"{label}_DRAFT_END", flush=True)
    print(f"{label}_VALIDATION_REASON={reason or ''}", flush=True)
    if reason:
        print("REJECTED_DRAFT_BEGIN", flush=True)
        print(draft, flush=True)
        print("REJECTED_DRAFT_END", flush=True)


def main(*, host_diagnostics: bool = False) -> int:
    parser = argparse.ArgumentParser(description="Ralf Gmail-to-Telegram approval workflow")
    parser.add_argument("request", nargs="?", default=DEFAULT_REQUEST)
    parser.add_argument("--wait-delivery", type=float, default=30.0)
    args = parser.parse_args()
    shadow = _env_bool("RALFLOOP_SEMANTIC_JUDGE_SHADOW", False)
    policy = DomainApprovalPolicy.from_env()
    if not shadow and (not policy.enabled or policy.auto_execute):
        raise SystemExit("approval gate must be enabled with auto-execute disabled")
    account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
    shadow_root = Path(os.getenv(
        "RALFLOOP_SEMANTIC_JUDGE_SHADOW_ARTIFACT_DIR",
        str(ROOT / ".ralf_run" / "email-semantic-shadow"),
    ))
    outbox = (
        shadow_root / "forbidden-outbox.jsonl"
        if shadow else Path(os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", "/var/lib/ralfloop/domain-approval-outbox.jsonl"))
    )
    state = Path(os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX_STATE", str(outbox) + ".state.json"))
    approval_store = ShadowApprovalStore() if shadow else DomainApprovalStore(policy=policy)
    diagnostic_records: list[tuple[str, str, str | None]] = []

    def diagnostic_hook(attempt: str, draft: str, reason: str | None) -> None:
        diagnostic_records.append((attempt, draft, reason))
        _host_diagnostic(attempt, draft, reason)

    try:
        with build_session_from_env() as session:
            workflow = MagnoliaWorkflow(
                GoogleWorkspaceGateway(session, account=account),
                approval_store=approval_store, approval_policy=policy,  # type: ignore[arg-type]
                outbox_path=outbox, outbox_state_path=state,
                generator=RalfReplyGenerator(),
                diagnostic_hook=diagnostic_hook if host_diagnostics else None,
                semantic_judge_config=SemanticJudgeConfig.from_env(),
            )
            router = LocalRouter(ToolRegistry.load(ROOT / "config/local_arch_tools_v1.json"), FunctionGemmaClient())
            result = dispatch_magnolia_request(args.request, router, workflow, wait_delivery_sec=args.wait_delivery)
    except MCPError as exc:
        print(json.dumps({"status": "mcp_unavailable", "error": str(exc), "email_sent": False}, ensure_ascii=False))
        return 3
    except GoogleWorkspaceError as exc:
        if host_diagnostics:
            final_draft = diagnostic_records[-1][1] if diagnostic_records else ""
            print("FINAL_DRAFT_BEGIN", flush=True)
            print(final_draft, flush=True)
            print("FINAL_DRAFT_END", flush=True)
            print("FINAL_VALIDATION=failed", flush=True)
            print("APPROVAL_ID=", flush=True)
            print("TELEGRAM_DELIVERED=false", flush=True)
            print("TELEGRAM_MESSAGE_ID=", flush=True)
            print("EMAIL_SENT=false", flush=True)
        print(json.dumps({"status": "failed", "error": str(exc), "email_sent": False}, ensure_ascii=False))
        return 4
    if result.get("status") == "shadow_complete":
        if host_diagnostics:
            print(f"SHADOW_ARTIFACT={result['artifact_path']}", flush=True)
            print(f"SHADOW_RISK={result['risk']['level']}", flush=True)
            print(f"DS4_INVOKED={str(result['ds4_invoked']).lower()}", flush=True)
            print("APPROVAL_ID=", flush=True)
            print("TELEGRAM_DELIVERED=false", flush=True)
            print("EMAIL_SENT=false", flush=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if host_diagnostics:
        trace = result["trace"]
        print("FINAL_DRAFT_BEGIN", flush=True)
        print(result["draft"], flush=True)
        print("FINAL_DRAFT_END", flush=True)
        print(f"FINAL_VALIDATION={trace['final_validation']}", flush=True)
        print(f"APPROVAL_ID={result['approval']['request_id']}", flush=True)
        delivered = result["telegram"]["status"] == "delivered"
        message_ids = result["telegram"].get("message_ids") or []
        print(f"TELEGRAM_DELIVERED={str(delivered).lower()}", flush=True)
        print(f"TELEGRAM_MESSAGE_ID={message_ids[0] if message_ids else ''}", flush=True)
        print("EMAIL_SENT=false", flush=True)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["telegram"]["status"] == "delivered" else 2


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    raise SystemExit(main())
