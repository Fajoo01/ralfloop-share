#!/usr/bin/env python3
from __future__ import annotations

"""Semantic MCP for F-Gas air-conditioner installation documentation."""

import base64
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.fgas_installation import convert_docx_to_pdf, extract_record, normalize_record, render_docx, validate_record
from src.mcp_transport import MCPClientSession, MCP_PROTOCOL_VERSION, UnixMCPTransport


def _schema(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "required": list(required), "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 60000}
SHORT = {"type": "string", "maxLength": 500}
BOOL = {"type": "boolean"}
CHAT = {"type": "string", "pattern": r"^wa_chat_[a-f0-9]{16}$"}
MESSAGE = {"type": "string", "pattern": r"^wa_msg_[a-f0-9]{16}$"}
RECORD_PROPERTIES: dict[str, dict[str, Any]] = {
    "client_name": SHORT, "client_cf": SHORT, "client_email": SHORT, "client_phone": SHORT,
    "client_type": {"type": "string", "enum": ["PRIVATO", "IMPRESA", "ENTE", "ALTRO"]},
    "installer_name": SHORT, "installer_cf": SHORT, "install_address": SHORT, "civic": SHORT,
    "city": SHORT, "province": SHORT, "use_destination": SHORT, "brand": SHORT, "model": SHORT,
    "serial": SHORT, "refrigerant": SHORT, "charge_kg": SHORT, "compressor_count": SHORT,
    "fixed_leak_detection": BOOL, "hermetically_sealed": BOOL,
    "equipment_type": {"type": "string", "enum": ["POMPA DI CALORE FISSA", "FISSA DI CONDIZIONAMENTO", "FISSA DI REFRIGERAZIONE"]},
    "intervention_date": SHORT, "sold_by_installer": BOOL, "purchase_reference": SHORT, "purchase_date": SHORT,
    "gas_recovered": BOOL, "recovered_refrigerant": SHORT, "recovered_kg": SHORT,
    "gas_added": BOOL, "added_refrigerant": SHORT, "added_kg": SHORT,
    "added_gas_type": {"type": "string", "enum": ["VERGINE", "RIGENERATO", "RICICLATO"]},
    "added_gas_client_supplied": BOOL, "observations": SHORT,
}
RECORD = {"type": "object", "properties": RECORD_PROPERTIES, "additionalProperties": False}
BASENAME = {"type": "string", "pattern": r"^[A-Za-z0-9._-]{1,100}$"}
TOOLS: dict[str, dict[str, Any]] = {
    "fgas_drive_status": _schema({}),
    "fgas_extract_from_text": _schema({"text": TEXT, "overrides": RECORD}, ("text",)),
    "fgas_validate_installation": _schema({"record": RECORD}, ("record",)),
    "fgas_render_installation": _schema({"record": RECORD, "output_basename": BASENAME, "allow_incomplete": BOOL}, ("record",)),
    "fgas_prepare_from_whatsapp": _schema({
        "chat_id": CHAT, "chat_title": SHORT,
        "message_ids": {"type": "array", "items": MESSAGE, "maxItems": 12},
        "overrides": RECORD, "output_basename": BASENAME, "allow_incomplete": BOOL,
    }),
}


class DriveFGasSource:
    def __init__(self) -> None:
        self.socket = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
        self.account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "").strip()
        self.timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))
        self.cache_dir = _state_root() / "drive-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def status(self) -> dict[str, Any]:
        if not self.account:
            return {"ok": False, "status": "DRIVE_ACCOUNT_NOT_CONFIGURED", "side_effects": 0}
        try:
            template_rows = self.search("name contains 'MODULO INSTALLAZIONE'")
            examples = self.search("fullText contains 'interventifgas@gmail.com'")
        except Exception as exc:
            return {"ok": False, "status": "DRIVE_UNAVAILABLE", "error": type(exc).__name__, "side_effects": 0}
        selected = self.select_template(template_rows)
        examples = [row for row in examples if row.get("id") != (selected or {}).get("id")]
        return {"ok": bool(selected), "status": "READY" if selected else "TEMPLATE_NOT_FOUND", "template": selected, "examples": examples[:10], "side_effects": 0}

    def search(self, query: str) -> list[dict[str, str]]:
        result = self._call({"operation": "search", "email": self.account, "query": query, "maxResults": 50})
        return _parse_drive_files(_raw_text(result))

    @staticmethod
    def select_template(rows: list[dict[str, str]]) -> dict[str, str] | None:
        preferred = [row for row in rows if row.get("name") == "MODULO INSTALLAZIONE 2.docx" and "wordprocessingml.document" in row.get("mime", "")]
        return preferred[0] if preferred else None

    def sync_template(self) -> tuple[Path, dict[str, str]]:
        if not self.account:
            raise RuntimeError("drive_account_not_configured")
        selected = self.select_template(self.search("name contains 'MODULO INSTALLAZIONE'"))
        if not selected:
            raise RuntimeError("fgas_drive_template_not_found")
        result = self._call({"operation": "download", "email": self.account, "fileId": selected["id"], "outputPath": f"fgas-template-{selected['id']}.docx"})
        downloaded = _parse_download_path(_raw_text(result))
        if not downloaded or not downloaded.exists():
            raise RuntimeError("fgas_drive_template_download_missing")
        target = self.cache_dir / "MODULO_INSTALLAZIONE_2_DRIVE.docx"
        shutil.copy2(downloaded, target)
        return target, selected

    def _call(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        with MCPClientSession(UnixMCPTransport(self.socket, connect_timeout=2.0), timeout=self.timeout, client_name="ralf-fgas-drive-read") as client:
            names = {tool.name for tool in client.list_tools()}
            if "manage_drive" not in names:
                raise RuntimeError("manage_drive_not_discovered")
            return client.call_tool("manage_drive", dict(arguments))


class WhatsAppFGasSource:
    def __init__(self) -> None:
        self.socket = os.getenv("RALF_WHATSAPP_MCP_SOCKET", "/run/ralf-whatsapp-mcp/mcp.sock")
        self.timeout = float(os.getenv("RALF_WHATSAPP_MCP_TIMEOUT", "12"))

    def collect(self, *, chat_id: str = "", chat_title: str = "", message_ids: list[str] | None = None) -> dict[str, Any]:
        with MCPClientSession(UnixMCPTransport(self.socket, connect_timeout=2.0), timeout=self.timeout, client_name="ralf-fgas-whatsapp-read") as client:
            names = {tool.name for tool in client.list_tools()}
            required = {"whatsapp_search_chats", "whatsapp_read_messages", "whatsapp_get_media"}
            if not required <= names:
                raise RuntimeError("whatsapp_fgas_tools_not_discovered")
            if not chat_id:
                if not chat_title:
                    raise ValueError("chat_id_or_chat_title_required")
                matches = _structured(client.call_tool("whatsapp_search_chats", {"query": chat_title, "limit": 20}))
                rows = matches.get("results") if isinstance(matches, Mapping) else None
                rows = rows if isinstance(rows, list) else []
                if len(rows) != 1:
                    return {"ok": False, "status": "CHAT_NOT_UNIQUE", "candidates": rows[:10], "side_effects": 0}
                chat_id = str(rows[0].get("chat_id") or "")
            history = _structured(client.call_tool("whatsapp_read_messages", {"chat_id": chat_id, "limit": 40}))
            messages = history.get("messages") if isinstance(history, Mapping) else None
            messages = [dict(row) for row in messages if isinstance(row, Mapping)] if isinstance(messages, list) else []
            chosen = list(message_ids or [])
            cluster_timestamp = ""
            cluster_rows = messages[-20:]
            if not chosen:
                non_text = [row for row in messages if str(row.get("kind") or "text") != "text" and row.get("message_id")]
                if non_text:
                    cluster_timestamp = str(non_text[-1].get("timestamp") or "")
                    if cluster_timestamp:
                        cluster_rows = [row for row in messages if str(row.get("timestamp") or "") == cluster_timestamp]
                    else:
                        cluster_rows = non_text[-8:]
                    chosen = [
                        str(row.get("message_id") or "") for row in cluster_rows
                        if str(row.get("kind") or "text") != "text" and row.get("message_id")
                    ][:12]
            else:
                chosen_set = set(chosen)
                selected_rows = [row for row in messages if str(row.get("message_id") or "") in chosen_set]
                timestamps = [str(row.get("timestamp") or "") for row in selected_rows if row.get("timestamp")]
                cluster_timestamp = timestamps[-1] if timestamps else ""
                if cluster_timestamp:
                    cluster_rows = [row for row in messages if str(row.get("timestamp") or "") == cluster_timestamp]
                else:
                    cluster_rows = selected_rows
            texts = [str(row.get("text") or "") for row in cluster_rows if row.get("text")]
            media_rows: list[dict[str, Any]] = []
            for message_id in chosen:
                result = client.call_tool("whatsapp_get_media", {"chat_id": chat_id, "message_id": message_id})
                payload = _structured(result)
                rows = payload.get("results") if isinstance(payload, Mapping) else None
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, Mapping):
                        media_rows.append(dict(row))
                        if row.get("extracted_text"):
                            texts.append(str(row["extracted_text"]))
                enhanced = _enhanced_image_text(result)
                if enhanced:
                    texts.append(enhanced)
            return {"ok": True, "status": "READY", "chat_id": chat_id, "message_ids": chosen, "cluster_timestamp": cluster_timestamp, "text": "\n".join(texts), "media": media_rows, "side_effects": 0}


