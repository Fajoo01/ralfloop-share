from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from openshell_backend.skills.abc_relcalc import calculate_relation_dict

from .models import (
    AnalysisResult,
    EvidenceKind,
    Provenance,
    RelationEvent,
    RelationSnapshot,
    SourceKind,
)
from .store import RelationStore


class RelationService:
    """Domain service used by MCP, CLI and tests.

    Canonical scoring consumes structured events directly through `abc_relcalc`.
    The older text-driven Formula Loop can still be attached as a diagnostic
    compatibility signal when a legacy report path is explicitly supplied.
    """

    def __init__(
        self,
        store: RelationStore,
        *,
        legacy_report_path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.legacy_report_path = Path(legacy_report_path).expanduser().resolve() if legacy_report_path else None

    @staticmethod
    def make_event_id(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return "abc_evt_" + hashlib.sha256(canonical).hexdigest()[:16]

    @staticmethod
    def make_snapshot_id(payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        return "abc_snap_" + hashlib.sha256(canonical).hexdigest()[:16]

    def record_event(
        self,
        *,
        occurred_at: datetime,
        kind: EvidenceKind,
        summary: str,
        source_kind: SourceKind,
        source_ref: str,
        actor: str | None = None,
        confidence: float = 1.0,
        weight: float = 0.0,
        tags: Iterable[str] = (),
        raw_excerpt: str | None = None,
        source_hash: str | None = None,
        supersedes_event_id: str | None = None,
    ) -> RelationEvent:
        clean_tags = tuple(sorted({str(tag).strip().lower() for tag in tags if str(tag).strip()}))
        identity = {
            "occurred_at": occurred_at.astimezone(timezone.utc).isoformat(),
            "kind": kind.value,
            "summary": summary.strip(),
            "source_kind": source_kind.value,
            "source_ref": source_ref.strip(),
            "actor": actor.strip() if actor else None,
        }
        event = RelationEvent(
            event_id=self.make_event_id(identity),
            occurred_at=occurred_at,
            actor=actor,
            kind=kind,
            summary=summary.strip(),
            raw_excerpt=raw_excerpt,
            confidence=confidence,
            weight=weight,
            tags=clean_tags,
            provenance=Provenance(
                source_kind=source_kind,
                source_ref=source_ref.strip(),
                source_hash=source_hash,
            ),
            supersedes_event_id=supersedes_event_id,
        )
        self.store.put_event(event)
        return event

    def get_state(self) -> dict[str, Any]:
        snapshot = self.store.latest_snapshot()
        analysis = self.analyze()
        return {
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
            "analysis": analysis.model_dump(mode="json"),
            "policy": self.policy_status(),
        }

    def timeline(self, *, limit: int = 100, kind: EvidenceKind | None = None) -> list[dict[str, Any]]:
        rows = self.store.list_events(limit=limit, kind=kind, active_only=True)
        return [row.model_dump(mode="json") for row in reversed(rows)]

    def search(self, query: str, *, limit: int = 50) -> list[dict[str, Any]]:
        return [row.model_dump(mode="json") for row in self.store.search_events(query, limit=limit)]

    def explain_event(self, event_id: str) -> dict[str, Any] | None:
        event = self.store.get_event(event_id)
        if event is None:
            return None
        caution: list[str] = []
        tags = set(event.tags)
        if event.kind == EvidenceKind.INFERENCE:
            caution.append("interpretation_not_observed_fact")
        if tags & {"mind_reading", "tone", "online_access", "tracking", "surveillance"}:
            caution.append("weak_or_intrusive_signal_do_not_escalate")
        if event.kind in {EvidenceKind.CONTRADICTION, EvidenceKind.BOUNDARY}:
            caution.append("negative_or_limiting_evidence")
        return {
            "event": event.model_dump(mode="json"),
            "caution": caution,
            "scoring_role": {
                "weight": event.weight,
                "confidence": event.confidence,
                "kind": event.kind.value,
            },
        }

    def analyze(self) -> AnalysisResult:
        events = self.store.list_events(limit=10_000, active_only=True)
        evidence = [
            {
                "kind": event.kind.value,
                "description": event.summary,
                "weight": event.weight,
                "confidence": event.confidence,
                "tags": list(event.tags),
            }
            for event in reversed(events)
        ]
        relcalc = calculate_relation_dict(evidence)
        counts = self.store.counts()
        warnings: list[str] = []
        if counts.get(EvidenceKind.INFERENCE.value, 0) > counts.get(EvidenceKind.OBSERVED_FACT.value, 0):
            warnings.append("inferences_outnumber_observed_facts")
        if any(
            set(event.tags) & {"mind_reading", "online_access", "tracking", "surveillance"}
            for event in events
        ):
            warnings.append("intrusive_or_mind_reading_evidence_present")

        formula_payload: dict[str, Any] | None = None
        if self.legacy_report_path and self.legacy_report_path.exists():
            try:
                from openshell_backend.skills.abc_formula_loop import score_text

                report_text = self.legacy_report_path.read_text(encoding="utf-8", errors="replace")
                raw = score_text(report_text, report_path=None, force_extract=False)
                formula_payload = {
                    "formula_version": raw.get("formula_version"),
                    "extractor_version": raw.get("extractor_version"),
                    "rlfull_current": raw.get("rlfull_current"),
                    "prudential_score": raw.get("prudential_score"),
                    "relcalc_score": raw.get("relcalc_score"),
                    "operative_range": raw.get("operative_range"),
                    "action": raw.get("action"),
                    "confidence": raw.get("confidence"),
                    "bias_flags": raw.get("bias_flags") or [],
                    "role": "legacy_text_diagnostic_not_canonical_state",
                }
            except Exception as exc:
                warnings.append(f"legacy_formula_loop_unavailable:{type(exc).__name__}")

        return AnalysisResult(
            relcalc=relcalc,
            formula_loop=formula_payload,
            active_event_count=len(events),
            observed_fact_count=counts.get(EvidenceKind.OBSERVED_FACT.value, 0),
            inference_count=counts.get(EvidenceKind.INFERENCE.value, 0),
            contradiction_count=counts.get(EvidenceKind.CONTRADICTION.value, 0),
            warnings=tuple(sorted(set(warnings))),
        )

    def put_snapshot(self, snapshot: RelationSnapshot) -> RelationSnapshot:
        self.store.put_snapshot(snapshot)
        return snapshot

    def create_snapshot(
        self,
        *,
        label: str,
        state_summary: str,
        hypotheses: tuple[Any, ...] = (),
        strategy_rules: tuple[Any, ...] = (),
        warnings: tuple[str, ...] = (),
        source_refs: tuple[str, ...] = (),
    ) -> RelationSnapshot:
        analysis = self.analyze().model_dump(mode="json")
        identity = {
            "label": label,
            "state_summary": state_summary,
            "source_refs": source_refs,
            "analysis": analysis,
        }
        snapshot = RelationSnapshot(
            snapshot_id=self.make_snapshot_id(identity),
            label=label,
            state_summary=state_summary,
            hypotheses=hypotheses,
            strategy_rules=strategy_rules,
            warnings=warnings,
            source_refs=source_refs,
            score_payload=analysis,
        )
        self.store.put_snapshot(snapshot)
        return snapshot

    @staticmethod
    def policy_status() -> dict[str, Any]:
        return {
            "outbound_actions": False,
            "message_sending": False,
            "surveillance": False,
            "online_status_inference": False,
            "facts_and_inferences_separated": True,
            "raw_chat_storage_default": False,
            "scores_are_model_estimates_not_ground_truth": True,
        }
