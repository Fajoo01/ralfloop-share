from __future__ import annotations

"""Experimental operational deliberation node.

This module is not a frontier reasoning model and does not activate runtime
behavior by itself. It provides deterministic, JSON-safe operational
deliberation: build hypotheses, critique contradictions, collect explicit
evidence needs, and return the smallest verifiable next action.
"""

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence


NODE_NAME = "reasoning_cycle_node"
NODE_VERSION = "0.1"
FAILURE_TERMS = ("traceback", "syntaxerror", "runtimeerror", "failed", "failure", "timeout", "error")
WRITE_TERMS = (
    "patch",
    "write",
    "scrivi",
    "scrivere",
    "modifica",
    "modificare",
    "implement",
    "implementa",
    "fix",
)
NETWORK_TERMS = ("fetch", "http", "https", "network", "rete", "external action", "azione esterna")
RUNTIME_TERMS = ("runtime", "cheshire", "baseline", "core")


@dataclass(frozen=True)
class ReasoningCycleInput:
    user_goal: str
    observations: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    memory: dict[str, Any] = field(default_factory=dict)
    last_result: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OperationalHypothesis:
    hypothesis_id: str
    claim: str
    rationale: str
    risk: str
    verification: str


@dataclass(frozen=True)
class ReasoningObjection:
    hypothesis_id: str
    objection: str
    severity: str
    evidence_needed: str


@dataclass(frozen=True)
class ReasoningCycleDecision:
    status: str
    selected_next_action: dict[str, Any]
    confidence: float
    stop_reason: str | None = None


@dataclass(frozen=True)
class ReasoningCyclePacket:
    node: str
    version: str
    input: ReasoningCycleInput
    hypotheses: list[OperationalHypothesis]
    objections: list[ReasoningObjection]
    evidence_needed: list[str]
    selected_next_action: dict[str, Any]
    confidence: float
    stop_reason: str | None
    decision: ReasoningCycleDecision

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["decision"] = asdict(self.decision)
        payload["internal_state_packet"] = {
            "hypotheses": [asdict(item) for item in self.hypotheses],
            "objections": [asdict(item) for item in self.objections],
            "evidence_needed": list(self.evidence_needed),
            "selected_next_action": dict(self.selected_next_action),
            "confidence": self.confidence,
            "stop_reason": self.stop_reason,
        }
        return payload


def _shorten_text(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def _constraints_text(packet: ReasoningCycleInput) -> str:
    return "\n".join(packet.constraints).lower()


def _goal_text(packet: ReasoningCycleInput) -> str:
    return packet.user_goal.lower()


def _constraints_protect_runtime(text: str) -> bool:
    return _contains_any(
        text,
        (
            "non toccare runtime",
            "runtime attivo",
            "active runtime",
            "baseline attiva",
            "baseline intoccabile",
            "runtime intoccabile",
        ),
    ) or (
        _contains_any(text, ("runtime", "baseline"))
        and _contains_any(text, ("non toccare", "intoccabile", "do not touch", "untouchable"))
    )


def _constraints_no_write(text: str) -> bool:
    return _contains_any(text, ("no write", "read-only", "readonly", "non modificare", "non scrivere"))


def _constraints_no_network(text: str) -> bool:
    return _contains_any(text, ("no network", "nessuna rete", "offline"))


def _hard_policy_violations(packet: ReasoningCycleInput) -> list[str]:
    goal_text = _goal_text(packet)
    constraint_text = _constraints_text(packet)
    violations: list[str] = []

    if _constraints_protect_runtime(constraint_text) and _contains_any(goal_text, WRITE_TERMS) and _contains_any(goal_text, RUNTIME_TERMS):
        violations.append("Runtime protected but goal asks to modify it.")
    if _constraints_no_network(constraint_text) and _contains_any(goal_text, NETWORK_TERMS):
        violations.append("No-network constraint conflicts with requested network/external action.")
    if _constraints_no_write(constraint_text) and _contains_any(goal_text, WRITE_TERMS):
        violations.append("No-write constraint conflicts with requested mutation.")
    return _dedupe_preserve_order(violations)


def has_hard_policy_violation(packet: ReasoningCycleInput) -> bool:
    return bool(_hard_policy_violations(packet))


def observation_from_tool_result(last_result: dict[str, Any] | None) -> str | None:
    """Summarize a tool result into one deterministic operational observation."""
    if not last_result:
        return None

    parts: list[str] = []
    payload_text = json.dumps(last_result, ensure_ascii=False, sort_keys=True).lower()

    if last_result.get("ok") is False:
        parts.append("ok=false")

    exit_code = last_result.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        parts.append(f"exit_code={exit_code}")

    for term, label in (
        ("timeout", "timeout"),
        ("traceback", "traceback"),
        ("syntaxerror", "syntaxerror"),
        ("runtimeerror", "runtimeerror"),
        ("failed", "failed"),
        ("failure", "failure"),
        ("error", "error"),
    ):
        if term in payload_text and label not in parts:
            parts.append(label)

    stderr = _shorten_text(last_result.get("stderr"))
    stdout = _shorten_text(last_result.get("stdout"))
    if stderr:
        parts.append(f"stderr: {stderr}")
    if stdout:
        parts.append(f"stdout: {stdout}")

    return "; ".join(parts) if parts else None


def _clamp_confidence(value: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, numeric))


