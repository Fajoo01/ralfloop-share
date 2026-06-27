from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


READ_ONLY_MARKERS = (
    "check_only",
    "read_only",
    "analysis_only",
    "no_write",
    "non modificare",
    "non toccare",
    "controlla",
    "verifica",
    "analizza",
    "audit",
)

PATCH_MARKERS = (
    "patch task",
    "patch_allowed",
    "write_allowed",
    "modifica",
    "correggi",
    "fixa",
    "fix ",
    "implementa",
    "integra",
    "crea file",
    "aggiorna file",
    "py_compile",
    "pytest",
    "git commit",
)

SEND_MARKERS = (
    "invia email",
    "manda email",
    "manda la email",
    "send email",
    "gmail send",
    "manda su telegram",
    "invia su telegram",
    "send telegram",
)

HUMAN_CONFIRMATION_MARKERS = (
    "conferma umana",
    "human confirmation",
    "approvo",
    "confermo",
    "invia",
)

DOMAIN_SKILLS = (
    ("bandi", ("bando", "bandi", "regione", "contributo", "rimodulazione")),
    ("atm", ("atm", "metro", "tram", "autobus", "percorso mezzi")),
    ("jellyfin", ("jellyfin", "film", "serie", "download", "fake video")),
    ("garden_detector", ("giardino", "garden", "detector", "persona", "telegram_sent")),
    ("abc_relcalc", ("abc_relcalc", "relcalc", "calcolatrice relazionale", "calculate_relation")),
    ("abc_memory", ("rl:abc", "rsc:abc", "abc_memory", "memoria abc", "abc memoria")),
    ("trade_republic", ("trade republic", "pac", "ribassi", "martingala")),
    ("email_ops", ("email_ops", "email ops")),
    ("responder", ("risponditore", "bozza", "gmail", "whatsapp", "wapp")),
)

MCP_CONNECTORS = (
    ("google_workspace.gmail", ("gmail", "email", "mail", "posta")),
    ("telegram.bot", ("telegram",)),
    ("google_workspace.drive", ("drive", "google drive", "documento condiviso")),
    ("browser", ("browser", "sito", "pagina web")),
)


@dataclass(frozen=True)
class CapabilityRoute:
    task_mode: str
    write_policy: str
    evidence_first: bool
    needs_human_confirmation: bool
    domain_skills: list[str] = field(default_factory=list)
    mcp_connectors: list[str] = field(default_factory=list)
    shell_evidence_tools: list[str] = field(default_factory=list)
    required_output_fields: list[str] = field(default_factory=list)
    blocked_actions: list[str] = field(default_factory=list)
    workflow: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _collect_named(text: str, registry: tuple[tuple[str, tuple[str, ...]], ...]) -> list[str]:
    out: list[str] = []
    for name, markers in registry:
        if _has_any(text, markers):
            out.append(name)
    return out


def route_task(
    user_goal: str,
    skill_context: str | None = None,
    extra_context: dict[str, Any] | None = None,
) -> CapabilityRoute:
    """Deterministic pre-route for Codex-like Ralf runs.

    Skills are domain knowledge. MCP connectors are external capabilities.
    Shell tools collect evidence. Sends and writes stay gated.
    """

    extra_context = extra_context or {}
    text = f"{user_goal or ''}\n{skill_context or ''}".lower()

    wants_send = _has_any(text, SEND_MARKERS)
    has_read_only = _has_any(text, READ_ONLY_MARKERS)
    has_patch = _has_any(text, PATCH_MARKERS)

    if wants_send:
        task_mode = "external_action"
        write_policy = "external_side_effect_requires_confirmation"
    elif has_patch and not has_read_only:
        task_mode = "patch_allowed"
        write_policy = "sandbox_write_allowed_after_repro"
    else:
        task_mode = "check_only"
        write_policy = "no_write"

    domain_skills = _collect_named(text, DOMAIN_SKILLS)
    mcp_connectors = _collect_named(text, MCP_CONNECTORS)

    if wants_send and "google_workspace.gmail" not in mcp_connectors:
        mcp_connectors.append("google_workspace.gmail")
    if "telegram" in text and "telegram.bot" not in mcp_connectors:
        mcp_connectors.append("telegram.bot")

    needs_human_confirmation = wants_send
    if extra_context.get("human_confirmed") is True:
        needs_human_confirmation = False

    shell_evidence_tools = ["rg", "find", "git status"]
    required_output_fields = ["command", "path", "exit_code"]
    workflow = ["classify_task", "collect_real_evidence", "cite_command_path_exit_code"]
    blocked_actions = ["invented_verification", "silent_send"]

    if task_mode == "patch_allowed":
        shell_evidence_tools.extend(["python3 -m py_compile", "pytest"])
        workflow.extend(["reproduce_failure", "apply_minimal_patch", "run_targeted_tests"])
        required_output_fields.extend(["diff", "tests"])
        blocked_actions.append("patch_without_repro")

    if task_mode == "external_action":
        workflow.extend(["prepare_draft", "request_human_confirmation", "execute_mcp_after_confirmation"])
        blocked_actions.extend(["send_without_human_confirmation", "write_external_state_without_confirmation"])

    if not domain_skills:
        domain_skills = ["general"]

    if not mcp_connectors:
        mcp_connectors = ["none"]

    reason = (
        "skills=domain_reasoning; mcp=external_actions; shell=evidence; "
        f"mode={task_mode}; write_policy={write_policy}"
    )

    return CapabilityRoute(
        task_mode=task_mode,
        write_policy=write_policy,
        evidence_first=True,
        needs_human_confirmation=needs_human_confirmation,
        domain_skills=domain_skills,
        mcp_connectors=mcp_connectors,
        shell_evidence_tools=shell_evidence_tools,
        required_output_fields=required_output_fields,
        blocked_actions=blocked_actions,
        workflow=workflow,
        reason=reason,
    )

