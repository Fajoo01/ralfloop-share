#!/usr/bin/env python3
from __future__ import annotations

"""Strict GitHub MCP facade over the already-authenticated gh CLI.

Read operations are available by default. Mutations are disabled unless the
broker process explicitly sets RALF_GITHUB_MCP_WRITE_ENABLED=1; Bot-tazzi's
approval layer remains responsible for enabling a write-capable execution path.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp_transport import MCP_PROTOCOL_VERSION

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
STATE_VALUES = {"open", "closed", "all"}
MAX_BODY = 60_000
MAX_TITLE = 500
READ_TOOLS = {
    "github_repo_get",
    "github_issue_list",
    "github_issue_get",
    "github_pr_list",
    "github_pr_get",
}
WRITE_TOOLS = {"github_issue_create", "github_issue_comment"}


def _schema(properties: Mapping[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "required": required, "additionalProperties": False}


REPO = {"type": "string", "pattern": REPO_RE.pattern, "maxLength": 201}
NUMBER = {"type": "integer", "minimum": 1, "maximum": 2_000_000_000}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
STATE = {"type": "string", "enum": sorted(STATE_VALUES), "default": "open"}
TOOLS = [
    {"name": "github_repo_get", "description": "Read GitHub repository metadata.", "inputSchema": _schema({"repo": REPO}, ["repo"])},
    {"name": "github_issue_list", "description": "List GitHub issues without changing repository state.", "inputSchema": _schema({"repo": REPO, "state": STATE, "limit": LIMIT}, ["repo"])},
    {"name": "github_issue_get", "description": "Read one GitHub issue and its comments URL metadata.", "inputSchema": _schema({"repo": REPO, "number": NUMBER}, ["repo", "number"])},
    {"name": "github_pr_list", "description": "List GitHub pull requests without changing repository state.", "inputSchema": _schema({"repo": REPO, "state": STATE, "limit": LIMIT}, ["repo"])},
    {"name": "github_pr_get", "description": "Read one GitHub pull request.", "inputSchema": _schema({"repo": REPO, "number": NUMBER}, ["repo", "number"])},
    {"name": "github_issue_create", "description": "Create a GitHub issue. Disabled unless the approval-bound write broker enables mutations.", "inputSchema": _schema({"repo": REPO, "title": {"type": "string", "minLength": 1, "maxLength": MAX_TITLE}, "body": {"type": "string", "maxLength": MAX_BODY}}, ["repo", "title"])},
    {"name": "github_issue_comment", "description": "Comment on a GitHub issue. Disabled unless the approval-bound write broker enables mutations.", "inputSchema": _schema({"repo": REPO, "number": NUMBER, "body": {"type": "string", "minLength": 1, "maxLength": MAX_BODY}}, ["repo", "number", "body"])},
]


def _error(code: str, detail: str = "") -> dict[str, Any]:
    payload = {"ok": False, "status": code, "detail": detail[:500], "side_effects": 0, "writes": 0, "sends": 0}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload, "isError": True}


def _success(operation: str, data: Any, *, writes: int = 0) -> dict[str, Any]:
    payload = {"ok": True, "operation": operation, "data": data, "side_effects": writes, "writes": writes, "sends": 0}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload, "isError": False}


def _repo(value: Any) -> str:
    repo = str(value or "").strip()
    if not REPO_RE.fullmatch(repo):
        raise ValueError("invalid_repo")
    allowed = [item.strip() for item in os.getenv("RALF_GITHUB_ALLOWED_REPOS", "Fajoo01/").split(",") if item.strip()]
    if allowed and not any(repo == item or (item.endswith("/") and repo.startswith(item)) for item in allowed):
        raise ValueError("repo_not_allowed")
    return repo


def _number(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("invalid_number")
    number = int(value)
    if not 1 <= number <= 2_000_000_000:
        raise ValueError("invalid_number")
    return number


def _run_gh(args: list[str], *, stdin: str | None = None) -> Any:
    env = dict(os.environ)
    env.setdefault("GH_PAGER", "cat")
    completed = subprocess.run(["gh", *args], input=stdin, text=True, capture_output=True, shell=False, timeout=45, env=env)
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "gh_failed").strip()[:500])
    raw = completed.stdout.strip()
    return json.loads(raw) if raw else {}


def _state(value: Any) -> str:
    state = str(value or "open").strip().lower()
    if state not in STATE_VALUES:
        raise ValueError("invalid_state")
    return state


def _limit(value: Any) -> int:
    limit = int(value or 20)
    if not 1 <= limit <= 100:
        raise ValueError("invalid_limit")
    return limit


class GitHubMCPServer:
    def list_tools(self) -> list[dict[str, Any]]:
        return [dict(item) for item in TOOLS]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        if name not in READ_TOOLS | WRITE_TOOLS:
            return _error("POLICY_DENIED")
        if name in WRITE_TOOLS and os.getenv("RALF_GITHUB_MCP_WRITE_ENABLED", "0") != "1":
            return _error("APPROVAL_REQUIRED", "GitHub mutations are disabled on the read broker")
        try:
            repo = _repo(arguments.get("repo"))
            if name == "github_repo_get":
                data = _run_gh(["api", f"repos/{repo}"])
                return _success(name, data)
            if name == "github_issue_list":
                state, limit = _state(arguments.get("state")), _limit(arguments.get("limit"))
                endpoint = f"repos/{repo}/issues?state={state}&per_page={limit}"
                data = [row for row in _run_gh(["api", endpoint]) if "pull_request" not in row]
                return _success(name, data)
            if name == "github_issue_get":
                data = _run_gh(["api", f"repos/{repo}/issues/{_number(arguments.get('number'))}"])
                return _success(name, data)
            if name == "github_pr_list":
                state, limit = _state(arguments.get("state")), _limit(arguments.get("limit"))
                data = _run_gh(["api", f"repos/{repo}/pulls?state={state}&per_page={limit}"])
                return _success(name, data)
            if name == "github_pr_get":
                data = _run_gh(["api", f"repos/{repo}/pulls/{_number(arguments.get('number'))}"])
                return _success(name, data)
            if name == "github_issue_create":
                title = str(arguments.get("title") or "").strip()
                body = str(arguments.get("body") or "")
                if not title or len(title) > MAX_TITLE or len(body) > MAX_BODY:
                    raise ValueError("invalid_issue")
                data = _run_gh(["api", "--method", "POST", f"repos/{repo}/issues", "-f", f"title={title}", "-f", f"body={body}"])
                return _success(name, data, writes=1)
            body = str(arguments.get("body") or "")
            if not body or len(body) > MAX_BODY:
                raise ValueError("invalid_comment")
            number = _number(arguments.get("number"))
            data = _run_gh(["api", "--method", "POST", f"repos/{repo}/issues/{number}/comments", "-f", f"body={body}"])
            return _success(name, data, writes=1)
        except ValueError as exc:
            return _error("POLICY_DENIED", str(exc))
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return _error("SOURCE_UNAVAILABLE", str(exc))


def _response(request: Mapping[str, Any], server: GitHubMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "ralf-github", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params")
        if not isinstance(params, Mapping):
            result = _error("POLICY_DENIED")
        else:
            result = server.call(str(params.get("name") or ""), params.get("arguments", {}))
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    server = GitHubMCPServer()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("request_not_object")
            response = _response(request, server)
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal_error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
