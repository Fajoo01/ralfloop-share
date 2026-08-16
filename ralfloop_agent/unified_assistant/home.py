from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .contracts import PolicyClass


class HomeEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str = Field(pattern=r"^[a-z_]+\.[a-z0-9_]+$")
    friendly_name: str = Field(min_length=1, max_length=160)
    aliases: tuple[str, ...] = Field(min_length=1, max_length=24)
    area: str = Field(min_length=1, max_length=96)
    domain: str | None = Field(default=None, pattern=r"^[a-z_]+$")
    device_class: str = Field(default="unknown", min_length=1, max_length=96)
    capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=24)
    allowed_actions: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias=AliasChoices("allowed_actions", "allowed_services"),
        serialization_alias="allowed_actions",
        max_length=16,
    )
    service_overrides: dict[str, str] = Field(default_factory=dict)
    expected_states: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    minimum: float | None = None
    maximum: float | None = None
    protected: bool = False
    auto_write: bool = False

    @model_validator(mode="after")
    def validate_safety(self) -> "HomeEntity":
        entity_domain = self.entity_id.split(".", 1)[0]
        if self.domain is not None and self.domain != entity_domain:
            raise ValueError("home_entity_domain_mismatch")
        if self.protected and self.auto_write:
            raise ValueError("protected_entity_cannot_auto_write")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("invalid_entity_range")
        return self

    @property
    def effective_domain(self) -> str:
        return self.domain or self.entity_id.split(".", 1)[0]

    @property
    def allowed_services(self) -> tuple[str, ...]:
        """Compatibility alias for pre-v1 callers."""
        return self.allowed_actions


class HomeCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: Literal[
        "read", "turn_on", "turn_off", "set_temperature", "adjust_temperature",
        "open_cover", "close_cover",
    ]
    target_text: str = Field(min_length=1, max_length=160)
    value: float | None = None
    all_in_area: bool = False


class HomePreparedAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: HomeCommand
    entities: tuple[HomeEntity, ...]
    policy: PolicyClass
    reason: str


class HomeExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "read", "confirmation_required", "protected_approval_required", "verified",
        "command_sent_unverified", "denied", "unavailable",
    ]
    policy: PolicyClass
    targets: tuple[str, ...] = ()
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    write_calls: int = Field(default=0, ge=0)
    reason: str


class HomeBackend(Protocol):
    def read_state(self, entity_id: str) -> Any: ...

    def call_service(self, service: str, entity_id: str, data: dict[str, Any]) -> Any: ...


class HomeEntityRegistry:
    def __init__(self, entities: tuple[HomeEntity, ...]) -> None:
        ids = [item.entity_id for item in entities]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_home_entity")
        aliases: dict[str, set[str]] = {}
        for entity in entities:
            for alias in (*entity.aliases, entity.friendly_name, entity.entity_id):
                aliases.setdefault(_fold(alias), set()).add(entity.entity_id)
        self.entities = entities
        self.by_id = {item.entity_id: item for item in entities}
        self.aliases = aliases

    @classmethod
    def load(cls, path: str | Path) -> "HomeEntityRegistry":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1 or not isinstance(payload.get("entities"), list):
            raise ValueError("invalid_home_entity_registry")
        return cls(tuple(HomeEntity.model_validate(item) for item in payload["entities"]))

    def resolve(self, text: str, *, all_in_area: bool = False) -> tuple[HomeEntity, ...]:
        folded = _fold(text)
        if all_in_area:
            areas = {item.area.casefold() for item in self.entities if _fold(item.area) in folded}
            if len(areas) != 1:
                raise ValueError("home_area_unresolved" if not areas else "home_area_ambiguous")
            matches = tuple(item for item in self.entities if item.area.casefold() in areas)
            if not matches:
                raise ValueError("home_target_unresolved")
            return matches
        scored: list[tuple[int, str]] = []
        for alias, entity_ids in self.aliases.items():
            if alias == folded or re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", folded):
                scored.extend((len(alias), entity_id) for entity_id in entity_ids)
        if not scored:
            raise ValueError("home_target_unresolved")
        best = max(score for score, _ in scored)
        ids = sorted({entity_id for score, entity_id in scored if score == best})
        if len(ids) != 1:
            raise ValueError("home_target_ambiguous")
        return (self.by_id[ids[0]],)


