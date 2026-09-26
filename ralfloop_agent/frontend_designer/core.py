from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
from uuid import uuid4

import requests

from ralfloop_agent.programmer import ProgrammerAgent, ProgrammerConfig
from src.mcp_transport import MCPClientSession, MCPError, MCPProtocolError, StdioMCPTransport


_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+$")


@dataclass(frozen=True)
class FrontendDesignerConfig:
    allowed_roots: tuple[Path, ...]
    artifact_root: Path
    android_sdk: Path
    android_avd_home: Path
    android_avd_name: str = "ralf_frontend_ci_api23"
    android_emulator_port: int | None = None
    phone_device_id: str | None = None
    remote_android_url: str = "http://127.0.0.1:19232/mcp"
    mcp_remote_bin: Path = Path("/opt/ralf-canva-mcp/node_modules/.bin/mcp-remote")
    web_allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")

    @classmethod
    def from_environment(cls, *, project_root: Path | None = None) -> "FrontendDesignerConfig":
        raw_roots = os.getenv("RALF_FRONTEND_WORKTREE_ROOTS", os.getenv("RALF_CODE_WORKTREE_ROOTS", ""))
        roots = tuple(Path(item).expanduser() for item in raw_roots.split(":") if item.strip())
        artifact_root = Path(
            os.getenv("RALF_FRONTEND_ARTIFACT_ROOT", str(Path.home() / ".local" / "state" / "ralf-frontend-designer"))
        ).expanduser()
        sdk_raw = (
            os.getenv("RALF_ANDROID_SDK_ROOT")
            or os.getenv("ANDROID_SDK_ROOT")
            or os.getenv("ANDROID_HOME")
            or str(Path.home() / "Android" / "Sdk")
        )
        android_sdk = Path(sdk_raw).expanduser()
        android_avd_home = Path(
            os.getenv("ANDROID_AVD_HOME", str(Path.home() / ".android" / "avd"))
        ).expanduser()
        hosts = tuple(
            item.strip().casefold()
            for item in os.getenv("RALF_FRONTEND_WEB_HOSTS", "localhost,127.0.0.1,::1").split(",")
            if item.strip()
        )
        port_raw = os.getenv("RALF_FRONTEND_ANDROID_EMULATOR_PORT", "").strip()
        emulator_port = int(port_raw) if port_raw.isdigit() else None
        return cls(
            allowed_roots=roots,
            artifact_root=artifact_root,
            android_sdk=android_sdk,
            android_avd_home=android_avd_home,
            android_avd_name=os.getenv("RALF_FRONTEND_ANDROID_AVD", "ralf_frontend_ci_api23").strip() or "ralf_frontend_ci_api23",
            android_emulator_port=emulator_port,
            phone_device_id=os.getenv("RALF_FRONTEND_PHONE_DEVICE_ID", "").strip() or None,
            remote_android_url=os.getenv("RALF_TIREMM_ANDROID_INTERNAL_URL", "http://127.0.0.1:19232/mcp").strip(),
            mcp_remote_bin=Path(os.getenv(
                "RALF_MCP_REMOTE_BIN",
                "/opt/ralf-canva-mcp/node_modules/.bin/mcp-remote",
            )),
            web_allowed_hosts=hosts,
        )


