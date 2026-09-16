from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ralfloop_agent.domains.bandi_weekly_research import WeeklyConfig, run_weekly


TOOLS = frozenset({
    "bandi_research_now",
    "bandi_latest",
    "bandi_search_latest",
    "bandi_get_opportunity",
})


class BandiResearchMCPServer:
    """Read-only funding discovery MCP backed by the existing Bandi weekly pipeline."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(
            state_dir or Path.home() / ".local/state/ralfloop/bandi_weekly_deep_research"
        ).expanduser()

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            _tool(
                "bandi_research_now",
                "Run bounded fresh grant discovery for Tiremm Innanz APS using official-source-first Bandi research.",
                {"focus": _text(500), "limit": _limit(1, 20)},
                [],
            ),
            _tool(
                "bandi_latest",
                "Read the latest persisted Bandi discovery report without network access.",
                {"limit": _limit(1, 50)},
                [],
            ),
            _tool(
                "bandi_search_latest",
                "Search the latest persisted Bandi report by topic, issuer, territory or project match.",
                {"query": _text(500), "limit": _limit(1, 20)},
                ["query"],
            ),
            _tool(
                "bandi_get_opportunity",
                "Get one opportunity from the latest persisted Bandi report by call key.",
                {"call_key": {"type": "string", "minLength": 8, "maxLength": 128, "pattern": r"^[a-f0-9-]+$"}},
                ["call_key"],
            ),
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        try:
            if name == "bandi_research_now":
                focus = str(arguments.get("focus") or "").strip()
                limit = int(arguments.get("limit", 8))
                config = WeeklyConfig.from_env()
                config.state_dir = self.state_dir
                extra = ()
                if focus:
                    extra = (("targeted", _targeted_query(focus)),)
                result = run_weekly(config, extra_queries=extra)
                payload = _run_payload(result, limit=limit)
            elif name == "bandi_latest":
                payload = _latest_payload(self.state_dir, limit=int(arguments.get("limit", 12)))
            elif name == "bandi_search_latest":
                latest = _latest_report(self.state_dir)
                query = str(arguments["query"]).strip()
                limit = int(arguments.get("limit", 10))
                items = _search_opportunities(latest.get("opportunities") or [], query, limit)
                payload = {
                    "status": "completed",
                    "query": query,
                    "items": [_compact(item) for item in items],
                    "report_json": str(_latest_report_path(self.state_dir) or ""),
                    "freshness": _freshness(latest),
                }
            else:
                latest = _latest_report(self.state_dir)
                call_key = str(arguments["call_key"])
                row = next((item for item in latest.get("opportunities") or [] if str(item.get("call_key")) == call_key), None)
                if row is None:
                    return _error("NOT_FOUND")
                payload = {
                    "status": "completed",
                    "opportunity": row,
                    "report_json": str(_latest_report_path(self.state_dir) or ""),
                    "freshness": _freshness(latest),
                }
            return _result(payload)
        except RuntimeError as exc:
            code = "BUSY" if "already_running" in str(exc) else "SOURCE_UNAVAILABLE"
            return _error(code)
        except (OSError, ValueError, json.JSONDecodeError):
            return _error("MALFORMED_RESPONSE")
        except Exception:
            return _error("SOURCE_UNAVAILABLE")


def _targeted_query(focus: str) -> str:
    clean = re.sub(r"\s+", " ", focus).strip()[:360]
    return f"{clean} bando contributo APS ETS Milano Lombardia 2026 fonte ufficiale"


def _latest_report_path(state_dir: Path) -> Path | None:
    status_path = state_dir / "status.json"
    if not status_path.is_file():
        return None
    status = json.loads(status_path.read_text(encoding="utf-8"))
    raw = str(status.get("last_report_json") or "")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_file() else None


def _latest_report(state_dir: Path) -> dict[str, Any]:
    path = _latest_report_path(state_dir)
    if path is None:
        return {"status": "no_report", "opportunities": []}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("bandi_report_invalid")
    return payload


def _latest_payload(state_dir: Path, *, limit: int) -> dict[str, Any]:
    report = _latest_report(state_dir)
    rows = report.get("opportunities") or []
    return {
        "status": report.get("status") or "no_report",
        "run_id": report.get("run_id"),
        "items": [_compact(item) for item in rows[:limit]],
        "metrics": report.get("metrics") or {},
        "errors": (report.get("errors") or [])[:10],
        "report_json": str(_latest_report_path(state_dir) or ""),
        "freshness": _freshness(report),
    }


def _run_payload(result: Any, *, limit: int) -> dict[str, Any]:
    return {
        "status": result.status,
        "run_id": result.run_id,
        "partial": result.partial,
        "items": [_compact(item) for item in result.opportunities[:limit]],
        "metrics": dict(result.metrics),
        "errors": list(result.errors[:10]),
        "report_json": result.report_json,
        "report_markdown": result.report_markdown,
        "freshness": {"observed_at": result.finished_at, "fresh": True},
    }


def _compact(item: Mapping[str, Any]) -> dict[str, Any]:
    retrieval = item.get("retrieval") if isinstance(item.get("retrieval"), Mapping) else {}
    return {
        "call_key": item.get("call_key"),
        "title": item.get("title"),
        "issuer": item.get("funding_body"),
        "primary_url": item.get("primary_url"),
        "status": item.get("status"),
        "deadline": item.get("deadline"),
        "days_to_deadline": item.get("days_to_deadline"),
        "score": item.get("score"),
        "priority": item.get("priority"),
        "territory": item.get("territory"),
        "themes": item.get("themes") or [],
        "tiremm_compatibility": item.get("tiremm_compatibility"),
        "candidate_project": item.get("candidate_project"),
        "why_tiremm_should_apply": item.get("why_tiremm_should_apply"),
        "criticalities": item.get("criticalities") or [],
        "next_three_actions": item.get("next_three_actions") or [],
        "amounts": item.get("amounts") or [],
        "eligibility_evidence": item.get("beneficiaries_evidence") or [],
        "document_completeness": item.get("document_completeness") or {},
        "citations": item.get("critical_claim_citations") or {},
        "retrieval_mode": retrieval.get("mode"),
        "lifecycle": item.get("lifecycle"),
        "changes": item.get("changes") or [],
    }


def _search_opportunities(rows: list[dict[str, Any]], query: str, limit: int) -> list[dict[str, Any]]:
    terms = [token for token in re.findall(r"[a-z0-9à-ÿ]+", query.casefold()) if len(token) > 2]
    generic = {"bandi", "bando", "tiremm", "innanz", "aps", "trova", "cerca", "valuta", "finanziamenti", "contributi"}
    terms = [token for token in terms if token not in generic]
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for row in rows:
        text = json.dumps({
            "title": row.get("title"), "issuer": row.get("funding_body"),
            "territory": row.get("territory"), "themes": row.get("themes"),
            "candidate_project": row.get("candidate_project"),
            "why": row.get("why_tiremm_should_apply"),
        }, ensure_ascii=False).casefold()
        lexical = sum(1 for term in terms if term in text)
        base = int(row.get("score") or 0)
        if not terms or lexical:
            scored.append((lexical, base, row))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:limit]]


def _freshness(report: Mapping[str, Any]) -> dict[str, Any]:
    raw = str(report.get("finished_at") or "")
    if not raw:
        return {"fresh": False, "observed_at": None, "age_hours": None}
    try:
        observed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds() / 3600
        return {"fresh": age <= 24, "observed_at": raw, "age_hours": round(max(0.0, age), 2)}
    except ValueError:
        return {"fresh": False, "observed_at": raw, "age_hours": None}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": required, "additionalProperties": False,
    }}


def _text(maximum: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _limit(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _result(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = {"ok": True, **dict(payload), "writes": 0, "sends": 0}
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "structuredContent": data, "isError": False}


def _error(code: str) -> dict[str, Any]:
    data = {"ok": False, "status": code, "writes": 0, "sends": 0}
    return {"content": [{"type": "text", "text": code}], "structuredContent": data, "isError": True}


__all__ = ["BandiResearchMCPServer", "TOOLS"]
