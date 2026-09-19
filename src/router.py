from __future__ import annotations

from ralfloop_agent.integration.collaboration_backend import select_collaboration_backend
from src.models import CapabilityRoute, JuryPolicy, VerificationPolicy
from src.routing_config import (
    any_unnegated_trigger_matches,
    any_trigger_matches,
    configured_jury_roles,
    load_routing_config,
    local_jury_style,
    local_jury_style_source,
    mcp_keywords,
    read_mcp_keywords,
    verification_config,
)
from src.skills import SkillsRegistry, is_local_maintenance_intent


ROUTING_CONFIG = load_routing_config()
MCP_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = mcp_keywords(ROUTING_CONFIG)
READ_MCP_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = read_mcp_keywords(ROUTING_CONFIG)


class CapabilityRouter:
    def __init__(self, skills_registry: SkillsRegistry | None = None) -> None:
        self.config = load_routing_config()
        self.mode_keywords = dict(self.config.get("mode_keywords") or {})
        self.jury_config = dict(self.config.get("jury") or {})
        self.mcp_keywords = mcp_keywords(self.config)
        self.read_mcp_keywords = read_mcp_keywords(self.config)
        self.jury_roles = configured_jury_roles(self.config)
        self.skills = skills_registry or SkillsRegistry()

    def route(self, user_goal: str) -> CapabilityRoute:
        local_maintenance = is_local_maintenance_intent(user_goal)
        mode, reason = (
            self._classify_local_maintenance_mode(user_goal)
            if local_maintenance
            else self._classify_mode(user_goal)
        )
        skills_used = self.skills.match(user_goal)
        if local_maintenance:
            skills_used = [
                skill for skill in skills_used
                if skill not in {"bandi", "bandi_browser_fill"}
            ]
            if "local_maintenance" not in skills_used:
                skills_used.insert(0, "local_maintenance")
        if mode == "external_action" and not local_maintenance:
            mcp_used = self._match_mcp(user_goal)
        elif not local_maintenance:
            mcp_used = self._match_read_mcp(user_goal)
        else:
            mcp_used = []
        requires_confirmation = mode == "external_action" and any(connector != "browser" for connector in mcp_used)
        if mode == "external_action" and not mcp_used:
            requires_confirmation = True
        jury_policy = self._jury_policy(user_goal, mode, skills_used, requires_confirmation)
        style = local_jury_style(self.config, jury_policy.triggers[0] if jury_policy.triggers else None)
        collaboration_backend = select_collaboration_backend(
            jury_policy,
            style=style,
            style_selection_source=local_jury_style_source(self.config),
            route_only=True,
        )
        verification_policy = self._verification_policy(mode)

        reasoning = (
            f"{reason}; skills={skills_used or ['none']}; "
            f"mcp={mcp_used or ['none']}; confirmation={requires_confirmation}; "
            f"jury={jury_policy.mode}; backend={collaboration_backend.selected_backend}; "
            f"verification={verification_policy.verifier_type}"
        )
        return CapabilityRoute(
            mode=mode,
            reasoning=reasoning,
            skills_used=skills_used,
            mcp_used=mcp_used,
            requires_confirmation=requires_confirmation,
            jury_policy=jury_policy,
            collaboration_backend=collaboration_backend,
            verification_policy=verification_policy,
            jury=jury_policy,
        )

    def _classify_mode(self, user_goal: str) -> tuple[str, str]:
        goal = user_goal.lower()
        patch_hit = any_unnegated_trigger_matches(self.mode_keywords.get("patch_allowed", []), goal)
        check_hit = any_trigger_matches(self.mode_keywords.get("check_only", []), goal)
        inspection_hit = any_trigger_matches(self.mode_keywords.get("read_only_system_inspection", []), goal)
        external_hit = any_unnegated_trigger_matches(self.mode_keywords.get("external_action", []), goal)
        side_effect_hit = any_unnegated_trigger_matches(
            self.mode_keywords.get("external_action_verbs", []),
            goal,
        )
        if side_effect_hit:
            return "external_action", "matched unnegated side-effect keyword"
        if inspection_hit and (check_hit or not patch_hit):
            return "read_only_system_inspection", "matched read-only system inspection keywords"
        if external_hit:
            return "external_action", "matched unnegated external connector/action keyword"
        if patch_hit:
            return "patch_allowed", "matched patch/fix keywords"
        if check_hit:
            return "check_only", "matched read/audit/check keywords"
        return "check_only", "defaulted to check_only for safety"

    def _classify_local_maintenance_mode(self, user_goal: str) -> tuple[str, str]:
        goal = user_goal.lower()
        protected = (
            "riavvia", "restart", "daemon-reload", "daemon reload", "reload service",
            "arresta", "stop service", "termina processo", "kill", "chmod", "chown",
        )
        if any_unnegated_trigger_matches(protected, goal):
            return "external_action", "matched canonical protected local maintenance action"
        if any_unnegated_trigger_matches(self.mode_keywords.get("patch_allowed", []), goal):
            return "patch_allowed", "matched local software patch intent"
        return "check_only", "matched local software maintenance inspection/test intent"

    def _match_mcp(self, user_goal: str) -> list[str]:
        goal = user_goal.lower()
        matched = []
        for connector, words in self.mcp_keywords:
            if any_unnegated_trigger_matches(words, goal):
                matched.append(connector)
        return matched

    def _match_read_mcp(self, user_goal: str) -> list[str]:
        goal = user_goal.lower()
        matched = []
        for connector, words in self.read_mcp_keywords:
            if any_trigger_matches(words, goal):
                matched.append(connector)
        return matched

    def _jury_policy(
        self,
        user_goal: str,
        mode: str,
        skills_used: list[str],
        requires_confirmation: bool,
    ) -> JuryPolicy:
        goal = user_goal.lower()
        triggers = []
        if any_trigger_matches(self.jury_config.get("explicit_triggers", []), goal):
            triggers.append("explicit_jury")
            return self._jury_result("required", "explicit_jury_or_telepathy_request", triggers, requires_confirmation)
        if requires_confirmation:
            triggers.append("external_side_effect")
            return self._jury_result("required", "external_action_needs_pre_execution_review", triggers, requires_confirmation)
        if mode == "patch_allowed":
            triggers.append("patch_task")
        min_skills = int(self.jury_config.get("min_skills_for_multi_skill", 2))
        if len(skills_used) >= min_skills:
            triggers.append("multi_skill")
        if any_trigger_matches(self.jury_config.get("complexity_triggers", []), goal):
            triggers.append("complexity_keyword")
        if triggers:
            return self._jury_result("advisory", "expanded_jury_for_risk_or_complexity", triggers, requires_confirmation)
        return JuryPolicy(requires_human_confirmation=requires_confirmation)

    def _jury_result(self, mode: str, reason: str, triggers: list[str], requires_confirmation: bool) -> JuryPolicy:
        return JuryPolicy(
            enabled=True,
            mode=mode,
            reason=reason,
            roles=self.jury_roles,
            triggers=triggers,
            requires_final_review=mode == "required",
            requires_human_confirmation=requires_confirmation,
        )

    def _verification_policy(self, mode: str) -> VerificationPolicy:
        cfg = verification_config(self.config, mode)
        return VerificationPolicy(**cfg)


def jury_policy_manifest() -> dict:
    config = load_routing_config()
    jury = dict(config.get("jury") or {})
    return {
        "modes": ["off", "advisory", "required"],
        "required_for": list(jury.get("required_for", [])),
        "advisory_for": list(jury.get("advisory_for", [])),
        "roles": configured_jury_roles(config),
        "style_selection_source": local_jury_style_source(config),
        "style_by_trigger": dict(jury.get("style_by_trigger") or {}),
        "final_score_owner": "deterministic_formula_or_route_policy",
    }


def route_task(user_goal: str) -> CapabilityRoute:
    return CapabilityRouter().route(user_goal)
