# -*- coding: utf-8 -*-
"""原文保留（C6-01 / C6-02）：按单条 turn 保留 7 天，引用/复核/must-keep pin 住。

- 按轮成对：同一 turn_idx 的 user/assistant 一起计时；
- append-only：只软归档（`turns.archived_at`），不硬删；
- 20 轮只是热窗，不是保留期；
- 原文到期前必须有 L1 原子，否则 pin 住并记 owed（C20）。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import control
import gradient

RETENTION_DAYS = max(1, int(float(os.environ.get("RP_TURN_RETENTION_DAYS") or 7)))
PIN_REVIEW = (os.environ.get("RP_TURN_PIN_REVIEW") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}


def _connect(db):
    conn = sqlite3.connect(getattr(db, "path", db), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _ctx(db):
    conn = _connect(db)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def register_round(db, sid, cid, turn_idx, days=RETENTION_DAYS) -> None:
    """一轮 user+assistant 落库后登记保留期，时间取两行 created_at 的较晚者。"""
    with _ctx(db) as c:
        row = c.execute("SELECT MAX(created_at) AS ts FROM turns"
                        " WHERE session_id=? AND character_id=? AND turn_idx=?",
                        (sid, cid, int(turn_idx))).fetchone()
    ts = control.parse_iso(row["ts"] if row else None)
    until = ((ts or datetime.now(timezone.utc))
             + timedelta(days=max(0, int(days)))).isoformat(timespec="seconds")
    with _ctx(db) as c:
        c.execute("INSERT INTO turn_retention"
                  "(character_id,session_id,turn_idx,retention_until) VALUES(?,?,?,?)"
                  " ON CONFLICT(character_id,session_id,turn_idx) DO UPDATE SET"
                  " retention_until=MAX(retention_until, excluded.retention_until)",
                  (cid, sid, int(turn_idx), until))


def pin(db, sid, cid, turn_idx, hours=168, reason="compression_review") -> None:
    control.pin_turns(db, cid, sid, int(turn_idx), until_hours=hours, reason=reason)


def prune(db, cid, now=None, days=RETENTION_DAYS) -> dict:
    """软归档到期且未 pin 的原文；没有 L1 原子的先 pin，禁止无洞过期。"""
    stamp = now or datetime.now(timezone.utc)
    holes = {int(r["turn_idx"]) for r in gradient.no_hole_audit(db, cid)}
    archived, pinned = 0, 0
    with _ctx(db) as c:
        rows = c.execute("SELECT * FROM turn_retention WHERE character_id=?"
                         " AND retention_until<=? AND COALESCE(pinned_until,'')<=?"
                         " ORDER BY turn_idx", (cid, stamp.isoformat(timespec="seconds"),
                                                stamp.isoformat(timespec="seconds"))).fetchall()
        for r in rows:
            idx = int(r["turn_idx"])
            if idx in holes:
                # 原文过期前必须有 L1；否则 pin 到问题关闭，不允许删
                until = (stamp + timedelta(days=days)).isoformat(timespec="seconds")
                c.execute("UPDATE turn_retention SET pinned_until=?, pin_reason='owed'"
                          " WHERE character_id=? AND session_id=? AND turn_idx=?",
                          (until, cid, r["session_id"], idx))
                pinned += 1
                continue
            cur = c.execute(
                "UPDATE turns SET archived_at=? WHERE session_id=? AND character_id=?"
                " AND turn_idx=? AND archived_at IS NULL",
                (stamp.isoformat(timespec="seconds"), r["session_id"], cid, idx))
            archived += cur.rowcount
    return {"archived": archived, "pinned": pinned, "holes": len(holes)}


def get_source(db, sid, cid, turn_idx, include_archived=True) -> list[dict]:
    """按轮回捞原文；默认允许回捞软归档内容。"""
    sql = ("SELECT turn_idx, role, content, source, created_at, archived_at"
           " FROM turns WHERE session_id=? AND character_id=? AND turn_idx=?")
    if not include_archived:
        sql += " AND archived_at IS NULL"
    with _ctx(db) as c:
        return [dict(r) for r in c.execute(sql + " ORDER BY id",
                                           (sid, cid, int(turn_idx))).fetchall()]


def audit(db, cid) -> dict:
    """保留期体检：到期未 pin、无洞、软归档数量。"""
    with _ctx(db) as c:
        expired = c.execute(
            "SELECT COUNT(*) FROM turn_retention WHERE character_id=?"
            " AND retention_until<=? AND COALESCE(pinned_until,'')<=?",
            (cid, control.now_iso(), control.now_iso())).fetchone()[0]
        archived = c.execute(
            "SELECT COUNT(*) FROM turns WHERE character_id=? AND archived_at IS NOT NULL",
            (cid,)).fetchone()[0]
    return {"expired_unpinned": int(expired),
            "no_hole": len(gradient.no_hole_audit(db, cid)),
            "archived_turns": int(archived)}


def restore(db, sid, cid, turn_idx) -> bool:
    """审计回滚：把软归档的原文恢复为可读（不硬删、不新建行）。"""
    with _ctx(db) as c:
        cur = c.execute("UPDATE turns SET archived_at=NULL WHERE session_id=?"
                        " AND character_id=? AND turn_idx=? AND archived_at IS NOT NULL",
                        (sid, cid, int(turn_idx)))
        return cur.rowcount > 0