def _as_text_list(value: Sequence[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_last_result(last_result: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not last_result:
        return None
    return dict(last_result)


def build_reasoning_input(
    user_goal: str,
    observations: Sequence[str] | str | None = None,
    constraints: Sequence[str] | str | None = None,
    memory: Mapping[str, Any] | None = None,
    last_result: Mapping[str, Any] | None = None,
) -> ReasoningCycleInput:
    return ReasoningCycleInput(
        user_goal=str(user_goal or "").strip(),
        observations=_as_text_list(observations),
        constraints=_as_text_list(constraints),
        memory=dict(memory or {}),
        last_result=_normalize_last_result(last_result),
    )


def _combined_text(packet: ReasoningCycleInput) -> str:
    parts = [packet.user_goal, *packet.observations, *packet.constraints]
    if packet.memory:
        parts.append(json.dumps(packet.memory, ensure_ascii=False, sort_keys=True))
    if packet.last_result:
        parts.append(json.dumps(packet.last_result, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts).lower()


def _constraint_flags(constraints: list[str]) -> dict[str, bool]:
    text = "\n".join(constraints).lower()
    return {
        "no_write": _constraints_no_write(text),
        "no_network": _constraints_no_network(text),
        "no_runtime": _constraints_protect_runtime(text),
        "no_destructive": any(term in text for term in ("non distrutt", "no destructive", "nessuna modifica distruttiva")),
        "default_deny": any(term in text for term in ("default-deny", "default deny")),
    }


def _infer_task_mode(packet: ReasoningCycleInput) -> str:
    text = _combined_text(packet)
    if not packet.user_goal:
        return "empty"
    if any(term in text for term in ("send email", "manda email", "pagamento", "external action")):
        return "external_action"
    if any(term in text for term in ("implementa", "implement", "patch", "fix", "correggi", "add ", "aggiungi")):
        return "patch_candidate"
    if any(term in text for term in ("verifica", "verify", "test", "probe", "diagnosi", "diagnose")):
        return "verification"
    return "exploration"


def _last_result_failed(last_result: dict[str, Any] | None, tool_observation: str | None = None) -> bool:
    if not last_result and not tool_observation:
        return False
    if not last_result:
        return _contains_any((tool_observation or "").lower(), FAILURE_TERMS)
    if last_result.get("ok") is False:
        return True
    exit_code = last_result.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    text = (tool_observation or json.dumps(last_result, ensure_ascii=False)).lower()
    return _contains_any(text, FAILURE_TERMS)


def _build_hypotheses(packet: ReasoningCycleInput, task_mode: str, flags: dict[str, bool]) -> list[OperationalHypothesis]:
    hypotheses: list[OperationalHypothesis] = []
    tool_observation = observation_from_tool_result(packet.last_result)

    if _last_result_failed(packet.last_result, tool_observation):
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_last_result",
                claim=f"The most recent tool result is the best first operational target: {tool_observation}.",
                rationale="A fresh failing result gives concrete evidence and avoids guessing.",
                risk="Anchoring on one symptom can hide a broader integration issue.",
                verification="Reproduce the smallest failing command or inspect the exact error payload.",
            )
        )
    elif tool_observation:
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_last_result_observation",
                claim=f"The most recent tool result gives usable evidence: {tool_observation}.",
                rationale="Fresh command output can narrow the next action even when it is not a failure.",
                risk="A successful stdout sample can still be incomplete.",
                verification="Compare the observation with the requested constraints before acting.",
            )
        )

    if packet.observations:
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_observations",
                claim="Current observations identify the safest next check.",
                rationale="The node should prefer observed facts over inferred causes.",
                risk="Observations can be stale, incomplete, or from a different runtime state.",
                verification="Cross-check one independent source before mutating code.",
            )
        )

    if task_mode == "patch_candidate":
        claim = "A minimal isolated implementation can advance the goal without activating runtime behavior."
        verification = "Add an importable module plus targeted unit tests, then run py_compile and pytest."
        if flags["no_write"]:
            claim = "The requested patch is blocked by a no-write constraint until evidence is gathered."
            verification = "Collect file and test evidence, then request or wait for write permission."
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_minimal_patch",
                claim=claim,
                rationale="The stated design favors reversible, testable changes and stable envelopes.",
                risk="Too little integration may leave the new node unused by the main loop.",
                verification=verification,
            )
        )
    elif task_mode == "verification":
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_verify_first",
                claim="A focused verification step is enough before deciding on mutation.",
                rationale="The goal is framed as diagnosis or verification.",
                risk="A probe can be too narrow and miss the real boundary condition.",
                verification="Run the shortest targeted check and record command, path, and exit code.",
            )
        )
    elif task_mode == "external_action":
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_external_gate",
                claim="The task requires a policy gate before any external side effect.",
                rationale="Default-deny policy treats external actions as confirmation-gated.",
                risk="Proceeding without confirmation can create irreversible side effects.",
                verification="Return a proposed action envelope and require explicit human confirmation.",
            )
        )
    elif task_mode == "empty":
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_empty_goal",
                claim="No operational action can be selected without a concrete goal.",
                rationale="The user_goal field is empty after normalization.",
                risk="Guessing would create unrelated work.",
                verification="Ask for a concrete goal or stop with a parseable stop_reason.",
            )
        )
    else:
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_explore",
                claim="A read-only orientation step should precede action.",
                rationale="The task mode is not specific enough for a safe mutation.",
                risk="Exploration can drift without a bounded output contract.",
                verification="Inspect the smallest relevant file set and produce a next action proposal.",
            )
        )

    if flags["no_runtime"]:
        hypotheses.append(
            OperationalHypothesis(
                hypothesis_id="h_runtime_boundary",
                claim="Runtime boundaries require an importable experimental node rather than active integration.",
                rationale="Constraints explicitly keep the baseline active runtime untouched.",
                risk="Experimental code may need later wiring once proven.",
                verification="Ensure no runtime entrypoint imports or executes the node by default.",
            )
        )

    return hypotheses