class FrontendDesigner:
    """Bounded frontend implementation + evidence-oriented UI validation.

    Design edits are delegated to Bot-tazzi Programmatore. Web validation uses
    Playwright screenshots. Native Android validation is emulator-first and can
    then use the already-authenticated internal Tiremm Android MCP for a real phone.
    """

    def __init__(self, config: FrontendDesignerConfig) -> None:
        self.config = config

    def _resolve_allowed(self, value: str | Path, *, must_exist: bool = True) -> Path:
        candidate = Path(value).expanduser().resolve()
        roots = []
        for raw in self.config.allowed_roots:
            try:
                roots.append(raw.expanduser().resolve())
            except OSError:
                continue
        if not roots:
            raise ValueError("frontend_allowed_roots_missing")
        if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
            raise ValueError("frontend_path_outside_allowlist")
        if must_exist and not candidate.exists():
            raise ValueError("frontend_path_missing")
        return candidate

    def _run_dir(self, kind: str) -> Path:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.config.artifact_root / f"{kind}-{stamp}-{uuid4().hex[:8]}"
        target.mkdir(parents=True, mode=0o700, exist_ok=False)
        return target

    @staticmethod
    def _run(argv: Sequence[str], *, cwd: Path | None = None, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv), cwd=str(cwd) if cwd else None, check=False, text=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout,
        )

    def inspect_project(self, workdir: str | Path) -> dict[str, Any]:
        root = self._resolve_allowed(workdir)
        if not root.is_dir():
            raise ValueError("frontend_workdir_not_directory")
        package_json = root / "package.json"
        pubspec = root / "pubspec.yaml"
        gradlew = root / "gradlew"
        manifests = sorted(root.glob("**/src/main/AndroidManifest.xml"))[:20]
        targets: list[str] = []
        framework: list[str] = []
        scripts: dict[str, Any] = {}
        dependencies: set[str] = set()
        if package_json.is_file():
            try:
                payload = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            scripts = payload.get("scripts") if isinstance(payload.get("scripts"), dict) else {}
            for bucket in ("dependencies", "devDependencies"):
                raw = payload.get(bucket)
                if isinstance(raw, dict):
                    dependencies.update(str(name) for name in raw)
            targets.append("web")
            for name, markers in {
                "react": {"react", "next"}, "vue": {"vue", "nuxt"},
                "svelte": {"svelte", "@sveltejs/kit"}, "react-native": {"react-native", "expo"},
            }.items():
                if dependencies & markers:
                    framework.append(name)
            if dependencies & {"react-native", "expo"}:
                targets.append("mobile")
        if pubspec.is_file():
            targets.append("mobile")
            framework.append("flutter")
        if gradlew.is_file() or manifests:
            targets.append("android")
            framework.append("android-gradle")
        if any(root.glob("**/*.html")) and "web" not in targets:
            targets.append("web")
            framework.append("html")
        return {
            "ok": True,
            "workdir": str(root),
            "targets": sorted(set(targets)),
            "frameworks": sorted(set(framework)),
            "package_scripts": sorted(str(key) for key in scripts)[:80],
            "android_manifests": [str(path.relative_to(root)) for path in manifests],
            "git_worktree": (root / ".git").is_file(),
            "writes": 0,
            "external_side_effects": 0,
        }

    def design_contract(
        self,
        *,
        workdir: str | Path,
        brief: str,
        target: str = "auto",
    ) -> dict[str, Any]:
        root = self._resolve_allowed(workdir)
        text = brief.strip()
        if not text or len(text) > 8000:
            raise ValueError("frontend_design_brief_invalid")
        requested = target.strip().casefold()
        if requested not in {"auto", "web", "android", "mobile", "cross_platform"}:
            raise ValueError("frontend_design_target_invalid")
        inspection = self.inspect_project(root)
        detected = inspection["targets"]
        effective = requested
        if requested == "auto":
            if "android" in detected or "mobile" in detected:
                effective = "mobile"
            elif "web" in detected:
                effective = "web"
            else:
                effective = "cross_platform"
        mobile_target = effective in {"android", "mobile", "cross_platform"}
        web_target = effective in {"web", "cross_platform"}
        test_matrix: list[dict[str, Any]] = []
        if web_target:
            test_matrix.extend([
                {"surface": "web", "viewport": [390, 844], "label": "mobile"},
                {"surface": "web", "viewport": [768, 1024], "label": "tablet"},
                {"surface": "web", "viewport": [1440, 900], "label": "desktop"},
            ])
        if mobile_target:
            test_matrix.extend([
                {"surface": "android_emulator", "required": True, "order": 1},
                {"surface": "physical_phone", "required": "when_requested_or_risk_requires", "order": 2, "precondition": "emulator_gate_ok"},
            ])
        contract = {
            "ok": True,
            "workdir": str(root),
            "brief": text,
            "requested_target": requested,
            "effective_target": effective,
            "detected_targets": detected,
            "frameworks": inspection["frameworks"],
            "principles": [
                "preserve_existing_brand_tokens_unless_brief_explicitly_changes_them",
                "mobile_first_information_hierarchy",
                "responsive_layout_without_device_specific_hardcoding",
                "semantic_accessibility_and_keyboard_focus_for_web",
                "minimum_48dp_touch_targets_for_mobile",
                "explicit_loading_empty_error_disabled_and_success_states",
                "text_must_remain_readable_under_content_growth_and_localization",
            ],
            "implementation_constraints": {
                "reuse_existing_components": True,
                "avoid_business_logic_changes": True,
                "avoid_new_frontend_dependency_unless_necessary": True,
                "preserve_platform_navigation_conventions": True,
                "no_commit_push_or_deploy": True,
            },
            "acceptance": {
                "no_horizontal_overflow": web_target,
                "keyboard_focus_visible": web_target,
                "semantic_labels": True,
                "loading_empty_error_states": True,
                "touch_target_min_dp": 48 if mobile_target else None,
                "emulator_first": mobile_target,
                "phone_after_emulator_only": mobile_target,
            },
            "test_matrix": test_matrix,
            "inspection": inspection,
            "next_steps": [
                "apply_design_in_isolated_worktree",
                "run_static_or_project_validator",
                "run_platform_visual_gate",
                "review_artifacts_before_promotion",
            ],
            "writes": 0,
            "external_side_effects": 0,
        }
        return contract

    def design_apply(
        self,
        *,
        workdir: str | Path,
        brief: str,
        target: str = "auto",
        validator_command: str = "git diff --check",
    ) -> dict[str, Any]:
        root = self._resolve_allowed(workdir)
        contract = self.design_contract(workdir=root, brief=brief, target=target)
        text = str(contract["brief"])
        requested_target = str(contract["requested_target"])
        inspection = contract["inspection"]
        task = (
            "Implementa esclusivamente il frontend richiesto nel worktree indicato. "
            "Mantieni invariata la logica applicativa non necessaria. Progetta mobile-first, responsive, "
            "accessibile (semantica, focus, contrasto, touch target), con stati loading/empty/error e "
            "senza hardcode specifico del dispositivo. Non fare commit, push o deploy. "
            f"Target dichiarato: {requested_target}. Target rilevati: {inspection['targets']}. "
            f"Criteri di accettazione: {json.dumps(contract['acceptance'], ensure_ascii=False, separators=(',', ':'))}. "
            f"Brief: {text}"
        )
        state_root = self.config.artifact_root / "programmer-state"
        result = ProgrammerAgent(ProgrammerConfig(
            workdir=root,
            task=task,
            validator_command=validator_command,
            allowed_roots=tuple(path.expanduser() for path in self.config.allowed_roots),
            state_root=state_root,
            allow_test_changes=True,
        )).run()
        payload = result.as_dict()
        payload.update({
            "ok": result.state.value == "candidate_ready",
            "design_contract": contract,
            "inspection": inspection,
            "writes": 1 if result.state.value == "candidate_ready" else 0,
            "external_side_effects": 0,
            "verification_next": "run frontend web/android gate before promotion",
        })
        return payload

    def _web_url_allowed(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("frontend_web_url_invalid")
        host = parsed.hostname.casefold()
        if host in self.config.web_allowed_hosts:
            return url
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except (ValueError, OSError):
            ip = None
        if ip is not None and ip.is_loopback:
            return url
        raise ValueError("frontend_web_host_not_allowed")

    def test_web(self, *, url: str) -> dict[str, Any]:
        safe_url = self._web_url_allowed(url)
        run_dir = self._run_dir("web")
        try:
            response = requests.get(safe_url, timeout=10, allow_redirects=True)
            status = response.status_code
            final_url = response.url
        except requests.RequestException as exc:
            return {"ok": False, "error": f"web_unreachable:{type(exc).__name__}", "artifacts": str(run_dir), "writes": 0}
        viewports = (("mobile", 390, 844), ("tablet", 768, 1024), ("desktop", 1440, 900))
        captures: list[dict[str, Any]] = []
        for label, width, height in viewports:
            output = run_dir / f"{label}-{width}x{height}.png"
            proc = self._run([
                "npx", "--yes", "playwright", "screenshot", "--browser", "chromium",
                "--viewport-size", f"{width},{height}", "--full-page",
                "--wait-for-timeout", "800", "--timeout", "30000", safe_url, str(output),
            ], timeout=60)
            captures.append({
                "label": label,
                "viewport": [width, height],
                "ok": proc.returncode == 0 and output.is_file() and output.stat().st_size > 0,
                "screenshot": str(output) if output.is_file() else None,
                "stderr": proc.stderr[-1200:],
            })
        report = {
            "ok": 200 <= status < 400 and all(item["ok"] for item in captures),
            "url": safe_url,
            "final_url": final_url,
            "http_status": status,
            "captures": captures,
            "artifacts": str(run_dir),
            "writes": 0,
            "external_side_effects": 0,
        }
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    def _adb(self) -> Path:
        candidate = self.config.android_sdk / "platform-tools" / "adb"
        return candidate if candidate.is_file() else Path("/usr/bin/adb")

    def _emulator(self) -> Path:
        candidate = self.config.android_sdk / "emulator" / "emulator"
        if not candidate.is_file():
            raise RuntimeError("android_emulator_not_installed")
        return candidate

    def _running_emulators(self) -> list[dict[str, str]]:
        adb_bin = str(self._adb())
        devices = self._run([adb_bin, "devices"], timeout=20)
        rows: list[dict[str, str]] = []
        for line in devices.stdout.splitlines():
            if not line.startswith("emulator-") or "\tdevice" not in line:
                continue
            serial = line.split()[0]
            name_result = self._run([adb_bin, "-s", serial, "emu", "avd", "name"], timeout=10)
            names = [item.strip() for item in name_result.stdout.splitlines() if item.strip() and item.strip() != "OK"]
            rows.append({"serial": serial, "avd": names[0] if names else ""})
        return rows

    def _emulator_ready(self, serial: str) -> bool:
        adb_bin = str(self._adb())
        state = self._run([adb_bin, "-s", serial, "get-state"], timeout=10)
        if state.returncode != 0 or state.stdout.strip() != "device":
            return False
        package = self._run([adb_bin, "-s", serial, "shell", "service", "check", "package"], timeout=10)
        window = self._run([adb_bin, "-s", serial, "shell", "service", "check", "window"], timeout=10)
        return "Service package: found" in package.stdout and "Service window: found" in window.stdout

    def emulator_status(self) -> dict[str, Any]:
        emulator = self._emulator()
        avds = self._run([str(emulator), "-list-avds"], timeout=20)
        running = self._running_emulators()
        available = [line.strip() for line in avds.stdout.splitlines() if line.strip()]
        configured = [item for item in running if item.get("avd") == self.config.android_avd_name]
        return {
            "ok": avds.returncode == 0,
            "configured_avd": self.config.android_avd_name,
            "configured_port": self.config.android_emulator_port,
            "available_avds": available,
            "running_emulators": running,
            "configured_running": configured,
            "configured_ready": bool(configured and self._emulator_ready(configured[0]["serial"])),
            "acceleration": "software" if not Path("/dev/kvm").exists() else "kvm",
            "writes": 0,
        }

    def _ensure_emulator(self, *, timeout: float = 420.0) -> str:
        status = self.emulator_status()
        configured = status["configured_running"]
        if configured and status["configured_ready"]:
            return str(configured[0]["serial"])
        if self.config.android_avd_name not in status["available_avds"]:
            raise RuntimeError("frontend_android_avd_missing")

        process: subprocess.Popen[bytes] | None = None
        run_dir = self._run_dir("emulator-boot")
        log_path = run_dir / "emulator.log"
        log = log_path.open("wb")
        if not configured:
            argv = [
                str(self._emulator()), "-avd", self.config.android_avd_name,
                "-no-window", "-no-audio", "-no-boot-anim", "-gpu", "swiftshader_indirect",
            ]
            if self.config.android_emulator_port is not None:
                argv.extend(["-port", str(self.config.android_emulator_port)])
            if not Path("/dev/kvm").exists():
                argv.extend(["-accel", "off", "-cores", "1", "-memory", "1024"])
            env = os.environ.copy()
            env.update({
                "ANDROID_HOME": str(self.config.android_sdk),
                "ANDROID_SDK_ROOT": str(self.config.android_sdk),
                "ANDROID_AVD_HOME": str(self.config.android_avd_home),
            })
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
            )
        else:
            log.close()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for row in self._running_emulators():
                if row.get("avd") == self.config.android_avd_name and self._emulator_ready(row["serial"]):
                    if not log.closed:
                        log.close()
                    return row["serial"]
            if process is not None and process.poll() is not None:
                if not log.closed:
                    log.close()
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-1600:] if log_path.is_file() else ""
                raise RuntimeError("frontend_android_emulator_exited:" + tail)
            time.sleep(3)
        if not log.closed:
            log.close()
        raise RuntimeError("frontend_android_emulator_boot_timeout")

    def _android_build(self, root: Path) -> Path:
        gradlew = root / "gradlew"
        if not gradlew.is_file():
            raise ValueError("frontend_android_gradlew_missing")
        proc = self._run([str(gradlew), "assembleDebug", "--no-daemon"], cwd=root, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError("frontend_android_build_failed:" + proc.stderr[-1000:])
        apks = sorted(root.glob("**/build/outputs/apk/**/*debug*.apk"), key=lambda path: path.stat().st_mtime, reverse=True)
        if not apks:
            raise RuntimeError("frontend_android_apk_not_found")
        return apks[0]

    def test_android_emulator(
        self,
        *,
        workdir: str | Path,
        package: str,
        apk_path: str | Path | None = None,
    ) -> dict[str, Any]:
        root = self._resolve_allowed(workdir)
        if not _PACKAGE_RE.fullmatch(package):
            raise ValueError("frontend_android_package_invalid")
        apk = self._resolve_allowed(apk_path) if apk_path else self._android_build(root)
        if not apk.is_file() or apk.suffix.casefold() != ".apk":
            raise ValueError("frontend_android_apk_invalid")
        serial = self._ensure_emulator()
        adb = str(self._adb())
        install = self._run([adb, "-s", serial, "install", "-r", str(apk)], timeout=240)
        if install.returncode != 0 or "Success" not in install.stdout:
            raise RuntimeError("frontend_android_install_failed:" + (install.stderr or install.stdout)[-1000:])
        launch = self._run([
            adb, "-s", serial, "shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1",
        ], timeout=60)
        if launch.returncode != 0:
            raise RuntimeError("frontend_android_launch_failed:" + launch.stderr[-1000:])
        time.sleep(2)
        run_dir = self._run_dir("android-emulator")
        screenshot = run_dir / "screen.png"
        with screenshot.open("wb") as stream:
            shot = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"], check=False, stdout=stream, stderr=subprocess.PIPE, timeout=30)
        dump_argv = [adb, "-s", serial, "shell", "uiautomator", "dump", "/sdcard/window.xml"]
        try:
            dump = self._run(dump_argv, timeout=90)
        except subprocess.TimeoutExpired:
            dump = subprocess.CompletedProcess(
                dump_argv, returncode=124, stdout="", stderr="uiautomator_dump_timeout"
            )
        xml = self._run([adb, "-s", serial, "shell", "cat", "/sdcard/window.xml"], timeout=30) if dump.returncode == 0 else dump
        (run_dir / "ui.xml").write_text(xml.stdout, encoding="utf-8")
        size = self._run([adb, "-s", serial, "shell", "wm", "size"], timeout=20)
        focus = self._run([adb, "-s", serial, "shell", "dumpsys", "window", "windows"], timeout=30)
        package_visible = package in focus.stdout or package in xml.stdout
        report = {
            "ok": shot.returncode == 0 and screenshot.is_file() and screenshot.stat().st_size > 0 and package_visible,
            "serial": serial,
            "package": package,
            "apk": str(apk),
            "display": size.stdout.strip(),
            "package_visible": package_visible,
            "screenshot": str(screenshot),
            "ui_dump": str(run_dir / "ui.xml"),
            "ui_dump_ok": dump.returncode == 0,
            "ui_dump_error": dump.stderr[-500:] if dump.returncode != 0 else "",
            "artifacts": str(run_dir),
            "gate": "emulator_first",
            "writes": 1,
            "external_side_effects": 0,
        }
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return report

    def _remote_android(self, tool: str, arguments: Mapping[str, Any]) -> Any:
        bridge = self.config.mcp_remote_bin
        if not bridge.is_file():
            raise RuntimeError("frontend_mcp_remote_binary_missing")
        try:
            with MCPClientSession(
                StdioMCPTransport([str(bridge), self.config.remote_android_url]),
                timeout=120,
                client_name="ralf-frontend-designer",
            ) as session:
                found = {item.name for item in session.list_tools()}
                if tool not in found:
                    raise MCPProtocolError("frontend_remote_android_tool_missing")
                payload = session.call_tool(tool, arguments)
        except (MCPError, MCPProtocolError, OSError) as exc:
            raise RuntimeError(f"frontend_remote_android_failed:{exc}") from exc

        structured = payload.get("structuredContent")
        if isinstance(structured, dict):
            if set(structured) == {"result"}:
                return structured["result"]
            return structured.get("result", structured)
        content = payload.get("content") or ()
        if content and isinstance(content[0], Mapping):
            text = content[0].get("text")
            if isinstance(text, str):
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return payload

    def test_android_phone(
        self,
        *,
        package: str,
        emulator_report: str | Path,
        device_id: str | None = None,
    ) -> dict[str, Any]:
        if not _PACKAGE_RE.fullmatch(package):
            raise ValueError("frontend_android_package_invalid")
        report_path = Path(emulator_report).expanduser().resolve()
        artifact_root = self.config.artifact_root.expanduser().resolve()
        if not report_path.is_file() or not report_path.is_relative_to(artifact_root):
            raise ValueError("frontend_emulator_report_invalid")
        try:
            emulator_evidence = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("frontend_emulator_report_invalid") from exc
        if not isinstance(emulator_evidence, dict) or emulator_evidence.get("gate") != "emulator_first":
            raise ValueError("frontend_emulator_report_invalid")
        if emulator_evidence.get("ok") is not True:
            raise RuntimeError("frontend_emulator_gate_not_passed")
        if str(emulator_evidence.get("package") or "") != package:
            raise RuntimeError("frontend_emulator_gate_package_mismatch")
        selected = (device_id or self.config.phone_device_id or "").strip()
        args = {"device_id": selected} if selected else {}
        before = self._remote_android("android_inspect", args)
        launch_args = {"package": package, **args}
        launched = self._remote_android("android_launch", launch_args)
        time.sleep(1)
        after = self._remote_android("android_inspect", args)
        run_dir = self._run_dir("android-phone")
        report = {
            "ok": True,
            "package": package,
            "device_id": selected or None,
            "before": before,
            "launch": launched,
            "after": after,
            "gate": "physical_phone_after_emulator",
            "artifacts": str(run_dir),
            "writes": 1,
            "external_side_effects": 1,
        }
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return report
