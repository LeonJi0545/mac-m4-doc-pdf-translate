"""SQLite Job 仓储（方案 §9）。

只用 SQLite + 文件系统，不引入 Redis / RabbitMQ / PostgreSQL（§23）。

线程模型：API 线程读、worker 线程写。用 WAL 让读不阻塞写，并且
**每个线程自己的 connection** —— ``check_same_thread`` 默认为 True 是对的，
不要为图省事把它关掉（那会把并发问题推迟到难以复现的运行期）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from app.core.errors import IllegalTransition
from app.core.logging import get_logger
from app.jobs.models import (
    ALLOWED_TRANSITIONS,
    FileStatus,
    Job,
    JobFile,
    JobStatus,
    JobStatusView,
    now_iso,
)

log = get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id          TEXT PRIMARY KEY,
  source_path     TEXT NOT NULL,
  output_path     TEXT NOT NULL,
  source_language TEXT NOT NULL,
  target_language TEXT NOT NULL,
  status          TEXT NOT NULL,
  progress        INTEGER NOT NULL DEFAULT 0,
  total           INTEGER NOT NULL DEFAULT 0,
  created_at      TEXT NOT NULL,
  started_at      TEXT,
  completed_at    TEXT,
  error           TEXT
);

CREATE TABLE IF NOT EXISTS job_files (
  job_id        TEXT NOT NULL REFERENCES jobs(job_id),
  seq           INTEGER NOT NULL,
  source_path   TEXT NOT NULL,
  output_path   TEXT NOT NULL,
  status        TEXT NOT NULL,
  error         TEXT,
  warnings_json TEXT,
  started_at    TEXT,
  completed_at  TEXT,
  PRIMARY KEY (job_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_job_files_status ON job_files(job_id, status);
"""


class JobStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._write_lock = threading.Lock()
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # ── 连接管理 ──────────────────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=10.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ── 写 ────────────────────────────────────────────────────────────────

    def create_job(self, job: Job, files: list[JobFile]) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (job_id, source_path, output_path, source_language,"
                " target_language, status, progress, total, created_at, started_at,"
                " completed_at, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.job_id, job.source_path, job.output_path, job.source_language,
                    job.target_language, str(job.status), job.progress, job.total,
                    job.created_at, job.started_at, job.completed_at, job.error,
                ),
            )
            conn.executemany(
                "INSERT INTO job_files (job_id, seq, source_path, output_path, status,"
                " error, warnings_json, started_at, completed_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        f.job_id, f.seq, f.source_path, f.output_path, str(f.status),
                        f.error, json.dumps(f.warnings, ensure_ascii=False),
                        f.started_at, f.completed_at,
                    )
                    for f in files
                ],
            )

    def set_status(self, job_id: str, target: JobStatus, *, error: str | None = None) -> None:
        """按状态机跃迁。非法跃迁抛 ``IllegalTransition``。"""
        with self._write_lock, self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            current = JobStatus(row["status"])
            if current == target:
                return
            if target not in ALLOWED_TRANSITIONS[current]:
                raise IllegalTransition(str(current), str(target))

            fields = ["status=?"]
            params: list[object] = [str(target)]
            if target is JobStatus.PARSING and current is JobStatus.QUEUED:
                fields.append("started_at=?")
                params.append(now_iso())
            if target in {JobStatus.COMPLETED, JobStatus.FAILED}:
                fields.append("completed_at=?")
                params.append(now_iso())
            if error is not None:
                fields.append("error=?")
                params.append(error)
            params.append(job_id)
            conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE job_id=?", params)

    def mark_file(
        self,
        job_id: str,
        seq: int,
        status: FileStatus,
        *,
        error: str | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE job_files SET status=?, error=?, warnings_json=?, completed_at=?"
                " WHERE job_id=? AND seq=?",
                (
                    str(status), error,
                    json.dumps(warnings or [], ensure_ascii=False),
                    now_iso(), job_id, seq,
                ),
            )

    def bump_progress(self, job_id: str) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET progress = MIN(progress + 1, total) WHERE job_id=?",
                (job_id,),
            )

    # ── 读 ────────────────────────────────────────────────────────────────

    def get_job(self, job_id: str) -> Job | None:
        row = self._conn().execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            return None
        return Job(
            job_id=row["job_id"],
            source_path=row["source_path"],
            output_path=row["output_path"],
            source_language=row["source_language"],
            target_language=row["target_language"],
            status=JobStatus(row["status"]),
            progress=row["progress"],
            total=row["total"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            error=row["error"],
        )

    def list_files(self, job_id: str) -> list[JobFile]:
        rows = self._conn().execute(
            "SELECT * FROM job_files WHERE job_id=? ORDER BY seq", (job_id,)
        ).fetchall()
        return [
            JobFile(
                job_id=r["job_id"],
                seq=r["seq"],
                source_path=r["source_path"],
                output_path=r["output_path"],
                status=FileStatus(r["status"]),
                error=r["error"],
                warnings=json.loads(r["warnings_json"] or "[]"),
                started_at=r["started_at"],
                completed_at=r["completed_at"],
            )
            for r in rows
        ]

    def get_status_view(self, job_id: str) -> JobStatusView | None:
        job = self.get_job(job_id)
        if job is None:
            return None
        files = self.list_files(job_id)
        counts = {s: 0 for s in FileStatus}
        for f in files:
            counts[f.status] += 1
        return JobStatusView(
            job_id=job.job_id,
            status=job.status,
            progress=job.progress,
            total=job.total,
            completed=counts[FileStatus.COMPLETED],
            skipped=counts[FileStatus.SKIPPED],
            failed=counts[FileStatus.FAILED],
            source_path=job.source_path,
            output_path=job.output_path,
            source_language=job.source_language,
            target_language=job.target_language,
            created_at=job.created_at,
            started_at=job.started_at,
            completed_at=job.completed_at,
            error=job.error,
            failures=[
                (f.source_path, f.error or "")
                for f in files
                if f.status is FileStatus.FAILED
            ],
            warnings=[
                (f.source_path, w) for f in files for w in f.warnings
            ],
        )

    def list_jobs(self, limit: int = 50) -> list[Job]:
        rows = self._conn().execute(
            "SELECT job_id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [j for j in (self.get_job(r["job_id"]) for r in rows) if j is not None]

    def pending_job_ids(self) -> list[str]:
        rows = self._conn().execute(
            "SELECT job_id FROM jobs WHERE status NOT IN ('completed','failed')"
            " ORDER BY created_at"
        ).fetchall()
        return [r["job_id"] for r in rows]
