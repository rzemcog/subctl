from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import SubctlError, ValidationError
from .service import SubscriptionService, summary_message


TERMINAL_STATUSES = ("succeeded", "failed", "interrupted")


class JobStore:
    def __init__(self, path: Path, *, retention: int = 100) -> None:
        self.path = Path(path)
        self.retention = retention
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    target TEXT,
                    status TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    attempt INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS render_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rendered INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    config_version INTEGER NOT NULL DEFAULT 0,
                    finished_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS fetch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT NOT NULL,
                    format TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    http_status INTEGER NOT NULL,
                    content_length INTEGER
                )
                """
            )
            connection.commit()

    def recover_active(self) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status='queued', message='recovered after service restart', attempt=attempt+1 "
                "WHERE status IN ('queued', 'running')"
            )
            connection.commit()
            return cursor.rowcount

    def create(self, kind: str, target: str | None = None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        created_at = _now()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs (id, kind, target, status, created_at) VALUES (?, ?, ?, 'queued', ?)",
                (job_id, kind, target, created_at),
            )
            connection.commit()
        return self.get(job_id)

    def claim_next(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            started_at = _now()
            connection.execute(
                "UPDATE jobs SET status='running', started_at=?, message=NULL WHERE id=?",
                (started_at, row["id"]),
            )
            connection.commit()
        return self.get(row["id"])

    def finish(self, job_id: str, *, status: str, message: str) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal status: {status}")
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status=?, message=?, finished_at=? WHERE id=?",
                (status, message[:1000], _now(), job_id),
            )
            connection.commit()
        self.prune()

    def get(self, job_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(limit, self.retention))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def latest(self, kind: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE kind=? ORDER BY created_at DESC LIMIT 1", (kind,)
            ).fetchone()
        return dict(row) if row is not None else None

    def prune(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM jobs WHERE id IN ("
                "SELECT id FROM jobs WHERE status IN ('succeeded','failed','interrupted') "
                "ORDER BY created_at DESC LIMIT -1 OFFSET ?)",
                (self.retention,),
            )
            connection.commit()

    def record_render_summary(self, job_id: str, summary: Any, *, config_version: int = 0) -> None:
        users = getattr(summary, "users", ())
        if not users:
            return
        finished_at = _now()
        with self._connect() as connection:
            for result in users:
                failed = int(getattr(result, "failed", 0) > 0)
                connection.execute(
                    "INSERT INTO render_results "
                    "(job_id, user_name, status, rendered, failed, error, config_version, finished_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        result.user_name,
                        "failed" if failed else "succeeded",
                        int(getattr(result, "rendered", 0)),
                        int(getattr(result, "failed", 0)),
                        getattr(result, "error", None),
                        config_version,
                        finished_at,
                    ),
                )
            connection.commit()

    def record_render_failure(self, job_id: str, user_name: str | None, message: str) -> None:
        if not user_name:
            return
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO render_results "
                "(job_id, user_name, status, failed, error, finished_at) VALUES (?, ?, 'failed', 1, ?, ?)",
                (job_id, user_name, message[:1000], _now()),
            )
            connection.commit()

    def record_fetch(self, user_name: str, format_name: str, http_status: int, content_length: int | None) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO fetch_events (user_name, format, fetched_at, http_status, content_length) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_name, format_name, _now(), http_status, content_length),
            )
            connection.execute(
                "DELETE FROM fetch_events WHERE id IN ("
                "SELECT id FROM fetch_events ORDER BY id DESC LIMIT -1 OFFSET 1000)"
            )
            connection.commit()

    def user_activity(self, user_name: str) -> dict[str, Any]:
        with self._connect() as connection:
            active = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running') AND "
                "(kind='render_all' OR (kind='render_user' AND target=?)) "
                "ORDER BY created_at LIMIT 1",
                (user_name,),
            ).fetchone()
            render = connection.execute(
                "SELECT * FROM render_results WHERE user_name=? ORDER BY id DESC LIMIT 1",
                (user_name,),
            ).fetchone()
            fetch_rows = connection.execute(
                "SELECT * FROM fetch_events WHERE user_name=? ORDER BY id DESC LIMIT 100",
                (user_name,),
            ).fetchall()
        if active is not None:
            render_view = {
                "status": active["status"],
                "job_id": active["id"],
                "started_at": active["started_at"],
                "finished_at": None,
                "error": None,
                "config_version": None,
            }
        elif render is not None:
            render_view = {
                "status": render["status"],
                "job_id": render["job_id"],
                "started_at": None,
                "finished_at": render["finished_at"],
                "error": render["error"],
                "config_version": render["config_version"],
            }
        else:
            render_view = {
                "status": "not_generated",
                "job_id": None,
                "started_at": None,
                "finished_at": None,
                "error": None,
                "config_version": None,
            }
        fetch: dict[str, Any] = {}
        for row in fetch_rows:
            if row["format"] not in fetch:
                fetch[row["format"]] = {
                    "fetched_at": row["fetched_at"],
                    "http_status": row["http_status"],
                    "content_length": row["content_length"],
                }
        return {"render": render_view, "fetch": fetch}


class JobRunner:
    ALLOWED_KINDS = {"render_user", "render_all", "refresh_provider"}

    def __init__(self, service: SubscriptionService, store: JobStore) -> None:
        self.service = service
        self.store = store
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.store.recover_active()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="subctl-web-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def enqueue(self, kind: str, target: str | None = None) -> dict[str, Any]:
        if kind not in self.ALLOWED_KINDS:
            raise ValidationError(f"unsupported job kind: {kind}")
        job = self.store.create(kind, target)
        self._wake.set()
        return job

    def _run(self) -> None:
        while not self._stop.is_set():
            job = self.store.claim_next()
            if job is None:
                self._wake.wait(timeout=0.5)
                self._wake.clear()
                continue
            try:
                message = self._execute(job)
            except Exception as exc:  # keep worker alive and expose safe error text
                safe_message = _safe_error(exc)
                self.store.record_render_failure(job["id"], job.get("target"), safe_message)
                self.store.finish(job["id"], status="failed", message=safe_message)
            else:
                self.store.finish(job["id"], status="succeeded", message=message)

    def _execute(self, job: dict[str, Any]) -> str:
        kind = job["kind"]
        target = job.get("target")
        if kind == "render_user":
            try:
                summary = self.service.render_user(name=target or "")
            except ValidationError as exc:
                if "provider cache is missing" not in str(exc):
                    raise
                self.service.refresh_provider()
                summary = self.service.render_user(name=target or "")
            self.store.record_render_summary(
                job["id"], summary, config_version=_service_settings_version(self.service)
            )
            return f"user={target}: {summary_message(summary)}"
        if kind == "render_all":
            summary = self.service.render_all()
            self.store.record_render_summary(
                job["id"], summary, config_version=_service_settings_version(self.service)
            )
            return summary_message(summary)
        if kind == "refresh_provider":
            self.service.refresh_provider()
            summary = self.service.render_all()
            self.store.record_render_summary(
                job["id"], summary, config_version=_service_settings_version(self.service)
            )
            return f"provider refreshed; {summary_message(summary)}"
        raise ValidationError(f"unsupported job kind: {kind}")


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, SubctlError):
        return str(exc)
    return f"operation failed: {type(exc).__name__}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _service_settings_version(service: Any) -> int:
    try:
        value = service.settings().get("version", 0)
        return value if isinstance(value, int) else 0
    except Exception:
        return 0
