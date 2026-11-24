# -*- coding: utf-8 -*-
"""记忆治理：事件时间、类型化撤回、失效传播、证据分层、矛盾留痕。

设计借鉴 QingyiPersonal 的认知账本，但只保留与平台无关、可独立运行的部分：

- occurred_at：事件真正发生的时间，与 recorded_at（created_at）分开；
- retract_kind：撤回分四类，撤掉的是解释，不是那段经历本身；
- memory_invalidations：派生对象失效的留痕，不删除原行；
- evidence_layer：亲历/听说/查到/推测/想象，推测与想象永不晋升为已验证；
- memory_contradictions：直接矛盾并列保留，不互相覆盖。

所有函数只接受 DB 句柄或连接，不依赖 QQ / NapBox / 人格系统。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Optional

RETRACT_KINDS = {
    "invalid_observation",   # 无效观察：看到/听到的内容本身不成立
    "misunderstanding",      # 误解：当时理解错了
    "belief_correction",     # 信念更正：后来知道正确答案
    "scope_revoke",          # 范围吊销：只在某个范围内有效，现予撤销
}

EVIDENCE_LAYERS = ("experienced", "hearsay", "found", "inferred", "imagined",
                   "unknown")
VERIFIABLE_LAYERS = {"experienced", "found"}

TABLES = """
CREATE TABLE IF NOT EXISTS memory_invalidations(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_id TEXT NOT NULL DEFAULT '',
  obj_kind TEXT NOT NULL,
  obj_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(obj_kind, obj_id, source_kind, source_id));
CREATE INDEX IF NOT EXISTS idx_invalid_obj
  ON memory_invalidations(obj_kind, obj_id);
CREATE TABLE IF NOT EXISTS memory_contradictions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_id TEXT NOT NULL DEFAULT '',
  a_kind TEXT NOT NULL, a_id TEXT NOT NULL,
  b_kind TEXT NOT NULL, b_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(a_kind, a_id, b_kind, b_id));
