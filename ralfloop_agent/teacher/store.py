from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Any


class TeacherStore:
    def __init__(self, db_path: str | Path | None = None) -> None:
        default = os.getenv(
            "RALF_TEACHER_DB",
            "/var/lib/ralfloop/teacher.sqlite3",
        )
        self.db_path = Path(db_path or default)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        self.ensure_schema(conn)
        return conn

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                student_id TEXT PRIMARY KEY,
                card_hash TEXT NOT NULL UNIQUE,
                school_level TEXT,
                class_year TEXT,
                preferences_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                student_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                topic TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_at REAL NOT NULL,
                ended_at REAL,
                FOREIGN KEY(student_id) REFERENCES students(student_id)
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                student_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS progress (
                student_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                topic TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                correct INTEGER NOT NULL DEFAULT 0,
                last_summary TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY(student_id, subject, topic)
            );
            """
        )
        conn.commit()

    @staticmethod
    def card_hash(card_id: str) -> str:
        return hashlib.sha256(
            ("tiremm-teacher-v1:" + card_id).encode("utf-8")
        ).hexdigest()

    def login(
        self,
        card_id: str,
        *,
        school_level: str | None = None,
        class_year: str | None = None,
    ) -> dict[str, Any]:
        card_hash = self.card_hash(card_id)
        now = time.time()

        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM students WHERE card_hash = ?",
                (card_hash,),
            ).fetchone()

            if row is None:
                student_id = "stu_" + uuid.uuid4().hex
                conn.execute(
                    """
                    INSERT INTO students(
                        student_id, card_hash, school_level, class_year,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        student_id,
                        card_hash,
                        school_level,
                        class_year,
                        now,
                        now,
                    ),
                )
            else:
                student_id = str(row["student_id"])
                new_level = (
                    school_level
                    if school_level is not None
                    else row["school_level"]
                )
                new_year = (
                    class_year
                    if class_year is not None
                    else row["class_year"]
                )
                conn.execute(
                    """
                    UPDATE students
                    SET school_level = ?, class_year = ?, updated_at = ?
                    WHERE student_id = ?
                    """,
                    (new_level, new_year, now, student_id),
                )

            conn.commit()
            return self.student(student_id)

    def student(self, student_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT student_id, school_level, class_year,
                       preferences_json, created_at, updated_at
                FROM students
                WHERE student_id = ?
                """,
                (student_id,),
            ).fetchone()

        if row is None:
            raise KeyError("student_not_found")

        return {
            "student_id": row["student_id"],
            "school_level": row["school_level"],
            "class_year": row["class_year"],
            "preferences": json.loads(row["preferences_json"] or "{}"),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def start_session(
        self,
        student_id: str,
        subject: str,
        topic: str,
    ) -> dict[str, Any]:
        self.student(student_id)
        session_id = "ses_" + uuid.uuid4().hex
        now = time.time()

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions(
                    session_id, student_id, subject, topic,
                    status, created_at
                ) VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (session_id, student_id, subject, topic, now),
            )
            conn.commit()

        return self.session(session_id)

    def session(self, session_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

        if row is None:
            raise KeyError("session_not_found")

        if row["status"] != "active":
            raise ValueError("session_not_active")

        return dict(row)

    def end_session(self, session_id: str) -> dict[str, Any]:
        now = time.time()

        with self.connect() as conn:
            row = conn.execute(
                "SELECT student_id FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            if row is None:
                raise KeyError("session_not_found")

            conn.execute(
                """
                UPDATE sessions
                SET status = 'ended', ended_at = ?
                WHERE session_id = ?
                """,
                (now, session_id),
            )
            conn.commit()

        return {
            "session_id": session_id,
            "student_id": row["student_id"],
            "status": "ended",
        }

    def event(
        self,
        session_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        session = self.session(session_id)

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO events(
                    session_id, student_id, kind,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    session["student_id"],
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                ),
            )
            conn.commit()

    def update_progress(
        self,
        *,
        student_id: str,
        subject: str,
        topic: str,
        correct: bool | None,
        summary: str,
    ) -> None:
        now = time.time()

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO progress(
                    student_id, subject, topic,
                    attempts, correct, last_summary, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(student_id, subject, topic)
                DO UPDATE SET
                    attempts = attempts + 1,
                    correct = correct + excluded.correct,
                    last_summary = excluded.last_summary,
                    updated_at = excluded.updated_at
                """,
                (
                    student_id,
                    subject,
                    topic,
                    1 if correct is True else 0,
                    summary[:4000],
                    now,
                ),
            )
            conn.commit()

    def progress(self, student_id: str) -> list[dict[str, Any]]:
        self.student(student_id)

        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT subject, topic, attempts, correct,
                       last_summary, updated_at
                FROM progress
                WHERE student_id = ?
                ORDER BY updated_at DESC
                """,
                (student_id,),
            ).fetchall()

        return [dict(row) for row in rows]
