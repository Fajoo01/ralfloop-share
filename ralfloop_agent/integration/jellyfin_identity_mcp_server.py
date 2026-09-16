from __future__ import annotations

import json
import os
import subprocess
import sys
import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

import requests


DEFAULT_CONFIG = "/home/sibilla-cumana/jellyfin-novita-agent/config.json"
DEDUP = "/home/sibilla-cumana/jellyfin-novita-agent/jellyfin_deduplicate.py"
DEDUP_PYTHON = "/home/sibilla-cumana/jellyfin-novita-agent/.venv_backend/bin/python"


class JellyfinMCPServer:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.headers = {"X-Emby-Token": token, "Content-Type": "application/json"}

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            tool("jellyfin_list_unidentified_movies", "List movies missing IMDb and TMDb identity.", {}, []),
            tool("jellyfin_get_movie_identity", "Read current provider identity for one Jellyfin movie.", {
                "item_id": {"type": "string", "minLength": 32},
            }, ["item_id"]),
            tool("jellyfin_search_movie_identity", "Search remote metadata for one Jellyfin movie.", {
                "item_id": {"type": "string", "minLength": 32}, "name": {"type": "string"},
                "year": {"type": "integer", "minimum": 1880, "maximum": 2100},
            }, ["item_id"]),
            tool("jellyfin_apply_movie_identity", "Apply one exact provider identity. Explicit confirmation required.", {
                "item_id": {"type": "string", "minLength": 32},
                "provider": {"type": "string", "enum": ["Tmdb", "Imdb"]},
                "provider_id": {"type": "string", "minLength": 1},
                "name": {"type": "string"},
                "year": {"type": "integer", "minimum": 1880, "maximum": 2100},
                "confirm": {"type": "boolean", "const": True},
            }, ["item_id", "provider", "provider_id", "confirm"]),
            tool("jellyfin_resolve_unidentified_movies", "Resolve only unique exact title/year matches; return ambiguous items unchanged.", {
                "confirm": {"type": "boolean", "const": True},
            }, ["confirm"]),
            tool("jellyfin_refresh_library", "Start a Jellyfin library refresh.", {
                "confirm": {"type": "boolean", "const": True},
            }, ["confirm"]),
            tool("jellyfin_deduplicate", "Remove lower-quality duplicate media after playback checks.", {
                "confirm": {"type": "boolean", "const": True},
            }, ["confirm"]),
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if name == "jellyfin_list_unidentified_movies" and not arguments:
                return result(self.unidentified(), False)
            if name == "jellyfin_get_movie_identity":
                return result(self.get_identity(arguments), False)
            if name == "jellyfin_search_movie_identity":
                return result(self.search(arguments), False)
            if name == "jellyfin_apply_movie_identity":
                return self.apply(arguments)
            if name == "jellyfin_resolve_unidentified_movies":
                return self.resolve_unidentified(arguments)
            if name == "jellyfin_refresh_library":
                return self.refresh(arguments)
            if name == "jellyfin_deduplicate":
                return self.deduplicate(arguments)
            return result({"ok": False, "status": "POLICY_DENIED"}, True)
        except requests.RequestException as exc:
            return result({"ok": False, "status": "SOURCE_UNAVAILABLE", "error": str(exc)[:300]}, True)

    def unidentified(self) -> dict[str, Any]:
        payload = self.get("/Items", {"Recursive": "true", "IncludeItemTypes": "Movie",
            "Fields": "Path,ProviderIds,ProductionYear", "Limit": 10000})
        rows = [{"id": row.get("Id"), "name": row.get("Name"), "year": row.get("ProductionYear"),
                 "path": row.get("Path"), "provider_ids": row.get("ProviderIds") or {}}
                for row in payload.get("Items", [])
                if not (row.get("ProviderIds") or {}).get("Tmdb") and not (row.get("ProviderIds") or {}).get("Imdb")]
        return {"ok": True, "status": "FOUND" if rows else "EMPTY", "count": len(rows), "items": rows}

    def get_identity(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        item_id = str(arguments.get("item_id") or "")
        payload = self.get("/Items", {
            "Ids": item_id,
            "Fields": "ProviderIds,ProductionYear",
            "Limit": 1,
        })
        current = next(iter(payload.get("Items") or []), {})
        if not current:
            raise ValueError("item_not_found")
        return {
            "ok": True,
            "status": "FOUND",
            "item_id": str(current.get("Id") or item_id),
            "name": current.get("Name"),
            "year": current.get("ProductionYear"),
            "provider_ids": current.get("ProviderIds") or {},
        }

    def search(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        item_id, rows = self._remote_search(arguments)
        items = [{"name": row.get("Name"), "year": row.get("ProductionYear"),
                  "provider_ids": row.get("ProviderIds") or {}, "overview": row.get("Overview")}
                 for row in rows[:10]]
        return {"ok": True, "status": "FOUND" if items else "EMPTY", "item_id": item_id, "items": items}

    def _remote_search(self, arguments: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        item_id = str(arguments.get("item_id") or "")
        found = self.get("/Items", {"Ids": item_id, "Fields": "ProviderIds,ProductionYear", "Limit": 1})
        current = next(iter(found.get("Items") or []), {})
        if not current:
            raise ValueError("item_not_found")
        query = {"ItemId": item_id, "SearchInfo": {
            "Name": str(arguments.get("name") or current.get("Name") or ""),
            "Year": arguments.get("year") or current.get("ProductionYear"),
            "ProviderIds": current.get("ProviderIds") or {},
        }}
        rows = self.post("/Items/RemoteSearch/Movie", query)
        return item_id, list(rows or [])

    def apply(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments.get("confirm") is not True:
            return result({"ok": False, "status": "CONFIRMATION_REQUIRED"}, True)
        provider = str(arguments.get("provider") or "")
        provider_id = str(arguments.get("provider_id") or "")
        _, rows = self._remote_search({"item_id": arguments.get("item_id"),
                                       "name": arguments.get("name"), "year": arguments.get("year")})
        selected = next((row for row in rows if str((row.get("ProviderIds") or {}).get(provider) or "") == provider_id), None)
        if not selected:
            return result({"ok": False, "status": "IDENTITY_NOT_IN_SEARCH_RESULTS"}, True)
        response = requests.post(self.url + f"/Items/RemoteSearch/Apply/{arguments['item_id']}",
            params={"replaceAllImages": "false"}, json=selected, headers=self.headers, timeout=60)
        response.raise_for_status()
        return result({"ok": True, "status": "APPLIED", "identity": selected}, False)

    def refresh(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments.get("confirm") is not True:
            return result({"ok": False, "status": "CONFIRMATION_REQUIRED"}, True)
        response = requests.post(self.url + "/Library/Refresh", headers=self.headers, timeout=30)
        response.raise_for_status()
        return result({"ok": True, "status": "STARTED"}, False)

    def resolve_unidentified(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments.get("confirm") is not True:
            return result({"ok": False, "status": "CONFIRMATION_REQUIRED"}, True)
        applied, unresolved = [], []
        for item in self.unidentified()["items"]:
            query_name, query_year = clean_identity_query(str(item.get("name") or ""), item.get("year"))
            _, rows = self._remote_search({"item_id": item["id"], "name": query_name, "year": query_year})
            exact = [row for row in rows if normalize_title(str(row.get("Name") or "")) == normalize_title(query_name)
                     and (not query_year or not row.get("ProductionYear")
                          or abs(int(row["ProductionYear"]) - int(query_year)) <= 1)]
            identities = {(row.get("ProviderIds") or {}).get("Tmdb") or
                          (row.get("ProviderIds") or {}).get("Imdb") for row in exact}
            identities.discard(None)
            if len(identities) != 1:
                unresolved.append({"id": item["id"], "name": item["name"], "reason": "ambiguous_or_no_exact_match"})
                continue
            selected = next(row for row in exact if ((row.get("ProviderIds") or {}).get("Tmdb") or
                            (row.get("ProviderIds") or {}).get("Imdb")) in identities)
            response = requests.post(self.url + f"/Items/RemoteSearch/Apply/{item['id']}",
                params={"replaceAllImages": "false"}, json=selected, headers=self.headers, timeout=60)
            response.raise_for_status()
            applied.append({"id": item["id"], "name": selected.get("Name"),
                            "provider_ids": selected.get("ProviderIds") or {}})
        return result({"ok": True, "status": "COMPLETED", "applied": applied,
                       "unresolved": unresolved, "applied_count": len(applied),
                       "unresolved_count": len(unresolved)}, False)

    def deduplicate(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if arguments.get("confirm") is not True:
            return result({"ok": False, "status": "CONFIRMATION_REQUIRED"}, True)
        completed = subprocess.run([DEDUP_PYTHON, DEDUP, "--execute"], capture_output=True, text=True, timeout=600)
        ok = completed.returncode == 0
        return result({"ok": ok, "status": "COMPLETED" if ok else "FAILED",
                       "output": completed.stdout[-2000:], "error": completed.stderr[-1000:]}, not ok)

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        response = requests.get(self.url + path, params=params, headers=self.headers, timeout=60)
        response.raise_for_status()
        return response.json()

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:
        response = requests.post(self.url + path, json=payload, headers=self.headers, timeout=60)
        response.raise_for_status()
        return response.json() if response.content else {}


def tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {"type": "object",
            "properties": properties, "required": required, "additionalProperties": False}}


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def clean_identity_query(name: str, year: Any) -> tuple[str, int | None]:
    detected = re.search(r"\b(18\d{2}|19\d{2}|20\d{2})\b", name)
    query_year = int(year or (detected.group(1) if detected else 0)) or None
    value = re.sub(r"\[[^]]*]|\([^)]*(?:rip|1080|720|x26[45]|ita|eng)[^)]*\)", " ", name, flags=re.I)
    value = re.sub(r"\b(?:18\d{2}|19\d{2}|20\d{2}|bluray|brrip|bdrip|dvdrip|webrip|webdl|x26[45]|h26[45]|1080p?|720p?|ita|eng|ac3|aac)\b.*$", "", value, flags=re.I)
    value = re.sub(r"[._]+", " ", value).strip(" -")
    return value or name, query_year


def result(payload: Mapping[str, Any], is_error: bool) -> dict[str, Any]:
    body = dict(payload)
    return {"content": [{"type": "text", "text": json.dumps(body, ensure_ascii=False)}],
            "structuredContent": body, "isError": is_error}


def response(request: Mapping[str, Any], server: JellyfinMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        value: Any = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "bottazzi-jellyfin", "version": "1"}}
    elif method == "tools/list":
        value = {"tools": server.list_tools()}
    elif method == "tools/call" and isinstance(request.get("params"), Mapping):
        params = request["params"]
        value = server.call(str(params.get("name") or ""), params.get("arguments", {}))
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": value}


def main() -> int:
    config = json.loads(Path(os.getenv("JELLYFIN_CONFIG", DEFAULT_CONFIG)).read_text(encoding="utf-8"))
    server = JellyfinMCPServer(str(config.get("jellyfin_url") or ""), str(config.get("jellyfin_token") or ""))
    for line in sys.stdin:
        try:
            request = json.loads(line)
            value = response(request, server) if isinstance(request, Mapping) else None
        except Exception:
            value = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal_error"}}
        if value is not None:
            print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
