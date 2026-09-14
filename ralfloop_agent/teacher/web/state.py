"""Application source of truth. Transactions own authentication and learning evidence."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo

from .learning import level, mastery_status, update_evidence, xp_points
from ..pedagogy import EvidenceType, KnowledgeState, LearnerProfile, default_learner_profile, update_knowledge_state


def day_at(now):
    return datetime.fromtimestamp(now, ZoneInfo("Europe/Rome")).date()


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def knowledge_evidence(kind: str, attempt_index: int) -> EvidenceType:
    if attempt_index > 0:
        return EvidenceType.CORRECTION
    if kind in {"multiple_choice", "true_false", "matching", "grouping", "definition_match"}:
        return EvidenceType.RECOGNITION
    if kind in {"free_answer", "fill_blank", "flashcards"}:
        return EvidenceType.RECALL
    if kind in {"simulation", "timed_challenge"}:
        return EvidenceType.TRANSFER
    if kind in {"guided_exercise", "ordering", "sequence"}:
        return EvidenceType.APPLICATION
    return EvidenceType.EXPLANATION


class State:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = Path(path), clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Only this new application's database is migrated, never the Teacher DB.
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='schema_version'").fetchone():
                version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                if version and version > 2:
                    raise ValueError("unsupported_schema_version")
            migrations = Path(__file__).with_name("migrations")
            conn.executescript(migrations.joinpath("001.sql").read_text())
            conn.executescript(migrations.joinpath("002_universal_tutor.sql").read_text())
            conn.execute("CREATE TABLE IF NOT EXISTS disabled_students(student TEXT PRIMARY KEY REFERENCES students(id))")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def backup(self, destination):
        with self.connect() as source, sqlite3.connect(destination) as target:
            source.backup(target)
        os.chmod(destination, 0o600)

    def register(self, card, credential, name, school_level, grade, school_track="", demo=False):
        if len(credential) < 8 or len(credential) > 128:
            raise ValueError("credential_length")
        limits = {"primary": 5, "middle": 3, "upper": 5, "adult": 20, "university": 20, "postgraduate": 20, "master": 20}
        if school_level not in limits or grade not in range(1, limits[school_level] + 1):
            raise ValueError("invalid_school_profile")
        if school_level == "upper" and school_track not in ("liceo", "tecnico", "professionale"):
            raise ValueError("invalid_school_track")
        if school_level != "upper" and school_track:
            raise ValueError("invalid_school_track")
        student, salt = secrets.token_hex(16), secrets.token_hex(16)
        hashed = hashlib.scrypt(credential.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        with self.connect() as conn:
            conn.execute("INSERT INTO students VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (student, digest("teacher-web-card:" + card.strip()), name, school_level, grade, school_track,
                          "", self.clock(), None, int(demo)))
            conn.execute("INSERT INTO credentials VALUES(?,?,?)", (student, salt, hashed))
        return student

    def learner_profile(self, student: str | dict) -> dict:
        row = student if isinstance(student, dict) else None
        student_id = row["id"] if row is not None else student
        if row is None:
            with self.connect() as conn:
                db_row = conn.execute("SELECT * FROM students WHERE id=?", (student_id,)).fetchone()
            if db_row is None:
                raise LookupError("student_not_found")
            row = dict(db_row)
        with self.connect() as conn:
            stored = conn.execute("SELECT profile_json FROM learner_profiles WHERE student=?", (student_id,)).fetchone()
        if stored:
            return LearnerProfile.model_validate_json(stored[0]).model_dump(mode="json")
        return default_learner_profile(row.get("school_level"), str(row.get("grade") or "")).model_dump(mode="json")

    def set_learner_profile(self, student: str, profile: dict) -> dict:
        self.require_student(student)
        validated = LearnerProfile.model_validate(profile)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO learner_profiles(student,profile_json,updated) VALUES(?,?,?) "
                "ON CONFLICT(student) DO UPDATE SET profile_json=excluded.profile_json,updated=excluded.updated",
                (student, validated.model_dump_json(), self.clock()),
            )
        return validated.model_dump(mode="json")

    def require_student(self, student: str) -> None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM students WHERE id=? AND id NOT IN (SELECT student FROM disabled_students)",
                (student,),
            ).fetchone()
        if not row:
            raise LookupError("student_not_found")

    def update_kc_mastery(
        self,
        student: str,
        component: str,
        correct: bool,
        evidence_type: EvidenceType,
    ) -> dict:
        self.require_student(student)
        now = self.clock()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM kc_mastery WHERE student=? AND component=?",
                (student, component),
            ).fetchone()
            state = KnowledgeState(
                component=component,
                probability=row["probability"] if row else 0.15,
                attempts=row["attempts"] if row else 0,
                successes=row["successes"] if row else 0,
                due_at=row["due"] if row else None,
                last_seen=row["last_seen"] if row else None,
            )
            updated = update_knowledge_state(
                state,
                correct=correct,
                evidence_type=evidence_type,
                now=now,
            )
            conn.execute(
                "INSERT INTO kc_mastery(student,component,probability,attempts,successes,due,last_seen) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(student,component) DO UPDATE SET "
                "probability=excluded.probability,attempts=excluded.attempts,successes=excluded.successes,"
                "due=excluded.due,last_seen=excluded.last_seen",
                (
                    student,
                    component,
                    updated.probability,
                    updated.attempts,
                    updated.successes,
                    updated.due_at,
                    updated.last_seen,
                ),
            )
        return updated.model_dump(mode="json")

    def login(self, card, credential, remote="local"):
        now = self.clock()
        card_key = digest("teacher-web-card:" + card.strip())
        keys = [card_key, digest("remote:" + remote)]
        # Rate limit counters commit even when authentication fails.
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key, limit in zip(keys, (8, 40)):
                row = conn.execute("SELECT * FROM login_limits WHERE key=?", (key,)).fetchone()
                if row and row["until"] > now and row["count"] >= limit:
                    raise PermissionError("login_throttled")
                conn.execute("INSERT INTO login_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN until<=? THEN 1 ELSE count+1 END, until=CASE WHEN until<=? THEN excluded.until ELSE until END", (key, now + 300, now, now))
            row = conn.execute("SELECT s.id,c.salt,c.digest FROM students s JOIN credentials c ON c.student=s.id WHERE membership_card_id=? AND s.id NOT IN (SELECT student FROM disabled_students)", (card_key,)).fetchone()
        salt = row["salt"] if row else "00" * 16
        computed = hashlib.scrypt(credential.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
        if not row or not secrets.compare_digest(computed, row["digest"]):
            raise PermissionError("invalid_credentials")
        token = secrets.token_urlsafe(32)
        with self.connect() as conn:
            conn.execute("DELETE FROM web_sessions WHERE expires<=?", (now,))
            conn.execute("INSERT INTO web_sessions VALUES(?,?,?)", (digest(token), row["id"], now + 8 * 3600))
            conn.execute("DELETE FROM login_limits WHERE key=?", (card_key,))
        return token

    def authenticate(self, token):
        with self.connect() as conn:
            row = conn.execute("SELECT s.* FROM students s JOIN web_sessions w ON w.student=s.id WHERE token_hash=? AND expires>? AND s.id NOT IN (SELECT student FROM disabled_students)", (digest(token), self.clock())).fetchone()
        if row is None:
            raise PermissionError("session_required")
        return dict(row)

    def logout(self, token):
        with self.connect() as conn:
            conn.execute("DELETE FROM web_sessions WHERE token_hash=?", (digest(token),))

    def owned(self, table, student, resource):
        if table not in ("activities", "materials", "audio_assets"):
            raise ValueError("invalid_resource")
        with self.connect() as conn:
            row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND student=?", (resource, student)).fetchone()
        if not row:
            raise LookupError("resource_unavailable")
        return dict(row)

    def states(self, student):
        with self.connect() as conn:
            return {r["topic"]: dict(r) for r in conn.execute("SELECT * FROM mastery WHERE student=?", (student,))}

    def recent(self, student, topic):
        with self.connect() as conn:
            rows = conn.execute("SELECT a.correct,a.error FROM attempts a JOIN activities t ON t.id=a.activity WHERE a.student=? AND t.topic=? ORDER BY a.id DESC LIMIT 6", (student, topic)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def progress(self, student):
        now, day = self.clock(), str(day_at(self.clock()))
        with self.connect() as conn:
            xp = conn.execute("SELECT COALESCE(SUM(points),0) FROM xp_events WHERE student=?", (student,)).fetchone()[0]
            badges = [r[0] for r in conn.execute("SELECT badge FROM badges WHERE student=?", (student,))]
            daily = conn.execute("SELECT * FROM daily_challenges WHERE student=? AND day=?", (student, day)).fetchone()
            days = {r[0] for r in conn.execute("SELECT day FROM daily_challenges WHERE student=? AND completed>0", (student,))}
            total = conn.execute("SELECT COUNT(*) FROM activities WHERE student=? AND completed IS NOT NULL", (student,)).fetchone()[0]
        cursor = day_at(now)
        if str(cursor) not in days:
            cursor -= timedelta(days=1)
        streak = 0
        while str(cursor) in days:
            streak += 1
            cursor -= timedelta(days=1)
        topics = []
        for topic, state in self.states(student).items():
            recent = self.recent(student, topic)
            topics.append({"topic": topic, "mastery": state["score"], "status": mastery_status(state["score"], state["attempts"], state["due"], now),
                           "attempt_count": state["attempts"], "success_count": state["successes"], "failure_count": state["failures"],
                           "recent_success_rate": round(sum(r["correct"] for r in recent) / max(1, len(recent)), 1),
                           "difficulty_reached": state["difficulty"], "needs_review": bool(state["due"] and state["due"] <= now)})
        return {"xp": xp, "level": level(xp), "badges": badges, "streak": streak, "completed": total,
                "daily": {"completed": daily["completed"] if daily else 0, "target": 5}, "topics": topics}

    def record_answer(self, student, activity, key, correct, error, feedback):
        now, day = self.clock(), str(day_at(self.clock()))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = conn.execute("SELECT activity,result FROM attempts WHERE student=? AND request_key=?", (student, key)).fetchone()
            if old:
                if old["activity"] != activity:
                    raise ValueError("idempotency_conflict")
                return json.loads(old["result"])
            row = conn.execute("SELECT * FROM activities WHERE id=? AND student=?", (activity, student)).fetchone()
            if not row:
                raise LookupError("resource_unavailable")
            if row["completed"] is not None:
                previous = conn.execute("SELECT result FROM attempts WHERE activity=? ORDER BY id DESC LIMIT 1", (activity,)).fetchone()
                result = json.loads(previous[0])
                return {**result, "xp_awarded": 0, "already_completed": True}
            attempts = conn.execute("SELECT COUNT(*) FROM attempts WHERE activity=?", (activity,)).fetchone()[0]
            if attempts >= 5:
                raise ValueError("attempt_limit")
            conn.execute("INSERT OR IGNORE INTO mastery(student,topic) VALUES(?,?)", (student, row["topic"]))
            current = conn.execute("SELECT * FROM mastery WHERE student=? AND topic=?", (student, row["topic"])).fetchone()
            # Same content cannot create evidence or XP repeatedly; review eligible after 7 days.
            duplicate = conn.execute("SELECT 1 FROM activities WHERE student=? AND fingerprint=? AND completed>? AND id!=?", (student, row["fingerprint"], now - 7 * 86400, activity)).fetchone()
            awarded, score = 0, current["score"]
            if not duplicate:
                score = update_evidence(score, correct, error, attempts == 0)
                due = now + (7 if score >= 80 else 2) * 86400
                conn.execute("UPDATE mastery SET score=?,attempts=attempts+1,successes=successes+?,failures=failures+?,consecutive=?,difficulty=MAX(difficulty,?),last_seen=?,due=? WHERE student=? AND topic=?",
                             (score, int(correct), int(not correct), current["consecutive"] + 1 if correct else 0, row["difficulty"], now, due, student, row["topic"]))
                kc_row = conn.execute(
                    "SELECT * FROM kc_mastery WHERE student=? AND component=?",
                    (student, row["topic"]),
                ).fetchone()
                kc_before = KnowledgeState(
                    component=row["topic"],
                    probability=kc_row["probability"] if kc_row else 0.15,
                    attempts=kc_row["attempts"] if kc_row else 0,
                    successes=kc_row["successes"] if kc_row else 0,
                    due_at=kc_row["due"] if kc_row else None,
                    last_seen=kc_row["last_seen"] if kc_row else None,
                )
                kc_after = update_knowledge_state(
                    kc_before,
                    correct=bool(correct),
                    evidence_type=knowledge_evidence(row["kind"], attempts),
                    now=now,
                )
                conn.execute(
                    "INSERT INTO kc_mastery(student,component,probability,attempts,successes,due,last_seen) "
                    "VALUES(?,?,?,?,?,?,?) ON CONFLICT(student,component) DO UPDATE SET "
                    "probability=excluded.probability,attempts=excluded.attempts,successes=excluded.successes,"
                    "due=excluded.due,last_seen=excluded.last_seen",
                    (student, row["topic"], kc_after.probability, kc_after.attempts, kc_after.successes, kc_after.due_at, kc_after.last_seen),
                )
            else:
                kc_after = None
            if correct:
                conn.execute("UPDATE activities SET completed=? WHERE id=?", (now, activity))
                if not duplicate:
                    points = xp_points(attempts == 0, attempts > 0, row["kind"])
                    # Cap daily activity awards: no incentive for endless repetitive clicking.
                    daily_xp = conn.execute("SELECT COALESCE(SUM(points),0) FROM xp_events WHERE student=? AND created>?", (student, now - 86400)).fetchone()[0]
                    points = min(points, max(0, 200 - daily_xp))
                    inserted = conn.execute("INSERT OR IGNORE INTO xp_events(student,points,reason,deduplication_key,created) VALUES(?,?,?,?,?)", (student, points, "learning_completion", "activity:" + activity, now)).rowcount
                    awarded += points * inserted
                    conn.execute("INSERT INTO daily_challenges(student,day,completed) VALUES(?,?,1) ON CONFLICT(student,day) DO UPDATE SET completed=completed+1", (student, day))
                    count = conn.execute("SELECT completed FROM daily_challenges WHERE student=? AND day=?", (student, day)).fetchone()[0]
                    if count == 5:
                        awarded += 10 * conn.execute("INSERT OR IGNORE INTO xp_events(student,points,reason,deduplication_key,created) VALUES(?,10,'daily_goal',?,?)", (student, "daily:" + day, now)).rowcount
                    earned = ["Primo passo"]
                    total = conn.execute("SELECT COUNT(*) FROM xp_events WHERE student=? AND reason='learning_completion'", (student,)).fetchone()[0]
                    earned += [label for threshold, label in [(10, "10 esercizi"), (50, "50 esercizi")] if total >= threshold]
                    if row["kind"] == "multiple_choice": earned.append("Primo quiz")
                    if row["kind"] == "simulation": earned.append("Esploratore STEM")
                    if row["reason"] == "spaced_review": earned.append("Ripasso completato")
                    if current["score"] < 80 <= score: earned.append("Argomento consolidato")
                    if current["failures"] and score >= 50: earned.append("Argomento recuperato")
                    if current["consecutive"] + 1 >= 5: earned.append("5 corrette consecutive")
                    days = {r[0] for r in conn.execute("SELECT day FROM daily_challenges WHERE student=? AND completed>0", (student,))}
                    for threshold in (3, 7):
                        if all(str(day_at(now) - timedelta(days=i)) in days for i in range(threshold)):
                            earned.append(f"Streak {threshold} giorni")
                    for badge in earned:
                        conn.execute("INSERT OR IGNORE INTO badges VALUES(?,?,?)", (student, badge, now))
            result = {"correct": correct, "error_type": error, "feedback": feedback, "xp_awarded": awarded, "mastery": score, "attempts": attempts + 1}
            if kc_after is not None:
                result["knowledge_mastery"] = round(kc_after.probability, 4)
                result["review_due"] = kc_after.due_at
            conn.execute("INSERT INTO attempts(student,activity,request_key,correct,error,result,created) VALUES(?,?,?,?,?,?,?)", (student, activity, key, int(correct), error, json.dumps(result), now))
            conn.execute("UPDATE students SET last_activity=? WHERE id=?", (now, student))
        return result
