from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ralfloop_agent.integration.execution_provenance import (
    ExecutionProvenance,
    empty_execution_provenance,
    guard_execution_claims,
    provenance_from_results,
)
from ralfloop_agent.providers.chat import ChatProvider
from src.models import Evidence

from .manager import ModelToolEnvelope, ModelToolManager
from .registry import ModelToolRegistry


MAX_TOOL_RESULT_CHARS = 24_000


class ModelToolDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["respond", "tool"]
    response: str | None = None
    tool_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action(self) -> "ModelToolDecision":
        if self.action == "respond":
            if not isinstance(self.response, str) or not self.response.strip():
                raise ValueError("respond_requires_response")
            if self.tool_id is not None or self.arguments:
                raise ValueError("respond_forbids_tool_fields")
        elif not self.tool_id:
            raise ValueError("tool_requires_tool_id")
        return self


@dataclass(frozen=True)
class ModelToolOrchestrationResult:
    response: str
    provider: str
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: ExecutionProvenance = field(default_factory=ExecutionProvenance)
    tool_results: tuple[ModelToolEnvelope, ...] = ()


_BARE_JSON_KEY_RE = re.compile(
    r'(?P<prefix>[{,]\s*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?=\s*:)'
)
_CONTROL_VALUE_RE = re.compile(
    r'"?(?P<key>action|tool_id|tool|capability|query_type)"?\s*:\s*"(?P<value>[^"]{1,128})"'
)


class ModelToolDecisionError(ValueError):
    def __init__(self, code: str, *, details: tuple[dict[str, str], ...] = ()) -> None:
        self.code = code
        self.details = details
        super().__init__(code)


def _complete_json_structure(text: str) -> str:
    expected: list[str] = []
    in_string = False
    escaped = False
    pairs = {"{": "}", "[": "]"}
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in pairs:
            expected.append(pairs[char])
        elif char in {"}", "]"}:
            if not expected or expected.pop() != char:
                return text
    if in_string or not expected:
        return text
    return text + "".join(reversed(expected))


def _parse_decision_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped.startswith("{"):
        raise ModelToolDecisionError("tool_decision_not_json_object")
    repaired_keys = _BARE_JSON_KEY_RE.sub(r'\g<prefix>"\g<key>"', stripped)
    candidates = (
        stripped,
        repaired_keys,
        _complete_json_structure(stripped),
        _complete_json_structure(repaired_keys),
    )
    payload: Any = None
    for candidate in dict.fromkeys(candidates):
        try:
            payload = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue
    else:
        raise ModelToolDecisionError("tool_decision_json_invalid")
    if not isinstance(payload, dict):
        raise ModelToolDecisionError("tool_decision_not_json_object")
    return payload


