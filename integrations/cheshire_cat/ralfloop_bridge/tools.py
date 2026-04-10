import os
import json
import requests

from cat.mad_hatter.decorators import tool

RALFLOOP_BASE_URL = os.getenv("RALFLOOP_BASE_URL", "http://127.0.0.1:19090").rstrip("/")


def _post(path: str, payload: dict | None = None):
    r = requests.post(f"{RALFLOOP_BASE_URL}{path}", json=payload or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def _get(path: str, params: dict | None = None):
    r = requests.get(f"{RALFLOOP_BASE_URL}{path}", params=params or {}, timeout=60)
    r.raise_for_status()
    return r.json()


def _delete(path: str):
    r = requests.delete(f"{RALFLOOP_BASE_URL}{path}", timeout=60)
    r.raise_for_status()
    return r.json()


def _create_sandbox():
    return _post("/sandboxes")


def _destroy_sandbox(sid: str):
    return _delete(f"/sandboxes/{sid}")


@tool
def ralfloop_list_dir(path: str = ".") -> str:
    """Usa Ralfloop per elencare una directory quando la richiesta è calcolabile/verificabile."""
    sb = _create_sandbox()
    try:
        data = _get(f"/sandboxes/{sb['id']}/list", params={"path": path})
        entries = data.get("entries", [])
        if not entries:
            return f"La directory {path} è vuota."
        return "Contenuto directory:\n" + "\n".join(entries)
    finally:
        _destroy_sandbox(sb["id"])


@tool
def ralfloop_read_file(path: str) -> str:
    """Usa Ralfloop per leggere un file in sandbox e restituire il contenuto."""
    sb = _create_sandbox()
    try:
        data = _get(f"/sandboxes/{sb['id']}/read", params={"path": path})
        return f"Contenuto del file {path}:\n{data.get('content', '')}"
    finally:
        _destroy_sandbox(sb["id"])


@tool
def ralfloop_exec(command: str, timeout_sec: int = 20) -> str:
    """Usa Ralfloop per eseguire un comando verificabile in sandbox."""
    sb = _create_sandbox()
    try:
        data = _post(
            f"/sandboxes/{sb['id']}/exec",
            {"command": command, "timeout_sec": timeout_sec},
        )
        return (
            f"exit_code={data.get('exit_code', 1)}\n"
            f"STDOUT:\n{data.get('stdout', '')}\n"
            f"STDERR:\n{data.get('stderr', '')}"
        )
    finally:
        _destroy_sandbox(sb["id"])


@tool
def ralfloop_probe_stream(url: str, referer: str = "", user_agent: str = "") -> str:
    """Usa Ralfloop per analizzare un flusso video o una pagina che contiene manifest video."""
    sb = _create_sandbox()
    try:
        data = _post(
            f"/sandboxes/{sb['id']}/probe_stream",
            {
                "url": url,
                "referer": referer or None,
                "user_agent": user_agent or None,
            },
        )
        return json.dumps(
            {
                "input_url": data.get("input_url"),
                "final_url": data.get("final_url"),
                "kind": data.get("kind"),
                "reachable": data.get("reachable"),
                "drm_suspected": data.get("drm_suspected"),
                "manifest": data.get("manifest", {}),
                "page_candidates": data.get("page_candidates", []),
                "notes": data.get("notes", []),
                "artifact": data.get("artifact"),
            },
            ensure_ascii=False,
            indent=2,
        )
    finally:
        _destroy_sandbox(sb["id"])
