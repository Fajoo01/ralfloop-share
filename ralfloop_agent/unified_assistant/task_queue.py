from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class TaskCategory(StrEnum):
    MONEY = "money"
    LOVE = "love"
    FAMILY = "family"
    GENERAL = "general"


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PriorityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: TaskCategory
    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    source: str = Field(min_length=1, max_length=96)
    reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=16)


class BotTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=96)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=6000)
    category: TaskCategory
    auto_priority: int = Field(ge=0, le=100)
    classifier_source: str = Field(min_length=1, max_length=96)
    classifier_confidence: float = Field(ge=0.0, le=1.0)
    priority_reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    human_rank: int | None = Field(default=None, ge=1)
    state: TaskState = TaskState.QUEUED
    blocked_reason: str | None = Field(default=None, max_length=1000)
    depends_on: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    deadline_epoch: int | None = Field(default=None, ge=0)
    created_at: int
    updated_at: int


class QueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(ge=1)
    task: BotTask
    human_pinned: bool
    runnable: bool
    wait_reasons: tuple[str, ...] = Field(default_factory=tuple)


class PriorityClassifier(Protocol):
    def classify(
        self,
        text: str,
        *,
        category_hint: TaskCategory | str | None = None,
    ) -> PriorityDecision:
        ...


class DeterministicJedPriorityClassifier:
    """Small local fallback behind the JED priority-classifier contract.

    This is intentionally deterministic and model-free. A live JED backend can
    replace it without changing queue semantics.
    """

    _BASE_SCORE = {
        TaskCategory.MONEY: 85,
        TaskCategory.LOVE: 85,
        TaskCategory.FAMILY: 85,
        TaskCategory.GENERAL: 50,
    }
    _KEYWORDS = {
        TaskCategory.MONEY: (
            "soldi", "denaro", "euro", "pagamento", "pagare", "fattura",
            "bonifico", "conto", "banca", "tari", "imposta", "tassa",
            "rimborso", "finanzi", "budget", "stipendio", "investiment",
            "costo", "scadenza pagamento",
        ),
        TaskCategory.LOVE: (
            "amore", "relazione", "coppia", "partner", "fidanz",
            "sentimental", "appuntamento", "romantic",
        ),
        TaskCategory.FAMILY: (
            "famiglia", "familiare", "madre", "mamma", "padre", "papà",
            "papa", "sorella", "fratello", "figlio", "figlia", "genitore",
            "nonna", "nonno",
        ),
    }

    def classify(
        self,
        text: str,
        *,
        category_hint: TaskCategory | str | None = None,
    ) -> PriorityDecision:
        if category_hint is not None:
            category = TaskCategory(str(category_hint))
            return PriorityDecision(
                category=category,
                score=self._BASE_SCORE[category],
                confidence=1.0,
                source="jed_deterministic_hint",
                reasons=(f"human_category_hint:{category.value}",),
            )

        normalized = _normalize(text)
        hits: dict[TaskCategory, int] = {}
        for category, words in self._KEYWORDS.items():
            hits[category] = sum(1 for word in words if word in normalized)

        strongest = max(hits.values(), default=0)
        if strongest <= 0:
            return PriorityDecision(
                category=TaskCategory.GENERAL,
                score=self._BASE_SCORE[TaskCategory.GENERAL],
                confidence=0.55,
                source="jed_deterministic",
                reasons=("no_priority_domain_match",),
            )

        category = next(
            cat
            for cat in (
                TaskCategory.MONEY,
                TaskCategory.LOVE,
                TaskCategory.FAMILY,
            )
            if hits[cat] == strongest
        )
        confidence = min(0.98, 0.70 + 0.08 * strongest)
        return PriorityDecision(
            category=category,
            score=self._BASE_SCORE[category],
            confidence=confidence,
            source="jed_deterministic",
            reasons=(
                f"priority_domain:{category.value}",
                f"matched_cues:{strongest}",
            ),
        )


