from __future__ import annotations

import html
import json
import math
import os
import re
import subprocess
import time
import urllib.parse
import contextvars
import concurrent.futures
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

DATA_DIR = Path("/home/sibilla-cumana/ralfloop_data/atm_telegram")
DESTINATIONS_PATH = DATA_DIR / "destinations.json"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OSM_COPYRIGHT = "Data © OpenStreetMap contributors"
CDP_HOST = os.environ.get("RALFLOOP_ATM_CDP_HOST", "127.0.0.1")
CDP_PORT = int(os.environ.get("RALFLOOP_ATM_CDP_PORT", "9237"))
ATM_LIVE_BROWSER_ENABLED = os.environ.get("RALFLOOP_ATM_LIVE_BROWSER", "1").strip().lower() not in {"0", "false", "no"}
ATM_BROWSER_HELPER_URL = os.environ.get("RALFLOOP_ATM_BROWSER_HELPER_URL", "http://127.0.0.1:19137").strip()
ATM_BROWSER_CLOSE_AFTER = os.environ.get("RALFLOOP_ATM_BROWSER_CLOSE_AFTER", "1").strip().lower() not in {"0", "false", "no"}
ATM_JSON_CDP_FALLBACK_ENABLED = os.environ.get("RALFLOOP_ATM_JSON_CDP_FALLBACK", "0").strip().lower() not in {"0", "false", "no"}


# LOCAL_ATM_ROUTER_CONFIG_START
ATM_LOCAL_ROUTER_BIN = Path(
    os.environ.get(
        "RALFLOOP_ATM_ROUTER_BIN",
        str(
            Path(__file__).resolve().parents[1]
            / "tools"
            / "atm_router"
            / "atm-router"
        ),
    )
)

ATM_LOCAL_ROUTER_GRAPH = Path(
    os.environ.get(
        "RALFLOOP_ATM_ROUTER_GRAPH",
        str(DATA_DIR / "atm-router-current.bin"),
    )
)

ATM_LOCAL_ROUTER_RADIUS_M = 700
ATM_LOCAL_ROUTER_STOP_LIMIT = 10

ATM_DIRECT_TOPOLOGY_PATH = Path(
    os.environ.get(
        "RALFLOOP_ATM_DIRECT_TOPOLOGY",
        str(DATA_DIR / "atm-direct-topology.json"),
    )
)
ATM_DIRECT_CANDIDATE_LIMIT = max(
    1,
    int(os.environ.get("RALFLOOP_ATM_DIRECT_CANDIDATE_LIMIT", "3")),
)
ATM_DIRECT_ORIGIN_RADIUS_M = max(
    100,
    int(os.environ.get("RALFLOOP_ATM_DIRECT_ORIGIN_RADIUS_M", "850")),
)
ATM_DIRECT_DEST_RADIUS_M = max(
    100,
    int(os.environ.get("RALFLOOP_ATM_DIRECT_DEST_RADIUS_M", "950")),
)
ATM_DIRECT_ACCESS_SLACK_M = max(
    0,
    int(os.environ.get("RALFLOOP_ATM_DIRECT_ACCESS_SLACK_M", "250")),
)
ATM_DIRECT_MAX_STOPS = max(
    1,
    int(os.environ.get("RALFLOOP_ATM_DIRECT_MAX_STOPS", "8")),
)
# LOCAL_ATM_ROUTER_CONFIG_END

router = APIRouter(prefix="/atm-telegram", tags=["atm-telegram"])

DEFAULT_DESTINATIONS = [
    {"name": "arci bellezza", "label": "ARCI Bellezza", "lat": 45.4487392, "lon": 9.1950134, "note": "Via Giovanni Bellezza 16/A, Milano"},
    {"name": "piscina suzzani", "label": "Piscina Suzzani", "lat": 45.5194893, "lon": 9.2061705, "note": "Via Luigi Beccali, Milano"},
]


class DestinationIn(BaseModel):
    name: str
    label: str | None = None
    aliases: list[str] | str | None = None
    lat: float
    lon: float
    note: str | None = ""


class PlanIn(BaseModel):
    lat: float
    lon: float
    destination: str


class PlanNamedIn(BaseModel):
    origin: str
    destination: str


class TelegramWebhookIn(BaseModel):
    update_id: int | None = None
    message: dict[str, Any] | None = None


class DestinationDeleteIn(BaseModel):
    name: str


class GeocodeIn(BaseModel):
    q: str


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _aliases(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = re.split(r"[,;\n]+", value)
    elif isinstance(value, list):
        raw = value
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        alias = _slug(str(item or ""))
        if alias and alias not in seen:
            out.append(alias)
            seen.add(alias)
    return out


def _http_json(url: str, *, method: str = "GET", data: bytes | None = None, timeout: int = 14) -> Any:
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "ralfloop-atm-telegram/0.1"})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _nominatim_search(query: str) -> list[dict[str, Any]]:
    text = (query or "").strip()
    if not text:
        return []
    if "milano" not in text.lower():
        text = f"{text}, Milano"
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({
        "format": "json",
        "limit": 6,
        "addressdetails": 1,
        "q": text,
    })
    try:
        rows = _http_json(url, timeout=10)
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        try:
            out.append({
                "label": str(row.get("display_name") or ""),
                "lat": float(row.get("lat")),
                "lon": float(row.get("lon")),
                "type": str(row.get("type") or row.get("class") or ""),
            })
        except Exception:
            continue
    return out


