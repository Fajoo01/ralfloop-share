from __future__ import annotations

from collections.abc import Callable
import logging

logger = logging.getLogger(__name__)


SkillHandler = Callable[[str], str]


def _skill_response(name: str, message: str) -> SkillHandler:
    def handler(user_goal: str) -> str:
        logger.info("skill_run skill=%s", name)
        return f"Skill {name}: {message}"

    return handler


class SkillsRegistry:
    def __init__(self) -> None:
        self.skills: dict[str, SkillHandler] = {
            "bandi": _skill_response("bandi", "applico vincoli progetto/bandi locali"),
            "abc_memory": _skill_response("abc_memory", "analizzo memoria relazionale/RSC"),
            "jellyfin": _skill_response("jellyfin", "analizzo log media/download"),
            "garden_detector": _skill_response("garden_detector", "analizzo detector giardino/log/codice"),
            "trade_republic": _skill_response("trade_republic", "analizzo portfolio e regole rebound"),
        }
        self.keywords: dict[str, tuple[str, ...]] = {
            "bandi": ("bandi", "regione", "progetto", "intesa", "sanpaolo", "san paolo"),
            "abc_memory": ("rl:abc", "rsc", "abc_memory", "relazionale", "rlfull", "rlcalc"),
            "jellyfin": ("jellyfin", "film", "download", "media", "audio"),
            "garden_detector": ("giardino", "garden", "detector", "go2rtc", "yolo", "gemma"),
            "trade_republic": ("trade republic", "portfolio", "rebound", "borsa"),
        }

    def match(self, user_goal: str) -> list[str]:
        goal = user_goal.lower()
        matched = []
        for skill_name, words in self.keywords.items():
            if any(word in goal for word in words):
                matched.append(skill_name)
        return matched

    def run(self, skill_name: str, user_goal: str) -> str:
        return self.skills[skill_name](user_goal)
