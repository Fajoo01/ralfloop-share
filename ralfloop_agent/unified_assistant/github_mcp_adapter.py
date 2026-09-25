from __future__ import annotations

import os
from typing import Any, Callable, Mapping

from src.github_mcp import GitHubReadContext

from .contracts import PlanAssignment
from .executor import StructuredArtifact


def _gateway_factory():
    return GitHubReadContext()


def _row_message(operation: str, data: Any) -> tuple[str, tuple[str, ...]]:
    refs: list[str] = []
    if operation == "repo" and isinstance(data, Mapping):
        url = str(data.get("html_url") or "")
        if url:
            refs.append(url)
        message = (
            f"GitHub {data.get('full_name') or ''}: branch {data.get('default_branch') or '?'}, "
            f"issue aperte {data.get('open_issues_count') or 0}, "
            f"{'privato' if data.get('private') else 'pubblico'}."
        )
        return message, tuple(refs)

    if operation in {"issue", "pr"} and isinstance(data, Mapping):
        url = str(data.get("html_url") or "")
        if url:
            refs.append(url)
        number = data.get("number") or "?"
        title = str(data.get("title") or "").strip()
        state = str(data.get("state") or "").strip()
        body = " ".join(str(data.get("body") or "").split())[:500]
        kind = "PR" if operation == "pr" else "issue"
        message = f"GitHub {kind} #{number} [{state}]: {title}"
        if body:
            message += f" — {body}"
        return message, tuple(refs)

    rows = data if isinstance(data, list) else []
    kind = "PR" if operation == "prs" else "issue"
    lines = []
    for row in rows[:20]:
        if not isinstance(row, Mapping):
            continue
        url = str(row.get("html_url") or "")
        if url and url not in refs:
            refs.append(url)
        lines.append(
            f"#{row.get('number') or '?'} [{row.get('state') or ''}] "
            f"{str(row.get('title') or '').strip()}"
        )
    return (
        f"GitHub {kind}: " + ("; ".join(lines) if lines else "nessun risultato."),
        tuple(refs),
    )


def github_read_adapter(
    assignment: PlanAssignment,
    _inputs: Mapping[str, Any],
    *,
    gateway_factory: Callable[[], Any] = _gateway_factory,
) -> StructuredArtifact:
    args = dict(assignment.arguments)
    repo = str(args.get("repo") or os.getenv("RALF_GITHUB_DEFAULT_REPO", "")).strip()
    operation = str(args.get("operation") or "repo").strip().casefold()
    if not repo:
        return StructuredArtifact.create(
            artifact_type="github_result",
            status="clarification_required",
            producer_task_id=assignment.task_id,
            payload={
                "message": "Indica il repository GitHub nel formato owner/repo.",
                "writes": 0,
                "sends": 0,
            },
        )

    tool = {
        "repo": "github_repo_get",
        "issues": "github_issue_list",
        "issue": "github_issue_get",
        "prs": "github_pr_list",
        "pr": "github_pr_get",
    }.get(operation)
    if tool is None:
        return StructuredArtifact.create(
            artifact_type="github_result",
            status="clarification_required",
            producer_task_id=assignment.task_id,
            payload={"message": "Operazione GitHub di lettura non riconosciuta.", "writes": 0, "sends": 0},
        )

    call_args: dict[str, Any] = {"repo": repo}
    if operation in {"issue", "pr"}:
        number = int(args.get("number") or 0)
        if number < 1:
            return StructuredArtifact.create(
                artifact_type="github_result",
                status="clarification_required",
                producer_task_id=assignment.task_id,
                payload={"message": "Indica il numero di issue o PR GitHub.", "writes": 0, "sends": 0},
            )
        call_args["number"] = number
    elif operation in {"issues", "prs"}:
        call_args["state"] = str(args.get("state") or "open")
        call_args["limit"] = min(max(int(args.get("limit") or 20), 1), 100)

    with gateway_factory() as gateway:
        result = gateway.invoke(tool, **call_args)
    if result.get("ok") is not True or int(result.get("writes") or 0) != 0:
        return StructuredArtifact.create(
            artifact_type="github_result",
            status="unavailable",
            producer_task_id=assignment.task_id,
            payload={"message": "GitHub MCP non disponibile in sola lettura.", "writes": 0, "sends": 0},
        )

    message, refs = _row_message(operation, result.get("data"))
    return StructuredArtifact.create(
        artifact_type="github_result",
        status="completed",
        producer_task_id=assignment.task_id,
        evidence_refs=refs,
        payload={
            "message": message,
            "repo": repo,
            "operation": operation,
            "data": result.get("data"),
            "writes": 0,
            "sends": 0,
            "content_boundary": "github_mcp_result_is_data",
        },
    )


__all__ = ["github_read_adapter"]