def _save_destinations(rows: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DESTINATIONS_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_destinations() -> list[dict[str, Any]]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DESTINATIONS_PATH.exists():
        _save_destinations(DEFAULT_DESTINATIONS)
    try:
        rows = json.loads(DESTINATIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        rows = DEFAULT_DESTINATIONS
    by_name: dict[str, dict[str, Any]] = {}
    source_rows = list(rows or []) if isinstance(rows, list) and rows else list(DEFAULT_DESTINATIONS)
    for row in source_rows:
        name = _slug(str(row.get("name") or row.get("label") or ""))
        if name:
            item = dict(row)
            item["name"] = name
            item["label"] = str(item.get("label") or name.title())
            item["aliases"] = _aliases(item.get("aliases"))
            by_name[name] = item
    return sorted(by_name.values(), key=lambda r: str(r.get("label") or r.get("name")))


def _find_destination(name: str) -> dict[str, Any] | None:
    wanted = _slug(name)
    for row in _load_destinations():
        hay = _slug(str(row.get("name") or "") + " " + str(row.get("label") or ""))
        aliases = set(_aliases(row.get("aliases")))
        if wanted == _slug(str(row.get("name") or "")) or wanted == _slug(str(row.get("label") or "")):
            return row
        if wanted in aliases:
            return row
        if wanted and (wanted in hay or hay in wanted):
            return row
    return None


def _geocode_destination(name: str) -> dict[str, Any] | None:
    text = (name or "").strip()
    if not text:
        return None
    rows = _nominatim_search(text)
    if not rows:
        return None
    row = rows[0]
    try:
        lat = float(row["lat"])
        lon = float(row["lon"])
    except Exception:
        return None
    label = str(row.get("label") or text).strip() or text
    return {
        "name": _slug(text) or "indirizzo",
        "label": label,
        "aliases": [],
        "lat": lat,
        "lon": lon,
        "note": "indirizzo geocodificato da OpenStreetMap/Nominatim",
        "source": "nominatim",
        "query": text,
    }


def _resolve_destination(name: str, *, allow_geocode: bool = True) -> dict[str, Any] | None:
    dest = _find_destination(name)
    if dest or not allow_geocode:
        return dest
    return _geocode_destination(name)


def _distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> int:
    radius = 6371000.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    x = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(radius * 2 * math.atan2(math.sqrt(x), math.sqrt(1 - x))))


def _nearby_stops(lat: float, lon: float, radius_m: int = 650, limit: int = 8) -> list[dict[str, Any]]:
    query = f"""
[out:json][timeout:12];
(
  node(around:{radius_m},{lat},{lon})["public_transport"="platform"];
  node(around:{radius_m},{lat},{lon})["highway"="bus_stop"];
  node(around:{radius_m},{lat},{lon})["railway"="tram_stop"];
  node(around:{radius_m},{lat},{lon})["railway"="station"];
  way(around:{radius_m},{lat},{lon})["public_transport"="platform"];
);
out center tags;
"""
    try:
        data = _http_json(OVERPASS_URL, method="POST", data=("data=" + urllib.parse.quote(query)).encode(), timeout=18)
    except Exception:
        return []
    stops: list[dict[str, Any]] = []
    seen: set[str] = set()
    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        slat = float(el.get("lat") or (el.get("center") or {}).get("lat") or 0)
        slon = float(el.get("lon") or (el.get("center") or {}).get("lon") or 0)
        if not slat or not slon:
            continue
        name = str(tags.get("name") or tags.get("ref") or "fermata").strip()
        key = f"{name}:{round(slat, 5)}:{round(slon, 5)}"
        if key in seen:
            continue
        seen.add(key)
        raw_hint = str(tags.get("route_ref") or "").strip()
        raw_ref = str(tags.get("ref") or "").strip()
        hint_parts = re.split(r"[,;/\s]+", raw_hint)
        line_hint = ", ".join(x for x in (_line_label(part) for part in hint_parts) if x)
        atm_stop_code = raw_ref if re.fullmatch(r"\d{4,6}", raw_ref) else ""
        modes = [k for k in ("bus", "tram", "subway", "train", "trolleybus") if str(tags.get(k) or "").lower() in {"yes", "true"}]
        stops.append({
            "name": name,
            "lat": slat,
            "lon": slon,
            "distance_m": _distance_m(lat, lon, slat, slon),
            "lines_hint": line_hint,
            "atm_stop_code": atm_stop_code,
            "modes": modes,
            "osm_id": f"{el.get('type')}/{el.get('id')}",
        })
    stops.sort(key=lambda s: int(s["distance_m"]))
    return stops[:limit]


def _maps_link(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> str:
    return "https://www.openstreetmap.org/directions?" + urllib.parse.urlencode({"engine": "fossgis_osrm_foot", "route": f"{a_lat},{a_lon};{b_lat},{b_lon}"})


def _atm_link(lat: float, lon: float) -> str:
    return "https://giromilano.atm.it/#/nearby/" + urllib.parse.quote(f"{lat},{lon}")


def _line_label(value: Any) -> str:
    text = str(value or "").strip().upper()
    text = re.sub(r"\s+", "", text)
    if re.fullmatch(r"\d{1,3}", text):
        return text
    if re.fullmatch(r"M[1-5]", text):
        return text
    if re.fullmatch(r"[A-Z]{1,2}\d{1,2}", text):
        return text
    return ""


def _line_labels(rows: list[dict[str, Any]], limit: int = 8) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        line = _line_label(row.get("line"))
        if line and line not in seen:
            out.append(line)
            seen.add(line)
        if len(out) >= limit:
            break
    return out


def _cdp_json(url: str, timeout: float = 1.5) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def _atm_browser_helper(action: str, timeout_s: float = 8.0) -> bool:
    if not ATM_BROWSER_HELPER_URL:
        return False
    try:
        url = ATM_BROWSER_HELPER_URL.rstrip("/") + "/" + action.lstrip("/")
        req = urllib.request.Request(url, data=b"{}", method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout_s) as res:
            payload = json.loads(res.read().decode("utf-8") or "{}")
        return bool(payload.get("ok"))
    except Exception:
        return False


def _cdp_call(ws: Any, seq: int, method: str, params: dict[str, Any] | None = None, timeout_s: float = 3.0) -> dict[str, Any]:
    try:
        ws.settimeout(timeout_s)
    except Exception:
        pass
    ws.send(json.dumps({"id": seq, "method": method, "params": params or {}}))
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        msg = json.loads(ws.recv())
        if msg.get("id") == seq:
            return msg
    return {}


def _cdp_eval_text(ws: Any, seq: int) -> str:
    expr = "document.body ? document.body.innerText : ''"
    res = _cdp_call(ws, seq, "Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return str(res.get("result", {}).get("result", {}).get("value") or "")


def _open_cdp_tab(url: str) -> dict[str, Any] | None:
    endpoint = f"http://{CDP_HOST}:{CDP_PORT}/json/new?" + urllib.parse.quote(url, safe=":/?=&,#")
    req = urllib.request.Request(endpoint, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=2) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception:
        return None


def _close_cdp_tab(page: dict[str, Any] | None) -> None:
    page_id = str((page or {}).get("id") or "").strip()
    if not page_id:
        return
    try:
        endpoint = f"http://{CDP_HOST}:{CDP_PORT}/json/close/" + urllib.parse.quote(page_id, safe="")
        _cdp_json(endpoint)
    except Exception:
        pass


def _browser_page_text(url: str, wait_s: float = 5.0) -> str:
    if not ATM_LIVE_BROWSER_ENABLED:
        return ""
    try:
        import websocket  # existing project dependency, used by /home/bandi/bin/bandi-chat-injector
    except Exception:
        return ""

    page = _open_cdp_tab(url)
    wsurl = (page or {}).get("webSocketDebuggerUrl")
    if not wsurl:
        try:
            for item in _cdp_json(f"http://{CDP_HOST}:{CDP_PORT}/json/list"):
                if str(item.get("url") or "").startswith("https://giromilano.atm.it/"):
                    wsurl = item.get("webSocketDebuggerUrl")
                    break
        except Exception:
            return ""
    if not wsurl:
        return ""

    ws = None
    try:
        ws = websocket.create_connection(wsurl, timeout=3, suppress_origin=True)
        _cdp_call(ws, 1, "Runtime.enable")
        _cdp_call(ws, 2, "Page.enable")
        text = ""
        deadline = time.time() + wait_s
        seq = 3
        while time.time() < deadline:
            text = _cdp_eval_text(ws, seq)
            seq += 1
            low = text.lower()
            if any(token in low for token in (" min", "minuti", "arrivi", "prossimi", "fermata")):
                break
            time.sleep(0.5)
        return text
    except Exception:
        return ""
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        _close_cdp_tab(page)


def _tpportal_direct_timeout(path: str) -> float:
    clean = path.lstrip("/")
    if clean.startswith("tpl/trips?"):
        return 24.0
    if "/linesummary" in clean:
        return 8.0
    if clean.startswith("tpl/journeyPatterns/"):
        return 6.0
    return 10.0


def _tpportal_fetch_json_direct(path: str, timeout: float | None = None) -> Any:
    url = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal/" + path.lstrip("/")
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 ralfloop-atm-telegram/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or _tpportal_direct_timeout(path)) as res:
            if int(getattr(res, "status", 0) or 0) >= 400:
                return None
            return json.loads(res.read().decode("utf-8"))
    except Exception:
        return None


def _browser_fetch_json(path: str) -> Any:
    if not ATM_LIVE_BROWSER_ENABLED:
        return None
    direct = _tpportal_fetch_json_direct(path)
    if direct is not None or not ATM_JSON_CDP_FALLBACK_ENABLED:
        return direct
    try:
        import websocket
    except Exception:
        return None
    opened_page = None
    try:
        pages = _cdp_json(f"http://{CDP_HOST}:{CDP_PORT}/json/list")
        page = next((p for p in pages if "giromilano.atm.it" in str(p.get("url") or "") and p.get("webSocketDebuggerUrl")), None)
        if not page:
            opened_page = _open_cdp_tab("https://giromilano.atm.it/")
            time.sleep(1.0)
            if opened_page and opened_page.get("webSocketDebuggerUrl"):
                page = opened_page
            else:
                pages = _cdp_json(f"http://{CDP_HOST}:{CDP_PORT}/json/list")
                page = next((p for p in pages if "giromilano.atm.it" in str(p.get("url") or "") and p.get("webSocketDebuggerUrl")), None)
        if not page:
            return None
        url = "https://giromilano.atm.it/proxy.tpportal/api/tpPortal/" + path.lstrip("/")
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=6, suppress_origin=True)
        try:
            _cdp_call(ws, 1, "Runtime.enable")
            expr = """
(async () => {
  const url = %s;
  const r = await fetch(url, {credentials: 'include'});
  const text = await r.text();
  return JSON.stringify({status: r.status, text});
})()
""" % json.dumps(url)
            res = _cdp_call(ws, 2, "Runtime.evaluate", {"expression": expr, "awaitPromise": True, "returnByValue": True}, timeout_s=18.0)
            raw = str(res.get("result", {}).get("result", {}).get("value") or "")
        finally:
            ws.close()
        payload = json.loads(raw)
        if int(payload.get("status") or 0) >= 400:
            return None
        return json.loads(str(payload.get("text") or "null"))
    except Exception:
        return None
    finally:
        if opened_page is not None:
            _close_cdp_tab(opened_page)

def _parse_live_minutes(page_text: str, line_labels: list[str]) -> dict[str, str]:
    text = re.sub(r"\s+", " ", page_text or " ").strip()
    if not text or not line_labels:
        return {}
    out: dict[str, str] = {}
    for line in line_labels:
        escaped = re.escape(line)
        patterns = [
            rf"(?<!\d){escaped}(?!\d).{{0,90}}?(\d{{1,2}})\s*(?:min|')",
            rf"(\d{{1,2}})\s*(?:min|').{{0,90}}?(?<!\d){escaped}(?!\d)",
        ]
        for pat in patterns:
            m = re.search(pat, text, flags=re.I)
            if m:
                out[line] = f"{int(m.group(1))} min"
                break
    return out



_ATM_PATTERN_CACHE = {}

def _atm_journey_pattern_stops(journey_pattern_id: str) -> list[dict[str, Any]]:
    import time
    jp = str(journey_pattern_id).strip()
    if jp in _ATM_PATTERN_CACHE:
        return _ATM_PATTERN_CACHE[jp]

    for _ in range(3):
        data = _browser_fetch_json(f"tpl/journeyPatterns/{urllib.parse.quote(jp, safe='|')}")
        stops = (data or {}).get("Stops") or []
        if isinstance(stops, list) and stops:
            _ATM_PATTERN_CACHE[jp] = stops
            return stops
        time.sleep(0.15)

    _ATM_PATTERN_CACHE[jp] = []
    return []


def _atm_stop_latlon(stop: dict[str, Any]) -> tuple[float, float] | None:
    loc = stop.get("Location") or {}
    try:
        lat = float(loc.get("Y"))
        lon = float(loc.get("X"))
        if lat and lon:
            return lat, lon
    except Exception:
        pass
    return None


def _nearest_atm_pattern_stop(
    stops: list[dict[str, Any]],
    lat: float,
    lon: float,
    max_distance_m: int = 280,
) -> tuple[int, dict[str, Any], int] | None:
    best = None
    for i, st in enumerate(stops):
        pos = _atm_stop_latlon(st)
        if not pos:
            continue
        dist = _distance_m(lat, lon, pos[0], pos[1])
        if best is None or dist < best[2]:
            best = (i, st, dist)
    if best is None or best[2] > max_distance_m:
        return None
    return best


def _atm_direct_fallback_options(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float, line_hints: list[str]) -> list[dict[str, Any]]:
    import re
    import time
    import urllib.parse

    if _distance_m(origin_lat, origin_lon, dest_lat, dest_lon) > 1800:
        return []

    deadline = time.monotonic() + 18.0

    def expired() -> bool:
        return time.monotonic() > deadline

    def code_of(st: dict[str, Any]) -> str:
        return str(st.get("Code") or "").strip()

    def name_of(st: dict[str, Any]) -> str:
        return str(st.get("Description") or st.get("Name") or "fermata").strip()

    def pos_of(st: dict[str, Any]):
        loc = st.get("Location") or {}
        try:
            return float(loc["Y"]), float(loc["X"])
        except Exception:
            return None

    def nearby_indices(stops, lat, lon, max_m):
        out = []
        for i, st in enumerate(stops):
            pos = pos_of(st)
            if not pos:
                continue
            d = _distance_m(lat, lon, pos[0], pos[1])
            if d <= max_m:
                out.append((i, st, d))
        return out

    def best_forward_pair(stops):
        origins = nearby_indices(stops, origin_lat, origin_lon, 850)
        destinations = nearby_indices(stops, dest_lat, dest_lon, 950)

        valid = []
        for oi, os, od in origins:
            for di, ds, dd in destinations:
                if oi >= di:
                    continue

                stops_count = di - oi
                if stops_count > 6:
                    continue

                valid.append((
                    od + dd,
                    stops_count,
                    od,
                    dd,
                    oi,
                    os,
                    di,
                    ds,
                ))

        if not valid:
            return None

        valid.sort(key=lambda x: (
            x[0],
            x[1],
            x[2],
            x[3],
        ))

        _, stops_count, od, dd, oi, os, di, ds = valid[0]
        return oi, os, od, di, ds, dd, stops_count

    def wait_rank(wait: str) -> int:
        low = str(wait or "").lower()
        if low == "in arrivo":
            return 0
        m = re.search(r"\d+", low)
        return int(m.group(0)) if m else 999

    def wait_from_stop(ocode: str, line: str, jp: str) -> str:
        if expired():
            return "n/d"

        snapshot = _atm_realtime_snapshot(
            ocode,
            [line],
        )

        if snapshot is not None:
            for observation in snapshot.get("observations") or []:
                if not isinstance(observation, dict):
                    continue

                row_line = _line_label(
                    observation.get("line")
                )
                row_jp = str(
                    observation.get("journey_pattern_id") or ""
                ).strip()

                if row_line == line and row_jp == jp:
                    return str(
                        observation.get("wait") or ""
                    ).strip() or "n/d"

            return "n/d"

        # Legacy path, utilizzato solo quando nessun provider MCP
        # è stato iniettato.
        data = _browser_fetch_json(
            f"tpl/stops/{urllib.parse.quote(ocode)}/linesummary"
        )

        for row in (data or {}).get("Lines") or []:
            line_obj = row.get("Line") or {}
            row_line = _line_label(
                line_obj.get("LineCode")
                or line_obj.get("LineId")
            )
            row_jp = str(
                row.get("JourneyPatternId") or ""
            ).strip()

            if row_line == line and row_jp == jp:
                return str(
                    row.get("WaitMessage") or ""
                ).strip() or "n/d"

        return "n/d"

    hints = []
    for x in line_hints or []:
        line = _line_label(x)
        if line and not line.startswith("Q") and line not in hints:
            hints.append(line)

    candidates = []
    for line in hints:
        for direction in ("0", "1"):
            candidates.append((line, f"{line}|{direction}"))

    options = []

    for line, jp in candidates:
        if expired():
            break

        stops = _atm_journey_pattern_stops(jp)
        if not stops:
            continue

        pair = best_forward_pair(stops)
        if not pair:
            continue

        oi, os, od, di, ds, dd, stops_count = pair

        ocode = code_of(os)
        opos = pos_of(os)
        dpos = pos_of(ds)

        if not opos or not dpos:
            continue

        wait = wait_from_stop(ocode, line, jp)

        options.append({
            "line": line,
            "direction": jp.split("|", 1)[1] if "|" in jp else "",
            "journey_pattern_id": jp,
            "wait": wait,
            "wait_source": (
                "atm_live"
                if _minutes_value(wait) is not None
                else "not_available"
            ),
            "origin_stop_code": ocode,
            "origin_stop_name": name_of(os),
            "origin_stop_lat": opos[0],
            "origin_stop_lon": opos[1],
            "origin_distance_m": int(round(od)),
            "dest_stop_code": code_of(ds),
            "dest_stop_name": name_of(ds),
            "dest_stop_lat": dpos[0],
            "dest_stop_lon": dpos[1],
            "dest_distance_m": int(round(dd)),
            "origin_idx": oi,
            "dest_idx": di,
            "stops_count": stops_count,
        })

    by_stop: dict[str, list[str]] = {}
    for opt in options:
        if str(opt.get("wait") or "").lower() not in ("", "n/d", "ricalcolo"):
            continue
        code = str(opt.get("origin_stop_code") or "")
        line = str(opt.get("line") or "")
        if code and line:
            by_stop.setdefault(code, []).append(line)

    for code, line_list in by_stop.items():
        if expired():
            break
        live = _atm_live_minutes({"atm_stop_code": code}, sorted(set(line_list)))
        arrivals = live.get("arrivals") or {}
        for opt in options:
            if opt.get("origin_stop_code") == code and opt.get("line") in arrivals:
                opt["wait"] = arrivals[opt["line"]]
                opt["wait_source"] = "atm_live"

    # DIRECT_FALLBACK_ETA_START
    current = datetime.now()

    for opt in options:
        origin_distance_m = int(
            opt["origin_distance_m"]
            if opt.get("origin_distance_m") is not None
            else 0
        )

        final_walk_m = int(
            opt["dest_distance_m"]
            if opt.get("dest_distance_m") is not None
            else 0
        )

        origin_walk_seconds = int(
            math.ceil(max(0, origin_distance_m) / 80.0) * 60
        )

        final_walk_seconds = int(
            math.ceil(max(0, final_walk_m) / 80.0) * 60
        )

        board_ready_at = (
            current
            + timedelta(seconds=origin_walk_seconds)
        )

        wait_min = _minutes_value(opt.get("wait"))

        live_departure_at = (
            current + timedelta(minutes=wait_min)
            if wait_min is not None
            else None
        )

        schedule = _gtfs_next_departure(
            str(opt.get("line") or ""),
            float(opt["origin_stop_lat"]),
            float(opt["origin_stop_lon"]),
            float(opt["dest_stop_lat"]),
            float(opt["dest_stop_lon"]),
            board_ready_at,
        )

        opt["origin_walk_seconds"] = origin_walk_seconds
        opt["board_ready_at"] = board_ready_at
        opt["final_walk_m"] = final_walk_m
        opt["final_walk_seconds"] = final_walk_seconds

        if not schedule:
            continue

        try:
            travel_seconds = int(
                schedule.get("travel_seconds")
            )
        except Exception:
            continue

        if travel_seconds < 0:
            continue

        live_catchable = (
            isinstance(live_departure_at, datetime)
            and live_departure_at >= board_ready_at
        )

        if live_catchable:
            departure_at = live_departure_at
            vehicle_arrival_at = (
                departure_at
                + timedelta(seconds=travel_seconds)
            )
            effective_wait_seconds = int(wait_min * 60)
            opt["wait_source"] = "atm_live"

        else:
            departure_at = schedule.get("departure_at")
            scheduled_arrival_at = schedule.get("arrival_at")

            if not isinstance(departure_at, datetime):
                continue

            if departure_at < board_ready_at:
                continue

            if isinstance(scheduled_arrival_at, datetime):
                vehicle_arrival_at = scheduled_arrival_at
            else:
                vehicle_arrival_at = (
                    departure_at
                    + timedelta(seconds=travel_seconds)
                )

            effective_wait_seconds = max(
                0,
                int(
                    (
                        departure_at - board_ready_at
                    ).total_seconds()
                ),
            )

            opt["wait"] = (
                f"{round(effective_wait_seconds / 60)} min"
            )
            opt["wait_source"] = str(
                schedule.get("source")
                or "gtfs_scheduled"
            )

        destination_arrival_at = (
            vehicle_arrival_at
            + timedelta(seconds=final_walk_seconds)
        )

        eta_seconds = int(
            (
                destination_arrival_at - current
            ).total_seconds()
        )

        if eta_seconds < 0:
            continue

        opt["wait_seconds"] = effective_wait_seconds
        opt["departure_at"] = departure_at
        opt["travel_seconds"] = travel_seconds
        opt["vehicle_arrival_at"] = vehicle_arrival_at
        opt["destination_arrival_at"] = destination_arrival_at
        opt["eta_seconds"] = eta_seconds

    # DIRECT_FALLBACK_ETA_END

    options.sort(key=lambda x: (
        (
            int(x["eta_seconds"])
            if x.get("eta_seconds") is not None
            else 10**9
        ),
        (
            int(x["origin_distance_m"])
            if x.get("origin_distance_m") is not None
            else 9999
        )
        + (
            int(x["dest_distance_m"])
            if x.get("dest_distance_m") is not None
            else 9999
        ),
        (
            int(x["stops_count"])
            if x.get("stops_count") is not None
            else 999
        ),
        wait_rank(str(x.get("wait") or "")),
        str(x.get("line") or ""),
    ))

    best_by_line = []
    used_lines = set()
    for opt in options:
        line = str(opt.get("line") or "")
        if not line or line in used_lines:
            continue
        used_lines.add(line)
        best_by_line.append(opt)

    return best_by_line[:6]


_ATM_REALTIME_PROVIDER: contextvars.ContextVar[Any] = contextvars.ContextVar("atm_realtime_provider", default=None)


def _atm_realtime_snapshot(
    stop_code: str,
    line_labels: list[str] | None = None,
) -> dict[str, Any] | None:
    stop_code = str(stop_code or "").strip()

    if not stop_code:
        return {
            "ok": False,
            "status": "not_available",
            "stop_code": "",
            "arrivals": {},
            "observations": [],
            "source": "no_stop_code",
        }

    provider = _ATM_REALTIME_PROVIDER.get()

    # None significa che il chiamante non ha iniettato il provider MCP:
    # il codice legacy può quindi mantenere il suo fallback diretto.
    if provider is None:
        return None

    try:
        snapshot_method = getattr(provider, "snapshot", None)

        if callable(snapshot_method):
            payload = snapshot_method(
                stop_code,
                list(line_labels) if line_labels is not None else None,
            )
        else:
            arrivals = provider.waits(
                stop_code,
                list(line_labels or []),
            )
            payload = {
                "ok": bool(arrivals),
                "status": "ok" if arrivals else "not_available",
                "stop_code": stop_code,
                "arrivals": arrivals or {},
                "observations": [],
                "source": "atm_live_mcp_compat",
            }
    except Exception:
        return {
            "ok": False,
            "status": "not_available",
            "stop_code": stop_code,
            "arrivals": {},
            "observations": [],
            "source": "atm_live_mcp_unavailable",
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "status": "not_available",
            "stop_code": stop_code,
            "arrivals": {},
            "observations": [],
            "source": "atm_live_mcp_invalid",
        }

    arrivals_raw = payload.get("arrivals") or {}
    observations_raw = payload.get("observations") or []

    arrivals = (
        {
            str(line): str(wait)
            for line, wait in arrivals_raw.items()
        }
        if isinstance(arrivals_raw, dict)
        else {}
    )

    observations = [
        dict(item)
        for item in observations_raw
        if isinstance(item, dict)
    ]

    return {
        "ok": bool(payload.get("ok")),
        "status": str(
            payload.get("status")
            or ("ok" if arrivals else "not_available")
        ),
        "stop_code": stop_code,
        "arrivals": arrivals,
        "observations": observations,
        "source": str(
            payload.get("source")
            or "atm_live_mcp"
        ),
    }


def _atm_live_minutes(stop: dict[str, Any], line_labels: list[str]) -> dict[str, Any]:
    if not stop or not line_labels:
        return {"status": "no_lines", "arrivals": {}}

    import time

    lat = stop.get("lat")
    lon = stop.get("lon")
    url = ""
    if lat is not None and lon is not None:
        url = _atm_link(float(lat), float(lon))

    stop_code = str(stop.get("atm_stop_code") or "").strip()

    snapshot = _atm_realtime_snapshot(
        stop_code,
        line_labels,
    )

    if snapshot is not None:
        return {
            "status": snapshot.get("status") or "not_available",
            "arrivals": snapshot.get("arrivals") or {},
            "url": url,
            "source": snapshot.get("source") or "atm_live_mcp",
        }

    if stop_code:
        for attempt in range(3):
            data = _browser_fetch_json(f"tpl/stops/{urllib.parse.quote(stop_code)}/linesummary")
            arrivals: dict[str, str] = {}

            if isinstance(data, dict):
                for row in data.get("Lines") or []:
                    line_obj = row.get("Line") or {}
                    raw_line = line_obj.get("LineCode") or line_obj.get("LineId")
                    line = _line_label(raw_line)
                    wait = str(row.get("WaitMessage") or "").strip()

                    if line and wait:
                        arrivals[line] = wait
                    if raw_line and str(raw_line).strip() and wait:
                        arrivals[str(raw_line).strip()] = wait

            filtered = {line: arrivals[line] for line in line_labels if line in arrivals}
            if filtered:
                return {
                    "status": "ok",
                    "arrivals": filtered,
                    "url": url,
                    "source": f"browser_cdp_tpportal_linesummary_attempt_{attempt + 1}",
                }

            time.sleep(0.35)

    if lat is None or lon is None:
        return {
            "status": "not_available",
            "arrivals": {},
            "url": "",
            "source": "no_stop_coordinates",
        }

    text = _browser_page_text(url)
    arrivals = _parse_live_minutes(text, line_labels)
    return {
        "status": "ok" if arrivals else "not_available",
        "arrivals": arrivals,
        "url": url,
        "source": "browser_page_text",
    }


def _nearby_route_lines(lat: float, lon: float, radius_m: int = 85, limit: int = 12) -> list[dict[str, Any]]:
    query = f"""
[out:json][timeout:12];
(
  node(around:{radius_m},{lat},{lon});
  way(around:{radius_m},{lat},{lon});
)->.near;
(
  relation(bn.near)["type"="route"]["route"~"^(bus|tram|subway|train|trolleybus)$"];
  relation(bw.near)["type"="route"]["route"~"^(bus|tram|subway|train|trolleybus)$"];
);
out tags;
"""
    try:
        data = _http_json(OVERPASS_URL, method="POST", data=("data=" + urllib.parse.quote(query)).encode(), timeout=18)
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    for el in data.get("elements", []):
        tags = el.get("tags") or {}
        ref = str(tags.get("ref") or "").strip()
        name = str(tags.get("name") or "").strip()
        route = str(tags.get("route") or "").strip()
        operator = str(tags.get("operator") or "").strip()

        label = _line_label(ref or name)
        if not label:
            continue

        key = f"{route}:{label}".lower()
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "line": label,
            "route": route,
            "name": name,
            "operator": operator,
        })

    def sort_key(x: dict[str, Any]) -> tuple[int, str]:
        line = str(x.get("line") or "")
        return (0 if line[:1].isdigit() else 1, line)

    out.sort(key=sort_key)
    return out[:limit]


