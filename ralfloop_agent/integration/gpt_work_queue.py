from __future__ import annotations

import os
import sqlite3
import time
from enum import StrEnum
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class GptJobState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    ACTIVE = "active"
    REVIEW = "review"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATES = {GptJobState.DONE, GptJobState.CANCELLED}


class GptWorkJob(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(min_length=1, max_length=96)
    title: str = Field(min_length=1, max_length=300)
    prompt: str = Field(default="", max_length=32_000)
    project_name: str = Field(default="", max_length=300)
    project_url: str | None = Field(default=None, max_length=1200)
    conversation_url: str | None = Field(default=None, max_length=1200)
    conversation_context_url: str | None = Field(default=None, max_length=1200)
    target_id: str | None = Field(default=None, max_length=160)
    state: GptJobState
    rank: int = Field(ge=1)
    last_error: str | None = Field(default=None, max_length=2000)
    last_assistant_text: str = Field(default="", max_length=24_000)
    created_at: int
    updated_at: int


class GptQueueSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_open_chats: int = Field(ge=1, le=12)


class GptWorkQueue:
    """Durable human-ordered spooler for GPT jobs.

    The queue deliberately stores project identity separately from the chat URL.
    Jobs are globally ranked, while the same records can be rendered in project
    sections. A slot limit is persisted here but enforced by the browser-facing
    controller, where the actual open ChatGPT conversations are observable.
    """

    DEFAULT_MAX_OPEN_CHATS = 3

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @classmethod
    def from_env(cls) -> "GptWorkQueue":
        path = os.getenv(
            "BOTTAZZI_GPT_WORK_QUEUE_DB",
            "/home/bandi/.local/state/bottazzi/gpt-session/work-queue.sqlite3",
        ).strip()
        return cls(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gpt_jobs (
                    job_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    prompt TEXT NOT NULL DEFAULT '',
                    project_name TEXT NOT NULL DEFAULT '',
                    project_url TEXT,
                    conversation_url TEXT,
                    conversation_context_url TEXT,
                    target_id TEXT,
                    state TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    last_error TEXT,
                    last_assistant_text TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
                """
            )
            columns = {str(row["name"]) for row in conn.execute("PRAGMA table_info(gpt_jobs)").fetchall()}
            if "conversation_context_url" not in columns:
                conn.execute("ALTER TABLE gpt_jobs ADD COLUMN conversation_context_url TEXT")
            if "last_assistant_text" not in columns:
                conn.execute("ALTER TABLE gpt_jobs ADD COLUMN last_assistant_text TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_gpt_jobs_rank ON gpt_jobs(rank, created_at)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_gpt_jobs_conversation ON gpt_jobs(conversation_url) WHERE conversation_url IS NOT NULL"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gpt_queue_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO gpt_queue_settings(setting_key, setting_value) VALUES('max_open_chats', ?)",
                (str(self.DEFAULT_MAX_OPEN_CHATS),),
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gpt_job_watchdog (
                    job_id TEXT PRIMARY KEY,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    last_recovery_at INTEGER NOT NULL DEFAULT 0,
                    transport_failure_count INTEGER NOT NULL DEFAULT 0,
                    last_transport_failure_at INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(job_id) REFERENCES gpt_jobs(job_id) ON DELETE CASCADE
                )
                """
            )
            watchdog_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(gpt_job_watchdog)").fetchall()
            }
            if "transport_failure_count" not in watchdog_columns:
                conn.execute(
                    "ALTER TABLE gpt_job_watchdog ADD COLUMN transport_failure_count INTEGER NOT NULL DEFAULT 0"
                )
            if "last_transport_failure_at" not in watchdog_columns:
                conn.execute(
                    "ALTER TABLE gpt_job_watchdog ADD COLUMN last_transport_failure_at INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gpt_job_status_probe (
                    job_id TEXT PRIMARY KEY,
                    assistant_turns INTEGER NOT NULL DEFAULT 0,
                    user_turns INTEGER NOT NULL DEFAULT 0,
                    assistant_text TEXT NOT NULL DEFAULT '',
                    sent_at INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(job_id) REFERENCES gpt_jobs(job_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gpt_job_goal_continue (
                    job_id TEXT PRIMARY KEY,
                    assistant_turns INTEGER NOT NULL DEFAULT 0,
                    assistant_text TEXT NOT NULL DEFAULT '',
                    sent_at INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY(job_id) REFERENCES gpt_jobs(job_id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS gpt_automation_pacing (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    rate_limit_count INTEGER NOT NULL DEFAULT 0,
                    backoff_until INTEGER NOT NULL DEFAULT 0,
                    rate_limit_active INTEGER NOT NULL DEFAULT 0,
                    last_rate_limit_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO gpt_automation_pacing(singleton) VALUES(1)"
            )

    @staticmethod
    def _project_url(value: str | None) -> str | None:
        text = str(value or "").strip().rstrip("/")
        if not text:
            return None
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        path = parsed.path.rstrip("/")
        if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
            raise ValueError("project_url_invalid")
        if not path.startswith("/g/"):
            raise ValueError("project_url_invalid")
        parts = [part for part in path.split("/") if part]
        if len(parts) < 2:
            raise ValueError("project_url_invalid")
        project_slug = parts[1]
        return f"https://chatgpt.com/g/{project_slug}/project"

    @staticmethod
    def _conversation_url(value: str | None) -> str | None:
        text = str(value or "").strip().rstrip("/")
        if not text:
            return None
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
            raise ValueError("conversation_url_invalid")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "c":
            conversation_id = parts[1]
        elif len(parts) >= 4 and parts[0] == "g" and parts[-2] == "c":
            conversation_id = parts[-1]
        else:
            raise ValueError("conversation_url_invalid")
        return f"https://chatgpt.com/c/{conversation_id}"

    @staticmethod
    def _conversation_context_url(value: str | None) -> str | None:
        text = str(value or "").strip().rstrip("/")
        if not text:
            return None
        parsed = urlparse(text)
        host = (parsed.hostname or "").lower()
        if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
            raise ValueError("conversation_context_url_invalid")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2 and parts[0] == "c":
            return f"https://chatgpt.com/c/{parts[1]}"
        if len(parts) >= 4 and parts[0] == "g" and parts[-2] == "c":
            return f"https://chatgpt.com/g/{parts[1]}/c/{parts[-1]}"
        raise ValueError("conversation_context_url_invalid")

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> GptWorkJob:
        return GptWorkJob(
            job_id=str(row["job_id"]),
            title=str(row["title"]),
            prompt=str(row["prompt"]),
            project_name=str(row["project_name"] or ""),
            project_url=str(row["project_url"]) if row["project_url"] else None,
            conversation_url=str(row["conversation_url"]) if row["conversation_url"] else None,
            conversation_context_url=str(row["conversation_context_url"]) if row["conversation_context_url"] else None,
            target_id=str(row["target_id"]) if row["target_id"] else None,
            state=GptJobState(str(row["state"])),
            rank=int(row["rank"]),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            last_assistant_text=str(row["last_assistant_text"] or ""),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )

    def settings(self) -> GptQueueSettings:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT setting_value FROM gpt_queue_settings WHERE setting_key='max_open_chats'"
            ).fetchone()
        try:
            value = int(row["setting_value"]) if row is not None else self.DEFAULT_MAX_OPEN_CHATS
        except (TypeError, ValueError):
            value = self.DEFAULT_MAX_OPEN_CHATS
        return GptQueueSettings(max_open_chats=max(1, min(12, value)))

    def set_max_open_chats(self, value: int) -> GptQueueSettings:
        value = int(value)
        if value < 1 or value > 12:
            raise ValueError("max_open_chats_out_of_range")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO gpt_queue_settings(setting_key, setting_value) VALUES('max_open_chats', ?) ON CONFLICT(setting_key) DO UPDATE SET setting_value=excluded.setting_value",
                (str(value),),
            )
        return self.settings()

    def _next_rank(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(rank), 0) AS max_rank FROM gpt_jobs WHERE state NOT IN ('done','cancelled')"
        ).fetchone()
        return int(row["max_rank"] or 0) + 1

    def create_job(
        self,
        title: str,
        *,
        prompt: str = "",
        project_name: str = "",
        project_url: str | None = None,
        conversation_url: str | None = None,
        conversation_context_url: str | None = None,
        target_id: str | None = None,
        state: GptJobState | str = GptJobState.QUEUED,
    ) -> GptWorkJob:
        title = str(title or "").strip()
        prompt = str(prompt or "").strip()
        project_name = str(project_name or "").strip()
        if not title:
            raise ValueError("job_title_required")
        if len(title) > 300 or len(prompt) > 32_000 or len(project_name) > 300:
            raise ValueError("job_field_too_large")
        normalized_project = self._project_url(project_url)
        normalized_conversation = self._conversation_url(conversation_url)
        normalized_context = self._conversation_context_url(conversation_context_url or conversation_url)
        if self._conversation_url(normalized_context) != normalized_conversation:
            raise ValueError("conversation_context_mismatch")
        state_value = GptJobState(str(state))
        now = int(self.clock())
        job_id = str(uuid4())
        try:
            with self._connect() as conn:
                rank = self._next_rank(conn)
                conn.execute(
                    """
                    INSERT INTO gpt_jobs(
                        job_id, title, prompt, project_name, project_url,
                        conversation_url, conversation_context_url, target_id, state, rank, last_error,
                        created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job_id,
                        title,
                        prompt,
                        project_name,
                        normalized_project,
                        normalized_conversation,
                        normalized_context,
                        str(target_id or "") or None,
                        state_value.value,
                        rank,
                        None,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("conversation_already_queued") from exc
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> GptWorkJob:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM gpt_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._row_to_job(row)

    def list_jobs(self, *, include_terminal: bool = False) -> list[GptWorkJob]:
        where = "" if include_terminal else "WHERE state NOT IN ('done','cancelled')"
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM gpt_jobs {where} ORDER BY rank ASC, created_at ASC, job_id ASC"
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def reorder(self, job_id: str, rank: int) -> GptWorkJob:
        rank = max(1, int(rank))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM gpt_jobs WHERE state NOT IN ('done','cancelled') ORDER BY rank ASC, created_at ASC, job_id ASC"
            ).fetchall()
            ids = [str(row["job_id"]) for row in rows]
            if job_id not in ids:
                raise KeyError(job_id)
            ids.remove(job_id)
            ids.insert(min(rank - 1, len(ids)), job_id)
            now = int(self.clock())
            for index, current_id in enumerate(ids, start=1):
                conn.execute(
                    "UPDATE gpt_jobs SET rank=?, updated_at=? WHERE job_id=?",
                    (index, now if current_id == job_id else int(self.clock()), current_id),
                )
        return self.get_job(job_id)

    def bind_chat(
        self,
        job_id: str,
        *,
        conversation_url: str | None,
        conversation_context_url: str | None = None,
        target_id: str | None,
        state: GptJobState | str = GptJobState.ACTIVE,
        last_error: str | None = None,
    ) -> GptWorkJob:
        normalized_conversation = self._conversation_url(conversation_url)
        normalized_context = self._conversation_context_url(conversation_context_url or conversation_url)
        if self._conversation_url(normalized_context) != normalized_conversation:
            raise ValueError("conversation_context_mismatch")
        state_value = GptJobState(str(state))
        now = int(self.clock())
        try:
            with self._connect() as conn:
                result = conn.execute(
                    "UPDATE gpt_jobs SET conversation_url=?, conversation_context_url=?, target_id=?, state=?, last_error=?, updated_at=? WHERE job_id=?",
                    (
                        normalized_conversation,
                        normalized_context,
                        str(target_id or "") or None,
                        state_value.value,
                        str(last_error or "") or None,
                        now,
                        job_id,
                    ),
                )
                if result.rowcount != 1:
                    raise KeyError(job_id)
        except sqlite3.IntegrityError as exc:
            raise ValueError("conversation_already_queued") from exc
        return self.get_job(job_id)

    def update_history_metadata(
        self,
        job_id: str,
        *,
        conversation_context_url: str | None,
        project_name: str = "",
        project_url: str | None = None,
    ) -> GptWorkJob:
        current = self.get_job(job_id)
        if not current.conversation_url:
            raise ValueError("conversation_url_required")
        normalized_context = self._conversation_context_url(
            conversation_context_url or current.conversation_url
        )
        if self._conversation_url(normalized_context) != current.conversation_url:
            raise ValueError("conversation_context_mismatch")
        normalized_project = self._project_url(project_url)
        normalized_name = str(project_name or "").strip()
        if len(normalized_name) > 300:
            raise ValueError("job_field_too_large")
        now = int(self.clock())
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE gpt_jobs SET conversation_context_url=?, project_name=?, project_url=?, updated_at=? WHERE job_id=?",
                (normalized_context, normalized_name, normalized_project, now, job_id),
            )
            if result.rowcount != 1:
                raise KeyError(job_id)
        return self.get_job(job_id)

    def set_state(
        self,
        job_id: str,
        state: GptJobState | str,
        *,
        last_error: str | None = None,
    ) -> GptWorkJob:
        state_value = GptJobState(str(state))
        now = int(self.clock())
        with self._connect() as conn:
            result = conn.execute(
                "UPDATE gpt_jobs SET state=?, last_error=?, updated_at=? WHERE job_id=?",
                (state_value.value, str(last_error or "") or None, now, job_id),
            )
            if result.rowcount != 1:
                raise KeyError(job_id)
        return self.get_job(job_id)

    def set_last_assistant_text(self, job_id: str, text: str) -> GptWorkJob:
        value = str(text or "").strip()[-24_000:]
        now = int(self.clock())
        with self._connect() as conn:
            result = conn.execute("UPDATE gpt_jobs SET last_assistant_text=?, updated_at=? WHERE job_id=?", (value, now, job_id))
            if result.rowcount != 1:
                raise KeyError(job_id)
        return self.get_job(job_id)

    def set_status_probe_baseline(
        self,
        job_id: str,
        *,
        assistant_turns: int,
        user_turns: int,
        assistant_text: str,
    ) -> dict[str, int | str]:
        self.get_job(job_id)
        now = int(self.clock())
        clean_text = str(assistant_text or "").strip()[-24_000:]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gpt_job_status_probe(job_id, assistant_turns, user_turns, assistant_text, sent_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    assistant_turns=excluded.assistant_turns,
                    user_turns=excluded.user_turns,
                    assistant_text=excluded.assistant_text,
                    sent_at=excluded.sent_at
                """,
                (job_id, max(0, int(assistant_turns)), max(0, int(user_turns)), clean_text, now),
            )
        return self.status_probe_state(job_id)

    def status_probe_state(self, job_id: str) -> dict[str, int | str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT assistant_turns, user_turns, assistant_text, sent_at FROM gpt_job_status_probe WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return {"assistant_turns": 0, "user_turns": 0, "assistant_text": "", "sent_at": 0}
        return {
            "assistant_turns": max(0, int(row["assistant_turns"] or 0)),
            "user_turns": max(0, int(row["user_turns"] or 0)),
            "assistant_text": str(row["assistant_text"] or ""),
            "sent_at": max(0, int(row["sent_at"] or 0)),
        }

    def clear_status_probe(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM gpt_job_status_probe WHERE job_id=?", (job_id,))

    def set_goal_continue_baseline(self, job_id: str, *, assistant_turns: int, assistant_text: str) -> dict[str, int | str]:
        self.get_job(job_id)
        now = int(self.clock())
        clean_text = str(assistant_text or "").strip()[-24_000:]
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gpt_job_goal_continue(job_id, assistant_turns, assistant_text, sent_at)
                VALUES(?,?,?,?)
                ON CONFLICT(job_id) DO UPDATE SET
                    assistant_turns=excluded.assistant_turns,
                    assistant_text=excluded.assistant_text,
                    sent_at=excluded.sent_at
                """,
                (job_id, max(0, int(assistant_turns)), clean_text, now),
            )
        return self.goal_continue_state(job_id)

    def goal_continue_state(self, job_id: str) -> dict[str, int | str]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT assistant_turns, assistant_text, sent_at FROM gpt_job_goal_continue WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return {"assistant_turns": 0, "assistant_text": "", "sent_at": 0}
        return {
            "assistant_turns": max(0, int(row["assistant_turns"] or 0)),
            "assistant_text": str(row["assistant_text"] or ""),
            "sent_at": max(0, int(row["sent_at"] or 0)),
        }

    def goal_continue_already_sent(self, job_id: str, *, assistant_turns: int, assistant_text: str) -> bool:
        state = self.goal_continue_state(job_id)
        return (
            int(state.get("sent_at", 0) or 0) > 0
            and int(state.get("assistant_turns", 0) or 0) == max(0, int(assistant_turns))
            and str(state.get("assistant_text", "")).strip() == str(assistant_text or "").strip()[-24_000:]
        )

    def automation_pacing_state(self) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT rate_limit_count, backoff_until, rate_limit_active, last_rate_limit_at FROM gpt_automation_pacing WHERE singleton=1"
            ).fetchone()
        if row is None:
            return {"rate_limit_count": 0, "backoff_until": 0, "rate_limit_active": 0, "last_rate_limit_at": 0}
        return {
            "rate_limit_count": max(0, int(row["rate_limit_count"] or 0)),
            "backoff_until": max(0, int(row["backoff_until"] or 0)),
            "rate_limit_active": 1 if int(row["rate_limit_active"] or 0) else 0,
            "last_rate_limit_at": max(0, int(row["last_rate_limit_at"] or 0)),
        }

    def note_temporary_access_limit(self, *, initial_backoff_s: int = 300, max_backoff_s: int = 3600) -> dict[str, int]:
        now = int(self.clock())
        initial_backoff_s = max(60, int(initial_backoff_s))
        max_backoff_s = max(initial_backoff_s, int(max_backoff_s))
        state = self.automation_pacing_state()
        count = state["rate_limit_count"]
        backoff_until = state["backoff_until"]
        if not state["rate_limit_active"] or now >= backoff_until:
            count += 1
            delay = min(max_backoff_s, initial_backoff_s * (2 ** max(0, count - 1)))
            backoff_until = now + delay
        with self._connect() as conn:
            conn.execute(
                "UPDATE gpt_automation_pacing SET rate_limit_count=?, backoff_until=?, rate_limit_active=1, last_rate_limit_at=? WHERE singleton=1",
                (count, backoff_until, now),
            )
        return self.automation_pacing_state()

    def clear_temporary_access_limit(self) -> dict[str, int]:
        now = int(self.clock())
        state = self.automation_pacing_state()
        if now < state["backoff_until"]:
            return state
        with self._connect() as conn:
            conn.execute(
                "UPDATE gpt_automation_pacing SET rate_limit_active=0, rate_limit_count=0, backoff_until=0 WHERE singleton=1"
            )
        return self.automation_pacing_state()

    def watchdog_state(self, job_id: str) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT recovery_count, last_recovery_at, transport_failure_count, last_transport_failure_at FROM gpt_job_watchdog WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if row is None:
            return {
                "recovery_count": 0,
                "last_recovery_at": 0,
                "transport_failure_count": 0,
                "last_transport_failure_at": 0,
            }
        return {
            "recovery_count": max(0, int(row["recovery_count"] or 0)),
            "last_recovery_at": max(0, int(row["last_recovery_at"] or 0)),
            "transport_failure_count": max(0, int(row["transport_failure_count"] or 0)),
            "last_transport_failure_at": max(0, int(row["last_transport_failure_at"] or 0)),
        }

    def mark_watchdog_recovery(self, job_id: str) -> dict[str, int]:
        self.get_job(job_id)
        now = int(self.clock())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gpt_job_watchdog(
                    job_id, recovery_count, last_recovery_at,
                    transport_failure_count, last_transport_failure_at
                ) VALUES(?, 1, ?, 0, 0)
                ON CONFLICT(job_id) DO UPDATE SET
                    recovery_count=gpt_job_watchdog.recovery_count + 1,
                    last_recovery_at=excluded.last_recovery_at,
                    transport_failure_count=0,
                    last_transport_failure_at=0
                """,
                (job_id, now),
            )
        return self.watchdog_state(job_id)

    def mark_watchdog_transport_failure(self, job_id: str) -> dict[str, int]:
        self.get_job(job_id)
        now = int(self.clock())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO gpt_job_watchdog(
                    job_id, recovery_count, last_recovery_at,
                    transport_failure_count, last_transport_failure_at
                ) VALUES(?, 0, 0, 1, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    transport_failure_count=gpt_job_watchdog.transport_failure_count + 1,
                    last_transport_failure_at=excluded.last_transport_failure_at
                """,
                (job_id, now),
            )
        return self.watchdog_state(job_id)

    def reset_watchdog_transport_failures(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE gpt_job_watchdog SET transport_failure_count=0, last_transport_failure_at=0 WHERE job_id=?",
                (job_id,),
            )

    def reset_watchdog(self, job_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM gpt_job_watchdog WHERE job_id=?", (job_id,))

    def next_queued(self) -> GptWorkJob | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM gpt_jobs WHERE state='queued' ORDER BY rank ASC, created_at ASC, job_id ASC LIMIT 1"
            ).fetchone()
        return self._row_to_job(row) if row is not None else None

    def snapshot(self) -> dict:
        jobs = self.list_jobs()
        projects: dict[str, dict] = {}
        for job in jobs:
            key = job.project_url or "__no_project__"
            section = projects.setdefault(
                key,
                {
                    "project_url": job.project_url,
                    "project_name": job.project_name or ("Senza progetto" if job.project_url is None else "Progetto ChatGPT"),
                    "job_ids": [],
                },
            )
            if job.project_name and section["project_name"] in {"", "Progetto ChatGPT"}:
                section["project_name"] = job.project_name
            section["job_ids"].append(job.job_id)
        return {
            "settings": self.settings().model_dump(mode="json"),
            "jobs": [job.model_dump(mode="json") for job in jobs],
            "projects": list(projects.values()),
        }