def _find_text_contradictions(packet: ReasoningCycleInput) -> list[str]:
    text = _combined_text(packet)
    contradictions = _hard_policy_violations(packet)

    success_seen = _contains_any(text, (" ok ", "ok=true", "success", "passed", "riuscito"))
    failure_seen = _contains_any(text, FAILURE_TERMS)
    if success_seen and failure_seen:
        contradictions.append("State mixes success and failure signals.")

    pairs = [
        ("works", "timeout"),
        ("funziona", "timeout"),
        ("active", "inactive"),
        ("cache", "fallback only"),
    ]
    for left, right in pairs:
        if left in text and right in text:
            contradictions.append(f"State contains both '{left}' and '{right}'.")

    if packet.last_result:
        result_text = json.dumps(packet.last_result, ensure_ascii=False).lower()
        if packet.last_result.get("ok") is True and _contains_any(result_text, FAILURE_TERMS):
            contradictions.append("last_result.ok is true but the payload contains failure language.")
        if packet.last_result.get("ok") is False and any(term in result_text for term in ("success", "passed")):
            contradictions.append("last_result.ok is false but the payload contains success language.")
    return _dedupe_preserve_order(contradictions)


def _critique_hypotheses(
    packet: ReasoningCycleInput,
    hypotheses: list[OperationalHypothesis],
    flags: dict[str, bool],
) -> list[ReasoningObjection]:
    objections: list[ReasoningObjection] = []
    tool_observation = observation_from_tool_result(packet.last_result)

    for hypothesis in hypotheses:
        claim_text = hypothesis.claim.lower()
        verify_text = hypothesis.verification.lower()
        if flags["no_write"] and any(term in claim_text + verify_text for term in ("patch", "write", "implement", "mutating", "module")):
            objections.append(
                ReasoningObjection(
                    hypothesis_id=hypothesis.hypothesis_id,
                    objection="No-write constraint conflicts with mutation-oriented action.",
                    severity="high",
                    evidence_needed="Read-only evidence or explicit permission before file writes.",
                )
            )
        if flags["no_network"] and any(term in claim_text + verify_text for term in ("network", "http", "fetch", "external")):
            objections.append(
                ReasoningObjection(
                    hypothesis_id=hypothesis.hypothesis_id,
                    objection="No-network constraint blocks external probing.",
                    severity="high",
                    evidence_needed="Use local files, fixtures, or recorded outputs.",
                )
            )
        if flags["no_runtime"] and any(term in claim_text for term in ("activating runtime", "active integration")):
            objections.append(
                ReasoningObjection(
                    hypothesis_id=hypothesis.hypothesis_id,
                    objection="Runtime boundary allows importable code but blocks active integration.",
                    severity="medium",
                    evidence_needed="Verify the main runtime entrypoints are untouched.",
                )
            )

    if not packet.observations and not packet.last_result:
        objections.append(
            ReasoningObjection(
                hypothesis_id="global",
                objection="The state packet has no observations or last_result.",
                severity="medium",
                evidence_needed="Collect one direct observation before choosing a risky action.",
            )
        )

    if _last_result_failed(packet.last_result, tool_observation):
        objections.append(
            ReasoningObjection(
                hypothesis_id="h_last_result",
                objection="Last tool result contains a failure signal.",
                severity="high",
                evidence_needed=f"Verify or reproduce last tool result: {tool_observation}.",
            )
        )

    for item in _hard_policy_violations(packet):
        objections.append(
            ReasoningObjection(
                hypothesis_id="hard_policy",
                objection=item,
                severity="high",
                evidence_needed="No evidence collection can authorize an explicitly forbidden action.",
            )
        )

    for item in _find_text_contradictions(packet):
        objections.append(
            ReasoningObjection(
                hypothesis_id="global",
                objection=item,
                severity="medium",
                evidence_needed="Resolve the inconsistent state fields before executing a broad action.",
            )
        )

    return objections