def _atm_trip_plan(lat: float, lon: float, dlat: float, dlon: float, dest_label: str) -> dict[str, Any] | None:
    now = datetime.now()
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    query = urllib.parse.urlencode({
        "FromPoint.X": lon,
        "FromPoint.Y": lat,
        "ToPoint.X": dlon,
        "ToPoint.Y": dlat,
        "Date": now.strftime("%Y-%m-%d"),
        "FromSeconds": seconds,
        "TransportModes": "Metro,Bus,Tram,Rail",
        "Lang": "it",
        "FromDescription": "posizione attuale",
        "ToDescription": dest_label,
    })
    data = _browser_fetch_json("tpl/trips?" + query)
    if not isinstance(data, dict):
        return None
    trips = data.get("Trips") or data.get("TripSolutions") or data.get("Solutions") or []
    if not isinstance(trips, list) or not trips:
        return None

    trip = next(
        (
            candidate
            for candidate in trips
            if _official_trip_has_transit({"raw": candidate, "summary": {}})
        ),
        trips[0],
    )
    actions = trip.get("Actions") or trip.get("TripActions") or trip.get("Segments") or []
    steps: list[str] = []
    lines: list[str] = []
    walk_m = 0
    board_stop_code = ""
    board_stop_name = ""
    first_transit_line = ""

    for action in actions:
        desc = str(action.get("ActionDescription") or action.get("Description") or "").strip()
        place = action.get("Place") or {}
        if isinstance(place, dict) and int(action.get("ActionType") or -1) == 4 and not board_stop_code:
            board_stop_code = str(place.get("Code") or "").strip()
            board_stop_name = str(place.get("Description") or "").strip()
        leg = action.get("Leg") or {}
        if isinstance(leg, dict):
            try:
                if int(leg.get("TravelMode", -1)) == 0:
                    walk_m += int(float(leg.get("Length") or 0))
            except Exception:
                pass
            journeys = leg.get("Journeys") or []
            if isinstance(journeys, list):
                for journey in journeys:
                    jp = journey.get("JourneyPattern") or {}
                    line = _line_label(jp.get("Code") or ((jp.get("Line") or {}).get("LineCode")))
                    if line and line not in lines:
                        lines.append(line)
                    if line and not first_transit_line:
                        first_transit_line = line
        if desc:
            desc = re.sub(r"\s+", " ", desc)
            steps.append(desc)

    duration = trip.get("Duration") or trip.get("TotalDuration") or trip.get("TravelTime")
    if not duration:
        for key in ("DurationMinutes", "TotalMinutes", "TravelTimeMinutes"):
            if trip.get(key) is not None:
                duration = f"{trip.get(key)} min"
                break

    live_wait = ""
    if board_stop_code and first_transit_line:
        live = _atm_live_minutes({"atm_stop_code": board_stop_code, "lat": lat, "lon": lon}, [first_transit_line])
        live_wait = str((live.get("arrivals") or {}).get(first_transit_line) or "")

    duration_min = _minutes_value(duration)
    wait_min = _minutes_value(live_wait)
    vehicle_eta = (now + timedelta(minutes=wait_min)).strftime("%H:%M") if wait_min is not None else ""
    destination_eta = (now + timedelta(minutes=duration_min + (wait_min or 0))).strftime("%H:%M") if duration_min is not None else None

    eta_seconds = (
        int((duration_min + wait_min) * 60)
        if duration_min is not None and wait_min is not None
        else None
    )

    return {
        "raw": trip,
        "summary": {
            "duration": str(duration or "").strip(),
            "walk_m": walk_m,
            "lines": lines,
            "steps": steps[:8],
            "first_line": first_transit_line,
            "board_stop_code": board_stop_code,
            "board_stop_name": board_stop_name,
            "live_wait": live_wait,
            "vehicle_eta": vehicle_eta,
            "destination_eta": destination_eta,
            "eta_seconds": eta_seconds,
        },
    }


def _minutes_value(value: Any) -> int | None:
    if str(value or "").strip().casefold() == "in arrivo":
        return 0
    if value is None:
        return None
    m = re.search(r"\d+", str(value))
    return int(m.group(0)) if m else None




# GTFS_SCHEDULE_HELPER_START

# GTFS_SUBWAY_INDEX_START

_GTFS_SUBWAY_INDEX_CACHE: dict[
    tuple[str, int, int],
    list[dict[str, Any]],
] = {}


