# -*- coding: utf-8 -*-
"""召回 shadow 埋点：只记录，不改任何决策。

目的：给「什么时候真的需要查记忆」攒数据，而不是一直靠手写阈值。

每次召回记一行：query 的轻量特征（长度、过去指向、线索词、时间词、问句）、
本会话、命中数、被丢弃数、走的通道。任何异常都吞掉，绝不影响主流程。

用法：
    python -m jinyv_memory.core.recall_shadow --db data/rp_memory.db
    python -m jinyv_memory.core.recall_shadow --db data/rp_memory.db --days 7
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta

CUE_WORDS = (
    "昨天", "前天", "上次", "之前", "以前", "还记得", "记得", "当时", "那天",
    "前几天", "上周", "上个月", "去年", "第一", "当初", "生日", "纪念",
    "说过", "答应", "承诺", "名字", "是谁", "我叫", "叫我",
)
TIME_WORDS = ("今天", "明天", "早上", "中午", "晚上", "现在", "几点", "多久",
              "刚才", "刚刚")
PAST_RE = re.compile(r"记得|还记得|之前|以前|上次|那天|当时|说过|答应|承诺|是谁|名字|叫")

KEEP = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_shadow(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT DEFAULT (datetime('now')),
  point TEXT NOT NULL DEFAULT 'recall',
  session_id TEXT NOT NULL DEFAULT '', character_id TEXT NOT NULL DEFAULT '',
  query TEXT NOT NULL DEFAULT '', qlen INTEGER NOT NULL DEFAULT 0,
  has_past INTEGER NOT NULL DEFAULT 0, has_cue INTEGER NOT NULL DEFAULT 0,
  has_time INTEGER NOT NULL DEFAULT 0, has_question INTEGER NOT NULL DEFAULT 0,
  hits INTEGER NOT NULL DEFAULT 0, dropped INTEGER NOT NULL DEFAULT 0,
  channels TEXT NOT NULL DEFAULT '[]');
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def features(q: str) -> dict:
    q = str(q or "").strip()
    return {
        "qlen": len(q),
        "has_past": int(bool(PAST_RE.search(q))),
        "has_cue": int(any(w in q for w in CUE_WORDS)),
        "has_time": int(any(w in q for w in TIME_WORDS)),
        "has_question": int(any(w in q for w in ("?", "？", "吗", "呢", "么"))),
    }


def log(conn: sqlite3.Connection, point: str, sid: str = "", cid: str = "",
        query: str = "", hits: int = 0, dropped: int = 0,
        channels=()) -> None:
    """追加一行；失败静默。channels 只存通道名，不存内容。"""
    try:
        ensure(conn)
        f = features(query)
        conn.execute(
            "INSERT INTO recall_shadow(point, session_id, character_id, query,"
            " qlen, has_past, has_cue, has_time, has_question, hits, dropped, channels)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (str(point)[:20], str(sid), str(cid), str(query or "")[:200],
             f["qlen"], f["has_past"], f["has_cue"], f["has_time"],
             f["has_question"], int(hits), int(dropped),
             json.dumps(sorted({str(c) for c in channels if c}), ensure_ascii=False)))
        conn.execute(
            "DELETE FROM recall_shadow WHERE id <= "
            "(SELECT MAX(id) FROM recall_shadow) - ?", (KEEP,))
        conn.commit()
    except Exception:
        pass


def stats(conn: sqlite3.Connection, days: int = 30) -> list:
    """按 query 特征分桶，看「查了」和「命中」的关系。"""
    ensure(conn)
    since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT has_past, has_cue, has_time, has_question, COUNT(*) n,"
        " AVG(hits) avg_hits, SUM(CASE WHEN hits>0 THEN 1 ELSE 0 END) hit_n"
        " FROM recall_shadow WHERE at>=? GROUP BY 1,2,3,4 ORDER BY n DESC", (since,)
    ).fetchall()
    out = []
    for r in rows:
        n = int(r["n"] or 0)
        out.append({
            "past": bool(r["has_past"]), "cue": bool(r["has_cue"]),
            "time": bool(r["has_time"]), "question": bool(r["has_question"]),
            "n": n, "avg_hits": round(float(r["avg_hits"] or 0), 2),
            "hit_rate": round(float(r["hit_n"] or 0) / n, 3) if n else 0.0,
        })
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("RP_DB") or "data/rp_memory.db")
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args(argv)
    conn = connect(args.db)
    try:
        for row in stats(conn, args.days):
            print(json.dumps(row, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