class FGasMCPServer:
    def __init__(self, drive: DriveFGasSource | None = None, whatsapp: WhatsAppFGasSource | None = None) -> None:
        self.drive = drive or DriveFGasSource()
        self.whatsapp = whatsapp or WhatsAppFGasSource()

    def list_tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "fgas_drive_status": "Read-only discovery of the current F-Gas Drive template and prior examples.",
            "fgas_extract_from_text": "Extract structured installation fields from supplied evidence text.",
            "fgas_validate_installation": "Validate workflow fields without inventing missing data.",
            "fgas_render_installation": "Render DOCX/PDF from the current Drive template.",
            "fgas_prepare_from_whatsapp": "Read forwarded WhatsApp evidence, extract/validate it, and render when complete.",
        }
        return [{"name": name, "description": descriptions[name], "inputSchema": schema} for name, schema in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS or _validate_schema(arguments, TOOLS[name]):
            return _mcp_result({"ok": False, "status": "POLICY_DENIED", "side_effects": 0}, error=True)
        try:
            if name == "fgas_drive_status":
                return _mcp_result(self.drive.status())
            if name == "fgas_extract_from_text":
                record = _apply_configured_defaults(
                    extract_record(str(arguments["text"]), arguments.get("overrides") or {})
                )
                return _mcp_result(_validation_payload(validate_record(record), record=record))
            if name == "fgas_validate_installation":
                return _mcp_result(_validation_payload(validate_record(arguments["record"])))
            if name == "fgas_render_installation":
                return _mcp_result(self._render(arguments["record"], basename=str(arguments.get("output_basename") or "MODULO_INSTALLAZIONE_COMPILATO"), allow_incomplete=bool(arguments.get("allow_incomplete", False))))
            if name == "fgas_prepare_from_whatsapp":
                source = self.whatsapp.collect(chat_id=str(arguments.get("chat_id") or ""), chat_title=str(arguments.get("chat_title") or ""), message_ids=list(arguments.get("message_ids") or ()))
                if not source.get("ok"):
                    return _mcp_result(source, error=True)
                source_text = _repair_truncated_years(
                    str(source.get("text") or ""), str(source.get("cluster_timestamp") or ""),
                )
                record = _apply_configured_defaults(
                    extract_record(source_text, arguments.get("overrides") or {})
                )
                validation = validate_record(record)
                payload = {**_validation_payload(validation, record=record), "source": {"chat_id": source.get("chat_id"), "message_ids": source.get("message_ids"), "cluster_timestamp": source.get("cluster_timestamp"), "media_count": len(source.get("media") or ())}}
                if validation.ok or bool(arguments.get("allow_incomplete", False)):
                    payload["render"] = self._render(validation.normalized, basename=str(arguments.get("output_basename") or "MODULO_INSTALLAZIONE_COMPILATO"), allow_incomplete=bool(arguments.get("allow_incomplete", False)))
                return _mcp_result(payload)
        except Exception as exc:
            return _mcp_result({"ok": False, "status": "SOURCE_UNAVAILABLE", "error": type(exc).__name__, "side_effects": 0}, error=True)
        return _mcp_result({"ok": False, "status": "POLICY_DENIED", "side_effects": 0}, error=True)

    def _render(self, record: Mapping[str, Any], *, basename: str, allow_incomplete: bool) -> dict[str, Any]:
        validation = validate_record(record)
        if validation.missing and not allow_incomplete:
            return {"ok": False, "status": "INCOMPLETE", "missing": list(validation.missing), "warnings": list(validation.warnings), "record": validation.normalized, "side_effects": 0}
        template, metadata = self.drive.sync_template()
        out_dir = _state_root() / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = _safe_basename(basename)
        docx_path = out_dir / f"{safe}.docx"
        pdf_path = out_dir / f"{safe}.pdf"
        render_docx(validation.normalized, template, docx_path)
        convert_docx_to_pdf(docx_path, pdf_path)
        return {"ok": True, "status": "RENDERED" if not validation.missing else "RENDERED_INCOMPLETE", "missing": list(validation.missing), "warnings": list(validation.warnings), "template": metadata, "artifacts": {"docx": str(docx_path), "pdf": str(pdf_path), "docx_sha256": _sha256(docx_path), "pdf_sha256": _sha256(pdf_path)}, "side_effects": 2, "external_writes": 0, "external_sends": 0}


def _state_root() -> Path:
    root = Path(os.getenv("RALF_FGAS_STATE_DIR", str(Path.home() / ".local/state/ralf/fgas")))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_basename(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")[:100]
    return value or "MODULO_INSTALLAZIONE_COMPILATO"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _enhanced_image_text(result: Mapping[str, Any]) -> str:
    structured = _structured(result)
    rows = structured.get("results") if isinstance(structured, Mapping) else None
    hint = " ".join(
        str(row.get("extracted_text") or "") for row in rows
        if isinstance(rows, list) and isinstance(row, Mapping)
    ).casefold()
    if not any(term in hint for term in ("bosch", "climate", "thermotech")):
        return ""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    encoded = next((
        str(item.get("data") or "") for item in content
        if isinstance(item, Mapping) and item.get("type") == "image" and item.get("data")
    ), "")
    if not encoded:
        return ""
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        return ""
    if not raw or len(raw) > 8 * 1024 * 1024:
        return ""
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return ""
    path = ""
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        image = Image.open(io.BytesIO(raw)).convert("L")
        image = ImageEnhance.Contrast(image).enhance(2.2)
        image = image.resize((image.width * 3, image.height * 3))
        image = image.filter(ImageFilter.SHARPEN)
        with tempfile.NamedTemporaryFile(prefix="ralf-fgas-ocr-", suffix=".png", delete=False) as handle:
            path = handle.name
            image.save(handle, format="PNG")
        completed = subprocess.run(
            [tesseract, path, "stdout", "-l", "ita+eng", "--psm", "6"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=30, check=False,
        )
        return " ".join(completed.stdout.split())[:12000] if completed.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def _raw_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(item.get("text") or "") for item in content if isinstance(item, Mapping) and item.get("type") == "text")


def _structured(result: Mapping[str, Any]) -> Mapping[str, Any]:
    value = result.get("structuredContent")
    return value if isinstance(value, Mapping) else {}


def _parse_drive_files(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("---") or " | " not in line:
            continue
        parts = [part.strip() for part in line.split(" | ")]
        if len(parts) < 3 or not re.fullmatch(r"[A-Za-z0-9_-]{10,}", parts[0]):
            continue
        rows.append({"id": parts[0], "name": parts[1], "mime": parts[2], "modified": parts[3] if len(parts) > 3 else "", "size": parts[4] if len(parts) > 4 else ""})
    return rows


def _parse_download_path(text: str) -> Path | None:
    match = re.search(r"(?m)^\*\*Path:\*\*\s+(.+)$", text)
    return Path(match.group(1).strip()) if match else None


def _apply_configured_defaults(record: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(record)
    installer_name = os.getenv("RALF_FGAS_DEFAULT_INSTALLER_NAME", "").strip()
    installer_cf = os.getenv("RALF_FGAS_DEFAULT_INSTALLER_CF", "").strip()
    private_destination = os.getenv("RALF_FGAS_PRIVATE_USE_DESTINATION", "").strip()
    if installer_name:
        out.setdefault("installer_name", installer_name)
    if installer_cf:
        out.setdefault("installer_cf", installer_cf)
    if str(out.get("client_type") or "").upper() == "PRIVATO" and private_destination:
        out.setdefault("use_destination", private_destination)
    return normalize_record(out)


def _repair_truncated_years(text: str, cluster_timestamp: str) -> str:
    """Repair a 3-digit year only when it equals the cluster year with one digit omitted."""
    match = re.search(r"\b(20\d{2})\b", cluster_timestamp)
    if not match:
        return text
    year = match.group(1)
    shortened = {year[:index] + year[index + 1:] for index in range(4)}

    def replace(item: re.Match[str]) -> str:
        token = item.group(3)
        if token not in shortened:
            return item.group(0)
        return f"{item.group(1)}/{item.group(2)}/{year}"

    return re.sub(r"\b([0-3]?\d)/([01]?\d)/(\d{3})\b", replace, text)


def _validation_payload(validation: Any, *, record: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": validation.ok, "status": "COMPLETE" if validation.ok else "INCOMPLETE", "record": dict(record or validation.normalized), "normalized": validation.normalized, "missing": list(validation.missing), "warnings": list(validation.warnings), "side_effects": 0}


def _validate_schema(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    return _validate_value(arguments, schema, "arguments")


def _validate_value(value: Any, schema: Mapping[str, Any], path: str) -> str:
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, Mapping):
            return f"{path}:object_required"
        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return f"{path}:unexpected_argument"
        missing = set(schema.get("required") or ()) - set(value)
        if missing:
            return f"{path}:missing_required_argument"
        for key, item in value.items():
            error = _validate_value(item, properties[key], f"{path}.{key}")
            if error:
                return error
        return ""
    if expected == "array":
        if not isinstance(value, list):
            return f"{path}:array_required"
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            return f"{path}:too_many_items"
        item_schema = schema.get("items") or {}
        for index, item in enumerate(value):
            error = _validate_value(item, item_schema, f"{path}[{index}]")
            if error:
                return error
        return ""
    if expected == "string":
        if not isinstance(value, str):
            return f"{path}:string_required"
        if len(value) < int(schema.get("minLength", 0)) or len(value) > int(schema.get("maxLength", 1_000_000)):
            return f"{path}:length_invalid"
        if schema.get("pattern") and not re.fullmatch(str(schema["pattern"]), value):
            return f"{path}:pattern_invalid"
        if schema.get("enum") and value not in set(schema["enum"]):
            return f"{path}:enum_invalid"
        return ""
    if expected == "boolean":
        return "" if isinstance(value, bool) else f"{path}:boolean_required"
    return ""


def _mcp_result(payload: Mapping[str, Any], *, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(dict(payload), ensure_ascii=False)}], "structuredContent": dict(payload), "isError": error}


def _response(request: Mapping[str, Any], server: FGasMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "ralf-fgas-installation", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params") or {}
        result = server.call(str(params.get("name") or ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    server = FGasMCPServer()
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = _response(request, server)
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal_error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