def _gtfs_subway_index(
    *,
    gtfs_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """
    Indicizza localmente le fermate realmente usate dalle linee metro GTFS.

    La cache è invalidata automaticamente quando cambiano mtime o dimensione
    del file gtfs.zip.
    """
    import csv
    import io
    import zipfile

    path = Path(
        gtfs_path
        or os.environ.get(
            "RALFLOOP_ATM_GTFS_PATH",
            "/home/sibilla-cumana/ralfloop_data/atm_telegram/gtfs.zip",
        )
    )

    if not path.is_file():
        return []

    try:
        stat = path.stat()
    except OSError:
        return []

    key = (
        str(path.resolve()),
        int(stat.st_mtime_ns),
        int(stat.st_size),
    )

    cached = _GTFS_SUBWAY_INDEX_CACHE.get(key)
    if cached is not None:
        return cached

    def rows(zf: zipfile.ZipFile, name: str):
        try:
            raw = zf.open(name)
        except KeyError:
            return

        with raw:
            text = io.TextIOWrapper(
                raw,
                encoding="utf-8-sig",
            )
            yield from csv.DictReader(text)

    try:
        with zipfile.ZipFile(path) as zf:
            route_to_line: dict[str, str] = {}

            for row in rows(zf, "routes.txt") or []:
                if str(row.get("route_type") or "").strip() != "1":
                    continue

                route_id = str(
                    row.get("route_id") or ""
                ).strip()

                short = str(
                    row.get("route_short_name") or ""
                ).strip().upper()

                if not route_id:
                    continue

                if short.startswith("M"):
                    line = short
                elif short:
                    line = f"M{short}"
                else:
                    rid = route_id.upper()
                    line = (
                        rid
                        if rid.startswith("M")
                        else f"M{rid}"
                    )

                route_to_line[route_id] = line

            if not route_to_line:
                _GTFS_SUBWAY_INDEX_CACHE.clear()
                _GTFS_SUBWAY_INDEX_CACHE[key] = []
                return []

            trip_to_line: dict[str, str] = {}

            for row in rows(zf, "trips.txt") or []:
                route_id = str(
                    row.get("route_id") or ""
                ).strip()

                line = route_to_line.get(route_id)
                if not line:
                    continue

                trip_id = str(
                    row.get("trip_id") or ""
                ).strip()

                if trip_id:
                    trip_to_line[trip_id] = line

            if not trip_to_line:
                _GTFS_SUBWAY_INDEX_CACHE.clear()
                _GTFS_SUBWAY_INDEX_CACHE[key] = []
                return []

            stop_lines: dict[str, set[str]] = {}

            for row in rows(zf, "stop_times.txt") or []:
                trip_id = str(
                    row.get("trip_id") or ""
                ).strip()

                line = trip_to_line.get(trip_id)
                if not line:
                    continue

                stop_id = str(
                    row.get("stop_id") or ""
                ).strip()

                if stop_id:
                    stop_lines.setdefault(
                        stop_id,
                        set(),
                    ).add(line)

            index: list[dict[str, Any]] = []

            for row in rows(zf, "stops.txt") or []:
                stop_id = str(
                    row.get("stop_id") or ""
                ).strip()

                lines = stop_lines.get(stop_id)
                if not lines:
                    continue

                try:
                    lat = float(row.get("stop_lat"))
                    lon = float(row.get("stop_lon"))
                except Exception:
                    continue

                for line in sorted(lines):
                    index.append({
                        "line": line,
                        "stop_id": stop_id,
                        "stop_name": str(
                            row.get("stop_name") or ""
                        ).strip(),
                        "lat": lat,
                        "lon": lon,
                    })

            # Manteniamo solo la versione corrente del feed.
            _GTFS_SUBWAY_INDEX_CACHE.clear()
            _GTFS_SUBWAY_INDEX_CACHE[key] = index

            return index

    except Exception:
        return []


def _gtfs_subway_lines_near(
    lat: float,
    lon: float,
    *,
    radius_m: int = 250,
    gtfs_path: str | Path | None = None,
) -> list[str]:
    """Linee metro GTFS con una fermata entro radius_m dal punto."""

    nearest_by_line: dict[str, int] = {}

    for item in _gtfs_subway_index(
        gtfs_path=gtfs_path,
    ):
        try:
            distance = int(round(_distance_m(
                lat,
                lon,
                float(item["lat"]),
                float(item["lon"]),
            )))
        except Exception:
            continue

        if distance > radius_m:
            continue

        line = str(item.get("line") or "").strip()
        if not line:
            continue

        previous = nearest_by_line.get(line)

        if previous is None or distance < previous:
            nearest_by_line[line] = distance

    return [
        line
        for line, _distance in sorted(
            nearest_by_line.items(),
            key=lambda item: (
                item[1],
                item[0],
            ),
        )
    ]


# GTFS_SUBWAY_INDEX_END



# GTFS_LINE_SCHEDULE_INDEX_START

_GTFS_LINE_SCHEDULE_CACHE: dict[
    tuple[str, int, int, tuple[str, ...]],
    dict[str, dict[str, Any]],
] = {}


def _gtfs_line_schedule_index(
    lines: list[str],
    *,
    gtfs_path: str | Path | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Carica in memoria solo le linee GTFS richieste.

    stop_times.txt viene scandito una sola volta per l'intero batch;
    le righe delle altre linee non vengono conservate.
    """
    import csv
    import io
    import zipfile

    path = Path(
        gtfs_path
        or os.environ.get(
            "RALFLOOP_ATM_GTFS_PATH",
            "/home/sibilla-cumana/ralfloop_data/atm_telegram/gtfs.zip",
        )
    )

    if not path.is_file():
        return {}

    wanted: set[str] = set()

    for raw in lines or []:
        line = str(raw or "").strip().upper()
        if line:
            wanted.add(line)

    if not wanted:
        return {}

    try:
        stat = path.stat()
    except OSError:
        return {}

    key = (
        str(path.resolve()),
        int(stat.st_mtime_ns),
        int(stat.st_size),
        tuple(sorted(wanted)),
    )

    cached = _GTFS_LINE_SCHEDULE_CACHE.get(key)
    if cached is not None:
        return cached

    # Un batch più ampio già caricato può soddisfare gratuitamente
    # una successiva richiesta per una singola linea.
    base_key = key[:3]

    for cached_key, cached_value in list(
        _GTFS_LINE_SCHEDULE_CACHE.items()
    ):
        if cached_key[:3] != base_key:
            continue

        cached_lines = set(cached_value)
        if wanted.issubset(cached_lines):
            return {
                line: cached_value[line]
                for line in sorted(wanted)
            }

    def rows(zf: zipfile.ZipFile, name: str):
        try:
            raw = zf.open(name)
        except KeyError:
            return

        with raw:
            text = io.TextIOWrapper(
                raw,
                encoding="utf-8-sig",
            )
            yield from csv.DictReader(text)

    def seconds(value: str | None) -> int | None:
        value = str(value or "").strip()
        if not value:
            return None

        try:
            hh, mm, ss = (
                int(part)
                for part in value.split(":")
            )
        except Exception:
            return None

        return hh * 3600 + mm * 60 + ss

    try:
        with zipfile.ZipFile(path) as zf:
            route_to_line: dict[str, str] = {}

            for row in rows(zf, "routes.txt") or []:
                route_id = str(
                    row.get("route_id") or ""
                ).strip()

                if not route_id:
                    continue

                short = str(
                    row.get("route_short_name") or ""
                ).strip().upper()

                route_type = str(
                    row.get("route_type") or ""
                ).strip()

                rid_upper = route_id.upper()

                if route_type == "1":
                    if short.startswith("M"):
                        canonical = short
                    elif short:
                        canonical = f"M{short}"
                    elif rid_upper.startswith("M"):
                        canonical = rid_upper
                    else:
                        canonical = f"M{rid_upper}"
                else:
                    canonical = short or rid_upper

                if canonical in wanted:
                    route_to_line[route_id] = canonical

            result: dict[str, dict[str, Any]] = {
                line: {
                    "trip_meta": {},
                    "trip_stop_times": {},
                }
                for line in sorted(wanted)
                if line in set(route_to_line.values())
            }

            if not result:
                _GTFS_LINE_SCHEDULE_CACHE[key] = {}
                return {}

            trip_to_line: dict[str, str] = {}

            for row in rows(zf, "trips.txt") or []:
                route_id = str(
                    row.get("route_id") or ""
                ).strip()

                line = route_to_line.get(route_id)
                if not line:
                    continue

                trip_id = str(
                    row.get("trip_id") or ""
                ).strip()

                if not trip_id:
                    continue

                meta = dict(row)

                result[line]["trip_meta"][trip_id] = meta
                trip_to_line[trip_id] = line

            if not trip_to_line:
                _GTFS_LINE_SCHEDULE_CACHE[key] = result
                return result

            # Piccolo: possiamo conservarlo integralmente e condividerlo
            # fra tutte le linee del batch.
            stop_coords: dict[
                str,
                tuple[float, float],
            ] = {}

            for row in rows(zf, "stops.txt") or []:
                stop_id = str(
                    row.get("stop_id") or ""
                ).strip()

                if not stop_id:
                    continue

                try:
                    stop_coords[stop_id] = (
                        float(row.get("stop_lat")),
                        float(row.get("stop_lon")),
                    )
                except Exception:
                    continue

            calendar_rows = list(
                rows(zf, "calendar.txt") or []
            )
            exception_rows = list(
                rows(zf, "calendar_dates.txt") or []
            )

            # Questa è la scansione costosa (~273 MiB raw), ma viene fatta
            # una volta sola per tutte le linee richieste.
            for row in rows(zf, "stop_times.txt") or []:
                trip_id = str(
                    row.get("trip_id") or ""
                ).strip()

                line = trip_to_line.get(trip_id)
                if not line:
                    continue

                stop_id = str(
                    row.get("stop_id") or ""
                ).strip()

                if not stop_id:
                    continue

                try:
                    sequence = int(
                        row.get("stop_sequence") or 0
                    )
                except Exception:
                    continue

                arrival_s = seconds(
                    row.get("arrival_time")
                )
                departure_s = seconds(
                    row.get("departure_time")
                )

                if (
                    arrival_s is None
                    and departure_s is None
                ):
                    continue

                result[line]["trip_stop_times"].setdefault(
                    trip_id,
                    [],
                ).append({
                    "stop_id": stop_id,
                    "sequence": sequence,
                    "arrival_s": arrival_s,
                    "departure_s": departure_s,
                })

            for data in result.values():
                data["stop_coords"] = stop_coords
                data["calendar_rows"] = calendar_rows
                data["exception_rows"] = exception_rows

            # Eliminiamo soltanto versioni obsolete dello stesso feed.
            for old_key in list(_GTFS_LINE_SCHEDULE_CACHE):
                if (
                    old_key[0] == key[0]
                    and old_key[:3] != key[:3]
                ):
                    _GTFS_LINE_SCHEDULE_CACHE.pop(old_key, None)

            _GTFS_LINE_SCHEDULE_CACHE[key] = result

            return result

    except Exception:
        return {}


# GTFS_LINE_SCHEDULE_INDEX_END


def _gtfs_next_departure(
    line: str,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    not_before: datetime,
    *,
    gtfs_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Prima corsa GTFS programmata prendibile fra due punti sulla stessa linea."""

    wanted_line = str(line or "").strip().upper()
    if not wanted_line:
        return None

    index = _gtfs_line_schedule_index(
        [wanted_line],
        gtfs_path=gtfs_path,
    )

    data = index.get(wanted_line)
    if not data:
        return None

    trip_meta = data.get("trip_meta") or {}
    trip_stop_times = data.get("trip_stop_times") or {}
    stop_coords = data.get("stop_coords") or {}
    calendar_rows = data.get("calendar_rows") or []
    exception_rows = data.get("exception_rows") or []

    if not trip_meta or not trip_stop_times or not stop_coords:
        return None

    def active_services(service_date) -> set[str]:
        ymd = service_date.strftime("%Y%m%d")
        weekday = service_date.strftime("%A").lower()

        active: set[str] = set()

        for row in calendar_rows:
            start_date = str(row.get("start_date") or "")
            end_date = str(row.get("end_date") or "")

            if (
                not start_date
                or not end_date
                or not (start_date <= ymd <= end_date)
            ):
                continue

            if str(row.get(weekday) or "") == "1":
                service_id = str(
                    row.get("service_id") or ""
                ).strip()
                if service_id:
                    active.add(service_id)

        for row in exception_rows:
            if str(row.get("date") or "") != ymd:
                continue

            service_id = str(
                row.get("service_id") or ""
            ).strip()

            exception_type = str(
                row.get("exception_type") or ""
            ).strip()

            if not service_id:
                continue

            if exception_type == "1":
                active.add(service_id)
            elif exception_type == "2":
                active.discard(service_id)

        return active

    service_dates = [
        not_before.date() - timedelta(days=1),
        not_before.date(),
    ]

    active_by_date = {
        day: active_services(day)
        for day in service_dates
    }

    all_active_services = set().union(
        *active_by_date.values()
    )

    active_trip_meta = {
        trip_id: meta
        for trip_id, meta in trip_meta.items()
        if str(meta.get("service_id") or "").strip()
        in all_active_services
    }

    if not active_trip_meta:
        return None

    used_stop_ids = {
        item["stop_id"]
        for trip_id, items in trip_stop_times.items()
        if trip_id in active_trip_meta
        for item in items
    }

    def nearest_stop(
        lat: float,
        lon: float,
    ) -> tuple[str, int] | None:
        best_id = None
        best_distance = None

        for stop_id in used_stop_ids:
            coords = stop_coords.get(stop_id)
            if not coords:
                continue

            distance = _distance_m(
                lat,
                lon,
                coords[0],
                coords[1],
            )

            if (
                best_distance is None
                or distance < best_distance
            ):
                best_id = stop_id
                best_distance = distance

        if (
            best_id is None
            or best_distance is None
            or best_distance > 500
        ):
            return None

        return best_id, int(round(best_distance))

    origin_match = nearest_stop(
        origin_lat,
        origin_lon,
    )
    dest_match = nearest_stop(
        dest_lat,
        dest_lon,
    )

    if not origin_match or not dest_match:
        return None

    origin_stop_id, origin_distance_m = origin_match
    dest_stop_id, dest_distance_m = dest_match

    if origin_stop_id == dest_stop_id:
        return None

    candidates: list[dict[str, Any]] = []

    for service_date, active_today in active_by_date.items():
        midnight = not_before.replace(
            year=service_date.year,
            month=service_date.month,
            day=service_date.day,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        for trip_id, meta in active_trip_meta.items():
            service_id = str(
                meta.get("service_id") or ""
            ).strip()

            if service_id not in active_today:
                continue

            items = sorted(
                trip_stop_times.get(trip_id) or [],
                key=lambda item: item["sequence"],
            )

            origins = [
                item
                for item in items
                if item["stop_id"] == origin_stop_id
            ]

            destinations = [
                item
                for item in items
                if item["stop_id"] == dest_stop_id
            ]

            for origin in origins:
                for dest in destinations:
                    if (
                        origin["sequence"]
                        >= dest["sequence"]
                    ):
                        continue

                    departure_s = (
                        origin["departure_s"]
                        if origin["departure_s"] is not None
                        else origin["arrival_s"]
                    )

                    arrival_s = (
                        dest["arrival_s"]
                        if dest["arrival_s"] is not None
                        else dest["departure_s"]
                    )

                    if (
                        departure_s is None
                        or arrival_s is None
                    ):
                        continue

                    departure_at = (
                        midnight
                        + timedelta(seconds=departure_s)
                    )

                    arrival_at = (
                        midnight
                        + timedelta(seconds=arrival_s)
                    )

                    if departure_at < not_before:
                        continue

                    if arrival_at < departure_at:
                        continue

                    candidates.append({
                        "line": wanted_line,
                        "route_id": str(
                            meta.get("route_id") or ""
                        ),
                        "service_id": service_id,
                        "trip_id": trip_id,
                        "headsign": str(
                            meta.get("trip_headsign") or ""
                        ),
                        "origin_stop_id": origin_stop_id,
                        "dest_stop_id": dest_stop_id,
                        "origin_distance_m": origin_distance_m,
                        "dest_distance_m": dest_distance_m,
                        "departure_at": departure_at,
                        "arrival_at": arrival_at,
                        "wait_seconds": int(
                            (
                                departure_at - not_before
                            ).total_seconds()
                        ),
                        "travel_seconds": int(
                            (
                                arrival_at - departure_at
                            ).total_seconds()
                        ),
                        "source": "gtfs_scheduled",
                    })

    if not candidates:
        return None

    candidates.sort(
        key=lambda candidate: (
            candidate["departure_at"],
            candidate["arrival_at"],
        )
    )

    return candidates[0]



# GTFS_SCHEDULE_HELPER_END


# ONE_TRANSFER_HELPER_START
def _atm_one_transfer_options(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    line_hints: list[str],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """
    Costruisce alternative con un solo cambio:
    prima linea di superficie -> interscambio -> metro.

    L'attesa della prima linea viene dal live ATM.
    Gli orari della metro sono programmati GTFS.
    """

    current = now or datetime.now()

    def stop_position(stop: dict[str, Any]) -> tuple[float, float] | None:
        loc = stop.get("Location") or {}
        try:
            return float(loc["Y"]), float(loc["X"])
        except Exception:
            return None

    def stop_code(stop: dict[str, Any]) -> str:
        return str(stop.get("Code") or "").strip()

    def stop_name(stop: dict[str, Any]) -> str:
        return str(
            stop.get("Description")
            or stop.get("Name")
            or "fermata"
        ).strip()

    first_lines: list[str] = []
    for raw in line_hints or []:
        line = _line_label(raw)
        if (
            line
            and not line.startswith("M")
            and line not in first_lines
        ):
            first_lines.append(line)

    options: list[dict[str, Any]] = []

    # Una sola scansione del grande stop_times.txt per tutte le linee
    # che potremo usare in questo calcolo.
    if first_lines:
        _gtfs_line_schedule_index(
            first_lines + ["M1", "M2", "M3", "M4", "M5"],
        )

    for first_line in first_lines:
        for direction in ("0", "1"):
            journey_pattern_id = f"{first_line}|{direction}"
            stops = _atm_journey_pattern_stops(journey_pattern_id)

            if not stops:
                continue

            origin_candidates: list[
                tuple[int, dict[str, Any], int]
            ] = []

            for idx, stop in enumerate(stops):
                pos = stop_position(stop)
                if not pos:
                    continue

                distance = _distance_m(
                    origin_lat,
                    origin_lon,
                    pos[0],
                    pos[1],
                )

                if distance <= 850:
                    origin_candidates.append(
                        (idx, stop, distance)
                    )

            if not origin_candidates:
                continue

            origin_candidates.sort(
                key=lambda x: x[2]
            )

            # Valutiamo le fermate di salita più plausibili.
            for origin_idx, board_stop, board_distance in origin_candidates[:3]:
                board_pos = stop_position(board_stop)
                if not board_pos:
                    continue

                board_code = stop_code(board_stop)

                live = _atm_live_minutes(
                    {
                        "atm_stop_code": board_code,
                        "lat": board_pos[0],
                        "lon": board_pos[1],
                    },
                    [first_line],
                )

                first_wait = str(
                    (live.get("arrivals") or {}).get(first_line)
                    or ""
                ).strip()

                first_wait_min = _minutes_value(first_wait)

                if first_wait_min is None:
                    continue

                first_wait_seconds = first_wait_min * 60

                origin_walk_seconds = int(
                    math.ceil(max(0, board_distance) / 80.0) * 60
                )
                board_ready_at = (
                    current
                    + timedelta(seconds=origin_walk_seconds)
                )
                live_departure_at = (
                    current
                    + timedelta(seconds=first_wait_seconds)
                )
                live_catchable = live_departure_at >= board_ready_at

                # Non scandiamo una linea intera senza limiti:
                # dodici fermate sono sufficienti per cercare
                # un interscambio locale senza moltiplicare le query.
                downstream = stops[
                    origin_idx + 1:
                    origin_idx + 13
                ]

                for transfer_offset, transfer_stop in enumerate(
                    downstream,
                    start=1,
                ):
                    transfer_idx = origin_idx + transfer_offset
                    transfer_pos = stop_position(transfer_stop)

                    if not transfer_pos:
                        continue

                    # L'interscambio metro viene scoperto dal GTFS
                    # locale: niente query remote per ogni fermata a valle.
                    second_lines: list[str] = []

                    for raw_second_line in _gtfs_subway_lines_near(
                        transfer_pos[0],
                        transfer_pos[1],
                        radius_m=250,
                    ):
                        second_line = _line_label(
                            raw_second_line
                        )

                        if (
                            second_line
                            and second_line != first_line
                            and second_line not in second_lines
                        ):
                            second_lines.append(second_line)

                    if not second_lines:
                        continue

                    # Per la prima tratta il GTFS serve esclusivamente
                    # per ricavare il tempo di marcia fra salita e cambio.
                    # La partenza effettiva è determinata dal live ATM.
                    first_schedule = _gtfs_next_departure(
                        first_line,
                        board_pos[0],
                        board_pos[1],
                        transfer_pos[0],
                        transfer_pos[1],
                        board_ready_at,
                    )

                    if not first_schedule:
                        continue

                    try:
                        first_travel_seconds = int(
                            first_schedule.get(
                                "travel_seconds"
                            )
                        )
                    except Exception:
                        continue

                    if first_travel_seconds < 0:
                        continue

                    if live_catchable:
                        first_departure_at = live_departure_at
                        effective_first_wait = first_wait
                        effective_first_wait_seconds = first_wait_seconds
                        first_wait_source = "atm_live"

                        transfer_arrival_at = (
                            first_departure_at
                            + timedelta(seconds=first_travel_seconds)
                        )
                    else:
                        first_departure_at = first_schedule.get(
                            "departure_at"
                        )
                        scheduled_arrival_at = first_schedule.get(
                            "arrival_at"
                        )

                        if not isinstance(first_departure_at, datetime):
                            continue

                        if first_departure_at < board_ready_at:
                            continue

                        if isinstance(scheduled_arrival_at, datetime):
                            transfer_arrival_at = scheduled_arrival_at
                        else:
                            transfer_arrival_at = (
                                first_departure_at
                                + timedelta(seconds=first_travel_seconds)
                            )

                        effective_first_wait_seconds = max(
                            0,
                            int(
                                (
                                    first_departure_at
                                    - board_ready_at
                                ).total_seconds()
                            ),
                        )
                        effective_first_wait = (
                            f"{round(effective_first_wait_seconds / 60)} min"
                        )
                        first_wait_source = str(
                            first_schedule.get("source")
                            or "gtfs_scheduled"
                        )

                    for second_line in second_lines:
                        # Prima query: individua la stazione GTFS effettiva
                        # e quindi la distanza fra fermata di superficie
                        # e accesso alla seconda linea.
                        second_probe = _gtfs_next_departure(
                            second_line,
                            transfer_pos[0],
                            transfer_pos[1],
                            dest_lat,
                            dest_lon,
                            transfer_arrival_at,
                        )

                        if not second_probe:
                            continue

                        try:
                            transfer_walk_m = max(
                                0,
                                int(second_probe.get("origin_distance_m") or 0),
                            )
                        except Exception:
                            transfer_walk_m = 0

                        transfer_walk_seconds = int(
                            math.ceil(transfer_walk_m / 80.0) * 60
                        )

                        metro_ready_at = (
                            transfer_arrival_at
                            + timedelta(seconds=transfer_walk_seconds)
                        )

                        # Se serve camminare per raggiungere la seconda linea,
                        # dobbiamo cercare nuovamente la prima corsa prendibile
                        # dall'istante in cui siamo davvero alla banchina.
                        if transfer_walk_seconds > 0:
                            second_schedule = _gtfs_next_departure(
                                second_line,
                                transfer_pos[0],
                                transfer_pos[1],
                                dest_lat,
                                dest_lon,
                                metro_ready_at,
                            )
                        else:
                            second_schedule = second_probe

                        if not second_schedule:
                            continue

                        departure_at = second_schedule.get(
                            "departure_at"
                        )
                        arrival_at = second_schedule.get(
                            "arrival_at"
                        )

                        if not isinstance(
                            departure_at,
                            datetime,
                        ):
                            continue

                        if not isinstance(
                            arrival_at,
                            datetime,
                        ):
                            continue

                        if departure_at < metro_ready_at:
                            continue

                        try:
                            final_walk_m = max(
                                0,
                                int(
                                    second_schedule.get("dest_distance_m")
                                    if second_schedule.get("dest_distance_m") is not None
                                    else second_probe.get("dest_distance_m") or 0
                                ),
                            )
                        except Exception:
                            final_walk_m = 0

                        final_walk_seconds = int(
                            math.ceil(final_walk_m / 80.0) * 60
                        )

                        destination_arrival_at = (
                            arrival_at
                            + timedelta(seconds=final_walk_seconds)
                        )

                        eta_seconds = int(
                            (
                                destination_arrival_at - current
                            ).total_seconds()
                        )

                        if eta_seconds < 0:
                            continue

                        options.append({
                            "first_line": first_line,
                            "first_direction": direction,
                            "first_journey_pattern_id": journey_pattern_id,

                            "origin_stop_code": board_code,
                            "origin_stop_name": stop_name(board_stop),
                            "origin_distance_m": int(
                                round(board_distance)
                            ),
                            "origin_idx": origin_idx,

                            "transfer_stop_code": stop_code(
                                transfer_stop
                            ),
                            "transfer_stop_name": stop_name(
                                transfer_stop
                            ),
                            "transfer_idx": transfer_idx,
                            "transfer_lat": transfer_pos[0],
                            "transfer_lon": transfer_pos[1],

                            "second_line": second_line,

                            "origin_walk_seconds": origin_walk_seconds,
                            "board_ready_at": board_ready_at,

                            "first_wait": effective_first_wait,
                            "first_wait_seconds": effective_first_wait_seconds,
                            "first_wait_source": first_wait_source,
                            "first_departure_at": first_departure_at,

                            "first_travel_seconds": first_travel_seconds,
                            "first_duration_source": "gtfs_scheduled",

                            "transfer_arrival_at": transfer_arrival_at,

                            "transfer_walk_m": transfer_walk_m,
                            "transfer_walk_seconds": transfer_walk_seconds,
                            "metro_ready_at": metro_ready_at,

                            "second_departure_at": departure_at,
                            "second_arrival_at": arrival_at,
                            "second_wait_seconds": int(
                                (
                                    departure_at
                                    - metro_ready_at
                                ).total_seconds()
                            ),
                            "second_wait_source": str(
                                second_schedule.get("source")
                                or "gtfs_scheduled"
                            ),

                            "final_walk_m": final_walk_m,
                            "final_walk_seconds": final_walk_seconds,
                            "destination_arrival_at": destination_arrival_at,
                            "eta_seconds": eta_seconds,

                            "source": (
                                "ATM live + GTFS scheduled"
                            ),
                        })

    options.sort(
        key=lambda item: (
            (
                int(item["eta_seconds"])
                if item.get("eta_seconds") is not None
                else 10**9
            ),
            (
                int(item["origin_distance_m"])
                if item.get("origin_distance_m") is not None
                else 10**9
            ),
            str(item.get("first_line") or ""),
            str(item.get("second_line") or ""),
        )
    )

    return options


# ONE_TRANSFER_HELPER_END

def _official_trip_has_transit(trip_plan: dict[str, Any] | None) -> bool:
    """Scarta solo i percorsi ATM ufficiali composti davvero solo da cammino."""
    if not isinstance(trip_plan, dict):
        return False

    summary = (trip_plan or {}).get("summary") or {}
    if summary.get("lines") or summary.get("first_line") or summary.get("board_stop_code"):
        return True

    raw = (trip_plan or {}).get("raw") or {}
    actions = raw.get("Actions") or raw.get("actions") or []
    if not isinstance(actions, list):
        return False

    saw_leg = False
    for action in actions:
        if not isinstance(action, dict):
            continue
        leg = action.get("Leg") or action.get("leg") or {}
        if not isinstance(leg, dict):
            continue

        saw_leg = True

        journeys = leg.get("Journeys") or leg.get("journeys") or []
        if isinstance(journeys, list) and journeys:
            return True

        try:
            mode = int(leg.get("TravelMode", leg.get("travelMode", -1)))
        except Exception:
            mode = -1

        # In ATM/GiroMilano il modo 0 è il tratto a piedi.
        # Qualunque altra Leg è trasporto, anche se non siamo riusciti a estrarre la linea.
        if mode != 0 and mode != -1:
            return True

    return False if saw_leg else bool(summary.get("steps") and not summary.get("walk_m"))


def _official_trip_is_direct(trip_plan: dict[str, Any] | None) -> bool:
    """True quando GiroMilano propone una sola salita su un mezzo pubblico."""
    if not _official_trip_has_transit(trip_plan):
        return False

    raw = (trip_plan or {}).get("raw") or {}
    actions = raw.get("Actions") or raw.get("actions") or []
    if not isinstance(actions, list):
        return False

    boardings = 0
    for action in actions:
        if not isinstance(action, dict):
            continue
        leg = action.get("Leg") or action.get("leg") or {}
        if not isinstance(leg, dict):
            continue
        journeys = leg.get("Journeys") or leg.get("journeys") or []
        if isinstance(journeys, list) and journeys:
            boardings += 1

    return boardings == 1


# ATM_ROUTE_RANKING_HELPER_START
def _rank_atm_route_candidates(
    official_trip: dict[str, Any] | None,
    one_transfer_options: list[dict[str, Any]] | None,
    *,
    direct_options: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """
    Ordina esclusivamente candidati con ETA numerico confrontabile.
    A parità esatta di ETA preferisce l'itinerario ufficiale ATM.
    """

    ranked: list[dict[str, Any]] = []

    if official_trip:
        summary = official_trip.get("summary") or {}
        eta = summary.get("eta_seconds")

        if eta is None:
            duration = summary.get("duration")
            try:
                duration_minutes = float(
                    str(duration).strip().replace(",", ".")
                )
            except (TypeError, ValueError):
                duration_minutes = -1.0

            if duration_minutes >= 0:
                eta = int(round(duration_minutes * 60))

        has_transit = (
            _official_trip_has_transit(official_trip)
            or bool(summary.get("lines"))
            or bool(summary.get("first_line"))
        )

        if has_transit and eta is not None:
            try:
                eta_seconds = int(eta)
            except Exception:
                eta_seconds = -1

            if eta_seconds >= 0:
                ranked.append({
                    "kind": "official_atm_trip",
                    "eta_seconds": eta_seconds,
                    "option": official_trip,
                })

    for option in direct_options or []:
        eta = option.get("eta_seconds")

        if eta is None:
            continue

        try:
            eta_seconds = int(eta)
        except Exception:
            continue

        if eta_seconds < 0:
            continue

        ranked.append({
            "kind": "direct_atm",
            "eta_seconds": eta_seconds,
            "option": option,
        })

    for option in one_transfer_options or []:
        eta = option.get("eta_seconds")

        if eta is None:
            continue

        try:
            eta_seconds = int(eta)
        except Exception:
            continue

        if eta_seconds < 0:
            continue

        ranked.append({
            "kind": "one_transfer",
            "eta_seconds": eta_seconds,
            "option": option,
        })

    ranked.sort(
        key=lambda candidate: (
            candidate["eta_seconds"],
            {
                "official_atm_trip": 0,
                "direct_atm": 1,
                "one_transfer": 2,
            }.get(candidate["kind"], 9),
        )
    )

    return ranked



# LOCAL_ATM_REALTIME_ROUTER_START

def _atm_wait_seconds(value: Any) -> int | None:
    if value is None:
        return None

    text = str(value).strip().lower()

    if not text:
        return None

    if text in {
        "in arrivo",
        "arrivo",
        "in fermata",
    }:
        return 0

    match = re.search(r"(\d+)", text)

    if not match:
        return None

    return int(match.group(1)) * 60


_ATM_DIRECT_TOPOLOGY_CACHE: tuple[tuple[str, int, int], dict[str, Any]] | None = None


def _load_atm_direct_topology() -> dict[str, Any] | None:
    global _ATM_DIRECT_TOPOLOGY_CACHE

    path = ATM_DIRECT_TOPOLOGY_PATH
    if not path.is_file():
        return None

    try:
        stat = path.stat()
    except OSError:
        return None

    key = (str(path.resolve()), int(stat.st_mtime_ns), int(stat.st_size))
    if _ATM_DIRECT_TOPOLOGY_CACHE and _ATM_DIRECT_TOPOLOGY_CACHE[0] == key:
        return _ATM_DIRECT_TOPOLOGY_CACHE[1]

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

    if not isinstance(payload, dict) or not isinstance(payload.get("patterns"), list):
        return None

    _ATM_DIRECT_TOPOLOGY_CACHE = (key, payload)
    return payload


def _topology_direct_candidates(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    payload = _load_atm_direct_topology()
    if not payload:
        return []

    best_by_line: dict[str, dict[str, Any]] = {}

    for pattern in payload.get("patterns") or []:
        if not isinstance(pattern, dict):
            continue

        line = _line_label(pattern.get("line"))
        direction = str(pattern.get("direction") or "").strip()
        stops = pattern.get("stops") or []
        if not line or not isinstance(stops, list) or len(stops) < 2:
            continue

        origin_points: list[tuple[int, int, dict[str, Any]]] = []
        dest_points: list[tuple[int, int, dict[str, Any]]] = []

        for index, stop in enumerate(stops):
            if not isinstance(stop, dict):
                continue
            try:
                slat = float(stop["lat"])
                slon = float(stop["lon"])
            except (KeyError, TypeError, ValueError):
                continue

            origin_distance = _distance_m(origin_lat, origin_lon, slat, slon)
            if origin_distance <= ATM_DIRECT_ORIGIN_RADIUS_M:
                origin_points.append((index, origin_distance, stop))

            dest_distance = _distance_m(dest_lat, dest_lon, slat, slon)
            if dest_distance <= ATM_DIRECT_DEST_RADIUS_M:
                dest_points.append((index, dest_distance, stop))

        pattern_best: dict[str, Any] | None = None

        for origin_index, origin_distance, origin_stop in origin_points:
            for dest_index, dest_distance, dest_stop in dest_points:
                if origin_index >= dest_index:
                    continue

                stops_count = dest_index - origin_index
                if stops_count > ATM_DIRECT_MAX_STOPS:
                    continue

                departure_s = origin_stop.get("departure_s")
                if departure_s is None:
                    departure_s = origin_stop.get("arrival_s")
                arrival_s = dest_stop.get("arrival_s")
                if arrival_s is None:
                    arrival_s = dest_stop.get("departure_s")

                travel_seconds = None
                try:
                    travel_value = int(arrival_s) - int(departure_s)
                    if 0 < travel_value <= 3 * 3600:
                        travel_seconds = travel_value
                except (TypeError, ValueError):
                    pass

                access_m = int(origin_distance + dest_distance)
                candidate = {
                    "line": line,
                    "direction": direction,
                    "origin_stop_code": str(origin_stop.get("id") or "").strip(),
                    "origin_stop_name": str(origin_stop.get("name") or "fermata").strip(),
                    "origin_stop_lat": float(origin_stop["lat"]),
                    "origin_stop_lon": float(origin_stop["lon"]),
                    "origin_distance_m": int(origin_distance),
                    "dest_stop_code": str(dest_stop.get("id") or "").strip(),
                    "dest_stop_name": str(dest_stop.get("name") or "fermata").strip(),
                    "dest_stop_lat": float(dest_stop["lat"]),
                    "dest_stop_lon": float(dest_stop["lon"]),
                    "dest_distance_m": int(dest_distance),
                    "access_m": access_m,
                    "stops_count": stops_count,
                    "travel_seconds": travel_seconds,
                    "topology_feed_version": str(payload.get("feed_version") or ""),
                    "topology_surface_end_date": str(payload.get("surface_end_date") or ""),
                }

                if pattern_best is None:
                    pattern_best = candidate
                    continue

                old_key = (
                    int(pattern_best["access_m"]),
                    int(pattern_best["stops_count"]),
                    int(pattern_best.get("travel_seconds") or 10**9),
                )
                new_key = (
                    access_m,
                    stops_count,
                    int(travel_seconds or 10**9),
                )
                if new_key < old_key:
                    pattern_best = candidate

        if pattern_best is None:
            continue

        old = best_by_line.get(line)
        if old is None:
            best_by_line[line] = pattern_best
            continue

        old_key = (
            int(old["access_m"]),
            int(old["stops_count"]),
            int(old.get("travel_seconds") or 10**9),
        )
        new_key = (
            int(pattern_best["access_m"]),
            int(pattern_best["stops_count"]),
            int(pattern_best.get("travel_seconds") or 10**9),
        )
        if new_key < old_key:
            best_by_line[line] = pattern_best

    ranked = sorted(
        best_by_line.values(),
        key=lambda item: (
            int(item["access_m"]),
            int(item["stops_count"]),
            int(item.get("travel_seconds") or 10**9),
            str(item["line"]),
        ),
    )
    if not ranked:
        return []

    best_access = int(ranked[0]["access_m"])
    useful = [
        item
        for item in ranked
        if int(item["access_m"]) <= best_access + ATM_DIRECT_ACCESS_SLACK_M
    ]

    return useful[: max(1, int(limit or ATM_DIRECT_CANDIDATE_LIMIT))]


def _legacy_stop_snapshot(stop_code: str, lines: list[str]) -> dict[str, Any]:
    data = _browser_fetch_json(
        "tpl/stops/" + urllib.parse.quote(stop_code, safe="") + "/linesummary"
    )
    wanted = {str(line) for line in lines}
    arrivals: dict[str, str] = {}
    observations: list[dict[str, str]] = []

    for row in (data or {}).get("Lines") or []:
        if not isinstance(row, dict):
            continue
        line_obj = row.get("Line") or {}
        if not isinstance(line_obj, dict):
            continue
        line = _line_label(line_obj.get("LineCode") or line_obj.get("LineId"))
        wait = str(row.get("WaitMessage") or "").strip()
        if not line or not wait or (wanted and line not in wanted):
            continue
        direction = str(row.get("Direction") if row.get("Direction") is not None else "").strip()
        observations.append({
            "line": line,
            "direction": direction,
            "journey_pattern_id": str(row.get("JourneyPatternId") or "").strip(),
            "wait": wait,
        })
        previous = arrivals.get(line)
        if previous is None:
            arrivals[line] = wait
        else:
            old_s = _atm_wait_seconds(previous)
            new_s = _atm_wait_seconds(wait)
            if new_s is not None and (old_s is None or new_s < old_s):
                arrivals[line] = wait

    return {
        "ok": bool(arrivals),
        "status": "ok" if arrivals else "not_available",
        "stop_code": stop_code,
        "arrivals": arrivals,
        "observations": observations,
        "source": "atm_tpportal_linesummary",
    }


def _topology_direct_options(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    candidates = _topology_direct_candidates(
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        limit=limit,
    )
    if not candidates:
        return []

    provider = _ATM_REALTIME_PROVIDER.get()
    grouped: dict[str, list[str]] = {}
    for candidate in candidates:
        code = str(candidate.get("origin_stop_code") or "").strip()
        line = str(candidate.get("line") or "").strip()
        if code and line:
            grouped.setdefault(code, []).append(line)

    def fetch(item: tuple[str, list[str]]) -> tuple[str, dict[str, Any]]:
        code, lines = item
        unique_lines = sorted(set(lines))
        try:
            if provider is not None:
                payload = provider.snapshot(code, unique_lines)
                if isinstance(payload, dict):
                    return code, dict(payload)
            return code, _legacy_stop_snapshot(code, unique_lines)
        except Exception:
            return code, {
                "ok": False,
                "status": "not_available",
                "arrivals": {},
                "observations": [],
                "source": "atm_realtime_unavailable",
            }

    snapshots: dict[str, dict[str, Any]] = {}
    items = list(grouped.items())
    batch_method = getattr(provider, "batch", None) if provider is not None else None
    if items and callable(batch_method):
        queries = [
            {
                "stop_code": code,
                "lines": sorted(set(lines)),
            }
            for code, lines in items
        ]
        try:
            batch_payload = batch_method(queries)
        except Exception:
            batch_payload = {}
        if isinstance(batch_payload, dict):
            for code, payload in batch_payload.items():
                if isinstance(payload, dict):
                    snapshots[str(code)] = dict(payload)

    missing_items = [
        item for item in items if item[0] not in snapshots
    ]
    if len(missing_items) == 1:
        code, payload = fetch(missing_items[0])
        snapshots[code] = payload
    elif missing_items:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(3, len(missing_items))
        ) as pool:
            for code, payload in pool.map(fetch, missing_items):
                snapshots[code] = payload

    def matching_wait(candidate: dict[str, Any]) -> str:
        code = str(candidate.get("origin_stop_code") or "")
        line = str(candidate.get("line") or "")
        direction = str(candidate.get("direction") or "")
        payload = snapshots.get(code) or {}
        exact: list[str] = []
        same_line: list[str] = []

        for observation in payload.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            if _line_label(observation.get("line")) != line:
                continue
            wait = str(observation.get("wait") or "").strip()
            if not wait:
                continue
            same_line.append(wait)
            obs_direction = str(observation.get("direction") or "").strip()
            if direction and obs_direction == direction:
                exact.append(wait)

        choices = exact or same_line
        if choices:
            choices.sort(key=lambda value: _atm_wait_seconds(value) if _atm_wait_seconds(value) is not None else 10**9)
            return choices[0]

        arrivals = payload.get("arrivals") or {}
        return str(arrivals.get(line) or "").strip() if isinstance(arrivals, dict) else ""

    now = datetime.now()
    for candidate in candidates:
        wait = matching_wait(candidate)
        wait_seconds = _atm_wait_seconds(wait)
        origin_walk_seconds = int(math.ceil(max(0, int(candidate["origin_distance_m"])) / 1.33))
        final_walk_seconds = int(math.ceil(max(0, int(candidate["dest_distance_m"])) / 1.33))
        travel_seconds = candidate.get("travel_seconds")
        try:
            travel_seconds = int(travel_seconds) if travel_seconds is not None else None
        except (TypeError, ValueError):
            travel_seconds = None

        catchable = (
            wait_seconds is not None
            and wait_seconds >= origin_walk_seconds
        )
        eta_seconds = (
            int(wait_seconds + travel_seconds + final_walk_seconds)
            if catchable and travel_seconds is not None
            else None
        )

        candidate["wait"] = wait or "n/d"
        candidate["wait_seconds"] = wait_seconds
        candidate["wait_source"] = (
            "atm_live"
            if catchable
            else "atm_live_too_soon"
            if wait_seconds is not None
            else "not_available"
        )
        candidate["origin_walk_seconds"] = origin_walk_seconds
        candidate["final_walk_m"] = int(candidate["dest_distance_m"])
        candidate["final_walk_seconds"] = final_walk_seconds
        candidate["catchable"] = catchable
        candidate["eta_seconds"] = eta_seconds
        candidate["departure_at"] = (
            now + timedelta(seconds=wait_seconds)
            if catchable and wait_seconds is not None
            else None
        )
        candidate["destination_arrival_at"] = (
            now + timedelta(seconds=eta_seconds)
            if eta_seconds is not None
            else None
        )
        candidate["source"] = "GTFS topology + ATM realtime"

    candidates.sort(
        key=lambda item: (
            0 if item.get("catchable") and item.get("eta_seconds") is not None else 1,
            int(item.get("eta_seconds") or 10**9),
            int(item.get("access_m") or 10**9),
            str(item.get("line") or ""),
        )
    )
    return candidates[: max(1, int(limit or ATM_DIRECT_CANDIDATE_LIMIT))]


def _local_atm_router_json(
    args: list[str],
    *,
    timeout_s: float = 3.0,
) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [str(ATM_LOCAL_ROUTER_BIN), *args],
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ):
        return None

    if completed.returncode != 0:
        return None

    try:
        payload = json.loads(completed.stdout)
    except (TypeError, ValueError):
        return None

    if not isinstance(payload, dict):
        return None

    if payload.get("status") != "ok":
        return None

    return payload


def _local_atm_realtime_route(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
) -> dict[str, Any] | None:
    if (
        not ATM_LOCAL_ROUTER_BIN.is_file()
        or not ATM_LOCAL_ROUTER_GRAPH.is_file()
    ):
        return None

    nearby = _local_atm_router_json(
        [
            "--nearby",
            str(ATM_LOCAL_ROUTER_GRAPH),
            str(origin_lat),
            str(origin_lon),
            str(ATM_LOCAL_ROUTER_RADIUS_M),
            str(ATM_LOCAL_ROUTER_STOP_LIMIT),
        ],
        timeout_s=2.0,
    )

    if not nearby:
        return None

    try:
        graph_day = int(
            nearby.get("service_date") or 0
        )
        today = int(
            datetime.now().strftime("%Y%m%d")
        )
    except Exception:
        return None

    # Non utilizziamo silenziosamente un grafo di un altro giorno.
    if graph_day != today:
        return None

    stops = nearby.get("stops") or []

    if not isinstance(stops, list) or not stops:
        return None

    observations: list[dict[str, Any]] = []
    queried_stop_ids: set[str] = set()

    def observe_stop(stop_id: str) -> int:
        stop_id = str(stop_id or "").strip()

        if not stop_id or stop_id in queried_stop_ids:
            return 0

        snapshot = _atm_realtime_snapshot(stop_id)

        if snapshot is not None:
            observed_at = time.time()
            added = 0

            for row in snapshot.get("observations") or []:
                if not isinstance(row, dict):
                    continue

                line = str(
                    row.get("line") or ""
                ).strip()

                direction = str(
                    row.get("direction") or ""
                ).strip()

                wait_seconds = _atm_wait_seconds(
                    row.get("wait")
                )

                if (
                    not line
                    or direction not in {"0", "1"}
                    or wait_seconds is None
                ):
                    continue

                observations.append({
                    "stop_id": stop_id,
                    "line": line,
                    "direction": direction,
                    "wait_seconds": wait_seconds,
                    "observed_at": observed_at,
                })
                added += 1

            queried_stop_ids.add(stop_id)
            return added

        # Legacy diretto solo se nessun realtime provider MCP
        # è stato iniettato.
        path = (
            "tpl/stops/"
            + urllib.parse.quote(stop_id)
            + "/linesummary"
        )

        # Il TPPortal può occasionalmente restituire Lines vuoto
        # anche per una fermata che pochi istanti dopo espone il
        # realtime. Ritentiamo brevemente, senza trasformare mai
        # GTFS o altri dati in falso realtime.
        for attempt in range(3):
            try:
                data = _browser_fetch_json(path)
            except Exception:
                data = None

            observed_at = time.time()
            added = 0

            if isinstance(data, dict):
                for row in data.get("Lines") or []:
                    if not isinstance(row, dict):
                        continue

                    line_obj = row.get("Line") or {}

                    if not isinstance(line_obj, dict):
                        continue

                    line = str(
                        line_obj.get("LineCode")
                        or line_obj.get("LineId")
                        or ""
                    ).strip()

                    raw_direction = row.get("Direction")

                    direction = (
                        str(raw_direction).strip()
                        if raw_direction is not None
                        else ""
                    )

                    wait_seconds = _atm_wait_seconds(
                        row.get("WaitMessage")
                    )

                    if (
                        not line
                        or direction not in {"0", "1"}
                        or wait_seconds is None
                    ):
                        continue

                    observations.append(
                        {
                            "stop_id": stop_id,
                            "line": line,
                            "direction": direction,
                            "wait_seconds": wait_seconds,
                            "observed_at": observed_at,
                        }
                    )
                    added += 1

            if added:
                queried_stop_ids.add(stop_id)
                return added

            if attempt < 2:
                time.sleep(0.15)

        # Dopo tre tentativi consideriamo conclusa questa
        # interrogazione: nessun dato viene inventato.
        queried_stop_ids.add(stop_id)
        return 0

    # Prima fase: realtime delle fermate raggiungibili a piedi
    # dall'origine, usato per scegliere la prima salita.
    for stop in stops:
        if not isinstance(stop, dict):
            continue

        observe_stop(
            str(stop.get("stop_id") or "")
        )

    # Anche in assenza di realtime di superficie la
    # metropolitana deve poter competere tramite GTFS.
    # Le osservazioni, quando presenti, restano necessarie per
    # validare le prime salite di superficie.

    def observation_key(
        observation: dict[str, Any],
    ) -> tuple[str, str, str]:
        return (
            str(observation["stop_id"]),
            str(observation["line"]),
            str(observation["direction"]),
        )

    # Ogni possibile prima salita viene valutata in modo
    # indipendente. Due fermate diverse della stessa linea non
    # devono condividere lo stesso stato shortest-path del C,
    # altrimenti un accesso pedonale peggiore può oscurarne uno
    # migliore.
    initial_candidate_keys: list[
        tuple[str, str, str]
    ] = []

    seen_initial_keys: set[
        tuple[str, str, str]
    ] = set()

    for observation in observations:
        key = observation_key(observation)

        if key in seen_initial_keys:
            continue

        seen_initial_keys.add(key)
        initial_candidate_keys.append(key)

    def build_live_args(
        allowed_keys: set[tuple[str, str, str]],
    ) -> tuple[str, list[str]]:
        route_wall = time.time()

        # Una sola osservazione per stop+linea+direzione.
        # Se ATM restituisce duplicati conserviamo l'arrivo
        # effettivo più vicino.
        live_by_key: dict[
            tuple[str, str, str],
            int,
        ] = {}

        for observation in observations:
            key = observation_key(observation)

            if key not in allowed_keys:
                continue

            age_seconds = max(
                0,
                int(
                    round(
                        route_wall
                        - float(
                            observation["observed_at"]
                        )
                    )
                ),
            )

            effective_wait = max(
                0,
                int(observation["wait_seconds"])
                - age_seconds,
            )

            previous = live_by_key.get(key)

            if (
                previous is None
                or effective_wait < previous
            ):
                live_by_key[key] = effective_wait

        route_time = datetime.now().strftime(
            "%H:%M:%S"
        )

        live_args: list[str] = []

        for (
            stop_id,
            line,
            direction,
        ), wait_seconds in sorted(
            live_by_key.items()
        ):
            live_args.extend(
                [
                    "--live",
                    stop_id,
                    line,
                    direction,
                    str(wait_seconds),
                ]
            )

        return route_time, live_args

    candidate_routes: list[dict[str, Any]] = []

    # Le linee metro vengono valutate separatamente dai
    # candidati di superficie. La prima salita è vincolata
    # esplicitamente alla linea metro, quindi i live di
    # superficie non possono oscurarla.
    #
    # Per eventuali salite successive continuiamo comunque a
    # interrogare ATM e a passare il realtime disponibile.
    for subway_route in ("M1", "M2", "M3", "M4", "M5"):
        allowed_keys: set[
            tuple[str, str, str]
        ] = set()

        candidate_route: dict[str, Any] | None = None

        for _round in range(4):
            route_time, live_args = build_live_args(
                allowed_keys
            )

            args = [
                "--route",
                str(ATM_LOCAL_ROUTER_GRAPH),
                str(origin_lat),
                str(origin_lon),
                str(dest_lat),
                str(dest_lon),
                route_time,
                "--first-route",
                subway_route,
                *live_args,
            ]

            route = _local_atm_router_json(
                args,
                timeout_s=4.0,
            )

            if not route:
                candidate_route = None
                break

            legs = route.get("legs") or []

            transit_legs = [
                leg
                for leg in legs
                if (
                    isinstance(leg, dict)
                    and leg.get("mode") == "transit"
                )
            ]

            if not transit_legs:
                candidate_route = None
                break

            first_leg = transit_legs[0]

            # --first-route deve restare una garanzia forte:
            # questo candidato rappresenta esclusivamente la
            # specifica linea metro richiesta.
            if (
                str(
                    first_leg.get("route") or ""
                ).strip()
                != subway_route
                or first_leg.get("live") is True
            ):
                candidate_route = None
                break

            candidate_route = route

            downstream_stop_ids: list[str] = []

            for leg in transit_legs[1:]:
                stop_id = str(
                    leg.get("from_stop_id") or ""
                ).strip()

                if stop_id:
                    downstream_stop_ids.append(stop_id)

            if not downstream_stop_ids:
                break

            before_keys = set(allowed_keys)

            for stop_id in dict.fromkeys(
                downstream_stop_ids
            ):
                if stop_id not in queried_stop_ids:
                    observe_stop(stop_id)

                for observation in observations:
                    if (
                        str(
                            observation.get("stop_id") or ""
                        ).strip()
                        != stop_id
                    ):
                        continue

                    allowed_keys.add(
                        observation_key(observation)
                    )

            if allowed_keys == before_keys:
                break

        if candidate_route is not None:
            candidate_routes.append(candidate_route)

    # Le prime salite di superficie restano isolate una per
    # volta e devono continuare a corrispondere a un live ATM.
    for first_key in initial_candidate_keys:
        allowed_keys = {first_key}

        candidate_route: dict[str, Any] | None = None

        # Primo calcolo + massimo tre refresh delle successive
        # fermate di salita.
        for _round in range(4):
            route_time, live_args = build_live_args(
                allowed_keys
            )

            if not live_args:
                candidate_route = None
                break

            args = [
                "--route",
                str(ATM_LOCAL_ROUTER_GRAPH),
                str(origin_lat),
                str(origin_lon),
                str(dest_lat),
                str(dest_lon),
                route_time,
                *live_args,
            ]

            route = _local_atm_router_json(
                args,
                timeout_s=4.0,
            )

            # Se un refresh realtime rende impossibile il
            # candidato, non conserviamo la vecchia versione
            # calcolata solamente con GTFS.
            if not route:
                candidate_route = None
                break

            legs = route.get("legs") or []

            transit_legs = [
                leg
                for leg in legs
                if (
                    isinstance(leg, dict)
                    and leg.get("mode") == "transit"
                )
            ]

            if not transit_legs:
                candidate_route = None
                break

            first_leg = transit_legs[0]

            # Il run appartiene a questa specifica prima salita.
            # Le osservazioni aggiunte per gli interscambi non
            # possono trasformarlo silenziosamente in un'altra
            # partenza.
            if (
                str(
                    first_leg.get("from_stop_id") or ""
                ).strip()
                != first_key[0]
                or str(
                    first_leg.get("route") or ""
                ).strip()
                != first_key[1]
                or first_leg.get("live") is not True
            ):
                candidate_route = None
                break

            candidate_route = route

            # La prima fermata è già osservata. Per ogni salita
            # successiva recuperiamo il realtime ATM e lo
            # aggiungiamo soltanto a questo candidato.
            downstream_stop_ids: list[str] = []

            for leg in transit_legs[1:]:
                stop_id = str(
                    leg.get("from_stop_id") or ""
                ).strip()

                if stop_id:
                    downstream_stop_ids.append(stop_id)

            if not downstream_stop_ids:
                break

            before_keys = set(allowed_keys)

            for stop_id in dict.fromkeys(
                downstream_stop_ids
            ):
                if stop_id not in queried_stop_ids:
                    observe_stop(stop_id)

                # Riutilizziamo anche un'osservazione già fatta
                # da un candidato precedente.
                for observation in observations:
                    if (
                        str(
                            observation.get("stop_id") or ""
                        ).strip()
                        != stop_id
                    ):
                        continue

                    allowed_keys.add(
                        observation_key(observation)
                    )

            # ATM non ha fornito nuovo realtime utilizzabile:
            # il candidato già calcolato resta valido.
            if allowed_keys == before_keys:
                break

        if candidate_route is not None:
            candidate_routes.append(candidate_route)

    if not candidate_routes:
        return None

    def route_rank(
        candidate: dict[str, Any],
    ) -> tuple[int, int, int]:
        def number_value(value: Any) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return 2**31 - 1

        legs = candidate.get("legs") or []

        intermediate_walk = 0
        boardings = 0

        for leg in legs:
            if not isinstance(leg, dict):
                continue

            if leg.get("mode") == "walk":
                value = number_value(
                    leg.get("walk_seconds")
                )

                if value != 2**31 - 1:
                    intermediate_walk += value

            elif leg.get("mode") == "transit":
                boardings += 1

        total_walk = (
            number_value(
                candidate.get("origin_walk_seconds")
            )
            + intermediate_walk
            + number_value(
                candidate.get("final_walk_seconds")
            )
        )

        # Stesso criterio lessicografico del router C:
        #   1. arrivo assoluto
        #   2. cammino totale
        #   3. numero di salite/cambi
        #
        # Non usiamo total_seconds: candidati calcolati pochi
        # istanti dopo avrebbero artificialmente un totale più
        # basso pur arrivando alla stessa ora.
        return (
            number_value(candidate.get("arrival_s")),
            total_walk,
            boardings,
        )

    return min(candidate_routes, key=route_rank)


# LOCAL_ATM_REALTIME_ROUTER_END


# ATM_ROUTE_RANKING_HELPER_END

def _build_plan_impl(lat: float, lon: float, destination_name: str) -> dict[str, Any]:
    dest = _resolve_destination(destination_name)
    if not dest:
        raise HTTPException(status_code=404, detail="destination_not_found")
    dlat, dlon = float(dest["lat"]), float(dest["lon"])

    # LOCAL_ATM_REALTIME_BUILD_PLAN_START
    local_atm_route = _local_atm_realtime_route(
        lat,
        lon,
        dlat,
        dlon,
    )

    if local_atm_route:
        return {
            "route_mode": "local_atm_realtime",
            "route_confidence": "high",
            "needs_official_route_lookup": False,
            "destination": dest,
            "origin": {
                "lat": lat,
                "lon": lon,
            },
            "local_atm_route": local_atm_route,
            "osm_route_url": _maps_link(
                lat,
                lon,
                dlat,
                dlon,
            ),
            "atm_nearby_url": _atm_link(
                lat,
                lon,
            ),
            "official_route_url": _atm_link(
                lat,
                lon,
            ),
            "source": "ATM realtime + router locale GTFS",
            "generated_at": int(time.time()),
        }
    # LOCAL_ATM_REALTIME_BUILD_PLAN_END

    short_destination = _distance_m(lat, lon, dlat, dlon) <= 1800
    direct_topology_options = (
        _topology_direct_options(
            lat,
            lon,
            dlat,
            dlon,
            limit=ATM_DIRECT_CANDIDATE_LIMIT,
        )
        if short_destination
        else []
    )

    # Se la topologia locale produce più dirette, oppure una sola diretta
    # con ETA completo e realtime prendibile, il trip planner remoto non
    # aggiunge informazione utile. Nessun nome di luogo o linea è codificato.
    topology_fast_path = (
        len(direct_topology_options) >= 2
        or (
            len(direct_topology_options) == 1
            and direct_topology_options[0].get("eta_seconds") is not None
            and direct_topology_options[0].get("wait_source") == "atm_live"
        )
    )
    if topology_fast_path:
        return {
            "destination": dest,
            "route_mode": "direct_atm",
            "route_confidence": "high",
            "needs_official_route_lookup": False,
            "origin": {"lat": lat, "lon": lon},
            "direct_atm_route": direct_topology_options[0],
            "direct_atm_options": direct_topology_options,
            "official_route_url": _atm_link(lat, lon),
            "atm_nearby_url": _atm_link(lat, lon),
            "source": "GTFS topology + ATM realtime",
            "generated_at": int(time.time()),
        }

    official_label = str(dest.get("label") or dest.get("name") or destination_name)
    trip_t0 = time.monotonic()
    trip_plan = _atm_trip_plan(lat, lon, dlat, dlon, official_label)
    if (
        not short_destination
        and (not trip_plan or not _official_trip_has_transit(trip_plan))
        and (time.monotonic() - trip_t0) < 8.0
    ):
        time.sleep(0.3)
        trip_plan = _atm_trip_plan(lat, lon, dlat, dlon, official_label)

    if trip_plan and _official_trip_has_transit(trip_plan):
        # Un itinerario ufficiale con una sola salita e realtime già
        # disponibile non ha bisogno della costosissima esplorazione
        # di fermate/linee alternative e delle relative chiamate HTTP seriali.
        if _official_trip_is_direct(trip_plan):
            return {
                "route_mode": "official_atm_trip",
                "route_confidence": "high",
                "needs_official_route_lookup": False,
                "destination": dest,
                "origin": {"lat": lat, "lon": lon},
                "official_route": trip_plan,
                "direct_atm_options": [],
                "one_transfer_options": [],
                "route_candidates": [
                    {
                        "kind": "official_atm_trip",
                        "eta_seconds": (trip_plan.get("summary") or {}).get("eta_seconds"),
                        "option": trip_plan,
                    }
                ],
                "osm_route_url": _maps_link(lat, lon, dlat, dlon),
                "atm_nearby_url": _atm_link(lat, lon),
                "official_route_url": _atm_link(lat, lon),
                "source": "GiroMilano ATM direct fast path",
                "generated_at": int(time.time()),
            }

        # Solo i percorsi con cambi/ambiguità entrano nel confronto
        # più costoso con alternative di superficie e metro.
        ranking_origin_stops = _nearby_stops(lat, lon)

        if not ranking_origin_stops:
            ranking_origin_stops = _nearby_stops(
                lat,
                lon,
                radius_m=900,
            )

        transfer_line_hints: list[str] = []

        for stop in ranking_origin_stops[:4]:
            stop_lines = _nearby_route_lines(
                float(stop["lat"]),
                float(stop["lon"]),
            )
            stop["lines"] = stop_lines

            for line in _line_labels(stop_lines):
                if line not in transfer_line_hints:
                    transfer_line_hints.append(line)

        one_transfer_options = (
            _atm_one_transfer_options(
                lat,
                lon,
                dlat,
                dlon,
                transfer_line_hints,
            )
            if transfer_line_hints
            else []
        )

        direct_atm_options = (
            _atm_direct_fallback_options(
                lat,
                lon,
                dlat,
                dlon,
                transfer_line_hints,
            )
            if transfer_line_hints
            else []
        )

        ranked_routes = _rank_atm_route_candidates(
            trip_plan,
            one_transfer_options,
            direct_options=direct_atm_options,
        )

        if (
            ranked_routes
            and ranked_routes[0]["kind"] == "direct_atm"
        ):
            return {
                "route_mode": "direct_atm",
                "route_confidence": "medium",
                "needs_official_route_lookup": False,
                "destination": dest,
                "origin": {"lat": lat, "lon": lon},
                "direct_atm_route": ranked_routes[0]["option"],
                "direct_atm_options": direct_atm_options,
                "official_route": trip_plan,
                "one_transfer_options": one_transfer_options,
                "route_candidates": ranked_routes,
                "osm_route_url": _maps_link(lat, lon, dlat, dlon),
                "atm_nearby_url": _atm_link(lat, lon),
                "official_route_url": _atm_link(lat, lon),
                "source": (
                    "ATM live + GTFS scheduled, "
                    "confrontato con GiroMilano ATM"
                ),
                "generated_at": int(time.time()),
            }

        if (
            ranked_routes
            and ranked_routes[0]["kind"] == "one_transfer"
        ):
            return {
                "route_mode": "one_transfer",
                "route_confidence": "medium",
                "needs_official_route_lookup": False,
                "destination": dest,
                "origin": {"lat": lat, "lon": lon},
                "one_transfer_route": ranked_routes[0]["option"],
                "direct_atm_options": direct_atm_options,
                "official_route": trip_plan,
                "one_transfer_options": one_transfer_options,
                "route_candidates": ranked_routes,
                "osm_route_url": _maps_link(lat, lon, dlat, dlon),
                "atm_nearby_url": _atm_link(lat, lon),
                "official_route_url": _atm_link(lat, lon),
                "source": (
                    "ATM live + GTFS scheduled, "
                    "confrontato con GiroMilano ATM"
                ),
                "generated_at": int(time.time()),
            }

        return {
            "route_mode": "official_atm_trip",
            "route_confidence": "high",
            "needs_official_route_lookup": False,
            "destination": dest,
            "origin": {"lat": lat, "lon": lon},
            "official_route": trip_plan,
            "direct_atm_options": direct_atm_options,
            "one_transfer_options": one_transfer_options,
            "route_candidates": ranked_routes,
            "osm_route_url": _maps_link(lat, lon, dlat, dlon),
            "atm_nearby_url": _atm_link(lat, lon),
            "official_route_url": _atm_link(lat, lon),
            "source": "GiroMilano ATM via browser CDP",
            "generated_at": int(time.time()),
        }

    if direct_topology_options:
        return {
            "destination": dest,
            "route_mode": "direct_atm",
            "route_confidence": "medium",
            "needs_official_route_lookup": False,
            "origin": {"lat": lat, "lon": lon},
            "direct_atm_route": direct_topology_options[0],
            "direct_atm_options": direct_topology_options,
            "official_route": trip_plan,
            "official_route_url": _atm_link(lat, lon),
            "atm_nearby_url": _atm_link(lat, lon),
            "source": "GTFS topology + ATM realtime",
            "generated_at": int(time.time()),
        }

    if not short_destination and dest.get("source") != "nominatim":
        return {
            "route_mode": "official_atm_lookup_failed",
            "route_confidence": "low",
            "needs_official_route_lookup": True,
            "destination": dest,
            "origin": {"lat": lat, "lon": lon},
            "nearest_origin_stop": None,
            "nearest_destination_stop": None,
            "nearby_origin_stops": [],
            "alternatives": [],
            "osm_route_url": _maps_link(lat, lon, dlat, dlon),
            "atm_nearby_url": _atm_link(lat, lon),
            "official_route_url": _atm_link(lat, lon),
            "live_arrivals": {},
            "direct_atm_options": [],
            "realtime_status": "not_available",
            "source": OSM_COPYRIGHT,
            "generated_at": int(time.time()),
        }

    origin_stops = _nearby_stops(lat, lon)
    if not origin_stops:
        origin_stops = _nearby_stops(lat, lon, radius_m=900)
    dest_stops = _nearby_stops(dlat, dlon)
    if not dest_stops:
        dest_stops = _nearby_stops(dlat, dlon, radius_m=900)

    for stop in origin_stops[:4]:
        stop["lines"] = _nearby_route_lines(float(stop["lat"]), float(stop["lon"]))
    for stop in dest_stops[:2]:
        stop["lines"] = _nearby_route_lines(float(stop["lat"]), float(stop["lon"]))

    live_lines = _line_labels((origin_stops[0] if origin_stops else {}).get("lines") or [])
    direct_atm_options = _atm_direct_fallback_options(lat, lon, dlat, dlon, live_lines)
    live = _atm_live_minutes(origin_stops[0], live_lines) if origin_stops else {"status": "no_origin_stop", "arrivals": {}}

    nearby_origin_stops = [
        {
            "from_stop": s,
            "walk_minutes_to_stop": max(1, round(int(s["distance_m"]) / 80)),
            "lines": s.get("lines") or [],
            "live_arrivals": live.get("arrivals") if i == 0 else {},
            "atm_realtime_url": _atm_link(float(s["lat"]), float(s["lon"])),
        }
        for i, s in enumerate(origin_stops[:4])
    ]
    return {
        "route_mode": "nearby_departures_fallback",
        "route_confidence": "low",
        "needs_official_route_lookup": True,
        "destination": dest,
        "origin": {"lat": lat, "lon": lon},
        "nearest_origin_stop": origin_stops[0] if origin_stops else None,
        "nearest_destination_stop": dest_stops[0] if dest_stops else None,
        "nearby_origin_stops": nearby_origin_stops,
        "alternatives": nearby_origin_stops,
        "osm_route_url": _maps_link(lat, lon, dlat, dlon),
        "atm_nearby_url": _atm_link(lat, lon),
        "official_route_url": _atm_link(lat, lon),
        "live_arrivals": live.get("arrivals") or {},
        "direct_atm_options": direct_atm_options,
        "realtime_status": live.get("status") or "not_available",
        "source": OSM_COPYRIGHT,
        "generated_at": int(time.time()),
    }


def build_plan(lat: float, lon: float, destination_name: str, realtime_provider: Any = None) -> dict[str, Any]:
    token = _ATM_REALTIME_PROVIDER.set(realtime_provider)
    helper_opened = _atm_browser_helper("open", timeout_s=10.0)
    try:
        return _build_plan_impl(lat, lon, destination_name)
    finally:
        _ATM_REALTIME_PROVIDER.reset(token)
        if helper_opened and ATM_BROWSER_CLOSE_AFTER:
            _atm_browser_helper("close", timeout_s=8.0)


def build_named_plan(origin_name: str, destination_name: str, realtime_provider: Any = None) -> dict[str, Any]:
    origin = _resolve_destination(origin_name)
    if not origin:
        raise HTTPException(status_code=404, detail="origin_not_found")
    plan = build_plan(float(origin["lat"]), float(origin["lon"]), destination_name, realtime_provider)
    plan["origin_label"] = origin.get("label") or origin.get("name")
    plan["origin_saved_name"] = origin.get("name")
    return plan


def _is_walking_only_alternative(text: str) -> bool:
    low = str(text or "").strip().lower()
    if not low:
        return False
    walking_only_patterns = (
        r"^alternativa\s*:\s*a piedi\b",
        r"^alternativa\s+a piedi\b",
        r"^vai\s+a piedi\b",
        r"^andare\s+a piedi\b",
        r"^percorso\s+a piedi\b",
        r"^tragitto\s+a piedi\b",
    )
    return any(re.search(pattern, low) for pattern in walking_only_patterns)


def render_reply(plan: dict[str, Any]) -> str:
    dest = plan["destination"]
    route_mode = str(plan.get("route_mode") or "").strip()

    def hhmm(value: Any) -> str:
        if hasattr(value, "strftime"):
            try:
                return value.strftime("%H:%M")
            except Exception:
                return ""
        return ""

    # LOCAL_ATM_REALTIME_RENDER_START
    if route_mode == "local_atm_realtime":
        route = plan.get("local_atm_route") or {}

        out = [
            str(
                dest.get("label")
                or dest.get("name")
                or "Destinazione"
            )
        ]

        def minute_text(seconds: Any) -> str:
            try:
                value = int(seconds)
            except Exception:
                return ""

            if value <= 0:
                return "0 min"

            minutes = max(
                1,
                round(value / 60),
            )

            return f"{minutes} min"

        origin_walk = route.get(
            "origin_walk_seconds"
        )

        origin_stop = str(
            route.get("origin_stop") or ""
        ).strip()

        try:
            origin_walk_i = int(
                origin_walk or 0
            )
        except Exception:
            origin_walk_i = 0

        if origin_walk_i > 0:
            text = (
                "A piedi "
                + minute_text(origin_walk_i)
            )

            if origin_stop:
                text += (
                    " fino a "
                    + origin_stop
                )

            out.append(text)

        first_transit = True

        for leg in route.get("legs") or []:
            if not isinstance(leg, dict):
                continue

            mode = str(
                leg.get("mode") or ""
            ).strip()

            if mode == "walk":
                walk_seconds = leg.get(
                    "walk_seconds"
                )

                text = "A piedi per il cambio"

                walk_text = minute_text(
                    walk_seconds
                )

                if walk_text:
                    text += ": " + walk_text

                from_name = str(
                    leg.get("from") or ""
                ).strip()

                to_name = str(
                    leg.get("to") or ""
                ).strip()

                if from_name and to_name:
                    text += (
                        f" ({from_name} → "
                        f"{to_name})"
                    )

                out.append(text)
                continue

            if mode != "transit":
                continue

            line = str(
                leg.get("route") or ""
            ).strip()

            from_name = str(
                leg.get("from") or ""
            ).strip()

            to_name = str(
                leg.get("to") or ""
            ).strip()

            prefix = (
                "Prendi"
                if first_transit
                else "Poi prendi"
            )

            text = prefix

            if line:
                text += f" {line}"

            if from_name:
                text += f" da {from_name}"

            if to_name:
                text += f" a {to_name}"

            out.append(text)

            if leg.get("live") is True:
                wait_seconds = leg.get(
                    "live_wait_seconds"
                )

                try:
                    wait_i = int(
                        wait_seconds
                    )
                except Exception:
                    wait_i = -1

                if wait_i == 0:
                    out.append(
                        f"Attesa live {line}: "
                        "in arrivo"
                    )
                elif wait_i > 0:
                    out.append(
                        f"Attesa live {line}: "
                        + minute_text(wait_i)
                    )
                else:
                    out.append(
                        "Attesa live non disponibile"
                    )
            else:
                out.append(
                    "Attesa live non disponibile"
                )
                if line:
                    out.append(
                        f"{line}: orario GTFS "
                        "programmato"
                    )

            first_transit = False

        final_walk = route.get(
            "final_walk_seconds"
        )

        try:
            final_walk_i = int(
                final_walk or 0
            )
        except Exception:
            final_walk_i = 0

        if final_walk_i > 0:
            out.append(
                "A piedi fino a destinazione: "
                + minute_text(final_walk_i)
            )

        total = route.get("total_seconds")

        try:
            total_i = int(total)
        except Exception:
            total_i = 0

        if total_i > 0:
            out.append(
                "Tempo totale stimato: "
                + minute_text(total_i)
            )

        arrival_s = route.get("arrival_s")

        try:
            arrival_i = int(arrival_s)
        except Exception:
            arrival_i = -1

        if arrival_i >= 0:
            arrival_i %= 24 * 3600

            hour = arrival_i // 3600
            minute = (
                arrival_i % 3600
            ) // 60

            out.append(
                f"Arrivo stimato: "
                f"{hour:02d}:{minute:02d}"
            )

        atm_url = (
            plan.get("official_route_url")
            or plan.get("atm_nearby_url")
        )

        if atm_url:
            out.append(f"ATM: {atm_url}")

        return "\n".join(out)
    # LOCAL_ATM_REALTIME_RENDER_END

    if route_mode == "direct_atm":
        direct_options = list(plan.get("direct_atm_options") or [])
        if len(direct_options) > 1:
            shown = direct_options[:ATM_DIRECT_CANDIDATE_LIMIT]
            out = [str(dest.get("label") or dest.get("name") or "Destinazione")]
            out.append(
                "Dirette utili: "
                + ", ".join(str(item.get("line") or "?") for item in shown)
            )
            out.append(
                "Attese reali: "
                + ", ".join(
                    f"{item.get('line')}: {item.get('wait') or 'n/d'}"
                    for item in shown
                )
            )
            for item in shown:
                line_name = str(item.get("line") or "").strip()
                origin_name = str(item.get("origin_stop_name") or "fermata").strip()
                dest_name = str(item.get("dest_stop_name") or "fermata").strip()
                stops_count = item.get("stops_count")
                detail = f"{line_name}: {origin_name} → {dest_name}"
                if stops_count:
                    detail += f" ({stops_count} fermate)"
                arrival = hhmm(item.get("destination_arrival_at"))
                if arrival:
                    detail += f", arrivo stimato {arrival}"
                out.append(detail)
            out.append(
                f"ATM: {plan.get('official_route_url') or plan.get('atm_nearby_url')}"
            )
            return "\n".join(out)

        route = plan.get("direct_atm_route") or {}
        line = str(route.get("line") or "").strip()
        wait = str(route.get("wait") or "").strip()
        wait_source = str(route.get("wait_source") or "").strip()
        origin_name = str(route.get("origin_stop_name") or "fermata").strip()
        dest_name = str(route.get("dest_stop_name") or "fermata").strip()

        out = [f"{dest.get('label') or dest.get('name')}"]

        if line:
            out.append(f"Prendi la linea {line}")

        out.append(f"Da {origin_name} a {dest_name}")

        stops_count = route.get("stops_count")
        if stops_count:
            out.append(f"Fermate: {stops_count}")

        if wait_source == "atm_live":
            if wait.lower() == "in arrivo":
                out.append(f"Attesa live {line}: in arrivo")
            elif wait:
                out.append(f"Attesa live {line}: {wait}")
            else:
                out.append("Attesa live non disponibile")
        elif wait_source == "gtfs_scheduled":
            out.append(
                f"Attesa programmata {line}: {wait or 'non disponibile'}"
            )
        else:
            out.append("Attesa live non disponibile")

        departure = hhmm(route.get("departure_at"))
        if departure:
            out.append(f"Partenza stimata: {departure}")

        final_walk_m = int(route.get("final_walk_m") or 0)
        if final_walk_m:
            out.append(f"A piedi dopo la discesa: {final_walk_m} m")

        arrival = hhmm(route.get("destination_arrival_at"))
        if arrival:
            out.append(f"Arrivo stimato a destinazione: {arrival}")

        eta_seconds = route.get("eta_seconds")
        if eta_seconds is not None:
            try:
                eta_min = round(int(eta_seconds) / 60)
                out.append(f"Tempo totale stimato: {eta_min} min")
            except Exception:
                pass

        alternatives = [
            item
            for item in (plan.get("direct_atm_options") or [])
            if item is not route
            and str(item.get("line") or "").strip()
            != line
        ][:2]
        if alternatives:
            out.append("Alternative dirette:")
            for alternative in alternatives:
                alt_line = str(alternative.get("line") or "").strip()
                alt_wait = str(alternative.get("wait") or "").strip()
                alt_source = str(alternative.get("wait_source") or "").strip()
                alt_origin = str(alternative.get("origin_stop_name") or "fermata").strip()
                alt_stops = alternative.get("stops_count")
                details = [f"{alt_line} da {alt_origin}"]
                if alt_wait and alt_wait.lower() != "n/d":
                    if alt_source == "atm_live_too_soon":
                        details.append(f"{alt_wait}, troppo vicino per raggiungerlo")
                    else:
                        details.append(f"attesa {alt_wait}")
                if alt_stops:
                    details.append(f"{alt_stops} fermate")
                alt_arrival = hhmm(alternative.get("destination_arrival_at"))
                if alt_arrival:
                    details.append(f"arrivo {alt_arrival}")
                out.append("- " + " · ".join(details))

        out.append(
            f"ATM: {plan.get('official_route_url') or plan.get('atm_nearby_url')}"
        )
        return "\n".join(out)

    if route_mode == "one_transfer":
        route = plan.get("one_transfer_route") or {}

        first_line = str(route.get("first_line") or "").strip()
        second_line = str(route.get("second_line") or "").strip()
        origin_name = str(route.get("origin_stop_name") or "fermata").strip()
        transfer_name = str(route.get("transfer_stop_name") or "interscambio").strip()

        first_wait = str(route.get("first_wait") or "").strip()
        first_wait_source = str(route.get("first_wait_source") or "").strip()

        out = [f"{dest.get('label') or dest.get('name')}"]

        if first_line:
            out.append(f"Prendi la linea {first_line} da {origin_name}")

        if first_wait_source == "atm_live":
            if first_wait.lower() == "in arrivo":
                out.append(f"Attesa live {first_line}: in arrivo")
            elif first_wait:
                out.append(f"Attesa live {first_line}: {first_wait}")
            else:
                out.append("Attesa live non disponibile")
        elif first_wait_source == "gtfs_scheduled":
            out.append(
                f"Attesa programmata {first_line}: "
                f"{first_wait or 'non disponibile'}"
            )
        else:
            out.append("Attesa live non disponibile")

        if second_line:
            out.append(
                f"Cambia a {transfer_name} e prendi {second_line}"
            )

        transfer_walk_m = int(route.get("transfer_walk_m") or 0)
        if transfer_walk_m:
            out.append(
                f"A piedi per il cambio: {transfer_walk_m} m"
            )

        second_wait_seconds = route.get("second_wait_seconds")
        if second_wait_seconds is not None and second_line:
            try:
                second_wait_min = round(
                    int(second_wait_seconds) / 60
                )
                source = str(
                    route.get("second_wait_source") or ""
                )
                if source == "gtfs_scheduled":
                    out.append(
                        f"Attesa {second_line} programmata: "
                        f"{second_wait_min} min"
                    )
                else:
                    out.append(
                        f"Attesa {second_line}: "
                        f"{second_wait_min} min"
                    )
            except Exception:
                pass

        final_walk_m = int(route.get("final_walk_m") or 0)
        if final_walk_m:
            out.append(
                f"A piedi dopo la seconda linea: {final_walk_m} m"
            )

        arrival = hhmm(route.get("destination_arrival_at"))
        if arrival:
            out.append(f"Arrivo stimato a destinazione: {arrival}")

        eta_seconds = route.get("eta_seconds")
        if eta_seconds is not None:
            try:
                eta_min = round(int(eta_seconds) / 60)
                out.append(f"Tempo totale stimato: {eta_min} min")
            except Exception:
                pass

        out.append(
            f"ATM: {plan.get('official_route_url') or plan.get('atm_nearby_url')}"
        )
        return "\n".join(out)

    if (
        route_mode == "official_atm_trip"
        or (
            not route_mode
            and _official_trip_has_transit(plan.get("official_route"))
        )
    ):
        summary = (plan.get("official_route") or {}).get("summary") or {}
        route_lines = ", ".join(summary.get("lines") or [])
        steps = list(summary.get("steps") or [])
        out = [f"{dest.get('label') or dest.get('name')}"]
        if route_lines:
            out.append(f"Prendi: {route_lines}")
        first_line = str(summary.get("first_line") or "").strip()
        live_wait = str(summary.get("live_wait") or "").strip()
        board_stop_code = str(summary.get("board_stop_code") or "").strip()

        if first_line and board_stop_code and not live_wait:
            live = _atm_live_minutes({"atm_stop_code": board_stop_code}, [first_line])
            arrivals = live.get("arrivals") or {}
            live_wait_from_stop = str(arrivals.get(first_line) or "").strip()
            live_wait = live_wait_from_stop or live_wait

        if first_line:
            wait_low = live_wait.lower()
            if wait_low == "in arrivo":
                out.append(f"Bus alla fermata: {first_line} in arrivo")
            elif wait_low == "ricalcolo":
                out.append(f"Bus alla fermata: {first_line} ricalcolo")
            elif live_wait:
                out.append(f"Bus alla fermata: {first_line} tra {live_wait}")
            else:
                out.append(f"Bus alla fermata: {first_line} n/d")

        if summary.get("vehicle_eta"):
            out.append(f"Bus alla fermata alle: {summary.get('vehicle_eta')}")
        if summary.get("duration"):
            duration = str(summary.get("duration"))
            out.append(f"Tempo stimato fino alla destinazione: {duration if 'min' in duration.lower() else duration + ' min'}")
        if summary.get("destination_eta"):
            out.append(f"Arrivo stimato a destinazione: {summary.get('destination_eta')}")
        if summary.get("walk_m"):
            out.append(f"A piedi: {summary.get('walk_m')} m")
        filtered_steps = [step for step in steps if not _is_walking_only_alternative(str(step))]
        out.extend(filtered_steps[:4])
        out.append(f"ATM: {plan.get('official_route_url')}")
        return "\n".join(out)
    first = plan.get("nearest_origin_stop") or {}
    to = plan.get("nearest_destination_stop") or {}
    direct_atm_options = plan.get("direct_atm_options") or []

    first_line_labels = _line_labels(first.get("lines") or [], limit=20) if first else []

    lines = [f"Destinazione: {dest.get('label') or dest.get('name')}"]
    if direct_atm_options:
        with_real_wait = [x for x in list(direct_atm_options) if x.get("wait")]
        shown = (with_real_wait or list(direct_atm_options))[:4]

        lines.append("Linee dirette ATM nella direzione giusta: " + ", ".join(str(x.get("line")) for x in shown))
        waits = ", ".join(f"{x.get('line')}: {x.get('wait') or 'n/d'}" for x in shown)
        lines.append(f"Attese reali: {waits}")

        best = shown[0]
        stops_count = best.get("stops_count")
        extra = f" ({stops_count} fermate)" if stops_count else ""
        lines.append(f"Da {best.get('origin_stop_name')} a {best.get('dest_stop_name')}{extra}")
        lines.append(f"ATM: {plan.get('official_route_url') or plan.get('atm_nearby_url')}")
        return "\n".join(lines)
    else:
        lines.append("Non ho ancora calcolato il percorso completo; ti mostro le partenze vicine e la fermata più vicina alla destinazione.")

    if first:
        walk_min = max(1, round(int(first.get("distance_m") or 0) / 80))
        line_labels = first_line_labels or _line_labels(first.get("lines") or [])
        lines_found = ", ".join(line_labels)
        arrivals = plan.get("live_arrivals") or {}
        waits = ", ".join(f"{line}: {arrivals.get(line) or 'n/d'}" for line in line_labels) if line_labels else ""
        lines.append(f"Da qui: {first.get('name')} ({first.get('distance_m')} m, {walk_min} min a piedi)")
        lines.append(f"Partenze utili da qui: {lines_found or 'non trovate in OSM'}")
        lines.append(f"Attese live da qui: {waits or 'non disponibili'}")
    if to:
        arr_lines = ", ".join(_line_labels(to.get("lines") or []))
        suffix = f" - linee vicine: {arr_lines}" if arr_lines else ""
        lines.append(f"Vicino alla destinazione: {to.get('name')} ({to.get('distance_m')} m){suffix}")
    nearby = (plan.get("nearby_origin_stops") or plan.get("alternatives") or [])[1:4]
    for alt in nearby:
        s = alt["from_stop"]
        alt_lines = ", ".join(_line_labels(alt.get("lines") or [], limit=6))
        if alt_lines:
            lines.append(f"Altre fermate vicine: {s.get('name')} - linee {alt_lines} - cammino {alt.get('walk_minutes_to_stop')} min")
    lines.append(f"Link ATM per calcolare il percorso completo: {plan.get('official_route_url') or plan.get('atm_nearby_url')}")
    return "\n".join(lines)


def _telegram_destination(message: dict[str, Any]) -> str:
    text = str(message.get("caption") or message.get("text") or "").strip()
    text = re.sub(r"^(vai|andare|portami|destinazione|verso|a)\s+", "", text, flags=re.I).strip()
    return text or "casa"


def _telegram_named_route(message: dict[str, Any]) -> tuple[str, str] | None:
    text = str(message.get("caption") or message.get("text") or "").strip()
    m = re.search(r"\batm\s*:\s*([^-–—>]+)\s*[-–—>]\s*(.+)$", text, flags=re.I)
    if not m:
        return None
    return m.group(1).strip(), m.group(2).strip()


@router.get("", response_class=HTMLResponse)
def atm_telegram_page() -> HTMLResponse:
    return HTMLResponse(_page_html())


@router.get("/api/destinations")
def api_destinations() -> dict[str, Any]:
    return {"destinations": _load_destinations(), "path": str(DESTINATIONS_PATH)}


@router.post("/api/destinations")
def api_save_destination(dest: DestinationIn) -> dict[str, Any]:
    rows = [r for r in _load_destinations() if _slug(str(r.get("name") or "")) != _slug(dest.name)]
    row = {"name": _slug(dest.name), "label": dest.label or dest.name, "aliases": _aliases(dest.aliases), "lat": dest.lat, "lon": dest.lon, "note": dest.note or ""}
    rows.append(row)
    _save_destinations(rows)
    return {"ok": True, "destination": row, "destinations": _load_destinations()}


@router.post("/api/destinations/delete")
def api_delete_destination(req: DestinationDeleteIn) -> dict[str, Any]:
    name = _slug(req.name)
    rows = [r for r in _load_destinations() if _slug(str(r.get("name") or "")) != name]
    _save_destinations(rows)
    return {"ok": True, "destinations": _load_destinations()}


@router.post("/api/geocode")
def api_geocode(req: GeocodeIn) -> dict[str, Any]:
    return {"results": _nominatim_search(req.q)}


@router.post("/api/plan")
def api_plan(req: PlanIn) -> dict[str, Any]:
    plan = build_plan(req.lat, req.lon, req.destination)
    plan["reply"] = render_reply(plan)
    return plan


@router.post("/api/plan-named")
def api_plan_named(req: PlanNamedIn) -> dict[str, Any]:
    plan = build_named_plan(req.origin, req.destination)
    plan["reply"] = render_reply(plan)
    return plan


@router.post("/telegram-webhook")
def telegram_webhook(req: TelegramWebhookIn) -> dict[str, Any]:
    msg = req.message or {}
    named = _telegram_named_route(msg)
    if named:
        plan = build_named_plan(named[0], named[1])
        return {"ok": True, "reply": render_reply(plan), "plan": plan}
    loc = msg.get("location") or {}
    if "latitude" not in loc or "longitude" not in loc:
        return {"ok": False, "error": "missing_telegram_location"}
    plan = build_plan(float(loc["latitude"]), float(loc["longitude"]), _telegram_destination(msg))
    return {"ok": True, "reply": render_reply(plan), "plan": plan}


def _page_html() -> str:
    destinations = json.dumps(_load_destinations(), ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ralf ATM Telegram</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>body{{margin:0;font-family:system-ui,Arial,sans-serif;background:#f7f7f5;color:#171717}}header{{padding:14px 18px;background:#111;color:#fff}}main{{display:grid;grid-template-columns:380px 1fr;min-height:calc(100vh - 52px)}}aside{{padding:14px;overflow:auto;border-right:1px solid #ddd}}#map{{min-height:calc(100vh - 52px)}}label{{display:block;font-size:13px;margin-top:10px}}input,select,button{{width:100%;box-sizing:border-box;padding:10px;margin-top:4px;border:1px solid #bbb;border-radius:6px;background:white}}button{{background:#0b6bcb;color:white;border:0;font-weight:700;cursor:pointer}}.row{{display:grid;grid-template-columns:1fr 1fr;gap:8px}}#results button{{margin-top:6px;text-align:left;background:#444}}pre{{white-space:pre-wrap;background:white;border:1px solid #ddd;padding:10px;border-radius:6px;max-height:42vh;overflow:auto}}.small{{font-size:12px;color:#555}}@media(max-width:800px){{main{{display:flex;flex-direction:column}}#map{{order:-1;min-height:42vh}}aside{{border-right:0;border-bottom:1px solid #ddd;max-height:none}}}}</style></head>
<body><header>Ralf ATM Telegram</header><main><aside>
<label>Destinazione</label><select id="dest"></select>
<div class="row"><label>Lat origine<input id="lat" placeholder="45.x"></label><label>Lon origine<input id="lon" placeholder="9.x"></label></div>
<button id="geo">Usa posizione browser</button><button id="plan">Calcola</button>
<label>Nuovo luogo</label><input id="name" placeholder="casa / palestra / ...">
<div class="row"><input id="addr" placeholder="cerca indirizzo"><button id="search">Cerca</button></div>
<div id="results"></div>
<div class="row"><label>Lat luogo<input id="dlat"></label><label>Lon luogo<input id="dlon"></label></div>
<input id="aliases" placeholder="alias separati da virgola: casa, home"><input id="note" placeholder="nota indirizzo"><button id="save">Salva luogo</button>
<div id="list"></div>
<p class="small">Click mappa = imposta lat/lon luogo. Telegram: invia location + testo destinazione.</p><pre id="out"></pre>
</aside><div id="map"></div></main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script><script>
let destinations={destinations}; let map=L.map('map').setView([45.4642,9.19],12); L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',{{maxZoom:19,attribution:'© OpenStreetMap'}}).addTo(map); let marker;
function fill(){{let s=document.getElementById('dest'); let list=document.getElementById('list'); s.innerHTML=''; list.innerHTML=''; destinations.forEach(d=>{{let o=document.createElement('option'); o.value=d.name; o.textContent=d.label||d.name; s.appendChild(o); L.marker([d.lat,d.lon]).addTo(map).bindPopup(d.label||d.name); let row=document.createElement('div'); row.style.cssText='display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:center;border-top:1px solid #ddd;padding:8px 0;font-size:13px'; row.innerHTML='<span><b>'+ (d.label||d.name) +'</b><br><span class=\"small\">alias: '+((d.aliases||[]).join(', ')||'nessuno')+'</span></span><button data-edit=\"'+d.name+'\" style=\"background:#555\">Modifica</button><button data-del=\"'+d.name+'\" style=\"background:#8b1a1a\">Elimina</button>'; list.appendChild(row);}}); list.querySelectorAll('button[data-edit]').forEach(b=>b.onclick=()=>{{let d=destinations.find(x=>x.name===b.dataset.edit); if(!d)return; document.getElementById('name').value=d.name||''; document.getElementById('aliases').value=(d.aliases||[]).join(', '); document.getElementById('dlat').value=(+d.lat).toFixed(6); document.getElementById('dlon').value=(+d.lon).toFixed(6); document.getElementById('note').value=d.note||''; map.setView([d.lat,d.lon],16); if(marker)marker.remove(); marker=L.marker([d.lat,d.lon]).addTo(map).bindPopup(d.label||d.name).openPopup(); document.getElementById('out').textContent='Modifica '+(d.label||d.name)+': cambia alias e premi Salva luogo'; }}); list.querySelectorAll('button[data-del]').forEach(b=>b.onclick=async()=>{{if(!confirm('Eliminare '+b.dataset.del+'?'))return;let r=await fetch('/atm-telegram/api/destinations/delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name:b.dataset.del}})}});let j=await r.json();destinations=j.destinations;fill();document.getElementById('out').textContent='eliminato '+b.dataset.del;}})}} fill();
map.on('click',e=>{{document.getElementById('dlat').value=e.latlng.lat.toFixed(6);document.getElementById('dlon').value=e.latlng.lng.toFixed(6); if(marker) marker.remove(); marker=L.marker(e.latlng).addTo(map);}});
document.getElementById('geo').onclick=()=>navigator.geolocation.getCurrentPosition(p=>{{document.getElementById('lat').value=p.coords.latitude.toFixed(6);document.getElementById('lon').value=p.coords.longitude.toFixed(6);map.setView([p.coords.latitude,p.coords.longitude],15);}});
document.getElementById('search').onclick=async()=>{{const resultsEl=document.getElementById('results');const addrEl=document.getElementById('addr');resultsEl.textContent='cerco...';let r=await fetch('/atm-telegram/api/geocode',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{q:addrEl.value}})}});let j=await r.json();resultsEl.innerHTML='';(j.results||[]).forEach(x=>{{let b=document.createElement('button');b.style.background='#444';b.textContent=x.label;b.onclick=()=>{{document.getElementById('dlat').value=(+x.lat).toFixed(6);document.getElementById('dlon').value=(+x.lon).toFixed(6);map.setView([x.lat,x.lon],16);if(marker)marker.remove();marker=L.marker([x.lat,x.lon]).addTo(map).bindPopup(x.label).openPopup();const nameEl=document.getElementById('name');if(!nameEl.value)nameEl.value=addrEl.value;}};resultsEl.appendChild(b);}});if(!(j.results||[]).length)resultsEl.textContent='nessun risultato';}};
document.getElementById('save').onclick=async()=>{{let body={{name:document.getElementById('name').value,label:document.getElementById('name').value,aliases:document.getElementById('aliases').value,lat:+document.getElementById('dlat').value,lon:+document.getElementById('dlon').value,note:document.getElementById('note').value}};let r=await fetch('/atm-telegram/api/destinations',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});let j=await r.json();destinations=j.destinations;fill();document.getElementById('out').textContent='salvato '+body.name;}};
document.getElementById('plan').onclick=async()=>{{const outEl=document.getElementById('out');outEl.textContent='calcolo...';let r=await fetch('/atm-telegram/api/plan',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{lat:+document.getElementById('lat').value,lon:+document.getElementById('lon').value,destination:document.getElementById('dest').value}})}});let j=await r.json();outEl.textContent=j.reply||JSON.stringify(j,null,2); if(j.nearest_origin_stop) L.marker([j.nearest_origin_stop.lat,j.nearest_origin_stop.lon]).addTo(map).bindPopup('Fermata: '+j.nearest_origin_stop.name).openPopup();}};
</script></body></html>"""

def main_cli(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="atm-telegram",
        description="CLI Ralf ATM: da coordinate GPS a fermata ATM vicina e link GiroMilano."
    )
    sub = ap.add_subparsers(dest="cmd")

    p_plan = sub.add_parser("plan", help="calcola fermate vicine e link ATM")
    p_plan.add_argument("--lat", type=float, required=True)
    p_plan.add_argument("--lon", type=float, required=True)
    p_plan.add_argument("-d", "--destination", required=True)
    p_plan.add_argument("--json", action="store_true", help="stampa JSON completo invece della risposta testuale")

    p_list = sub.add_parser("list", help="lista destinazioni salvate")
    p_list.add_argument("--json", action="store_true")

    p_add = sub.add_parser("add", help="salva una destinazione")
    p_add.add_argument("name")
    p_add.add_argument("--label")
    p_add.add_argument("--lat", type=float, required=True)
    p_add.add_argument("--lon", type=float, required=True)
    p_add.add_argument("--note", default="")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        rows = _load_destinations()
        if args.json:
            print(json.dumps({"destinations": rows, "path": str(DESTINATIONS_PATH)}, ensure_ascii=False, indent=2))
        else:
            for r in rows:
                print(f"{r.get('label') or r.get('name')} | {r.get('lat')},{r.get('lon')} | {r.get('note') or ''}")
        return 0

    if args.cmd == "add":
        rows = [r for r in _load_destinations() if _slug(str(r.get("name") or "")) != _slug(args.name)]
        row = {
            "name": _slug(args.name),
            "label": args.label or args.name,
            "aliases": [],
            "lat": args.lat,
            "lon": args.lon,
            "note": args.note or "",
        }
        rows.append(row)
        _save_destinations(rows)
        print(f"salvata: {row['label']} -> {row['lat']},{row['lon']}")
        return 0

    if args.cmd == "plan":
        plan = build_plan(args.lat, args.lon, args.destination)
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            print(render_reply(plan))
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main_cli())
