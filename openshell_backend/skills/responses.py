from __future__ import annotations

from typing import Any

from ralfloop_agent.autofix.diagnoser import diagnose_skill_failure, diagnosis_to_dict


def build_skill_insufficient_response(
    *,
    skill_name: str,
    user_goal: str,
    final_answer: str,
    validation: dict[str, Any] | None,
    include_runtime_fields: bool = False,
) -> dict[str, Any]:
    audit_summary = [f"fastpath::skill::{skill_name}", "skill_output_insufficient"]
    diagnosis = diagnose_skill_failure(
        skill_name=skill_name,
        final_answer=final_answer,
        validation=validation,
        audit_summary=audit_summary,
    )

    payload: dict[str, Any] = {
        "ok": False,
        "mode": f"skill::{skill_name}",
        "stop_reason": "skill_output_insufficient",
        "final_answer": final_answer,
        "current_role": f"skill::{skill_name}",
        "role_history": [f"skill::{skill_name}"],
        "audit_summary": audit_summary,
        "autofix_candidate": {
            "user_goal": user_goal or "",
            "stop_reason": "skill_output_insufficient",
            "validation_details": validation or {},
            "diagnosis": diagnosis_to_dict(diagnosis),
            "history": [
                {
                    "tool_name": f"skill::{skill_name}",
                    "tool_input": {"user_goal": user_goal or ""},
                    "role": f"skill::{skill_name}",
                }
            ],
        },
    }

    if include_runtime_fields:
        payload["used_profiles"] = {}
        payload["used_models"] = {}
        payload["used_rag"] = {}
        payload["artifacts"] = []

    return payload