def _dedupe_preserve_order(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
    return out


def _select_next_action(
    packet: ReasoningCycleInput,
    task_mode: str,
    flags: dict[str, bool],
    objections: list[ReasoningObjection],
) -> tuple[str, dict[str, Any], float, str | None]:
    high_objection = any(item.severity == "high" for item in objections)
    tool_observation = observation_from_tool_result(packet.last_result)
    failure_description = f" Last result: {tool_observation}." if tool_observation else ""
    if has_hard_policy_violation(packet):
        return (
            "blocked",
            {
                "action_type": "none",
                "description": "Blocked by hard_policy_violation.",
                "commands": [],
                "writes_allowed": False,
                "requires_human_confirmation": False,
            },
            0.2,
            "hard_policy_violation",
        )

    if task_mode == "empty":
        return (
            "stop",
            {
                "action_type": "stop",
                "description": "No action selected because user_goal is empty.",
                "commands": [],
                "writes_allowed": False,
                "requires_human_confirmation": False,
            },
            0.2,
            "empty_user_goal",
        )

    if task_mode == "external_action":
        return (
            "blocked",
            {
                "action_type": "request_confirmation",
                "description": "Return a default-deny proposal before any external side effect.",
                "commands": [],
                "writes_allowed": False,
                "requires_human_confirmation": True,
            },
            0.72,
            "human_confirmation_required",
        )

    if flags["no_write"] and task_mode == "patch_candidate":
        return (
            "continue",
            {
                "action_type": "collect_evidence",
                "description": "Run read-only inspection before any patch because no-write is active.",
                "commands": ["rg --files", "rg -n '<relevant symbol>'"],
                "writes_allowed": False,
                "requires_human_confirmation": False,
            },
            0.76,
            None,
        )

    if task_mode == "patch_candidate" and flags["no_runtime"]:
        return (
            "continue",
            {
                "action_type": "write_experimental_module",
                "description": "Add an importable experimental module and targeted tests without runtime wiring.",
                "commands": ["python3 -m py_compile <new files>", "pytest <targeted tests>", "git diff --check"],
                "writes_allowed": True,
                "requires_human_confirmation": False,
            },
            0.82 if not high_objection else 0.68,
            None,
        )

    if high_objection:
        return (
            "continue",
            {
                "action_type": "run_minimal_probe",
                "description": "Resolve high-severity objection with the smallest local verification." + failure_description,
                "commands": ["python3 -m py_compile <target>", "pytest <targeted tests>"],
                "writes_allowed": False,
                "requires_human_confirmation": False,
            },
            0.62,
            None,
        )

    if task_mode == "verification":
        return (
            "continue",
            {
                "action_type": "run_targeted_verification",
                "description": "Execute the narrowest local check and preserve command/output evidence.",
                "commands": ["python3 -m py_compile <target>", "pytest <targeted tests>"],
                "writes_allowed": False,
                "requires_human_confirmation": False,
            },
            0.78,
            None,
        )

    return (
        "continue",
        {
            "action_type": "orient_readonly",
            "description": "Inspect relevant local files and return one minimal next action.",
            "commands": ["rg --files", "rg -n '<domain terms>'"],
            "writes_allowed": False,
            "requires_human_confirmation": False,
        },
        0.66,
        None,
    )


def run_reasoning_cycle(
    user_goal: str,
    observations: Sequence[str] | str | None = None,
    constraints: Sequence[str] | str | None = None,
    memory: Mapping[str, Any] | None = None,
    last_result: Mapping[str, Any] | None = None,
) -> ReasoningCyclePacket:
    packet_input = build_reasoning_input(user_goal, observations, constraints, memory, last_result)
    tool_observation = observation_from_tool_result(packet_input.last_result)
    if tool_observation and tool_observation not in packet_input.observations:
        packet_input = ReasoningCycleInput(
            user_goal=packet_input.user_goal,
            observations=[*packet_input.observations, tool_observation],
            constraints=packet_input.constraints,
            memory=packet_input.memory,
            last_result=packet_input.last_result,
        )
    flags = _constraint_flags(packet_input.constraints)
    task_mode = _infer_task_mode(packet_input)
    hypotheses = _build_hypotheses(packet_input, task_mode, flags)
    objections = _critique_hypotheses(packet_input, hypotheses, flags)
    evidence_needed = _dedupe_preserve_order([item.evidence_needed for item in objections])
    status, selected_next_action, confidence, stop_reason = _select_next_action(packet_input, task_mode, flags, objections)
    confidence = _clamp_confidence(confidence)
    decision = ReasoningCycleDecision(
        status=status,
        selected_next_action=selected_next_action,
        confidence=confidence,
        stop_reason=stop_reason,
    )
    return ReasoningCyclePacket(
        node=NODE_NAME,
        version=NODE_VERSION,
        input=packet_input,
        hypotheses=hypotheses,
        objections=objections,
        evidence_needed=evidence_needed,
        selected_next_action=selected_next_action,
        confidence=confidence,
        stop_reason=stop_reason,
        decision=decision,
    )


def _load_json_arg(raw: str | None, label: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{label} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit(f"{label} must be a JSON object")
    return parsed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the experimental Ralfloop reasoning cycle node.")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--observation", action="append", default=[])
    parser.add_argument("--constraint", action="append", default=[])
    parser.add_argument("--memory-json")
    parser.add_argument("--last-result-json")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args(argv)

    result = run_reasoning_cycle(
        user_goal=args.goal,
        observations=args.observation,
        constraints=args.constraint,
        memory=_load_json_arg(args.memory_json, "--memory-json"),
        last_result=_load_json_arg(args.last_result_json, "--last-result-json"),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