class HomeIntentParser:
    """Conservative deterministic parser. Document/email content never calls this directly."""

    _ACTION = re.compile(
        r"\b(accendi|spegni|imposta|metti|porta|apri|chiudi|abbassa|alza)\b",
        re.I,
    )

    def parse(self, text: str, *, last_entity: str | None = None) -> HomeCommand:
        folded = _fold(text)
        all_in_area = bool(re.search(r"\b(?:tutto|tutte|tutti)\b", folded))
        value_match = re.search(r"\b(?:a|su)\s+(\d{1,2}(?:[.,]\d+)?)\s*(?:°|gradi|c)?\b", folded)
        value = float(value_match.group(1).replace(",", ".")) if value_match else None
        pronoun_only = bool(re.fullmatch(r"(?:abbass|alz)ala(?:\s+un\s+po['’]?)?", folded))
        if pronoun_only:
            if not last_entity:
                raise ValueError("home_followup_target_missing")
            return HomeCommand(
                operation="adjust_temperature",
                target_text=last_entity,
                value=-1.0 if folded.startswith("abbass") else 1.0,
            )
        if re.search(r"\b(?:quanto|temperatura|che\s+temperatura|stato)\b", folded) or (
            re.search(r"\bfa\s+(?:caldo|freddo)\b", folded) and not self._ACTION.search(folded)
        ):
            return HomeCommand(operation="read", target_text=_target_fragment(folded))
        if re.search(r"\baccendi\b", folded):
            operation = "turn_on"
        elif re.search(r"\bspegni\b", folded):
            operation = "turn_off"
        elif re.search(r"\bapri\b", folded):
            operation = "open_cover"
        elif re.search(r"\bchiudi\b", folded):
            operation = "close_cover"
        elif value is not None and re.search(r"\b(?:imposta|metti|porta)\b", folded):
            operation = "set_temperature"
        else:
            raise ValueError("home_intent_not_actionable")
        return HomeCommand(
            operation=operation,
            target_text=_target_fragment(folded),
            value=value,
            all_in_area=all_in_area,
        )


class HomeActionPolicy:
    _SERVICE = {
        "turn_on": "turn_on",
        "turn_off": "turn_off",
        "set_temperature": "set_temperature",
        "adjust_temperature": "set_temperature",
        "open_cover": "open_cover",
        "close_cover": "close_cover",
    }
    _AUTO_DOMAINS = {"light", "media_player", "scene", "switch", "climate"}

    def classify(self, command: HomeCommand, entities: tuple[HomeEntity, ...]) -> tuple[PolicyClass, str]:
        if command.operation == "read":
            return PolicyClass.READ, "state_read"
        allowed_action = "set_temperature" if command.operation == "adjust_temperature" else command.operation
        for entity in entities:
            if allowed_action not in entity.allowed_actions:
                return PolicyClass.DENY, "service_not_allowlisted"
            if command.operation in {"set_temperature", "adjust_temperature"} and entity.effective_domain != "climate":
                return PolicyClass.DENY, "capability_incompatible"
            if command.operation == "set_temperature" and command.value is None:
                return PolicyClass.DENY, "temperature_missing"
            if command.operation == "set_temperature" and not _in_range(command.value, entity):
                return PolicyClass.DENY, "value_out_of_range"
        if any(entity.protected for entity in entities):
            domains = {entity.effective_domain for entity in entities}
            if domains & {"alarm_control_panel", "lock"}:
                return PolicyClass.PROTECTED, "access_or_alarm_protected"
            return PolicyClass.CONFIRM_WRITE, "protected_entity_confirmation"
        if all(
            entity.auto_write and entity.effective_domain in self._AUTO_DOMAINS
            for entity in entities
        ):
            return PolicyClass.AUTO_WRITE, "allowlisted_reversible_write"
        return PolicyClass.CONFIRM_WRITE, "write_not_auto_allowlisted"