class BotTazziTaskQueue:
    """Persistent task queue with JED auto-priority and human-pinned slots."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        classifier: PriorityClassifier | None = None,
        clock=time.time,
    ) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.classifier = classifier or DeterministicJedPriorityClassifier()
        self._clock = clock
        self._ensure_schema()

    @classmethod
    def from_env(cls) -> "BotTazziTaskQueue":
        configured = os.getenv("BOTTAZZI_TASK_QUEUE_DB", "").strip()
        if configured:
            path = Path(configured).expanduser()
        else:
            state_root = Path(
                os.getenv("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
            ).expanduser()
            path = state_root / "ralfloop" / "bottazzi-task-queue.sqlite3"
        return cls(path)

    def create_task(
        self,
        title: str,
        *,
        description: str = "",
        category_hint: TaskCategory | str | None = None,
        deadline_epoch: int | None = None,
        depends_on: tuple[str, ...] = (),
    ) -> BotTask:
        clean_title = title.strip()
        if not clean_title:
            raise ValueError("task_title_required")
        decision = self.classifier.classify(
            f"{clean_title}\n{description}".strip(),
            category_hint=category_hint,
        )
        now = int(self._clock())
        reasons = list(decision.reasons)
        score = decision.score
        deadline_boost = self._deadline_boost(deadline_epoch, now)
        if deadline_boost:
            score = min(100, score + deadline_boost)
            reasons.append(f"deadline_boost:{deadline_boost}")
        task_id = f"task-{uuid4().hex}"
        with self._connect() as conn:
            conn.execute(
                """
                insert into tasks (
                    task_id, title, description, category, auto_priority,
                    classifier_source, classifier_confidence, priority_reasons,
                    human_rank, state, blocked_reason, depends_on_json,
                    deadline_epoch, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, null, ?, null, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    clean_title,
                    description.strip(),
                    decision.category.value,
                    score,
                    decision.source,
                    decision.confidence,
                    json.dumps(reasons, ensure_ascii=False),
                    TaskState.QUEUED.value,
                    json.dumps(list(depends_on)),
                    deadline_epoch,
                    now,
                    now,
                ),
            )
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> BotTask:
        with self._connect() as conn:
            row = conn.execute(
                "select * from tasks where task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._row_to_task(row)

    def list_queue(self, *, include_terminal: bool = False) -> tuple[QueueEntry, ...]:
        tasks = list(self._load_tasks(include_terminal=include_terminal))
        ranked = self._rank(tasks)
        completed = {
            task.task_id
            for task in self._load_tasks(include_terminal=True)
            if task.state is TaskState.COMPLETED
        }
        entries: list[QueueEntry] = []
        for position, task in enumerate(ranked, start=1):
            wait = self._wait_reasons(task, completed)
            entries.append(
                QueueEntry(
                    position=position,
                    task=task,
                    human_pinned=task.human_rank is not None,
                    runnable=task.state is TaskState.QUEUED and not wait,
                    wait_reasons=wait,
                )
            )
        return tuple(entries)

    def pin(self, task_id: str, rank: int) -> tuple[QueueEntry, ...]:
        if rank < 1:
            raise ValueError("rank_must_be_positive")
        current = list(self.list_queue())
        if not current:
            raise KeyError(task_id)
        target = next((entry for entry in current if entry.task.task_id == task_id), None)
        if target is None:
            raise KeyError(task_id)
        rank = min(rank, len(current))
        ordered_ids = [entry.task.task_id for entry in current if entry.task.task_id != task_id]
        ordered_ids.insert(rank - 1, task_id)
        pinned_ids = {
            entry.task.task_id for entry in current if entry.human_pinned
        }
        pinned_ids.add(task_id)
        now = int(self._clock())
        with self._connect() as conn:
            for position, item_id in enumerate(ordered_ids, start=1):
                if item_id in pinned_ids:
                    conn.execute(
                        "update tasks set human_rank = ?, updated_at = ? where task_id = ?",
                        (position, now, item_id),
                    )
        return self.list_queue()

    def unpin(self, task_id: str) -> BotTask:
        self.get_task(task_id)
        now = int(self._clock())
        with self._connect() as conn:
            conn.execute(
                "update tasks set human_rank = null, updated_at = ? where task_id = ?",
                (now, task_id),
            )
        return self.get_task(task_id)

    def set_state(
        self,
        task_id: str,
        state: TaskState | str,
        *,
        blocked_reason: str | None = None,
    ) -> BotTask:
        target = TaskState(str(state))
        self.get_task(task_id)
        if target is TaskState.BLOCKED and not (blocked_reason or "").strip():
            raise ValueError("blocked_reason_required")
        now = int(self._clock())
        reason = (blocked_reason or "").strip() or None
        if target is not TaskState.BLOCKED:
            reason = None
        with self._connect() as conn:
            conn.execute(
                """
                update tasks
                set state = ?, blocked_reason = ?, updated_at = ?
                where task_id = ?
                """,
                (target.value, reason, now, task_id),
            )
        return self.get_task(task_id)

    def next_runnable(self) -> QueueEntry | None:
        for entry in self.list_queue():
            if entry.runnable:
                return entry
        return None

    def snapshot(self) -> dict:
        queue = self.list_queue()
        next_entry = next((entry for entry in queue if entry.runnable), None)
        return {
            "classifier": "JED",
            "classifier_backend": type(self.classifier).__name__,
            "priority_domains": [
                TaskCategory.MONEY.value,
                TaskCategory.LOVE.value,
                TaskCategory.FAMILY.value,
            ],
            "human_override": "pinned_slot_authoritative",
            "count": len(queue),
            "next_runnable_task_id": (
                next_entry.task.task_id if next_entry is not None else None
            ),
            "priority_conflicts": self._priority_conflicts(queue),
            "tasks": [entry.model_dump(mode="json") for entry in queue],
        }

    @staticmethod
    def _priority_conflicts(queue: tuple[QueueEntry, ...]) -> list[dict[str, object]]:
        conflicts: list[dict[str, object]] = []
        for index, pinned in enumerate(queue):
            if not pinned.human_pinned:
                continue
            for candidate in queue[index + 1:]:
                if candidate.human_pinned:
                    continue
                if candidate.task.auto_priority <= pinned.task.auto_priority:
                    continue
                conflicts.append({
                    "pinned_task_id": pinned.task.task_id,
                    "pinned_position": pinned.position,
                    "candidate_task_id": candidate.task.task_id,
                    "candidate_position": candidate.position,
                    "candidate_auto_priority": candidate.task.auto_priority,
                    "reason": "higher_auto_priority_below_human_pin",
                })
                if len(conflicts) >= 32:
                    return conflicts
        return conflicts

    @staticmethod
    def _deadline_boost(deadline_epoch: int | None, now: int) -> int:
        if deadline_epoch is None:
            return 0
        remaining = deadline_epoch - now
        if remaining <= 0:
            return 15
        if remaining <= 24 * 3600:
            return 12
        if remaining <= 3 * 24 * 3600:
            return 8
        if remaining <= 7 * 24 * 3600:
            return 4
        return 0

    @staticmethod
    def _wait_reasons(task: BotTask, completed: set[str]) -> tuple[str, ...]:
        reasons: list[str] = []
        if task.state is TaskState.BLOCKED:
            reasons.append(task.blocked_reason or "blocked")
        elif task.state is TaskState.RUNNING:
            reasons.append("already_running")
        if task.depends_on:
            missing = [item for item in task.depends_on if item not in completed]
            if missing:
                reasons.append("dependencies:" + ",".join(missing))
        return tuple(reasons)

    @staticmethod
    def _rank(tasks: list[BotTask]) -> list[BotTask]:
        if not tasks:
            return []
        pinned = sorted(
            (task for task in tasks if task.human_rank is not None),
            key=lambda task: (
                int(task.human_rank or 1),
                task.updated_at,
                task.task_id,
            ),
        )
        automatic = sorted(
            (task for task in tasks if task.human_rank is None),
            key=lambda task: (
                -task.auto_priority,
                task.deadline_epoch if task.deadline_epoch is not None else 2**63 - 1,
                task.created_at,
                task.task_id,
            ),
        )
        slots: list[BotTask | None] = [None] * len(tasks)
        for task in pinned:
            preferred = min(max(int(task.human_rank or 1), 1), len(slots)) - 1
            index = preferred
            while index < len(slots) and slots[index] is not None:
                index += 1
            if index >= len(slots):
                index = preferred - 1
                while index >= 0 and slots[index] is not None:
                    index -= 1
            if index >= 0:
                slots[index] = task
        automatic_iter = iter(automatic)
        for index, item in enumerate(slots):
            if item is None:
                slots[index] = next(automatic_iter)
        return [task for task in slots if task is not None]

    def _load_tasks(self, *, include_terminal: bool) -> tuple[BotTask, ...]:
        query = "select * from tasks"
        params: tuple[str, ...] = ()
        if not include_terminal:
            query += " where state not in (?, ?)"
            params = (TaskState.COMPLETED.value, TaskState.CANCELLED.value)
        query += " order by created_at, task_id"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(self._row_to_task(row) for row in rows)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> BotTask:
        return BotTask(
            task_id=str(row["task_id"]),
            title=str(row["title"]),
            description=str(row["description"] or ""),
            category=TaskCategory(str(row["category"])),
            auto_priority=int(row["auto_priority"]),
            classifier_source=str(row["classifier_source"]),
            classifier_confidence=float(row["classifier_confidence"]),
            priority_reasons=tuple(json.loads(row["priority_reasons"] or "[]")),
            human_rank=int(row["human_rank"]) if row["human_rank"] is not None else None,
            state=TaskState(str(row["state"])),
            blocked_reason=(
                str(row["blocked_reason"]) if row["blocked_reason"] is not None else None
            ),
            depends_on=tuple(json.loads(row["depends_on_json"] or "[]")),
            deadline_epoch=(
                int(row["deadline_epoch"]) if row["deadline_epoch"] is not None else None
            ),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists tasks (
                    task_id text primary key,
                    title text not null,
                    description text not null default '',
                    category text not null,
                    auto_priority integer not null,
                    classifier_source text not null,
                    classifier_confidence real not null,
                    priority_reasons text not null default '[]',
                    human_rank integer,
                    state text not null,
                    blocked_reason text,
                    depends_on_json text not null default '[]',
                    deadline_epoch integer,
                    created_at integer not null,
                    updated_at integer not null
                )
                """
            )
            conn.execute(
                """
                create index if not exists idx_tasks_active_priority
                on tasks(state, human_rank, auto_priority desc, created_at)
                """
            )


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


__all__ = [
    "BotTask",
    "BotTazziTaskQueue",
    "DeterministicJedPriorityClassifier",
    "PriorityClassifier",
    "PriorityDecision",
    "QueueEntry",
    "TaskCategory",
    "TaskState",
]
