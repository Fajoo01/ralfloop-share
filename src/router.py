from __future__ import annotations

import re

from src.models import CapabilityRoute
from src.skills import SkillsRegistry


CHECK_ONLY_RE = re.compile(r"\b(leggi|controlla|log|audit|mostra|review|diagnosi)\b", re.I)
PATCH_RE = re.compile(r"\b(correggi|fix|patch|risolvi bug|bugfix|ripara)\b", re.I)
EXTERNAL_RE = re.compile(r"\b(invia|manda|scrivi su drive|posta|telegram|browser|email|gmail|drive)\b", re.I)

MCP_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("google_workspace.gmail", ("email", "gmail", "mail", "invia", "manda")),
    ("telegram", ("telegram",)),
    ("google_workspace.drive", ("drive", "docs", "scrivi su drive")),
    ("browser", ("browser", "pagina", "url")),
)


class CapabilityRouter:
    def __init__(self, skills_registry: SkillsRegistry | None = None) -> None:
        self.skills = skills_registry or SkillsRegistry()

    def route(self, user_goal: str) -> CapabilityRoute:
        mode, reason = self._classify_mode(user_goal)
        skills_used = self.skills.match(user_goal)
        mcp_used = self._match_mcp(user_goal) if mode == "external_action" else []
        requires_confirmation = mode == "external_action" and any(connector != "browser" for connector in mcp_used)
        if mode == "external_action" and not mcp_used:
            requires_confirmation = True

        reasoning = (
            f"{reason}; skills={skills_used or ['none']}; "
            f"mcp={mcp_used or ['none']}; confirmation={requires_confirmation}"
        )
        return CapabilityRoute(
            mode=mode,
            reasoning=reasoning,
            skills_used=skills_used,
            mcp_used=mcp_used,
            requires_confirmation=requires_confirmation,
        )

    def _classify_mode(self, user_goal: str) -> tuple[str, str]:
        if PATCH_RE.search(user_goal):
            return "patch_allowed", "matched patch/fix keywords"
        if EXTERNAL_RE.search(user_goal):
            return "external_action", "matched external connector/action keywords"
        if CHECK_ONLY_RE.search(user_goal):
            return "check_only", "matched read/audit/check keywords"
        return "check_only", "defaulted to check_only for safety"

    def _match_mcp(self, user_goal: str) -> list[str]:
        goal = user_goal.lower()
        matched = []
        for connector, words in MCP_KEYWORDS:
            if any(word in goal for word in words):
                matched.append(connector)
        return matched


def route_task(user_goal: str) -> CapabilityRoute:
    return CapabilityRouter().route(user_goal)
