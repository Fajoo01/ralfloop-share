from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .core import FrontendDesigner

MCP_PROTOCOL_VERSION = "2025-03-26"


def _schema(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
PACKAGE = {"type": "string", "pattern": r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$"}
TARGET = {"type": "string", "enum": ["auto", "web", "android", "mobile", "cross_platform"]}

TOOLS: dict[str, dict[str, Any]] = {
    "frontend_inspect_project": _schema({"workdir": PATH}, ("workdir",)),
    "frontend_design_contract": _schema({
        "workdir": PATH,
        "brief": {"type": "string", "minLength": 1, "maxLength": 8000},
        "target": TARGET,
    }, ("workdir", "brief")),
    "frontend_design_apply": _schema({
        "workdir": PATH,
        "brief": {"type": "string", "minLength": 1, "maxLength": 8000},
        "target": TARGET,
        "validator_command": {"type": "string", "minLength": 1, "maxLength": 1000},
    }, ("workdir", "brief")),
    "frontend_test_web": _schema({
        "url": {"type": "string", "minLength": 1, "maxLength": 2048},
    }, ("url",)),
    "frontend_emulator_status": _schema({}),
    "frontend_test_android_emulator": _schema({
        "workdir": PATH,
        "package": PACKAGE,
        "apk_path": PATH,
    }, ("workdir", "package")),
    "frontend_test_android": _schema({
        "workdir": PATH,
        "package": PACKAGE,
        "apk_path": PATH,
        "use_phone": {"type": "boolean"},
        "phone_device_id": {"type": "string", "minLength": 1, "maxLength": 256},
    }, ("workdir", "package")),
    "frontend_test_android_phone": _schema({
        "package": PACKAGE,
        "emulator_report": PATH,
        "device_id": {"type": "string", "minLength": 1, "maxLength": 256},
    }, ("package", "emulator_report")),
}


class FrontendDesignerMCPServer:
    def __init__(self, designer: FrontendDesigner) -> None:
        self.designer = designer

    def list_tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "frontend_inspect_project": "Inspect a frontend worktree and detect web/mobile/Android frameworks without modifying it.",
            "frontend_design_contract": "Create a structured cross-platform UI contract with acceptance criteria and a verification matrix.",
            "frontend_design_apply": "Apply a bounded frontend-only change through Bot-tazzi Programmatore; no commit, push or deploy.",
            "frontend_test_web": "Validate a local/private web UI at mobile, tablet and desktop viewports and capture screenshots.",
            "frontend_emulator_status": "Read Android emulator availability, exact configured AVD identity and readiness.",
            "frontend_test_android_emulator": "Build/install/launch an Android candidate in the configured emulator and create emulator-first evidence.",
            "frontend_test_android": "Run the Android emulator gate and optionally validate the already-installed package on a Tiremm Remote phone only after the gate passes.",
            "frontend_test_android_phone": "Validate on a Tiremm Remote Android device only after a matching successful emulator report.",
        }
        return [
            {"name": name, "description": descriptions[name], "inputSchema": schema}
            for name, schema in TOOLS.items()
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS:
            return _error("NOT_FOUND")
        if not isinstance(arguments, Mapping):
            return _error("MALFORMED_REQUEST")
        unknown = set(arguments) - set(TOOLS[name]["properties"])
        missing = set(TOOLS[name]["required"]) - set(arguments)
        if unknown or missing:
            return _error("MALFORMED_REQUEST")
        try:
            args = dict(arguments)
            if name == "frontend_inspect_project":
                value = self.designer.inspect_project(args["workdir"])
            elif name == "frontend_design_contract":
                value = self.designer.design_contract(
                    workdir=args["workdir"], brief=args["brief"], target=args.get("target", "auto")
                )
            elif name == "frontend_design_apply":
                value = self.designer.design_apply(
                    workdir=args["workdir"],
                    brief=args["brief"],
                    target=args.get("target", "auto"),
                    validator_command=args.get("validator_command", "git diff --check"),
                )
            elif name == "frontend_test_web":
                value = self.designer.test_web(url=args["url"])
            elif name == "frontend_emulator_status":
                value = self.designer.emulator_status()
            elif name == "frontend_test_android_emulator":
                value = self.designer.test_android_emulator(
                    workdir=args["workdir"], package=args["package"], apk_path=args.get("apk_path")
                )
            elif name == "frontend_test_android":
                if "use_phone" in args and not isinstance(args["use_phone"], bool):
                    return _error("MALFORMED_REQUEST:use_phone_must_be_boolean")
                emulator = self.designer.test_android_emulator(
                    workdir=args["workdir"], package=args["package"], apk_path=args.get("apk_path")
                )
                phone = None
                if args.get("use_phone"):
                    report = Path(str(emulator["artifacts"])) / "report.json"
                    phone = self.designer.test_android_phone(
                        package=args["package"],
                        emulator_report=report,
                        device_id=args.get("phone_device_id"),
                    )
                value = {
                    "ok": bool(emulator.get("ok")) and (phone is None or bool(phone.get("ok"))),
                    "gate": "emulator_first_then_optional_phone",
                    "emulator": emulator,
                    "phone": phone,
                    "writes": int(emulator.get("writes", 0)) + int(phone.get("writes", 0) if phone else 0),
                    "external_side_effects": int(phone.get("external_side_effects", 0) if phone else 0),
                }
            elif name == "frontend_test_android_phone":
                value = self.designer.test_android_phone(
                    package=args["package"], emulator_report=args["emulator_report"], device_id=args.get("device_id")
                )
            else:
                return _error("NOT_FOUND")
            return _result(value)
        except (TypeError, ValueError) as exc:
            return _error(f"MALFORMED_REQUEST:{exc}")
        except RuntimeError as exc:
            return _error(str(exc))
        except OSError as exc:
            return _error(f"SOURCE_UNAVAILABLE:{type(exc).__name__}")


def _result(value: Any) -> dict[str, Any]:
    ok = not (isinstance(value, Mapping) and value.get("ok") is False)
    payload = {"ok": ok, "result": value}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "structuredContent": payload,
        "isError": not ok,
    }


def _error(code: str) -> dict[str, Any]:
    payload = {"ok": False, "error": code}
    return {
        "content": [{"type": "text", "text": code}],
        "structuredContent": payload,
        "isError": True,
    }


__all__ = ["MCP_PROTOCOL_VERSION", "TOOLS", "FrontendDesignerMCPServer"]
