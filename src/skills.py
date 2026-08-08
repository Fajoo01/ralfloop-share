from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
import json
import logging
import os
from pathlib import Path

from src.routing_config import load_routing_config, trigger_matches

logger = logging.getLogger(__name__)


SkillHandler = Callable[[str], str]
DEFAULT_SKILLS_DIR = Path("/home/sibilla-cumana/ralfloop_data/skills")


@dataclass
class SkillSpec:
    name: str
    description: str
    triggers: tuple[str, ...] = field(default_factory=tuple)
    source: str = "builtin"
    enabled: bool = True
    runtime_module: str | None = None


def _skill_response(spec: SkillSpec) -> SkillHandler:
    def handler(user_goal: str) -> str:
        logger.info("skill_run skill=%s source=%s", spec.name, spec.source)
        return f"Skill {spec.name}: {spec.description}"

    return handler


class SkillsRegistry:
    def __init__(self, skills_dir: str | Path | None = None) -> None:
        self.config = load_routing_config()
        configured_dir = (
            skills_dir
            or os.getenv("RALF_SKILLS_DIR")
            or self.config.get("skills_dir")
            or DEFAULT_SKILLS_DIR
        )
        self.skills_dir = Path(configured_dir)
        self.low_signal_triggers = {
            str(trigger).lower() for trigger in self.config.get("low_signal_triggers", [])
        }
        self.specs = self._load_specs()
        self.skills: dict[str, SkillHandler] = {
            name: _skill_response(spec) for name, spec in self.specs.items()
        }
        self.keywords: dict[str, tuple[str, ...]] = {
            name: spec.triggers for name, spec in self.specs.items()
        }

    def match(self, user_goal: str) -> list[str]:
        goal = user_goal.lower()
        scored: list[tuple[int, int, str]] = []
        for index, (skill_name, words) in enumerate(self.keywords.items()):
            strong_hits = []
            low_hits = []
            for raw_word in words:
                word = raw_word.strip().lower()
                if not word or not trigger_matches(word, goal):
                    continue
                if word in self.low_signal_triggers:
                    low_hits.append(word)
                else:
                    strong_hits.append(word)
            if strong_hits or len(low_hits) >= 2:
                score = sum(len(hit) for hit in strong_hits) + len(low_hits)
                scored.append((-score, index, skill_name))
        return [name for _, _, name in sorted(scored)]

    def run(self, skill_name: str, user_goal: str) -> str:
        return self.skills[skill_name](user_goal)

    def metadata(self, skill_name: str) -> dict:
        spec = self.specs[skill_name]
        return {
            "name": spec.name,
            "description": spec.description,
            "triggers": list(spec.triggers),
            "source": spec.source,
            "runtime_module": spec.runtime_module,
        }

    def _load_specs(self) -> dict[str, SkillSpec]:
        specs = {spec.name: spec for spec in self._load_legacy_specs()}
        for spec in self._load_external_specs():
            previous = specs.get(spec.name)
            if previous is None:
                specs[spec.name] = spec
                continue
            merged_triggers = _dedupe((*previous.triggers, *spec.triggers))
            specs[spec.name] = SkillSpec(
                name=previous.name,
                description=spec.description or previous.description,
                triggers=merged_triggers,
                source=f"{previous.source}+{spec.source}",
                enabled=previous.enabled and spec.enabled,
                runtime_module=spec.runtime_module or previous.runtime_module,
            )
        return specs

    def _load_legacy_specs(self) -> list[SkillSpec]:
        specs = []
        for raw in self.config.get("legacy_skill_aliases", []) or []:
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            triggers = _dedupe(
                str(value).strip().lower()
                for value in raw.get("triggers", [])
                if str(value).strip()
            )
            specs.append(
                SkillSpec(
                    name=name,
                    description=str(raw.get("description") or name),
                    triggers=triggers,
                    source="config:legacy_skill_aliases",
                    enabled=True,
                    runtime_module=raw.get("runtime_module"),
                )
            )
        return specs

    def _load_external_specs(self) -> list[SkillSpec]:
        return list(_load_external_specs_cached(str(self.skills_dir)))


@lru_cache(maxsize=8)
def _load_external_specs_cached(skills_dir: str) -> tuple[SkillSpec, ...]:
    base = Path(skills_dir)
    if not base.exists():
        return ()
    specs: list[SkillSpec] = []
    for manifest_path in sorted(base.glob("*/skill.json")):
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("skill_manifest_skip path=%s error=%s", manifest_path, exc)
            continue
        if raw.get("enabled", True) is False:
            continue
        name = str(raw.get("skill_name") or raw.get("name") or manifest_path.parent.name).strip()
        if not name:
            continue
        description = str(raw.get("description") or name).strip()
        triggers = _manifest_triggers(raw)
        specs.append(
            SkillSpec(
                name=name,
                description=description,
                triggers=triggers,
                source=str(manifest_path),
                enabled=True,
                runtime_module=raw.get("runtime_module"),
            )
        )
    return tuple(specs)


def _manifest_triggers(raw: dict) -> tuple[str, ...]:
    values: list[str] = []
    for key in ("triggers", "examples"):
        raw_values = raw.get(key) or []
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        values.extend(str(value) for value in raw_values)
    return _dedupe(value.strip().lower() for value in values if str(value).strip())


def _dedupe(values) -> tuple[str, ...]:
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)
