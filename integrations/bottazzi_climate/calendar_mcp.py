from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.mcp_transport import MCPClientSession, MCPError, MCPProtocolError, UnixMCPTransport

from .planner import CalendarEvent


class CalendarMCPError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalendarReadResult:
    events: tuple[CalendarEvent, ...]
    window_start: datetime
    window_end: datetime
    listing_complete: bool
    listed_count: int


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _content_text(result: Mapping[str, Any]) -> str:
    rows = result.get("content")
    if not isinstance(rows, list):
        raise CalendarMCPError("calendar_mcp_content_missing")
    parts = [
        str(row.get("text") or "")
        for row in rows
        if isinstance(row, Mapping) and row.get("type") == "text"
    ]
    text = "\n".join(part for part in parts if part)
    if not text:
        raise CalendarMCPError("calendar_mcp_text_missing")
    return text


def _listed_event_ids(text: str) -> tuple[str, ...]:
    ids = re.findall(r"(?m)^\[[ xX]\].*?_\(([^)]+)\)_\s*$", text)
    return tuple(dict.fromkeys(item.strip() for item in ids if item.strip()))


def _field(text: str, name: str) -> str:
    match = re.search(
        rf"(?m)^\*\*{re.escape(name)}:\*\*\s*(.+?)\s*$", text
    )
    return match.group(1).strip() if match else ""


def _heading(text: str) -> str:
    match = re.search(r"(?m)^##\s+(.+?)\s*$", text)
    return match.group(1).strip() if match else ""


def _choose_year(month: int, day: int, reference: datetime) -> int:
    candidates: list[tuple[float, int]] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            value = datetime(year, month, day, tzinfo=reference.tzinfo)
        except ValueError:
            continue
        candidates.append((abs((value - reference).total_seconds()), year))
    if not candidates:
        raise CalendarMCPError("calendar_date_invalid")
    return min(candidates)[1]


def _parse_when(value: str, *, timezone: str, reference: datetime) -> tuple[datetime, datetime, bool]:
    tz = ZoneInfo(timezone)
    ref = reference.astimezone(tz)
    timed = re.match(
        r"^(?:[A-Za-z]{3},\s+)?([A-Za-z]{3})\s+(\d{1,2})\s+"
        r"(\d{2}):(\d{2})\s*[–-]\s*(\d{2}):(\d{2})$",
        value.strip(),
    )
    if timed:
        month = _MONTHS.get(timed.group(1).casefold())
        if month is None:
            raise CalendarMCPError("calendar_month_unknown")
        day = int(timed.group(2)); year = _choose_year(month, day, ref)
        start = datetime(year, month, day, int(timed.group(3)), int(timed.group(4)), tzinfo=tz)
        end = datetime(year, month, day, int(timed.group(5)), int(timed.group(6)), tzinfo=tz)
        if end <= start:
            end += timedelta(days=1)
        return start, end, False
    all_day = re.match(
        r"^(?:[A-Za-z]{3},\s+)?([A-Za-z]{3})\s+(\d{1,2})(?:\s+\(all day\))?$",
        value.strip(), flags=re.IGNORECASE,
    )
    if all_day:
        month = _MONTHS.get(all_day.group(1).casefold())
        if month is None:
            raise CalendarMCPError("calendar_month_unknown")
        day = int(all_day.group(2)); year = _choose_year(month, day, ref)
        start = datetime(year, month, day, tzinfo=tz)
        return start, start + timedelta(days=1), True
    raise CalendarMCPError(f"calendar_when_unparsed:{value[:120]}")


def parse_event_detail(
    text: str,
    *,
    event_id: str,
    calendar_id: str,
    timezone: str,
    reference: datetime,
) -> CalendarEvent:
    summary = _heading(text)
    when = _field(text, "When")
    if not summary or not when:
        raise CalendarMCPError("calendar_event_detail_incomplete")
    start, end, all_day = _parse_when(when, timezone=timezone, reference=reference)
    return CalendarEvent(
        event_id=event_id, summary=summary, start=start, end=end,
        location=_field(text, "Where"), calendar_id=calendar_id,
        description=_field(text, "Description"),
        conference_url=_field(text, "Meet"), all_day=all_day,
    )


class GoogleCalendarMCPReader:
    def __init__(self, policy: Mapping[str, Any]) -> None:
        cal = policy["calendar"]
        runtime = policy.get("calendar_runtime") or {}
        self.account = str(cal["account"])
        self.calendar_id = str(cal["calendar_id"])
        self.timezone = str(policy.get("timezone") or "Europe/Rome")
        self.socket_path = str(runtime.get("socket_path") or "/run/ralf-google-workspace-mcp/mcp.sock")
        self.timeout = float(runtime.get("timeout_seconds", 20.0))
        self.lookahead_days = int(runtime.get("lookahead_days", 14))
        self.max_results = int(runtime.get("max_results", 50))

    def _open(self) -> MCPClientSession:
        session = MCPClientSession(
            UnixMCPTransport(self.socket_path), timeout=self.timeout,
            client_name="bottazzi-sede-climate-calendar",
        )
        session.initialize()
        tools = {tool.name: tool for tool in session.list_tools()}
        tool = tools.get("manage_calendar")
        if tool is None:
            session.close()
            raise CalendarMCPError("manage_calendar_not_discovered")
        operations = set(
            ((((tool.input_schema.get("properties") or {}).get("operation") or {}).get("enum")) or [])
        )
        if not {"list", "get"} <= operations:
            session.close()
            raise CalendarMCPError("manage_calendar_read_schema_mismatch")
        return session

    def list_upcoming(self, now: datetime) -> CalendarReadResult:
        tz = ZoneInfo(self.timezone)
        start = now.astimezone(tz)
        end = start + timedelta(days=max(self.lookahead_days, 1))
        limit = max(1, min(self.max_results, 50))
        session = self._open()
        try:
            listed = session.call_tool("manage_calendar", {
                "operation": "list", "email": self.account,
                "calendarId": self.calendar_id,
                "timeMin": start.isoformat(), "timeMax": end.isoformat(),
                "maxResults": limit,
            })
            event_ids = _listed_event_ids(_content_text(listed))
            events: list[CalendarEvent] = []
            for event_id in event_ids:
                detail = session.call_tool("manage_calendar", {
                    "operation": "get", "email": self.account,
                    "calendarId": self.calendar_id, "eventId": event_id,
                })
                events.append(parse_event_detail(
                    _content_text(detail), event_id=event_id,
                    calendar_id=self.calendar_id, timezone=self.timezone,
                    reference=start,
                ))
        except (MCPError, MCPProtocolError, OSError, ValueError) as exc:
            raise CalendarMCPError("calendar_mcp_read_failed") from exc
        finally:
            session.close()
        events.sort(key=lambda item: (item.start, item.end, item.event_id))
        return CalendarReadResult(
            tuple(events), start, end,
            listing_complete=len(event_ids) < limit,
            listed_count=len(event_ids),
        )