"""


def _connect(db) -> sqlite3.Connection:
    conn = sqlite3.connect(str(getattr(db, "path", db)), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure_schema(conn) -> None:
    """幂等迁移：老库就地加列，不重建、不丢数据。"""
    additions = {
        "event_cards": {
            "occurred_at": "TEXT DEFAULT NULL",
            "retract_kind": "TEXT NOT NULL DEFAULT ''",
            "evidence_layer": "TEXT NOT NULL DEFAULT 'unknown'",
        },
        "durable_facts": {
            "occurred_at": "TEXT DEFAULT NULL",
            "retract_kind": "TEXT NOT NULL DEFAULT ''",
            "evidence_layer": "TEXT NOT NULL DEFAULT 'unknown'",
        },
        "memory_units": {
            "invalidated_by": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, cols in additions.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.executescript(TABLES)


def can_verify(layer: Optional[str]) -> bool:
    """推测与想象永不晋升为「已验证」；未知来源也不给晋升。"""
    return str(layer or "unknown") in VERIFIABLE_LAYERS


def _turn_list(raw) -> list[int]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _invalidate_units(conn, cid: str, turns: Iterable[int],
                      source_kind: str, source_id: str, reason: str) -> int:
    """把来源轮次与撤回对象重叠的记忆单元标记失效（不删除）。"""
    wanted = set(int(t) for t in turns)
    if not wanted or not cid:
        return 0
    count = 0
    rows = conn.execute(
        "SELECT id, source_turns FROM memory_units WHERE character_id=?",
        (cid,)).fetchall()
    for row in rows:
        if not (wanted & set(_turn_list(row["source_turns"]))):
            continue
        conn.execute(
            "INSERT OR IGNORE INTO memory_invalidations"
            "(character_id,obj_kind,obj_id,source_kind,source_id,reason)"
            " VALUES(?,?,?,?,?,?)",
            (cid, "unit", row["id"], source_kind, source_id, reason))
        conn.execute("UPDATE memory_units SET invalidated_by=? WHERE id=? AND invalidated_by=''",
                     (source_id, row["id"]))
        count += 1
    return count


def _invalidate_superseding(conn, cid: str, table: str, key: str, obj_id: str,
                            source_kind: str, reason: str) -> int:
    """沿 supersedes 链把「基于它的修订」标记失效，原行保留。"""
    count = 0
    frontier = [str(obj_id)]
    seen = set(frontier)
    while frontier:
        current = frontier.pop()
        rows = conn.execute(
            f"SELECT {key} AS kid FROM {table} WHERE character_id=? AND supersedes_id=?",
            (cid, current)).fetchall()
        for row in rows:
            kid = str(row["kid"])
            if kid in seen:
                continue
            seen.add(kid)
            conn.execute(
                "INSERT OR IGNORE INTO memory_invalidations"
                "(character_id,obj_kind,obj_id,source_kind,source_id,reason)"
                " VALUES(?,?,?,?,?,?)",
                (cid, "fact" if table == "durable_facts" else "event",
                 kid, source_kind, obj_id, reason))
            frontier.append(kid)
            count += 1
    return count


def retract(db, kind: str, obj_id, retract_kind: str = "invalid_observation",
            by: str = "", cascade: bool = True) -> dict:
    """撤回一条事实或事件卡，并传播失效。

    kind: "fact" | "event"；retract_kind 取 RETRACT_KINDS 之一。
    返回 {retracted, invalidated_units, invalidated_derived}；对象不存在返回 0。
    """
    kind = str(kind or "")
    if kind not in ("fact", "event"):
        raise ValueError("kind 只能是 fact 或 event")
    retract_kind = str(retract_kind or "invalid_observation")
    if retract_kind not in RETRACT_KINDS:
        raise ValueError(f"未知撤回类型：{retract_kind}")

    conn = _connect(db)
    try:
        if kind == "fact":
            row = conn.execute(
                "SELECT id, character_id, turn_end FROM durable_facts WHERE id=?",
                (obj_id,)).fetchone()
            if not row:
                return {"retracted": 0, "invalidated_units": 0, "invalidated_derived": 0}
            cid = str(row["character_id"] or "")
            cur = conn.execute(
                "UPDATE durable_facts SET retracted_at=datetime('now'), retract_kind=?,"
                " retracted_by=?, revision=revision+1"
                " WHERE id=? AND IFNULL(retracted_at,'')=''",
                (retract_kind, str(by or "governance"), obj_id))
            units = _invalidate_units(conn, cid, [row["turn_end"]],
                                      "fact", str(obj_id), retract_kind) if cascade else 0
            derived = _invalidate_superseding(conn, cid, "durable_facts", "id",
                                              str(obj_id), "fact", retract_kind) if cascade else 0
        else:
            row = conn.execute(
                "SELECT event_id, character_id, turn_start, turn_end FROM event_cards"
                " WHERE event_id=?", (obj_id,)).fetchone()
            if not row:
                return {"retracted": 0, "invalidated_units": 0, "invalidated_derived": 0}
            cid = str(row["character_id"] or "")
            cur = conn.execute(
                "UPDATE event_cards SET retracted_at=datetime('now'), retract_kind=?,"
                " retracted_by=?, revision=revision+1"
                " WHERE event_id=? AND IFNULL(retracted_at,'')=''",
                (retract_kind, str(by or "governance"), obj_id))
            turns = range(int(row["turn_start"]), int(row["turn_end"]) + 1)
            units = _invalidate_units(conn, cid, turns, "event", str(obj_id),
                                      retract_kind) if cascade else 0
            derived = _invalidate_superseding(conn, cid, "event_cards", "event_id",
                                              str(obj_id), "event", retract_kind) if cascade else 0
        conn.commit()
        return {"retracted": int(cur.rowcount or 0),
                "invalidated_units": units,
                "invalidated_derived": derived}
    finally:
        conn.close()


def invalidate(db, obj_kind: str, obj_id: str, source_kind: str, source_id: str,
               cid: str = "", reason: str = "") -> bool:
    """登记一条失效关系；对象是记忆单元时同步打标记。"""
    conn = _connect(db)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO memory_invalidations"
            "(character_id,obj_kind,obj_id,source_kind,source_id,reason)"
            " VALUES(?,?,?,?,?,?)",
            (str(cid or ""), str(obj_kind), str(obj_id), str(source_kind),
             str(source_id), str(reason or "")))
        if obj_kind == "unit":
            conn.execute("UPDATE memory_units SET invalidated_by=? WHERE id=? AND invalidated_by=''",
                         (str(source_id), str(obj_id)))
        conn.commit()
        return True
    finally:
        conn.close()


def record_contradiction(db, a_kind: str, a_id: str, b_kind: str, b_id: str,
                         cid: str = "", reason: str = "") -> bool:
    """登记直接矛盾：两条记忆并列保留，交给读者判断，不自动覆盖。

    (a, b) 与 (b, a) 视为同一条，登记前统一排序，保证幂等。
    """
    left, right = (str(a_kind), str(a_id)), (str(b_kind), str(b_id))
    if right < left:
        left, right = right, left
    conn = _connect(db)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO memory_contradictions"
            "(character_id,a_kind,a_id,b_kind,b_id,reason) VALUES(?,?,?,?,?,?)",
            (str(cid or ""), left[0], left[1], right[0], right[1],
             str(reason or "")))
        conn.commit()
        return True
    finally:
        conn.close()


def contradictions(db, obj_kind: str, obj_id: str) -> list[dict]:
    conn = _connect(db)
    try:
        rows = conn.execute(
            "SELECT * FROM memory_contradictions"
            " WHERE (a_kind=? AND a_id=?) OR (b_kind=? AND b_id=?)"
            " ORDER BY id DESC", (obj_kind, str(obj_id), obj_kind, str(obj_id))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_verified(db, kind: str, obj_id, delta: int = 1) -> bool:
    """只有亲历/查到过的对象才允许累加 verified；推测、想象一律拒绝。"""
    kind = str(kind or "")
    if kind not in ("fact", "event"):
        raise ValueError("kind 只能是 fact 或 event")
    conn = _connect(db)
    try:
        if kind == "fact":
            row = conn.execute("SELECT evidence_layer FROM durable_facts WHERE id=?",
                               (obj_id,)).fetchone()
            table, key = "durable_facts", "id"
        else:
            row = conn.execute("SELECT evidence_layer FROM event_cards WHERE event_id=?",
                               (obj_id,)).fetchone()
            table, key = "event_cards", "event_id"
        if not row or not can_verify(row["evidence_layer"]):
            return False
        conn.execute(f"UPDATE {table} SET verified=COALESCE(verified,0)+? WHERE {key}=?",
                     (max(1, int(delta)), obj_id))
        conn.commit()
        return True
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
