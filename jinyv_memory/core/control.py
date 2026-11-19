# -*- coding: utf-8 -*-
"""控制层（薄）：只存 ID、状态、指针，不复制摘要与原文。

本模块承担《梯度记忆与热上下文压缩》C0-01 / C0-02：
- `threads` 长期目标/线程
- `compression_reviews` 压缩审核（五字段）
- `question_ledger` 问题指纹与重问门禁
- `turn_retention` 原文按轮保留与 pin
- `memory_units` / `memory_rewrites` 梯度记忆的内容层（L1/L3/L4）
- `seal_state` 封口状态（按 topic）

单源规则：
- 承诺正文与状态只认 `promise.py` 账本，本模块不建 `commitments` 表；
  需要查询投影时用 `project_commitments()` 现算，不落库。
- 事件正文只在 `event_cards`；原文只在 `turns`；审核表只存引用。
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

STATES = ("OPEN", "DORMANT", "CLOSED")
REVIEW_STATUS = ("pending", "passed", "rejected", "owed")
QUESTION_STATUS = ("active", "owed", "answered", "unanswered")
SEAL_STATES = ("OPEN", "PAUSE_2D", "NO_PUSH", "TOTAL_SILENCE", "SAFETY")
ACTION_LEVELS = (0, 1, 2, 3, 4)

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads(
  id TEXT PRIMARY KEY,
  character_id TEXT NOT NULL,
  title TEXT NOT NULL,
  state TEXT NOT NULL,
  review_policy TEXT NOT NULL DEFAULT 'trigger',
  opened_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_threads_state ON threads(character_id, state);

CREATE TABLE IF NOT EXISTS compression_reviews(
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  source_turns TEXT NOT NULL,
  retention_until TEXT NOT NULL,
  max_retention_until TEXT,
  backend_red_flags TEXT NOT NULL DEFAULT '[]',
  ai_mark TEXT NOT NULL DEFAULT '{}',
  miscompressed_history TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  reviewed_at TEXT);
CREATE INDEX IF NOT EXISTS idx_reviews_status
  ON compression_reviews(character_id, status, created_at);

CREATE TABLE IF NOT EXISTS question_ledger(
  id TEXT PRIMARY KEY,
  character_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  reason_class TEXT NOT NULL,
  evidence_id TEXT,
  action_level INTEGER NOT NULL,
  last_asked_at TEXT,
  next_eligible_at TEXT,
  ask_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(character_id, fingerprint, reason_class));
CREATE INDEX IF NOT EXISTS idx_questions_topic
  ON question_ledger(character_id, topic, next_eligible_at);

CREATE TABLE IF NOT EXISTS turn_retention(
  character_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  turn_idx INTEGER NOT NULL,
  retention_until TEXT NOT NULL,
  pinned_until TEXT,
  pin_reason TEXT,
  PRIMARY KEY(character_id, session_id, turn_idx));
CREATE INDEX IF NOT EXISTS idx_retention_due
  ON turn_retention(character_id, retention_until);
CREATE INDEX IF NOT EXISTS idx_retention_pin
  ON turn_retention(character_id, pinned_until);

CREATE TABLE IF NOT EXISTS memory_units(
  id TEXT PRIMARY KEY,
  character_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  level INTEGER NOT NULL CHECK(level IN (1,2,3,4)),
  floor_level INTEGER NOT NULL,
  episode_id TEXT,
  thread_id TEXT,
  bucket_key TEXT,
  content_json TEXT NOT NULL,
  source_turns TEXT NOT NULL,
  atom_type TEXT,
  salience INTEGER NOT NULL,
  score_front INTEGER,
  score_back INTEGER,
  score_source TEXT NOT NULL,
  rewrite_count INTEGER NOT NULL DEFAULT 0,
  skipped_levels TEXT NOT NULL DEFAULT '[]',
  frozen INTEGER NOT NULL DEFAULT 0,
  frozen_hash TEXT,
  supersedes TEXT,
  retrieval_count INTEGER NOT NULL DEFAULT 0,
  retention_until TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_units_level ON memory_units(character_id, level, salience);
CREATE INDEX IF NOT EXISTS idx_units_bucket ON memory_units(character_id, bucket_key);

CREATE TABLE IF NOT EXISTS memory_rewrites(
  id TEXT PRIMARY KEY,
  unit_id TEXT NOT NULL,
  from_level INTEGER,
  to_level INTEGER,
  reason TEXT NOT NULL,
  reviewer TEXT,
  created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_rewrites_unit ON memory_rewrites(unit_id, created_at);

CREATE TABLE IF NOT EXISTS seal_state(
  character_id TEXT NOT NULL,
  topic TEXT NOT NULL,
  status TEXT NOT NULL,
  since TEXT NOT NULL,
  until TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(character_id, topic));
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(text) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _c(db):
    conn = _connect(getattr(db, "path", db))
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema(conn) -> None:
    """幂等建表；DB.__init__ 与旧库升级都走这里。"""
    conn.executescript(SCHEMA)


def _json(value, default):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default


def _row(row) -> Optional[dict]:
    if row is None:
        return None
    d = dict(row)
    for key in ("meta_json", "backend_red_flags", "ai_mark", "miscompressed_history",
                "content_json", "source_turns", "skipped_levels"):
        if key in d:
            d[key] = _json(d[key], {} if key in ("meta_json", "ai_mark") else [])
    return d


# ---------------------------------------------------------------- threads
def upsert_thread(db, cid, title, state="OPEN", review_policy="trigger",
                  thread_id=None, meta=None) -> str:
    tid = thread_id or new_id("thr")
    stamp = now_iso()
    with _c(db) as c:
        old = c.execute("SELECT opened_at FROM threads WHERE id=?", (tid,)).fetchone()
        opened = old["opened_at"] if old else stamp
        c.execute("INSERT OR REPLACE INTO threads"
                  "(id,character_id,title,state,review_policy,opened_at,updated_at,meta_json)"
                  " VALUES(?,?,?,?,?,?,?,?)",
                  (tid, cid, str(title or "")[:120], state, review_policy,
                   opened, stamp, json.dumps(meta or {}, ensure_ascii=False)))
    return tid


def get_thread(db, cid, thread_id) -> Optional[dict]:
    with _c(db) as c:
        return _row(c.execute("SELECT * FROM threads WHERE id=? AND character_id=?",
                              (thread_id, cid)).fetchone())


def list_threads(db, cid, include_closed=False, limit=20) -> list[dict]:
    sql = "SELECT * FROM threads WHERE character_id=?"
    if not include_closed:
        sql += " AND state!='CLOSED'"
    sql += " ORDER BY updated_at DESC LIMIT ?"
    with _c(db) as c:
        return [_row(r) for r in c.execute(sql, (cid, max(1, int(limit))))]


def set_thread_state(db, cid, thread_id, state) -> bool:
    if state not in STATES:
        return False
    with _c(db) as c:
        cur = c.execute("UPDATE threads SET state=?, updated_at=?"
                        " WHERE id=? AND character_id=?",
                        (state, now_iso(), thread_id, cid))
        return cur.rowcount > 0


# ---------------------------------------------------------------- reviews
def create_review(db, card_id, cid, source_turns, retention_until,
                  max_retention_until="", red_flags=(), ai_mark=None,
                  status="pending", review_id=None) -> str:
    rid = review_id or new_id("rev")
    stamp = now_iso()
    with _c(db) as c:
        c.execute("INSERT INTO compression_reviews"
                  "(id,card_id,character_id,source_turns,retention_until,"
                  " max_retention_until,backend_red_flags,ai_mark,miscompressed_history,"
                  " status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (rid, str(card_id), cid, json.dumps(source_turns, ensure_ascii=False),
                   str(retention_until or ""), str(max_retention_until or ""),
                   json.dumps(list(red_flags or []), ensure_ascii=False),
                   json.dumps(ai_mark or {}, ensure_ascii=False), "[]", status, stamp))
    return rid


def update_review(db, review_id, status=None, ai_mark=None, red_flags=None,
                  miscompressed=None, reviewed=True) -> bool:
    if status is not None and status not in REVIEW_STATUS:
        return False
    sets, args = [], []
    if status is not None:
        sets.append("status=?")
        args.append(status)
    if ai_mark is not None:
        sets.append("ai_mark=?")
        args.append(json.dumps(ai_mark, ensure_ascii=False))
    if red_flags is not None:
        sets.append("backend_red_flags=?")
        args.append(json.dumps(list(red_flags), ensure_ascii=False))
    if miscompressed is not None:
        sets.append("miscompressed_history=?")
        args.append(json.dumps(list(miscompressed), ensure_ascii=False))
    if reviewed:
        sets.append("reviewed_at=?")
        args.append(now_iso())
    if not sets:
        return False
    args.append(review_id)
    with _c(db) as c:
        cur = c.execute(f"UPDATE compression_reviews SET {', '.join(sets)} WHERE id=?",
                        args)
        return cur.rowcount > 0


def list_reviews(db, cid, status=None, limit=50) -> list[dict]:
    sql = "SELECT * FROM compression_reviews WHERE character_id=?"
    args = [cid]
    if status:
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _c(db) as c:
        return [_row(r) for r in c.execute(sql, args)]


# ---------------------------------------------------------------- questions
def record_question(db, cid, topic, fingerprint, reason_class, evidence_id="",
                    action_level=1, min_gap_hours=72, status="active") -> str:
    """同 (cid,fingerprint,reason) 只更新计数与时间，不新增第二条。"""
    stamp = now_iso()
    gap = timedelta(hours=max(0.0, float(min_gap_hours or 0)))
    nxt = (parse_iso(stamp) + gap).isoformat(timespec="seconds")
    level = max(0, min(4, int(action_level)))
    with _c(db) as c:
        row = c.execute("SELECT id FROM question_ledger WHERE character_id=?"
                        " AND fingerprint=? AND reason_class=?",
                        (cid, fingerprint, reason_class)).fetchone()
        if row:
            c.execute("UPDATE question_ledger SET topic=?, evidence_id=?, action_level=?,"
                      " next_eligible_at=?, ask_count=ask_count+1, status=?, updated_at=?"
                      " WHERE id=?",
                      (topic, evidence_id, level, nxt, status, stamp, row["id"]))
            return row["id"]
        qid = new_id("q")
        c.execute("INSERT INTO question_ledger"
                  "(id,character_id,topic,fingerprint,reason_class,evidence_id,"
                  " action_level,last_asked_at,next_eligible_at,ask_count,status,"
                  " created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, cid, topic, fingerprint, reason_class, evidence_id, level,
                   "", nxt, 0, status, stamp, stamp))
        return qid


def mark_asked(db, question_id, min_gap_hours=72, status="active",
               evidence_id=None) -> None:
    stamp = now_iso()
    nxt = (parse_iso(stamp) + timedelta(hours=max(0.0, float(min_gap_hours or 0)))
           ).isoformat(timespec="seconds")
    with _c(db) as c:
        if evidence_id is None:
            c.execute("UPDATE question_ledger SET last_asked_at=?, next_eligible_at=?,"
                      " ask_count=ask_count+1, status=?, updated_at=? WHERE id=?",
                      (stamp, nxt, status, stamp, question_id))
        else:
            c.execute("UPDATE question_ledger SET last_asked_at=?, next_eligible_at=?,"
                      " ask_count=ask_count+1, status=?, evidence_id=?, updated_at=?"
                      " WHERE id=?", (stamp, nxt, status, evidence_id, stamp, question_id))


def get_question(db, cid, fingerprint, reason_class) -> Optional[dict]:
    with _c(db) as c:
        return _row(c.execute("SELECT * FROM question_ledger WHERE character_id=?"
                              " AND fingerprint=? AND reason_class=?",
                              (cid, fingerprint, reason_class)).fetchone())


def list_questions(db, cid, topic=None, limit=50) -> list[dict]:
    sql = "SELECT * FROM question_ledger WHERE character_id=?"
    args = [cid]
    if topic:
        sql += " AND topic=?"
        args.append(topic)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _c(db) as c:
        return [_row(r) for r in c.execute(sql, args)]


def set_question_status(db, question_id, status) -> bool:
    if status not in QUESTION_STATUS:
        return False
    with _c(db) as c:
        cur = c.execute("UPDATE question_ledger SET status=?, updated_at=? WHERE id=?",
                        (status, now_iso(), question_id))
        return cur.rowcount > 0


# ---------------------------------------------------------------- retention
def put_retention(db, cid, sid, turn_idx, days=7, conn=None) -> None:
    until = (parse_iso(now_iso()) + timedelta(days=max(0, int(days)))).isoformat(
        timespec="seconds")
    sql = ("INSERT INTO turn_retention"
           "(character_id,session_id,turn_idx,retention_until) VALUES(?,?,?,?)"
           " ON CONFLICT(character_id,session_id,turn_idx) DO UPDATE SET"
           " retention_until=MAX(retention_until, excluded.retention_until)")
    if conn is not None:
        conn.execute(sql, (cid, sid, int(turn_idx), until))
        return
    with _c(db) as c:
        c.execute(sql, (cid, sid, int(turn_idx), until))


def pin_turns(db, cid, sid, turn_idx, until_hours=168, reason="compression_review",
              conn=None) -> None:
    until = (parse_iso(now_iso()) + timedelta(hours=max(0, float(until_hours)))
             ).isoformat(timespec="seconds")
    sql = ("INSERT INTO turn_retention"
           "(character_id,session_id,turn_idx,retention_until,pinned_until,pin_reason)"
           " VALUES(?,?,?,?,?,?) ON CONFLICT(character_id,session_id,turn_idx) DO UPDATE SET"
           " pinned_until=MAX(COALESCE(pinned_until,''), excluded.pinned_until),"
           " pin_reason=excluded.pin_reason")
    if conn is not None:
        conn.execute(sql, (cid, sid, int(turn_idx), until, until, str(reason)[:60]))
        return
    with _c(db) as c:
        c.execute(sql, (cid, sid, int(turn_idx), until, until, str(reason)[:60]))


def list_retention(db, cid, sid=None, limit=500) -> list[dict]:
    sql = "SELECT * FROM turn_retention WHERE character_id=?"
    args = [cid]
    if sid:
        sql += " AND session_id=?"
        args.append(sid)
    sql += " ORDER BY turn_idx LIMIT ?"
    args.append(max(1, int(limit)))
    with _c(db) as c:
        return [dict(r) for r in c.execute(sql, args)]


def expired_unpinned(db, cid, now=None) -> list[dict]:
    stamp = (now or now_iso())
    with _c(db) as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM turn_retention WHERE character_id=?"
            " AND retention_until<=? AND COALESCE(pinned_until,'')<=? ORDER BY turn_idx",
            (cid, stamp, stamp))]


# ---------------------------------------------------------------- units
def add_unit(db, cid, sid, level, floor_level, content, source_turns,
             episode_id="", thread_id="", bucket_key="", atom_type="",
             salience=60, score_front=None, score_back=None,
             score_source="rule", retention_until="", unit_id=None,
             conn=None) -> str:
    uid = unit_id or new_id("atom" if int(level) == 1 else "unit")
    stamp = now_iso()
    sql = ("INSERT OR REPLACE INTO memory_units"
           "(id,character_id,session_id,level,floor_level,episode_id,thread_id,"
           " bucket_key,content_json,source_turns,atom_type,salience,score_front,"
           " score_back,score_source,rewrite_count,skipped_levels,frozen,frozen_hash,"
           " supersedes,retrieval_count,retention_until,created_at,updated_at)"
           " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    args = (uid, cid, sid, int(level), int(floor_level), episode_id, thread_id,
            bucket_key, json.dumps(content, ensure_ascii=False),
            json.dumps(source_turns or [], ensure_ascii=False), atom_type,
            int(max(0, min(100, salience))), score_front, score_back,
            score_source, 0, "[]", 0, "", "", 0, retention_until, stamp, stamp)
    if conn is not None:
        conn.execute(sql, args)
        return uid
    with _c(db) as c:
        c.execute(sql, args)
    return uid


def get_unit(db, unit_id) -> Optional[dict]:
    with _c(db) as c:
        return _row(c.execute("SELECT * FROM memory_units WHERE id=?", (unit_id,)).fetchone())


def list_units(db, cid, level=None, min_salience=None, episode_id=None,
               limit=200) -> list[dict]:
    sql = "SELECT * FROM memory_units WHERE character_id=?"
    args = [cid]
    if level is not None:
        sql += " AND level=?"
        args.append(int(level))
    if min_salience is not None:
        sql += " AND salience>=?"
        args.append(int(min_salience))
    if episode_id:
        sql += " AND episode_id=?"
        args.append(episode_id)
    sql += " ORDER BY salience DESC, updated_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    with _c(db) as c:
        return [_row(r) for r in c.execute(sql, args)]


def update_unit(db, unit_id, **fields) -> bool:
    allowed = {"level", "floor_level", "bucket_key", "content_json", "atom_type",
               "salience", "score_front", "score_back", "score_source",
               "rewrite_count", "skipped_levels", "frozen", "frozen_hash",
               "retrieval_count", "retention_until", "episode_id", "thread_id"}
    sets, args = [], []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in ("content_json", "skipped_levels"):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        args.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    args += [now_iso(), unit_id]
    with _c(db) as c:
        cur = c.execute(f"UPDATE memory_units SET {', '.join(sets)} WHERE id=?", args)
        return cur.rowcount > 0


def add_rewrite(db, unit_id, from_level, to_level, reason, reviewer="auto",
                conn=None) -> str:
    rid = new_id("rw")
    args = (rid, unit_id, from_level, to_level, str(reason or "")[:120], reviewer,
            now_iso())
    sql = ("INSERT INTO memory_rewrites(id,unit_id,from_level,to_level,reason,"
           "reviewer,created_at) VALUES(?,?,?,?,?,?,?)")
    if conn is not None:
        conn.execute(sql, args)
    else:
        with _c(db) as c:
            c.execute(sql, args)
    return rid


def count_rewrites(db, unit_id) -> int:
    with _c(db) as c:
        return c.execute("SELECT COUNT(*) FROM memory_rewrites WHERE unit_id=?",
                         (unit_id,)).fetchone()[0]


# ---------------------------------------------------------------- seals
def set_seal(db, cid, topic, status, hours=48) -> bool:
    if status not in SEAL_STATES:
        return False
    stamp = now_iso()
    until = ""
    if status == "PAUSE_2D":
        until = (parse_iso(stamp) + timedelta(hours=max(0.0, float(hours or 48)))
                 ).isoformat(timespec="seconds")
    with _c(db) as c:
        c.execute("INSERT OR REPLACE INTO seal_state"
                  "(character_id,topic,status,since,until,updated_at) VALUES(?,?,?,?,?,?)",
                  (cid, str(topic or "general"), status, stamp, until, stamp))
    return True


def get_seal(db, cid, topic) -> Optional[dict]:
    with _c(db) as c:
        return _row(c.execute("SELECT * FROM seal_state WHERE character_id=?"
                              " AND topic=?", (cid, topic)).fetchone())


def list_seals(db, cid) -> list[dict]:
    with _c(db) as c:
        return [dict(r) for r in c.execute("SELECT * FROM seal_state WHERE character_id=?"
                                           " ORDER BY updated_at DESC", (cid,))]


def clear_seal(db, cid, topic) -> bool:
    with _c(db) as c:
        cur = c.execute("DELETE FROM seal_state WHERE character_id=? AND topic=?",
                        (cid, topic))
        return cur.rowcount > 0


# ---------------------------------------------------------------- 单源 / 投影
def project_commitments(ledger: dict, cid: str, thread_id="") -> list[dict]:
    """账本 → 承诺投影（只读，不落库）。正文仍归 promise.py 账本。"""
    out = []
    for p in (ledger or {}).get("promises") or []:
        if not isinstance(p, dict):
            continue
        out.append({
            "id": p.get("id"), "character_id": cid, "thread_id": thread_id,
            "text": p.get("text"), "strict": bool(p.get("strict")),
            "status": "ACTIVE", "created_turn": p.get("created_turn", 0),
            "source_turns": p.get("source_turns") or [],
            "blocker": p.get("blocker", ""),
            "master_stance": p.get("master_stance", ""),
            "next_action": p.get("next_action", ""),
            "next_check_at": p.get("next_check_at", ""),
            "last_discussed_at": p.get("last_discussed_at", ""),
            "last_asked_at": p.get("last_asked_at", ""),
            "ask_count": int(p.get("ask_count") or 0),
        })
    return out


CONTENT_COLUMNS = ("summary", "content", "body", "text", "raw", "quote", "quotes")
# 只审计控制层；memory_units 是内容层（L1/L3/L4），允许存 content_json
CONTROL_TABLES = ("threads", "compression_reviews", "question_ledger",
                  "turn_retention", "seal_state")


def single_source_audit(db) -> list[str]:
    """控制层不得出现 summary/content/body/raw 等正文字段；返回违规描述。"""
    bad = []
    with _c(db) as c:
        for table in CONTROL_TABLES:
            cols = {r["name"].lower() for r in
                    c.execute(f"PRAGMA table_info({table})")}
            for col in cols:
                if col in CONTENT_COLUMNS or col.startswith(("summary", "body")):
                    bad.append(f"{table}.{col}")
    return bad
