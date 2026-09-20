from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable

from .models import Hypothesis, StrategyRule
from .service import RelationService


LEGACY_WARNING = "legacy_model_values_are_historical_estimates_not_empirical_probabilities"


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", text.casefold()).strip("_")
    return value[:120] or "legacy"


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise ValueError(f"legacy_json_not_object:{path.name}")
    return value


def _walk_numbers(prefix: str, value: Any) -> Iterable[tuple[str, float]]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        number = float(value)
        if 0.0 <= number <= 1.0:
            yield prefix, number
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk_numbers(child_prefix, child)


def _summary_parts(payloads: list[tuple[Path, dict[str, Any]]]) -> list[str]:
    parts: list[str] = []
    for _, data in payloads:
        context = data.get("context")
        if isinstance(context, dict) and context.get("status_summary"):
            parts.append(str(context["status_summary"]))
        state = data.get("current_state")
        if isinstance(state, dict):
            for key in ("relationship_shape", "main_dynamic", "dominant_reading"):
                if state.get(key):
                    parts.append(f"{key}: {state[key]}")
        interpretation = data.get("interpretation")
        if isinstance(interpretation, dict):
            for key in ("best_label", "main_block"):
                if interpretation.get(key):
                    parts.append(f"{key}: {interpretation[key]}")
        strategy = data.get("strategy")
        if isinstance(strategy, dict):
            if strategy.get("current_mode"):
                parts.append(f"strategy_mode: {strategy['current_mode']}")
            if strategy.get("main_objective"):
                parts.append(f"strategy_objective: {strategy['main_objective']}")
        if data.get("core_pattern"):
            parts.append(f"core_pattern: {data['core_pattern']}")
        if data.get("best_current_label"):
            parts.append(f"best_current_label: {data['best_current_label']}")
    deduped: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part not in seen:
            seen.add(part)
            deduped.append(part)
    return deduped


def _strategy_rules(payloads: list[tuple[Path, dict[str, Any]]]) -> tuple[StrategyRule, ...]:
    rules: list[StrategyRule] = []
    seen: set[str] = set()
    for _, data in payloads:
        strategy = data.get("strategy")
        if not isinstance(strategy, dict):
            continue
        for bucket, priority in (("do", 70), ("dont", 90)):
            values = strategy.get(bucket)
            if not isinstance(values, list):
                continue
            for text in values:
                if not isinstance(text, str) or not text.strip():
                    continue
                key = f"legacy_{bucket}_{_slug(text)}"
                if key in seen:
                    continue
                seen.add(key)
                rules.append(StrategyRule(
                    rule_id=key,
                    text=text.strip(),
                    rationale=f"Imported from legacy strategy/{bucket}; review before treating as current.",
                    priority=priority,
                ))
        moves = strategy.get("next_best_moves")
        if isinstance(moves, list):
            for move in moves:
                if not isinstance(move, dict) or not move.get("move"):
                    continue
                text = str(move.get("message") or move.get("action") or move.get("move"))
                rationale = str(move.get("goal") or "Legacy proposed move; requires current-state review.")
                key = f"legacy_move_{_slug(str(move['move']))}"
                if key in seen:
                    continue
                seen.add(key)
                rules.append(StrategyRule(rule_id=key, text=text, rationale=rationale, priority=50))
    return tuple(rules[:50])


def _hypotheses(payloads: list[tuple[Path, dict[str, Any]]]) -> tuple[Hypothesis, ...]:
    rows: list[Hypothesis] = []
    seen: set[str] = set()
    for _, data in payloads:
        for root in ("probabilities", "magenta_status"):
            block = data.get(root)
            if not isinstance(block, dict):
                continue
            for key, probability in _walk_numbers(root, block):
                if key in seen:
                    continue
                seen.add(key)
                rows.append(Hypothesis(
                    key=f"legacy.{key}",
                    statement=f"Legacy model estimate: {key}",
                    probability=probability,
                    confidence=0.35,
                    status="weak",
                ))
    return tuple(rows[:50])


def _legacy_timeline(payloads: list[tuple[Path, dict[str, Any]]]) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for path, data in payloads:
        timeline = data.get("recent_timeline")
        if not isinstance(timeline, list):
            continue
        for item in timeline:
            if not isinstance(item, dict) or not str(item.get("event") or "").strip():
                continue
            rows.append({
                "event": str(item["event"]).strip(),
                "meaning": str(item.get("meaning") or "").strip(),
                "status": "weak_historical_note",
                "confidence": 0.35,
                "source_ref": f"legacy_json:{path.name}",
            })
    return tuple(rows[:100])


def import_legacy_bundle(service: RelationService, paths: Iterable[str | Path]) -> dict[str, Any]:
    payloads: list[tuple[Path, dict[str, Any]]] = []
    source_refs: list[str] = []
    warnings: list[str] = [LEGACY_WARNING]
    skipped = 0

    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            warnings.append(f"missing:{path}")
            skipped += 1
            continue
        try:
            data = _load_json(path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            warnings.append(f"invalid:{path.name}:{type(exc).__name__}")
            skipped += 1
            continue
        payloads.append((path, data))
        source_refs.append(f"legacy_json:{path.name}:sha256:{_hash_file(path)}")

    if not payloads:
        raise ValueError("no_valid_legacy_payloads")

    parts = _summary_parts(payloads)
    state_summary = "\n".join(parts)[:5000] or "Legacy ABC state imported; no normalized summary fields found."
    label = "legacy-import: " + ", ".join(path.name for path, _ in payloads)[:260]
    snapshot = service.create_snapshot(
        label=label,
        state_summary=state_summary,
        hypotheses=_hypotheses(payloads),
        strategy_rules=_strategy_rules(payloads),
        warnings=tuple(sorted(set(warnings))),
        source_refs=tuple(source_refs[:50]),
        legacy_timeline=_legacy_timeline(payloads),
    )
    return {
        "snapshot": snapshot.model_dump(mode="json"),
        "imported_files": len(payloads),
        "skipped_files": skipped,
        "warnings": sorted(set(warnings)),
    }


__all__ = ["LEGACY_WARNING", "import_legacy_bundle"]
