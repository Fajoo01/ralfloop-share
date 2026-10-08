#!/usr/bin/env python3
"""Apply the reviewed exact-body fast path to a COPY of the production release.

No live files are edited. Abort if the source code differs from expectations.
"""
import argparse
from pathlib import Path

def replace_one(s: str, old: str, new: str, label: str) -> str:
    found=s.count(old)
    if found != 1:
        raise RuntimeError(f"{label}: expected exactly one anchor, got {found}")
    return s.replace(old,new,1)

def main(dest: Path) -> None:
    base=dest/"ralfloop_agent/unified_assistant"
    core=base/"core.py"
    runtime=base/"runtime.py"
    source_core=core.read_text()
    source_runtime=runtime.read_text()
    x=source_core
    x=replace_one(x,
        "def handle(self, text: str, *, domain_hint: str | None = None) -> UnifiedAssistantResult:",
        "def handle(self, text: str, *, domain_hint: str | None = None,\n               email_exact_body: str | None = None) -> UnifiedAssistantResult:",
        "core.handle signature")
    x=replace_one(x,
        '        if assignment.domain == "email":\n            return self._compose_email(text, plan.model_dump(mode="json"))',
        '        if assignment.domain == "email":\n            return self._compose_email(text, plan.model_dump(mode="json"), exact_body=email_exact_body)',
        "core email route")
    x=replace_one(x,
        "        structured_artifacts: tuple[Mapping[str, Any], ...] = (),\n    ) -> UnifiedAssistantResult:\n        if not all((self.email_memory, self.email_pipeline, self.recipient_resolver)):",
        "        structured_artifacts: tuple[Mapping[str, Any], ...] = (),\n        exact_body: str | None = None,\n    ) -> UnifiedAssistantResult:\n        if not all((self.email_memory, self.email_pipeline, self.recipient_resolver)):",
        "core email compose signature")
    x=replace_one(x,
        "        outcome = self.email_pipeline.compose(working)\n        if outcome.hard_guard != \"passed\" or outcome.final_validator != \"passed\":",
        '        if isinstance(exact_body, str) and exact_body.strip():\n'
        '            validator = getattr(self.email_pipeline, "validate_exact", None)\n'
        '            if validator is None:\n'
        '                return self._result("blocked", "Validatore testo esatto non disponibile.", send_calls=0)\n'
        '            outcome = validator(working, exact_body)\n'
        '        else:\n'
        '            outcome = self.email_pipeline.compose(working)\n'
        '        if outcome.hard_guard != "passed" or outcome.final_validator != "passed":',
        "core deterministic validation")
    y=source_runtime
    y=replace_one(y,
        '    if otp_result is not None:\n'
        '        result = otp_result\n'
        '    elif browser_interaction_provider is not None:\n'
        '        with browser_interaction_provider.request_scope():\n'
        '            result = core.handle(text)\n'
        '    else:\n'
        '        result = core.handle(text)\n'
        '    current_email = conversation.state.pending.email\n'
        '    email_exact_body = context.get("email_exact_body")',
        '    email_exact_body = context.get("email_exact_body")\n'
        '    exact_body = email_exact_body if isinstance(email_exact_body, str) and email_exact_body.strip() else None\n'
        '    if otp_result is not None:\n'
        '        result = otp_result\n'
        '    elif browser_interaction_provider is not None:\n'
        '        with browser_interaction_provider.request_scope():\n'
        '            result = core.handle(text, email_exact_body=exact_body)\n'
        '    else:\n'
        '        result = core.handle(text, email_exact_body=exact_body)\n'
        '    current_email = conversation.state.pending.email',
        "runtime upstream exact text")
    y=replace_one(y,
        '        and bool(current_email.payload.get("gmail_bound_send"))\n'
        '        and not current_email.approval_ref',
        '        and bool(current_email.payload.get("gmail_bound_send"))\n'
        '        and not bool(context.get("email_manual_draft_only"))\n'
        '        and not current_email.approval_ref',
        "manual drafts never request send approval")
    for target,body in ((core,x),(runtime,y)):
        if body == target.read_text(): raise RuntimeError("patch is no-op")
        target.chmod(0o644)
        target.write_text(body)
        target.chmod(0o444)
    print(f"EXACTBODY_PATCH_OK {dest}")

if __name__ == "__main__":
    parser=argparse.ArgumentParser()
    parser.add_argument("destination")
    main(Path(parser.parse_args().destination))
