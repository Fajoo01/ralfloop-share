from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport
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


def _text_payload(result: Mapping[str, Any]) -> str:
    content = result.get("content") or ()
    return "\n".join(
        str(item.get("text") or "")
        for item in content
        if isinstance(item, Mapping) and item.get("type") == "text"
    ).strip()


def _infer_year(month: int, day: int, reference: datetime, tz: ZoneInfo) -> int:
    candidate = datetime(reference.year, month, day, 12, 0, tzinfo=tz)
    if candidate < reference - timedelta(days=180):
        return reference.year + 1
    if candidate > reference + timedelta(days=180):
        return reference.year - 1
    return reference.year


def _parse_timed_when(value: str, reference: datetime, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    match = re.fullmatch(
        r"(?P<dow>[A-Za-z]{3}), (?P<month>[A-Za-z]{3}) (?P<day>\d{1,2}) "
        r"(?P<start>\d{1,2}:\d{2})[–-](?P<end>\d{1,2}:\d{2})",
        value.strip(),
    )
    if not match:
        return None
    month = datetime.strptime(match.group("month"), "%b").month
    day = int(match.group("day"))
    year = _infer_year(month, day, reference, tz)
    start_time = datetime.strptime(match.group("start"), "%H:%M").time()
    end_time = datetime.strptime(match.group("end"), "%H:%M").time()
    start = datetime.combine(datetime(year, month, day).date(), start_time, tzinfo=tz)
    end = datetime.combine(datetime(year, month, day).date(), end_time, tzinfo=tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _line_value(text: str, label: str) -> str:
    prefix = f"**{label}:**"
    line = next((row for row in text.splitlines() if row.startswith(prefix)), "")
    return line[len(prefix):].strip() if line else ""


def parse_event_detail(
    text: str,
    *,
    event_id: str,
    calendar_id: str,
    timezone: str,
    reference: datetime,
) -> CalendarEvent:
    tz = ZoneInfo(timezone)
    ref = reference.astimezone(tz) if reference.tzinfo else reference.replace(tzinfo=tz)
    summary_line = next((row for row in text.splitlines() if row.startswith("## ")), "")
    summary = summary_line[3:].strip()
    when = _line_value(text, "When")
    if not summary or not when:
        raise CalendarMCPError("calendar_event_detail_incomplete")
    timed = _parse_timed_when(when, ref, tz)
    all_day = timed is None
    if timed is None:
        match = re.fullmatch(r"[A-Za-z]{3}, ([A-Za-z]{3}) (\d{1,2})", when.strip())
        if not match:
            raise CalendarMCPError("calendar_event_time_unparseable")
        month = datetime.strptime(match.group(1), "%b").month
        day = int(match.group(2))
        year = _infer_year(month, day, ref, tz)
        start = datetime(year, month, day, tzinfo=tz)
        end = start + timedelta(days=1)
    else:
        start, end = timed
    return CalendarEvent(
        event_id=event_id,
        summary=summary,
        start=start,
        end=end,
        location=_line_value(text, "Where"),
        calendar_id=calendar_id,
        description=_line_value(text, "Description"),
        conference_url=_line_value(text, "Meet"),
        all_day=all_day,
    )


class GoogleCalendarMCPReader:
    def __init__(self, policy: Mapping[str, Any]) -> None:
        self.policy = policy
        self.calendar_cfg = policy["calendar"]
        self.runtime_cfg = policy.get("calendar_runtime") or {}
        self.account = str(self.calendar_cfg["account"])
        self.calendar_id = str(self.calendar_cfg["calendar_id"])
        self.timezone = str(policy.get("timezone") or "Europe/Rome")

    def _session(self) -> MCPClientSession:
        return MCPClientSession(
            UnixMCPTransport(str(
                self.runtime_cfg.get("socket_path")
                or "/run/ralf-google-workspace-mcp/mcp.sock"
            )),
            timeout=float(self.runtime_cfg.get("timeout_seconds", 20.0)),
            client_name="bottazzi-sede-climate-calendar",
        )

    @staticmethod
    def _validate_tool(session: MCPClientSession) -> None:
        tool = next((item for item in session.list_tools() if item.name == "manage_calendar"), None)
        if tool is None:
            raise CalendarMCPError("manage_calendar_not_discovered")
        props = tool.input_schema.get("properties") or {}
        operations = set((props.get("operation") or {}).get("enum") or ())
        if not {"list", "get"} <= operations:
            raise CalendarMCPError("calendar_read_operations_missing")

    def _call(self, session: MCPClientSession, operation: str, **arguments: Any) -> str:
        if operation not in {"list", "get"}:
            raise CalendarMCPError("calendar_write_operation_forbidden")
        result = session.call_tool(
            "manage_calendar",
            {"operation": operation, "email": self.account, **arguments},
        )
        text = _text_payload(result)
        if not text:
            raise CalendarMCPError("calendar_empty_response")
        return text

    def list_upcoming(self, now: datetime) -> CalendarReadResult:
        tz = ZoneInfo(self.timezone)
        observed = now.astimezone(tz) if now.tzinfo else now.replace(tzinfo=tz)
        lookahead_days = max(1, int(self.runtime_cfg.get("lookahead_days", 14)))
        max_results = min(max(1, int(self.runtime_cfg.get("max_results", 50))), 50)
        window_end = observed + timedelta(days=lookahead_days)
        session = self._session()
        listing_complete = True
        events: list[CalendarEvent] = []
        try:
            session.initialize()
            self._validate_tool(session)
            listing = self._call(
                session,
                "list",
                calendarId=self.calendar_id,
                timeMin=observed.isoformat(),
                timeMax=window_end.isoformat(),
                maxResults=max_results,
            )
            event_ids = list(dict.fromkeys(re.findall(r"_\(([^()]+)\)_", listing)))
            if len(event_ids) >= max_results:
                listing_complete = False
            for event_id in event_ids:
                try:
                    detail = self._call(
                        session, "get", calendarId=self.calendar_id, eventId=event_id
                    )
                    event = parse_event_detail(
                        detail,
                        event_id=event_id,
                        calendar_id=self.calendar_id,
                        timezone=self.timezone,
                        reference=observed,
                    )
                except (CalendarMCPError, MCPProtocolError, ValueError):
                    listing_complete = False
                    continue
                if event.end > observed and event.start <= window_end:
                    events.append(event)
        finally:
            session.close()
        events.sort(key=lambda item: (item.start, item.end, item.event_id))
        return CalendarReadResult(
            events=tuple(events),
            window_start=observed,
            window_end=window_end,
            listing_complete=listing_complete,
            listed_count=len(events),
        )


__all__ = [
    "CalendarMCPError",
    "CalendarReadResult",
    "GoogleCalendarMCPReader",
    "parse_event_detail",
]
