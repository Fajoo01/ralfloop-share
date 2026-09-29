#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

HOST = os.getenv("BOTTAZZI_CALENDAR_FEED_HOST", "10.252.14.7")
PORT = int(os.getenv("BOTTAZZI_CALENDAR_FEED_PORT", "19320"))
UID = os.getenv("BOTTAZZI_NEXTCLOUD_CALENDAR_UID", "bottazzi-agenda")
URI = os.getenv("BOTTAZZI_NEXTCLOUD_CALENDAR_URI", "bottazzi-agenda")
COMPOSE = os.getenv("BOTTAZZI_NEXTCLOUD_COMPOSE", "/home/bandi/selfhost/nextcloud/docker-compose.yml")
STATIC_DIR = Path(__file__).resolve().parent
UID_RE = re.compile(r"^[A-Za-z0-9._@+-]{1,240}$")


def _occ(*args: str, input_data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    cmd = ["docker", "compose", "-f", COMPOSE, "exec", "-T", "-u", "www-data", "nextcloud", "php", "occ", *args]
    return subprocess.run(cmd, input=input_data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

def export_calendar() -> bytes:
    completed = _occ("calendar:export", UID, URI, "--format=ical", "--no-interaction", "--no-warnings")
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.decode("utf-8", "replace")[-1000:])
    if b"BEGIN:VCALENDAR" not in completed.stdout:
        raise RuntimeError("Nextcloud export did not return an iCalendar payload")
    return completed.stdout


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("timezone_required")
    return dt


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid_date") from exc

def _ics(event_id: str, title: str, start: datetime | date, end: datetime | date, description: str, all_day: bool) -> bytes:
    if end <= start:
        raise ValueError("end_before_start")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if all_day:
        start_line = f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}"
        end_line = f"DTEND;VALUE=DATE:{end.strftime('%Y%m%d')}"
    else:
        start_line = f"DTSTART:{start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        end_line = f"DTEND:{end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    text = "\r\n".join((
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tiremm Innanz//Bot-tazzi Calendar UI//IT",
        "CALSCALE:GREGORIAN", "BEGIN:VEVENT", f"UID:{event_id}", f"DTSTAMP:{stamp}",
        start_line, end_line, f"SUMMARY:{_escape(title)}", f"DESCRIPTION:{_escape(description)}",
        "END:VEVENT", "END:VCALENDAR", "",
    ))
    return text.encode("utf-8")


def upsert_event(payload: dict) -> dict:
    title = str(payload.get("title") or "").strip()
    if not title or len(title) > 500:
        raise ValueError("invalid_title")
    description = str(payload.get("description") or "")[:10000]
    all_day = bool(payload.get("allDay"))
    start = _parse_date(str(payload.get("start") or "")) if all_day else _parse_iso(str(payload.get("start") or ""))
    end = _parse_date(str(payload.get("end") or "")) if all_day else _parse_iso(str(payload.get("end") or ""))
    event_id = str(payload.get("id") or "").strip() or f"ui-{uuid4().hex}@bottazzi.local"
    if not UID_RE.fullmatch(event_id):
        raise ValueError("invalid_uid")
    completed = _occ(
        "calendar:import", UID, URI, "--format=ical", "--errors=1", "--validation=2",
        "--supersede", "--show-created", "--show-updated", "--no-interaction", "--no-warnings",
        input_data=_ics(event_id, title, start, end, description, all_day),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", "replace")[-1000:]
        raise RuntimeError("calendar_import_failed:" + detail)
    return {"ok": True, "id": event_id, "allDay": all_day}


def delete_event(event_id: str) -> dict:
    event_id = str(event_id or "").strip()
    if not UID_RE.fullmatch(event_id):
        raise ValueError("invalid_uid")
    php = r'''require "/var/www/html/lib/base.php"; $target=getenv("TARGET_UID"); $b=\OC::$server->get(\OCA\DAV\CalDAV\CalDavBackend::class); $found=false; foreach($b->getCalendarsForUser("principals/users/bottazzi-agenda") as $c){ if($c["uri"]!=="bottazzi-agenda") continue; foreach($b->getCalendarObjects($c["id"]) as $x){ $o=$b->getCalendarObject($c["id"],$x["uri"]); $d=$o["calendardata"]; if(is_resource($d)){$d=stream_get_contents($d);} if(preg_match("/^UID:(.+)$/m",$d,$m) && trim($m[1])===$target){ $b->deleteCalendarObject($c["id"],$x["uri"],0,true); $found=true; break 2; } } } echo $found?"DELETED\n":"NOT_FOUND\n";'''
    cmd = ["docker", "compose", "-f", COMPOSE, "exec", "-T", "-e", f"TARGET_UID={event_id}", "-u", "www-data", "nextcloud", "php", "-r", php]
    completed = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    if completed.returncode != 0:
        raise RuntimeError("calendar_delete_failed:" + completed.stderr.decode("utf-8", "replace")[-600:])
    if b"DELETED" not in completed.stdout:
        raise ValueError("event_not_found")
    return {"ok": True, "id": event_id, "deleted": True}


class Handler(BaseHTTPRequestHandler):
    server_version = "BotTazziCalendarFeed/3"

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send(200, "text/plain; charset=utf-8", b"ok\n")
            return
        if path == "/calendar.ics":
            try:
                self._send(200, "text/calendar; charset=utf-8", export_calendar())
            except Exception as exc:
                self._send(503, "text/plain; charset=utf-8", ("calendar export unavailable: " + str(exc) + "\n").encode())
            return
        static = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/bottazzi-calendar.apk": ("bottazzi-calendar.apk", "application/vnd.android.package-archive"),
        }.get(path)
        if static is None:
            self.send_error(404)
            return
        filename, content_type = static
        self._send(200, content_type, (STATIC_DIR / filename).read_bytes())

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/events":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 2 or length > 65536:
                raise ValueError("invalid_body_size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid_json")
            result = upsert_event(payload)
            self._send(200, "application/json; charset=utf-8", json.dumps(result).encode())
        except ValueError as exc:
            self._send(400, "application/json; charset=utf-8", json.dumps({"ok": False, "error": str(exc)}).encode())
        except Exception as exc:
            self._send(503, "application/json; charset=utf-8", json.dumps({"ok": False, "error": str(exc)[-400:]}).encode())

    def do_DELETE(self) -> None:
        if self.path.split("?", 1)[0] != "/api/events":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            result = delete_event(str(payload.get("id") or ""))
            self._send(200, "application/json; charset=utf-8", json.dumps(result).encode())
        except ValueError as exc:
            self._send(400, "application/json; charset=utf-8", json.dumps({"ok": False, "error": str(exc)}).encode())
        except Exception as exc:
            self._send(503, "application/json; charset=utf-8", json.dumps({"ok": False, "error": str(exc)[-400:]}).encode())

    def log_message(self, fmt: str, *args: object) -> None:
        print("calendar-feed", self.address_string(), fmt % args, flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
