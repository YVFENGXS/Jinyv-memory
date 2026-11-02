# -*- coding: utf-8 -*-
"""跨进程租约与幂等任务登记。

两个独立的表：
- leases     回复租约 / 单例资源，带 token 与过期时间，进程崩溃后自动过期。
- job_runs   定时任务按 (job_id, run_key) 去重，失败可重试，超过次数或超时可接管。

实现用 SQLite `BEGIN IMMEDIATE` 串行化抢锁；同机多进程共享同一 runtime.db。
系统时钟回拨会让过期判断提前或延后，因此租约时间只用于抢占，不作为业务时间。
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Lease:
    resource: str
    owner: str
    token: str
    expires_at: float

    @property
    def ttl(self) -> float:
        return max(0.0, self.expires_at - time.time())


_SCHEMA = """
CREATE TABLE IF NOT EXISTS leases(
  resource TEXT PRIMARY KEY,
  owner TEXT NOT NULL,
  token TEXT NOT NULL,
  acquired_at REAL NOT NULL,
  heartbeat_at REAL NOT NULL,
  expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_leases_expires ON leases(expires_at);
CREATE TABLE IF NOT EXISTS job_runs(
  job_id TEXT NOT NULL,
  run_key TEXT NOT NULL,
  owner TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1,
  started_at REAL NOT NULL,
  finished_at REAL,
  error TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(job_id, run_key)
);
CREATE INDEX IF NOT EXISTS idx_job_runs_status ON job_runs(status, started_at);
"""


class LeaseManager:
    def __init__(self, path: str | Path, max_job_attempts: int = 3):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.max_job_attempts = max(1, int(max_job_attempts))
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def acquire(self, resource: str, owner: str, ttl: float = 30.0) -> Lease | None:
        now = time.time()
        token = uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM leases WHERE resource=? AND expires_at<=?",
                         (resource, now))
            row = conn.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO leases(resource,owner,token,acquired_at,heartbeat_at,expires_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (resource, owner, token, now, now, now + max(1.0, ttl)),
                )
                conn.execute("COMMIT")
                return Lease(resource, owner, token, now + max(1.0, ttl))
            if row["owner"] == owner:
                expires = now + max(1.0, ttl)
                conn.execute(
                    "UPDATE leases SET token=?, heartbeat_at=?, expires_at=?"
                    " WHERE resource=? AND owner=?",
                    (token, now, expires, resource, owner),
                )
                conn.execute("COMMIT")
                return Lease(resource, owner, token, expires)
            conn.execute("ROLLBACK")
            return None
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def renew(self, lease: Lease, ttl: float = 30.0) -> Lease | None:
        now = time.time()
        expires = now + max(1.0, ttl)
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE leases SET heartbeat_at=?, expires_at=?"
                    " WHERE resource=? AND token=?",
                    (now, expires, lease.resource, lease.token),
                )
            if cur.rowcount != 1:
                return None
            return Lease(lease.resource, lease.owner, lease.token, expires)
        finally:
            conn.close()

    def release(self, lease: Lease) -> bool:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM leases WHERE resource=? AND token=?",
                                   (lease.resource, lease.token))
            return cur.rowcount == 1
        finally:
            conn.close()

    def get(self, resource: str) -> Lease | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM leases WHERE resource=?", (resource,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return Lease(row["resource"], row["owner"], row["token"], float(row["expires_at"]))

    def sweep(self) -> int:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute("DELETE FROM leases WHERE expires_at<=?", (time.time(),))
            return cur.rowcount
        finally:
            conn.close()

    def claim_job(self, job_id: str, run_key: str, owner: str,
                  ttl: float = 60.0) -> bool:
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM job_runs WHERE job_id=? AND run_key=?",
                (job_id, run_key),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO job_runs(job_id,run_key,owner,status,attempts,started_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (job_id, run_key, owner, "running", 1, now),
                )
                conn.execute("COMMIT")
                return True
            retryable = (
                row["status"] == "failed"
                and int(row["attempts"]) < self.max_job_attempts
            )
            stale = row["status"] == "running" and float(row["started_at"]) + ttl <= now
            if retryable or stale:
                conn.execute(
                    "UPDATE job_runs SET owner=?, status='running',"
                    " attempts=attempts+1, started_at=?, finished_at=NULL, error=''"
                    " WHERE job_id=? AND run_key=?",
                    (owner, now, job_id, run_key),
                )
                conn.execute("COMMIT")
                return True
            conn.execute("ROLLBACK")
            return False
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def finish_job(self, job_id: str, run_key: str, ok: bool, error: str = "") -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "UPDATE job_runs SET status=?, finished_at=?, error=?"
                    " WHERE job_id=? AND run_key=?",
                    ("done" if ok else "failed", time.time(), str(error or ""),
                     job_id, run_key),
                )
        finally:
            conn.close()

    def job_status(self, job_id: str, run_key: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM job_runs WHERE job_id=? AND run_key=?",
                (job_id, run_key),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