def _normalize_decision_payload(
    payload: dict[str, Any],
    catalog: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    action = payload.get("action")
    if action == "respond":
        return payload
    if not isinstance(action, str) or not action.strip():
        raise ModelToolDecisionError("tool_decision_action_missing")

    tool_id = payload.get("tool_id")
    tool_alias = payload.get("tool")
    capability = payload.get("capability")
    direct_capability = action if action not in {"tool", "capability"} else None
    identity_values = (tool_id, tool_alias, capability, direct_capability)
    if any(value is not None and not isinstance(value, str) for value in identity_values):
        raise ModelToolDecisionError("tool_decision_identity_invalid")

    candidates = list(catalog)
    if isinstance(tool_id, str) and tool_id:
        candidates = [row for row in candidates if row.get("tool_id") == tool_id]
    if isinstance(tool_alias, str) and tool_alias:
        candidates = [
            row
            for row in candidates
            if tool_alias in {row.get("tool_id"), row.get("capability")}
        ]
    if isinstance(capability, str) and capability:
        candidates = [row for row in candidates if row.get("capability") == capability]
    if isinstance(direct_capability, str):
        candidates = [
            row for row in candidates if row.get("capability") == direct_capability
        ]

    has_alias = any(
        isinstance(value, str) and value
        for value in (tool_alias, capability, direct_capability)
    )
    if action == "tool" and isinstance(tool_id, str) and tool_id and not has_alias:
        selected = next(
            (row for row in catalog if row.get("tool_id") == tool_id),
            None,
        )
        if selected is None:
            return {
                "action": "tool",
                "response": payload.get("response"),
                "tool_id": tool_id,
                "arguments": payload.get("arguments", {}),
            }
    else:
        selected = candidates[0] if len(candidates) == 1 else None

    if selected is None:
        known_identity = any(
            value in {row.get("tool_id"), row.get("capability")}
            for value in identity_values
            if isinstance(value, str)
            for row in catalog
        )
        code = (
            "tool_decision_identity_mismatch"
            if known_identity
            else "tool_decision_tool_alias_unresolved"
        )
        raise ModelToolDecisionError(code)

    arguments = payload.get("arguments", {})
    if not isinstance(arguments, dict):
        raise ModelToolDecisionError("tool_decision_arguments_not_object")
    arguments = dict(arguments)
    input_schema = selected.get("input_schema")
    properties = input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    if isinstance(properties, dict):
        for key in properties:
            if key in payload and key not in arguments:
                arguments[key] = payload[key]
    return {
        "action": "tool",
        "response": None,
        "tool_id": selected.get("tool_id"),
        "arguments": arguments,
    }


def _strict_decision(
    text: str,
    *,
    catalog: Sequence[Mapping[str, Any]] = (),
) -> ModelToolDecision:
    payload = _parse_decision_payload(text)
    normalized = _normalize_decision_payload(payload, catalog)
    try:
        return ModelToolDecision.model_validate(normalized)
    except ValueError as exc:
        errors = getattr(exc, "errors", lambda: ())()
        details = tuple({
            "field": ".".join(str(part) for part in row.get("loc", ())) or "$",
            "type": str(row.get("type") or "validation_error"),
        } for row in errors)
        raise ModelToolDecisionError("tool_decision_schema_invalid", details=details) from exc


def _looks_like_control_decision(text: str) -> bool:
    try:
        payload = _parse_decision_payload(text)
    except ValueError:
        stripped = text.strip()
        return (
            stripped.startswith("{")
            and re.search(
                r'(?:"?action"?|"?tool_id"?|"?tool"?|"?capability"?)\s*:',
                stripped,
            )
            is not None
        )
    return any(key in payload for key in ("action", "tool_id", "tool", "capability"))


def _decision_diagnostic(text: str, exc: ValueError) -> dict[str, Any]:
    reason = exc.code if isinstance(exc, ModelToolDecisionError) else "tool_decision_invalid"
    redacted: dict[str, Any] = {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "length": len(text),
    }
    try:
        payload = _parse_decision_payload(text)
    except ValueError:
        for match in _CONTROL_VALUE_RE.finditer(text):
            redacted[match.group("key")] = match.group("value")
    else:
        redacted["keys"] = sorted(str(key) for key in payload)
        for key in ("action", "tool_id", "tool", "capability", "query_type"):
            value = payload.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                redacted[key] = value
        arguments = payload.get("arguments")
        if isinstance(arguments, dict):
            redacted["argument_keys"] = sorted(str(key) for key in arguments)
    output = {"reason": reason, "redacted_payload": redacted}
    if isinstance(exc, ModelToolDecisionError) and exc.details:
        output["validation_errors"] = list(exc.details)
    return output


def _ready_catalog(registry: ModelToolRegistry) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec in registry.specs.values():
        availability = registry.availability(spec)
        if not availability.ok:
            continue
        rows.append(
            {
                "tool_id": spec.tool_id,
                "capability": spec.capability,
                "input_schema": spec.input_schema,
                "output_schema": spec.output_schema,
            }
        )
    return rows


def _decision_messages(
    message: str,
    history: Sequence[Mapping[str, str]],
    catalog: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    system = (
        "You are Ralf's local orchestrator. Decide whether one listed specialist "
        "tool materially helps answer the user. Tools return typed evidence; they do "
        "not perform shell or external side effects. The deep_web_research tool may "
        "perform read-only public web requests and inspect only redacted allowlisted local logs. "
        "Select it for explicit multi-source, current, citation-backed research or log inspection. "
        "Never invent files, document corpora, "
        "schemas, or tool results. If no tool is necessary or its required input is not "
        "present in the user message, answer directly. Output exactly one JSON object, "
        "without markdown: either "
        '{"action":"respond","response":"natural answer","tool_id":null,"arguments":{}} '
        "or "
        '{"action":"tool","response":null,"tool_id":"...","arguments":{...}}. '
        "Available tools:\n"
        + json.dumps(list(catalog), ensure_ascii=False, sort_keys=True)
    )
    return [
        {"role": "system", "content": system},
        *[{"role": str(row["role"]), "content": str(row["content"])} for row in history],
        {"role": "user", "content": message},
    ]


def _result_messages(
    message: str,
    envelope: ModelToolEnvelope,
) -> list[dict[str, str]]:
    encoded = json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    if len(encoded) > MAX_TOOL_RESULT_CHARS:
        encoded = encoded[:MAX_TOOL_RESULT_CHARS] + "...<truncated>"
    return [
        {
            "role": "system",
            "content": (
                "You are Ralf. Answer naturally in the user's language using only the "
                "specialist result envelope supplied below. State explicitly when the "
                "tool failed. Do not claim shell commands, external actions, or facts not "
                "contained in the envelope. Keep identifiers and provenance accurate."
            ),
        },
        {
            "role": "user",
            "content": f"{message}\n\n<model_tool_result>\n{encoded}\n</model_tool_result>",
        },
    ]


def _render_deep_research(envelope: ModelToolEnvelope) -> str:
    if not envelope.ok:
        return f"Ricerca approfondita non completata: {envelope.error_type or 'tool_runtime_error'}."
    answer = str(envelope.output.get("answer") or "").strip()
    claims = envelope.output.get("claims")
    citations = envelope.output.get("citations")
    if not answer or not isinstance(claims, list) or not isinstance(citations, list):
        return "Ricerca approfondita completata senza un risultato strutturato utilizzabile."

    claim_lines: list[str] = []
    seen: set[str] = set()
    for row in claims:
        if not isinstance(row, dict):
            continue
        text = str(row.get("text") or "").strip()
        if not text or text in seen or text == answer:
            continue
        seen.add(text)
        ids = [
            str(item).strip()
            for item in row.get("citation_ids", [])
            if str(item).strip()
        ]
        suffix = f" [{'; '.join(ids)}]" if ids else ""
        claim_lines.append(f"- {text}{suffix}")

    source_lines = []
    for row in citations:
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("source_id") or "").strip()
        url = str(row.get("url") or "").strip()
        if source_id and url:
            source_lines.append(f"- [{source_id}] {url}")

    sections = [answer]
    if claim_lines:
        sections.append("Analisi:\n" + "\n".join(claim_lines))
    if source_lines:
        sections.append("Fonti:\n" + "\n".join(source_lines))
    return "\n\n".join(sections)



def _normalize_tool_arguments(
    tool_id: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    normalized = dict(arguments)

    # La modalità di rete è una policy interna, non un argomento controllabile
    # dal modello orchestratore.
    if tool_id == "deep_web_research_agentcpm_v1":
        allowed = {"query", "domains", "seed_urls", "max_steps", "max_sources"}
        normalized = {
            key: value
            for key, value in normalized.items()
            if key in allowed
        }

        query_folded = str(normalized.get("query") or "").casefold()
        log_only = any(
            marker in query_folded
            for marker in (
                "controlla i log",
                "leggi i log",
                "cerca nei log",
                "analizza i log",
                "log recenti",
                "log del backend",
                "log di ralf",
            )
        ) and not any(marker in query_folded for marker in ("web", "internet", "online"))
        if not log_only:
            if "max_steps" in normalized:
                normalized["max_steps"] = max(10, min(int(normalized["max_steps"]), 30))
            if "max_sources" in normalized:
                normalized["max_sources"] = max(8, min(int(normalized["max_sources"]), 30))

        seed_urls = [
            str(value).strip()
            for value in normalized.get("seed_urls", [])
            if str(value).strip()
        ]
        domains: list[str] = []
        query_text = str(normalized.get("query") or "").casefold()
        seed_hosts = {
            (urlsplit(url).hostname or "").casefold().rstrip(".")
            for url in seed_urls
            if (urlsplit(url).hostname or "").strip()
        }
        for value in normalized.get("domains", []):
            raw = str(value).strip().casefold()
            if not raw:
                continue
            parsed = urlsplit(raw if "://" in raw else f"//{raw}")
            host = (parsed.hostname or "").rstrip(".")
            explicitly_requested = bool(host) and (
                host in query_text
                or any(host == seed_host or seed_host.endswith("." + host) for seed_host in seed_hosts)
            )
            if explicitly_requested and host not in domains:
                domains.append(host)
        for host in sorted(seed_hosts):
            if host and host not in domains:
                domains.append(host)

        if seed_urls:
            normalized["seed_urls"] = seed_urls
        else:
            normalized.pop("seed_urls", None)
        if domains:
            normalized["domains"] = domains
        else:
            normalized.pop("domains", None)

    return normalized


def _preserve_research_request(message: str, arguments: dict[str, Any]) -> dict[str, Any]:
    directive_terms = {
        "analysis",
        "analisi",
        "approfondita",
        "approfondito",
        "come",
        "con",
        "delle",
        "degli",
        "deep",
        "documentata",
        "documentato",
        "fai",
        "nella",
        "nelle",
        "per",
        "research",
        "ricerca",
        "sul",
        "sulla",
        "sulle",
        "una",
        "uno",
    }
    context_terms = {
        token
        for token in re.findall(r"[a-zà-ÿ0-9_-]{3,}", message.casefold())
        if token not in directive_terms
    }
    if len(context_terms) < 3:
        return arguments
    return {**arguments, "query": message.strip()}


class ModelToolOrchestrator:
    def __init__(
        self,
        provider: ChatProvider,
        manager: ModelToolManager,
        registry: ModelToolRegistry,
    ) -> None:
        self.provider = provider
        self.manager = manager
        self.registry = registry

    def run(
        self,
        message: str,
        *,
        history: Sequence[Mapping[str, str]] = (),
        model: str | None = None,
    ) -> ModelToolOrchestrationResult:
        catalog = _ready_catalog(self.registry)
        if not catalog:
            direct = self.provider.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You are Ralf. Answer naturally in the user's language. "
                            "No specialist tool is currently available; never claim one ran."
                        ),
                    },
                    *history,
                    {"role": "user", "content": message},
                ],
                model=model,
            )
            provenance = empty_execution_provenance(
                provider=direct.provider,
                endpoint=str(direct.metadata.get("endpoint") or "") or None,
                model_id=direct.model,
            )
            answer, blocked = guard_execution_claims(direct.text, provenance)
            return ModelToolOrchestrationResult(
                response=answer,
                provider=direct.provider,
                model=direct.model,
                metadata={**direct.metadata, "tool_decision": "no_ready_tools", "execution_claim_blocked": blocked},
                provenance=provenance,
            )

        decision_result = self.provider.chat(
            _decision_messages(message, history, catalog),
            model=model,
        )
        try:
            decision = _strict_decision(decision_result.text, catalog=catalog)
        except ValueError as exc:
            provenance = empty_execution_provenance(
                provider=decision_result.provider,
                endpoint=str(decision_result.metadata.get("endpoint") or "") or None,
                model_id=decision_result.model,
            )
            diagnostic = _decision_diagnostic(decision_result.text, exc)
            if _looks_like_control_decision(decision_result.text):
                fingerprint = diagnostic["redacted_payload"]["sha256"][:12]
                answer = (
                    "Richiesta tool non eseguita: "
                    f"{diagnostic['reason']} (fingerprint={fingerprint})."
                )
                blocked = False
            else:
                answer, blocked = guard_execution_claims(decision_result.text, provenance)
            return ModelToolOrchestrationResult(
                response=answer,
                provider=decision_result.provider,
                model=decision_result.model,
                metadata={
                    **decision_result.metadata,
                    "tool_decision": "invalid",
                    "tool_decision_error": diagnostic["reason"],
                    "tool_decision_diagnostic": diagnostic,
                    "execution_claim_blocked": blocked,
                },
                provenance=provenance,
            )

        if decision.action == "respond":
            provenance = empty_execution_provenance(
                provider=decision_result.provider,
                endpoint=str(decision_result.metadata.get("endpoint") or "") or None,
                model_id=decision_result.model,
            )
            answer, blocked = guard_execution_claims(decision.response or "", provenance)
            return ModelToolOrchestrationResult(
                response=answer,
                provider=decision_result.provider,
                model=decision_result.model,
                metadata={**decision_result.metadata, "tool_decision": "respond", "execution_claim_blocked": blocked},
                provenance=provenance,
            )

        tool_id = decision.tool_id or ""
        tool_arguments = _normalize_tool_arguments(tool_id, decision.arguments)
        if tool_id == "deep_web_research_agentcpm_v1":
            tool_arguments = _preserve_research_request(message, tool_arguments)
        tool_result = self.manager.invoke(tool_id, tool_arguments)
        evidence = Evidence(
            command=f"model_tool:{tool_result.tool_id}",
            path=str(tool_result.provenance.get("snapshot") or tool_result.model_id),
            exit_code=0 if tool_result.ok else 1,
            stdout=json.dumps(tool_result.output, ensure_ascii=False, sort_keys=True)[:4096] if tool_result.ok else None,
            stderr=tool_result.error_type if not tool_result.ok else None,
        )
        provenance = provenance_from_results(
            [(tool_result.input_hash, evidence)],
            provider=decision_result.provider,
            endpoint=str(decision_result.metadata.get("endpoint") or "") or None,
            model_id=decision_result.model,
        )
        if tool_result.tool_id == "deep_web_research_agentcpm_v1":
            return ModelToolOrchestrationResult(
                response=_render_deep_research(tool_result),
                provider=decision_result.provider,
                model=decision_result.model,
                metadata={
                    **decision_result.metadata,
                    "tool_decision": "tool",
                    "tool_id": tool_result.tool_id,
                    "tool_ok": tool_result.ok,
                    "tool_error_type": tool_result.error_type,
                    "execution_claim_blocked": False,
                    "final_render": "deterministic_tool_answer",
                },
                provenance=provenance,
                tool_results=(tool_result,),
            )
        final = self.provider.chat(_result_messages(message, tool_result), model=model)
        answer, blocked = guard_execution_claims(final.text, provenance)
        return ModelToolOrchestrationResult(
            response=answer,
            provider=final.provider,
            model=final.model,
            metadata={
                **final.metadata,
                "tool_decision": "tool",
                "tool_id": tool_result.tool_id,
                "tool_ok": tool_result.ok,
                "tool_error_type": tool_result.error_type,
                "execution_claim_blocked": blocked,
            },
            provenance=provenance,
            tool_results=(tool_result,),
        )


__all__ = [
    "ModelToolDecision",
    "ModelToolOrchestrationResult",
    "ModelToolOrchestrator",
]