class HomeWorkflow:
    def __init__(self, registry: HomeEntityRegistry, backend: HomeBackend) -> None:
        self.registry = registry
        self.backend = backend
        self.policy = HomeActionPolicy()

    def prepare(self, command: HomeCommand) -> HomePreparedAction:
        entities = self.registry.resolve(command.target_text, all_in_area=command.all_in_area)
        if command.all_in_area:
            entities = tuple(item for item in entities if command.operation in item.allowed_actions)
            if not entities:
                raise ValueError("home_area_has_no_compatible_targets")
        policy, reason = self.policy.classify(command, entities)
        return HomePreparedAction(command=command, entities=entities, policy=policy, reason=reason)

    def execute(
        self,
        prepared: HomePreparedAction,
        *,
        confirmed: bool = False,
        protected_approval: bool = False,
    ) -> HomeExecutionResult:
        targets = tuple(item.entity_id for item in prepared.entities)
        if prepared.policy is PolicyClass.DENY:
            return HomeExecutionResult(
                status="denied", policy=prepared.policy, targets=targets, reason=prepared.reason
            )
        if prepared.policy is PolicyClass.CONFIRM_WRITE and not confirmed:
            return HomeExecutionResult(
                status="confirmation_required", policy=prepared.policy, targets=targets,
                reason="explicit_confirmation_required",
            )
        if prepared.policy is PolicyClass.PROTECTED and not protected_approval:
            return HomeExecutionResult(
                status="protected_approval_required", policy=prepared.policy, targets=targets,
                reason="bound_protected_approval_required",
            )
        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        writes = 0
        try:
            before = {entity.entity_id: self.backend.read_state(entity.entity_id) for entity in prepared.entities}
            if any(_is_unavailable(value) for value in before.values()):
                return HomeExecutionResult(
                    status="unavailable", policy=PolicyClass.DENY, targets=targets,
                    before=before, write_calls=0, reason="home_entity_unavailable",
                )
            if prepared.command.operation == "read":
                return HomeExecutionResult(
                    status="read", policy=PolicyClass.READ, targets=targets,
                    before=before, after=before, verified=True, reason="state_observed",
                )
            for entity in prepared.entities:
                service = _service_for(entity, prepared.command.operation)
                value = prepared.command.value
                if prepared.command.operation == "adjust_temperature":
                    current = _temperature(before[entity.entity_id])
                    value = current + float(value or 0)
                    if not _in_range(value, entity):
                        return HomeExecutionResult(
                            status="denied", policy=PolicyClass.DENY, targets=targets,
                            before=before, reason="adjusted_value_out_of_range",
                        )
                data = {"temperature": value} if service == "set_temperature" else {}
                self.backend.call_service(service, entity.entity_id, data)
                writes += 1
            after = {entity.entity_id: self.backend.read_state(entity.entity_id) for entity in prepared.entities}
        except Exception:
            return HomeExecutionResult(
                status="unavailable", policy=prepared.policy, targets=targets,
                before=before, after=after, write_calls=writes,
                reason="home_backend_unavailable",
            )
        verified = all(
            _verify(prepared.command, entity, before[entity.entity_id], after[entity.entity_id])
            for entity in prepared.entities
        )
        return HomeExecutionResult(
            status="verified" if verified else "command_sent_unverified",
            policy=prepared.policy,
            targets=targets,
            before=before,
            after=after,
            verified=verified,
            write_calls=writes,
            reason="state_readback_matches" if verified else "state_readback_mismatch",
        )


def _target_fragment(text: str) -> str:
    value = re.sub(r"\b(?:accendi|spegni|imposta|metti|porta|apri|chiudi|quanto|fa|caldo|freddo|stato|temperatura)\b", " ", text)
    value = re.sub(r"\b(?:a|su)\s+\d{1,2}(?:[.,]\d+)?\s*(?:°|gradi|c)?\b", " ", value)
    value = re.sub(r"\b(?:il|la|le|i|lo|in|nella|nel|di|che|tutto|tutte|tutti|quanto)\b", " ", value)
    compact = " ".join(value.split()).strip(" .!?")
    if not compact:
        raise ValueError("home_target_missing")
    return compact


def _fold(value: str) -> str:
    return " ".join(value.casefold().replace("’", "'").split()).strip(" .!?")


def _in_range(value: float | None, entity: HomeEntity) -> bool:
    if value is None:
        return False
    return (entity.minimum is None or value >= entity.minimum) and (
        entity.maximum is None or value <= entity.maximum
    )


def _temperature(state: Any) -> float:
    if isinstance(state, dict):
        value = state.get("temperature", state.get("current_temperature", state.get("state")))
    else:
        value = state
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("home_temperature_unavailable") from exc


def _verify(command: HomeCommand, entity: HomeEntity, before: Any, after: Any) -> bool:
    state = after.get("state") if isinstance(after, dict) else after
    expected = entity.expected_states.get(command.operation, ())
    if expected:
        return str(state).casefold() in {value.casefold() for value in expected}
    if command.operation == "turn_on":
        return str(state).casefold() in {"on", "playing", "active"}
    if command.operation == "turn_off":
        return str(state).casefold() in {"off", "idle", "standby"}
    if command.operation == "open_cover":
        return str(state).casefold() in {"open", "opening"}
    if command.operation == "close_cover":
        return str(state).casefold() in {"closed", "closing"}
    if command.operation == "set_temperature":
        return abs(_temperature(after) - float(command.value or 0)) < 0.01
    if command.operation == "adjust_temperature":
        return abs(_temperature(after) - (_temperature(before) + float(command.value or 0))) < 0.01
    return before == after


def _service_for(entity: HomeEntity, operation: str) -> str:
    return entity.service_overrides.get(operation, HomeActionPolicy._SERVICE[operation])


def _is_unavailable(state: Any) -> bool:
    value = state.get("state") if isinstance(state, dict) else state
    return str(value).casefold() in {"unavailable", "unknown", "none", ""}


__all__ = [
    "HomeActionPolicy",
    "HomeBackend",
    "HomeCommand",
    "HomeEntity",
    "HomeEntityRegistry",
    "HomeExecutionResult",
    "HomeIntentParser",
    "HomePreparedAction",
    "HomeWorkflow",
]
