from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, time as wall_time
import fcntl
import json
import os
from pathlib import Path
import socket
import sqlite3
import time
from typing import Any, Iterator
from zoneinfo import ZoneInfo
import hashlib

from .models import PROTECTED_ACTIONS, RETRYABLE_JOB_STATES


ROME = ZoneInfo("Europe/Rome")
JOB_STATES = {
    "queued",
    "waiting_night_window",
    "running",
    "deferred_resource_busy",
    "completed",
    "timeout",
    "invalid_output",
    "invalid_input",
    "orphaned_attempt",
    "failed",
    "awaiting_user_approval",
    "approved",
    "rejected",
    "dispatched",
    "cancelled",
}
BACKOFF_SECONDS = (300, 900, 1800)
DEFAULT_LEASE_SECONDS = 120


def now_rome() -> str:
    return datetime.now(ROME).isoformat(timespec="seconds")


def in_night_window(moment: datetime | None = None) -> bool:
    current = (moment or datetime.now(ROME)).astimezone(ROME).timetz().replace(tzinfo=None)
    return current >= wall_time(23, 0) or current <= wall_time(7, 30)


class GlmQueue:
    def __init__(self, state_dir: str | Path, *, max_attempts: int = 3) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.db_path = self.state_dir / "queue.sqlite3"
        self.lock_path = self.state_dir / "worker.lock"
        self.max_attempts = max_attempts
        with self.connect() as conn:
            self.ensure_schema(conn)
        self.db_path.chmod(0o600)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys=on")
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma synchronous=full")
        return conn

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            create table if not exists schema_migrations (
                version integer primary key,
                applied_at text not null
            );
            create table if not exists glm_jobs (
                task_id text primary key,
                task_type text not null,
                state text not null,
                priority integer not null default 0,
                deadline text,
                created_at text not null,
                updated_at text not null,
                next_attempt_epoch integer not null default 0,
                attempts integer not null default 0,
                input_artifact text not null,
                packet_artifact text not null,
                packet_hash text not null,
                result_artifact text,
                result_hash text,
                validation_artifact text,
                artifact_version integer not null default 1,
                external_action text,
                last_error text,
                envelope_json text,
                run_token text,
                worker_id text,
                started_at text,
                heartbeat_epoch integer,
                lease_expires_epoch integer,
                process_pid integer,
                resource_deferrals integer not null default 0,
                ngen_override integer,
                timeout_override integer,
                approval_request_id text,
                completed_at text
            );
            create index if not exists glm_jobs_schedule_idx
                on glm_jobs(state, next_attempt_epoch, priority desc, deadline, created_at);
            create table if not exists glm_approvals (
                approval_id integer primary key autoincrement,
                task_id text not null,
                artifact_hash text not null,
                artifact_version integer not null,
                action_type text not null,
                decision text not null,
                approver text not null,
                channel text not null,
                decided_at text not null,
                invalidated_at text,
                reason text,
                unique(task_id, artifact_hash, artifact_version, action_type, decision)
            );
            create table if not exists glm_audit (
                event_id integer primary key autoincrement,
                created_at text not null,
                event text not null,
                task_id text,
                metadata_json text not null
            );
            create table if not exists glm_settings (
                key text primary key,
                value text not null,
                updated_at text not null
            );
            create table if not exists glm_attempts (
                run_token text primary key,
                task_id text not null,
                worker_id text not null,
                claimed_at text not null,
                started_at text,
                finished_at text,
                generation_attempt integer,
                resource_deferral integer,
                process_pid integer,
                status text not null,
                artifact_dir text,
                envelope_json text,
                stale integer not null default 0,
                foreign key(task_id) references glm_jobs(task_id)
            );
            create index if not exists glm_attempts_task_idx
                on glm_attempts(task_id, claimed_at);
            """
        )
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(1, ?)",
            (now_rome(),),
        )
        columns = {row[1] for row in conn.execute("pragma table_info(glm_jobs)")}
        if "approval_request_id" not in columns:
            conn.execute("alter table glm_jobs add column approval_request_id text")
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(2, ?)",
            (now_rome(),),
        )
        additions = {
            "worker_id": "text",
            "started_at": "text",
            "heartbeat_epoch": "integer",
            "lease_expires_epoch": "integer",
            "process_pid": "integer",
            "resource_deferrals": "integer not null default 0",
            "ngen_override": "integer",
            "timeout_override": "integer",
        }
        columns = {row[1] for row in conn.execute("pragma table_info(glm_jobs)")}
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(f"alter table glm_jobs add column {name} {declaration}")
        conn.execute(
            "insert or ignore into schema_migrations(version, applied_at) values(3, ?)",
            (now_rome(),),
        )

    def enqueue(self, job: dict[str, Any]) -> dict[str, Any]:
        state = str(job.get("state") or "queued")
        if state not in JOB_STATES:
            raise ValueError("invalid_job_state")
        action = str(job.get("external_action") or "") or None
        if action is not None and action not in PROTECTED_ACTIONS:
            raise ValueError("unsupported_external_action")
        stamp = now_rome()
        with self.connect() as conn:
            conn.execute("begin immediate")
            existing = conn.execute("select * from glm_jobs where task_id=?", (job["task_id"],)).fetchone()
            if existing:
                conn.commit()
                result = self._row(existing)
                result["idempotent"] = True
                return result
            conn.execute(
                """
                insert into glm_jobs(
                    task_id, task_type, state, priority, deadline, created_at, updated_at,
                    input_artifact, packet_artifact, packet_hash, external_action,
                    ngen_override, timeout_override, last_error
                ) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job["task_id"], job["task_type"], state, int(job.get("priority") or 0),
                    job.get("deadline"), stamp, stamp, job["input_artifact"],
                    job["packet_artifact"], job["packet_hash"], action,
                    job.get("ngen_override"), job.get("timeout_override"), job.get("last_error"),
                ),
            )
            conn.commit()
        self.audit("job_enqueued", job["task_id"], state=state, packet_hash=job["packet_hash"])
        return self.get(job["task_id"]) or {}

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from glm_jobs where task_id=?", (task_id,)).fetchone()
        return self._row(row) if row else None

    def list(self, *, limit: int = 100, state: str | None = None) -> list[dict[str, Any]]:
        query = "select * from glm_jobs"
        params: list[Any] = []
        if state:
            query += " where state=?"
            params.append(state)
        query += " order by created_at desc limit ?"
        params.append(max(1, min(limit, 500)))
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row(row) for row in rows]

    def recover_running(self, *, epoch: int | None = None) -> int:
        """Release only expired claims whose recorded adapter process is gone."""
        current_epoch = int(time.time()) if epoch is None else int(epoch)
        stamp = now_rome()
        recovered: list[tuple[str, str]] = []
        with self.connect() as conn:
            conn.execute("begin immediate")
            rows = conn.execute(
                """select task_id,run_token,process_pid,lease_expires_epoch from glm_jobs
                   where state='running' and run_token is not null
                     and coalesce(lease_expires_epoch,0) <= ?""",
                (current_epoch,),
            ).fetchall()
            for row in rows:
                if _pid_alive(row["process_pid"]):
                    continue
                updated = conn.execute(
                    """update glm_jobs set state='orphaned_attempt', updated_at=?,
                       run_token=null, worker_id=null, heartbeat_epoch=null,
                       lease_expires_epoch=null, process_pid=null,
                       last_error='worker_recovered_expired_orphan'
                       where task_id=? and state='running' and run_token=?
                         and coalesce(lease_expires_epoch,0) <= ?""",
                    (stamp, row["task_id"], row["run_token"], current_epoch),
                )
                if updated.rowcount == 1:
                    conn.execute(
                        """update glm_attempts set status='orphaned_attempt', finished_at=?
                           where run_token=? and status in ('claimed','running')""",
                        (stamp, row["run_token"]),
                    )
                    recovered.append((str(row["task_id"]), str(row["run_token"])))
            conn.commit()
        for task_id, token in recovered:
            self.audit("running_job_recovered", task_id, run_token=token[:12])
        return len(recovered)

    def claim_next(
        self,
        *,
        force: bool = False,
        worker_id: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        if self.is_paused():
            return None
        if not force and not in_night_window():
            with self.connect() as conn:
                conn.execute(
                    "update glm_jobs set state='waiting_night_window', updated_at=? where state='queued'",
                    (now_rome(),),
                )
            return None
        stamp = now_rome()
        epoch = int(time.time())
        token = os.urandom(16).hex()
        identity = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        lease = epoch + max(30, min(int(lease_seconds), 14_400))
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                """
                select * from glm_jobs
                where state in ('queued','waiting_night_window','deferred_resource_busy','timeout','invalid_output','orphaned_attempt')
                  and next_attempt_epoch <= ? and attempts < ? and run_token is null
                order by priority desc,
                         case when deadline is null or deadline='' then 1 else 0 end,
                         deadline asc, created_at asc
                limit 1
                """,
                (epoch, self.max_attempts),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            updated = conn.execute(
                """update glm_jobs set state='running', updated_at=?, run_token=?,
                   worker_id=?, started_at=?, heartbeat_epoch=?, lease_expires_epoch=?,
                   process_pid=null
                   where task_id=? and state=? and run_token is null
                     and next_attempt_epoch <= ? and attempts < ?""",
                (
                    stamp, token, identity, stamp, epoch, lease,
                    row["task_id"], row["state"], epoch, self.max_attempts,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                """insert into glm_attempts(
                   run_token,task_id,worker_id,claimed_at,status
                   ) values(?,?,?,?, 'claimed')""",
                (token, row["task_id"], identity, stamp),
            )
            conn.commit()
        job = self.get(str(row["task_id"]))
        self.audit("job_claimed", str(row["task_id"]), run_token=token[:12], worker_id=identity)
        return job

    def claim_task(
        self,
        task_id: str,
        *,
        force: bool = False,
        worker_id: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Atomically claim one exact task without considering any other queue row."""
        if self.is_paused():
            return None
        if not force and not in_night_window():
            with self.connect() as conn:
                conn.execute(
                    """update glm_jobs set state='waiting_night_window', updated_at=?
                       where task_id=? and state='queued'""",
                    (now_rome(), task_id),
                )
            return None
        stamp = now_rome()
        epoch = int(time.time())
        token = os.urandom(16).hex()
        identity = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        lease = epoch + max(30, min(int(lease_seconds), 14_400))
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                """select * from glm_jobs where task_id=?
                   and state in ('queued','waiting_night_window','deferred_resource_busy','timeout','invalid_output','orphaned_attempt')
                   and next_attempt_epoch <= ? and attempts < ? and run_token is null""",
                (task_id, epoch, self.max_attempts),
            ).fetchone()
            if not row:
                conn.commit()
                return None
            updated = conn.execute(
                """update glm_jobs set state='running', updated_at=?, run_token=?,
                   worker_id=?, started_at=?, heartbeat_epoch=?, lease_expires_epoch=?,
                   process_pid=null
                   where task_id=? and state=? and run_token is null
                     and next_attempt_epoch <= ? and attempts < ?""",
                (
                    stamp, token, identity, stamp, epoch, lease,
                    row["task_id"], row["state"], epoch, self.max_attempts,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                """insert into glm_attempts(run_token,task_id,worker_id,claimed_at,status)
                   values(?,?,?,?, 'claimed')""",
                (token, row["task_id"], identity, stamp),
            )
            conn.commit()
        job = self.get(task_id)
        self.audit("job_claimed", task_id, run_token=token[:12], worker_id=identity, exact_task=True)
        return job

    def attach_attempt_artifact(self, task_id: str, run_token: str, artifact_dir: str) -> bool:
        with self.connect() as conn:
            updated = conn.execute(
                """update glm_attempts set artifact_dir=?
                   where task_id=? and run_token=? and status in ('claimed','running')""",
                (artifact_dir, task_id, run_token),
            )
        return updated.rowcount == 1

    def register_process(self, task_id: str, run_token: str, pid: int) -> bool:
        epoch = int(time.time())
        with self.connect() as conn:
            conn.execute("begin immediate")
            updated = conn.execute(
                """update glm_jobs set process_pid=?, heartbeat_epoch=?,
                   lease_expires_epoch=?, updated_at=?
                   where task_id=? and state='running' and run_token=?""",
                (pid, epoch, epoch + DEFAULT_LEASE_SECONDS, now_rome(), task_id, run_token),
            )
            if updated.rowcount == 1:
                conn.execute(
                    """update glm_attempts set process_pid=?, started_at=?, status='running'
                       where task_id=? and run_token=?""",
                    (pid, now_rome(), task_id, run_token),
                )
            conn.commit()
        return updated.rowcount == 1

    def heartbeat(self, task_id: str, run_token: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> bool:
        epoch = int(time.time())
        with self.connect() as conn:
            updated = conn.execute(
                """update glm_jobs set heartbeat_epoch=?, lease_expires_epoch=?, updated_at=?
                   where task_id=? and state='running' and run_token=?""",
                (epoch, epoch + max(30, min(int(lease_seconds), 14_400)), now_rome(), task_id, run_token),
            )
        return updated.rowcount == 1

    def record_orphan(
        self,
        task_id: str,
        *,
        artifact_dir: str,
        status: str,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        token = "orphan-" + hashlib.sha256(artifact_dir.encode()).hexdigest()[:24]
        stamp = now_rome()
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select * from glm_jobs where task_id=?", (task_id,)).fetchone()
            if not row:
                conn.rollback()
                return {"status": "orphan_task_missing", "task_id": task_id}
            conn.execute(
                """insert or ignore into glm_attempts(
                   run_token,task_id,worker_id,claimed_at,started_at,finished_at,
                   generation_attempt,status,artifact_dir,envelope_json,stale
                   ) values(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    token, task_id, "orphan-recovery", stamp, stamp, stamp,
                    None, status, artifact_dir, json.dumps(envelope, sort_keys=True),
                    0 if row["state"] == "orphaned_attempt" and row["run_token"] is None else 1,
                ),
            )
            if row["state"] == "orphaned_attempt" and row["run_token"] is None:
                attempts = min(self.max_attempts, int(row["attempts"]) + 1)
                next_state = status if attempts < self.max_attempts else "failed"
                conn.execute(
                    """update glm_jobs set state=?, attempts=?, updated_at=?, last_error=?,
                       envelope_json=?, next_attempt_epoch=?
                       where task_id=? and state='orphaned_attempt' and run_token is null""",
                    (
                        next_state, attempts, stamp, "recovered_orphaned_attempt",
                        json.dumps(envelope, sort_keys=True),
                        int(time.time()) + BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)],
                        task_id,
                    ),
                )
            conn.commit()
        self.audit("orphan_attempt_recorded", task_id, artifact_dir=artifact_dir, status=status)
        return self.get(task_id) or {}

    def finish(
        self,
        task_id: str,
        *,
        run_token: str,
        provider_status: str,
        envelope: dict[str, Any],
        generation_started: bool | int,
        result_artifact: str | None = None,
        result_hash: str | None = None,
        validation_artifact: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        stamp = now_rome()
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute(
                "select * from glm_jobs where task_id=? and state='running' and run_token=?",
                (task_id, run_token),
            ).fetchone()
            if not row:
                conn.rollback()
                self.audit("stale_run_finalization_rejected", task_id, run_token=run_token[:12], provider_status=provider_status)
                return {"status": "stale_run_token", "task_id": task_id, "run_token": run_token}
            generation_count = int(generation_started)
            if generation_count < 0:
                conn.rollback()
                raise ValueError("generation_count_invalid")
            attempts = int(row["attempts"]) + generation_count
            deferrals = int(row["resource_deferrals"] or 0) + (1 if provider_status == "deferred_resource_busy" else 0)
            if provider_status == "completed":
                state = "awaiting_user_approval" if row["external_action"] else "completed"
                next_epoch = 0
            elif provider_status == "deferred_resource_busy":
                state = "deferred_resource_busy"
                next_epoch = int(time.time()) + BACKOFF_SECONDS[min(max(deferrals - 1, 0), len(BACKOFF_SECONDS) - 1)]
            elif provider_status in {"timeout", "invalid_output", "orphaned_attempt"}:
                state = provider_status if attempts < self.max_attempts else "failed"
                next_epoch = int(time.time()) + BACKOFF_SECONDS[min(max(attempts - 1, 0), len(BACKOFF_SECONDS) - 1)]
            elif provider_status == "invalid_input":
                state = "invalid_input"
                next_epoch = 0
            else:
                state = "failed"
                next_epoch = 0
            updated = conn.execute(
                """
                update glm_jobs set state=?, updated_at=?, next_attempt_epoch=?,
                  attempts=?, resource_deferrals=?,
                  result_artifact=coalesce(?, result_artifact), result_hash=coalesce(?, result_hash),
                  validation_artifact=coalesce(?, validation_artifact), last_error=?,
                  envelope_json=?, run_token=null, worker_id=null, heartbeat_epoch=null,
                  lease_expires_epoch=null, process_pid=null, completed_at=?
                where task_id=? and state='running' and run_token=?
                """,
                (
                    state, stamp, next_epoch, attempts, deferrals,
                    result_artifact, result_hash, validation_artifact,
                    error, json.dumps(envelope, sort_keys=True), stamp if state in {"completed", "awaiting_user_approval"} else None,
                    task_id, run_token,
                ),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return {"status": "stale_run_token", "task_id": task_id, "run_token": run_token}
            conn.execute(
                """update glm_attempts set finished_at=?, generation_attempt=?,
                   resource_deferral=?, status=?, envelope_json=?
                   where task_id=? and run_token=?""",
                (
                    stamp, attempts if generation_count else None,
                    deferrals if provider_status == "deferred_resource_busy" else None,
                    provider_status, json.dumps(envelope, sort_keys=True), task_id, run_token,
                ),
            )
            conn.commit()
        self.audit(
            "job_finished", task_id, state=state, provider_status=provider_status,
            attempts=attempts, resource_deferrals=deferrals, run_token=run_token[:12],
        )
        return self.get(task_id) or {}

    def retry(
        self,
        task_id: str,
        *,
        ngen_override: int | None = None,
        timeout_override: int | None = None,
    ) -> dict[str, Any]:
        epoch = int(time.time())
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select * from glm_jobs where task_id=?", (task_id,)).fetchone()
            if not row:
                conn.rollback()
                return {"status": "not_found", "task_id": task_id}
            lease_active = bool(row["lease_expires_epoch"] and int(row["lease_expires_epoch"]) > epoch)
            process_active = _pid_alive(row["process_pid"])
            if row["state"] == "running" or (row["run_token"] and (lease_active or process_active)):
                conn.rollback()
                return {
                    "status": "active_run_not_retryable", "task_id": task_id,
                    "state": row["state"], "worker_id": row["worker_id"],
                    "lease_active": lease_active, "process_active": process_active,
                }
            if row["state"] not in RETRYABLE_JOB_STATES:
                conn.rollback()
                return {"status": "not_retryable", "task_id": task_id, "state": row["state"]}
            if int(row["attempts"]) >= self.max_attempts:
                conn.rollback()
                return {"status": "max_attempts_reached", "task_id": task_id, "attempts": row["attempts"]}
            updated = conn.execute(
                """update glm_jobs set state='queued', next_attempt_epoch=0, updated_at=?,
                   run_token=null, worker_id=null, heartbeat_epoch=null,
                   lease_expires_epoch=null, process_pid=null,
                   ngen_override=coalesce(?,ngen_override),
                   timeout_override=coalesce(?,timeout_override)
                   where task_id=? and state=? and attempts=?""",
                (now_rome(), ngen_override, timeout_override, task_id, row["state"], row["attempts"]),
            )
            if updated.rowcount != 1:
                conn.rollback()
                return {"status": "retry_state_changed", "task_id": task_id}
            conn.commit()
        self.audit("job_retry_requested", task_id)
        return self.get(task_id) or {}

    def prepare_reparse(self, task_id: str) -> dict[str, Any] | None:
        token = "reparse-" + os.urandom(12).hex()
        epoch = int(time.time())
        identity = f"{socket.gethostname()}:{os.getpid()}:reparse"
        with self.connect() as conn:
            conn.execute("begin immediate")
            updated = conn.execute(
                """update glm_jobs set state='running', run_token=?, worker_id=?,
                   started_at=?, heartbeat_epoch=?, lease_expires_epoch=?, updated_at=?
                   where task_id=? and state='invalid_output' and run_token is null""",
                (token, identity, now_rome(), epoch, epoch + DEFAULT_LEASE_SECONDS, now_rome(), task_id),
            )
            if updated.rowcount == 1:
                conn.execute(
                    """insert into glm_attempts(run_token,task_id,worker_id,claimed_at,status)
                       values(?,?,?,?, 'claimed')""",
                    (token, task_id, identity, now_rome()),
                )
            conn.commit()
        if updated.rowcount != 1:
            return None
        self.audit("invalid_output_reparse_started", task_id)
        return self.get(task_id)

    def cancel(self, task_id: str) -> dict[str, Any]:
        job = self.get(task_id)
        if not job:
            return {"status": "not_found", "task_id": task_id}
        if job["state"] == "running":
            return {"status": "active_job_not_cancellable", "task_id": task_id}
        if job["state"] not in {"queued", "waiting_night_window", "deferred_resource_busy", "timeout", "invalid_output", "orphaned_attempt"}:
            return {"status": "not_cancellable", "task_id": task_id, "state": job["state"]}
        self._set_state(task_id, "cancelled")
        self.audit("job_cancelled", task_id)
        return self.get(task_id) or {}

    def decide(
        self,
        task_id: str,
        decision: str,
        *,
        approver: str,
        channel: str,
        reason: str = "",
    ) -> dict[str, Any]:
        job = self.get(task_id)
        if not job:
            return {"status": "not_found", "task_id": task_id}
        if decision not in {"approved", "rejected"}:
            raise ValueError("invalid_decision")
        if job["state"] not in {"awaiting_user_approval", decision}:
            return {"status": "approval_not_pending", "task_id": task_id, "state": job["state"]}
        action = str(job.get("external_action") or "")
        artifact_hash = str(job.get("result_hash") or "")
        if action not in PROTECTED_ACTIONS or not artifact_hash:
            return {"status": "approval_scope_invalid", "task_id": task_id}
        stamp = now_rome()
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """insert or ignore into glm_approvals(
                   task_id, artifact_hash, artifact_version, action_type, decision,
                   approver, channel, decided_at, reason
                   ) values(?,?,?,?,?,?,?,?,?)""",
                (task_id, artifact_hash, job["artifact_version"], action, decision, approver, channel, stamp, reason),
            )
            conn.execute("update glm_jobs set state=?, updated_at=? where task_id=?", (decision, stamp, task_id))
            conn.commit()
        self.audit("artifact_" + decision, task_id, artifact_hash=artifact_hash, version=job["artifact_version"], action=action, approver=approver, channel=channel)
        return self.get(task_id) or {}

    def approval_allows_dispatch(self, task_id: str, *, artifact_hash: str, action: str) -> bool:
        job = self.get(task_id)
        if not job or job["state"] != "approved" or job.get("result_hash") != artifact_hash or job.get("external_action") != action:
            return False
        with self.connect() as conn:
            row = conn.execute(
                """select 1 from glm_approvals where task_id=? and artifact_hash=?
                   and artifact_version=? and action_type=? and decision='approved'
                   and invalidated_at is null limit 1""",
                (task_id, artifact_hash, job["artifact_version"], action),
            ).fetchone()
        return bool(row)

    def replace_result(self, task_id: str, *, result_artifact: str, result_hash: str) -> dict[str, Any]:
        job = self.get(task_id)
        if not job:
            return {"status": "not_found"}
        stamp = now_rome()
        version = int(job["artifact_version"]) + 1
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute("update glm_approvals set invalidated_at=? where task_id=? and invalidated_at is null", (stamp, task_id))
            conn.execute(
                """update glm_jobs set result_artifact=?, result_hash=?, artifact_version=?,
                   state=case when external_action is null then 'completed' else 'awaiting_user_approval' end,
                   approval_request_id=null, updated_at=? where task_id=?""",
                (result_artifact, result_hash, version, stamp, task_id),
            )
            conn.commit()
        self.audit("artifact_replaced", task_id, artifact_hash=result_hash, version=version)
        return self.get(task_id) or {}

    def attach_approval_request(self, task_id: str, request_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute(
                "update glm_jobs set approval_request_id=?, updated_at=? where task_id=? and state='awaiting_user_approval'",
                (request_id, now_rome(), task_id),
            )
        self.audit("telegram_approval_requested", task_id, approval_request_id=request_id)
        return self.get(task_id) or {}

    def set_paused(self, paused: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                """insert into glm_settings(key,value,updated_at) values('paused',?,?)
                   on conflict(key) do update set value=excluded.value, updated_at=excluded.updated_at""",
                ("1" if paused else "0", now_rome()),
            )
        self.audit("queue_paused" if paused else "queue_resumed", None)

    def is_paused(self) -> bool:
        with self.connect() as conn:
            row = conn.execute("select value from glm_settings where key='paused'").fetchone()
        return bool(row and row["value"] == "1")

    def metrics(self) -> dict[str, Any]:
        with self.connect() as conn:
            counts = {row["state"]: row["count"] for row in conn.execute("select state,count(*) as count from glm_jobs group by state")}
            last = conn.execute("""select completed_at, envelope_json, result_artifact from glm_jobs
                where state in ('completed','awaiting_user_approval','approved','dispatched')
                order by completed_at desc limit 1""").fetchone()
            retries = conn.execute("select coalesce(sum(case when attempts>1 then attempts-1 else 0 end),0) as count from glm_jobs").fetchone()["count"]
            total_generations = conn.execute("select coalesce(sum(attempts),0) as count from glm_jobs").fetchone()["count"]
            total_deferrals = conn.execute("select coalesce(sum(resource_deferrals),0) as count from glm_jobs").fetchone()["count"]
        last_envelope = json.loads(last["envelope_json"]) if last and last["envelope_json"] else {}
        return {
            "queue_length": sum(counts.get(state, 0) for state in ("queued", "waiting_night_window", "deferred_resource_busy", "timeout", "invalid_output", "orphaned_attempt")),
            "running_job": counts.get("running", 0),
            "success_count": sum(counts.get(state, 0) for state in ("completed", "awaiting_user_approval", "approved", "dispatched")),
            "deferred_count": counts.get("deferred_resource_busy", 0),
            "timeout_count": counts.get("timeout", 0),
            "invalid_output_count": counts.get("invalid_output", 0),
            "orphaned_attempt_count": counts.get("orphaned_attempt", 0),
            "retry_count": retries,
            "generation_count": total_generations,
            "resource_deferral_count": total_deferrals,
            "last_success": last["completed_at"] if last else None,
            "last_job_duration_ms": last_envelope.get("duration_ms"),
            "last_launcher_exit_code": last_envelope.get("exit_code"),
            "last_result_artifact": last["result_artifact"] if last else None,
            "states": counts,
            "paused": self.is_paused(),
        }

    def audit(self, event: str, task_id: str | None, **metadata: Any) -> None:
        # Metadata only: prompts/documents remain protected task artifacts.
        with self.connect() as conn:
            conn.execute(
                "insert into glm_audit(created_at,event,task_id,metadata_json) values(?,?,?,?)",
                (now_rome(), event, task_id, json.dumps(metadata, sort_keys=True, ensure_ascii=False)),
            )

    @contextmanager
    def worker_lock(self, *, blocking: bool = False) -> Iterator[bool]:
        handle = self.lock_path.open("a+")
        self.lock_path.chmod(0o600)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            try:
                fcntl.flock(handle.fileno(), operation)
            except BlockingIOError:
                yield False
                return
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "acquired_at": now_rome()}) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            yield True
        finally:
            handle.close()

    def _set_state(self, task_id: str, state: str) -> None:
        if state not in JOB_STATES:
            raise ValueError("invalid_job_state")
        with self.connect() as conn:
            conn.execute("update glm_jobs set state=?, updated_at=? where task_id=?", (state, now_rome(), task_id))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        envelope = data.pop("envelope_json", None)
        data["envelope"] = json.loads(envelope) if envelope else None
        return data


def _pid_alive(value: Any) -> bool:
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
