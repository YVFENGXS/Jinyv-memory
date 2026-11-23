# -*- coding: utf-8 -*-
"""记忆文本账本：证明「没丢过、没被悄悄改过」。

做法很朴素：每条记忆记下正文的 SHA256 和长度，账本只增不改。

- `sync()` 把库里新增/改写的正文补记成新版本（旧版本保留）；
- `verify()` 逐条比对，报告丢了、改了、还没记账的；
- `retire()` 把「合法删除/合并/撤回」登记下来，以后不再算丢；
- 改写不会阻断业务：库里的正文永远以业务为准，账本只负责留痕。

这里不碰记忆语义，也不改任何正文；纯审计，失败不影响主流程。

用法：
    python -m jinyv_memory.core.ledger --db data/rp_memory.db --verify
    python -m jinyv_memory.core.ledger --db data/rp_memory.db --sync
    python -m jinyv_memory.core.ledger --db data/rp_memory.db --retire fact 12 \
        --reason "合并到 15" --replaced-by 15
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from datetime import datetime

KINDS = ("fact", "card", "turn", "promise")
# 有对应数据库表的来源；promise 存在账本 JSON 里，只登记不比对正文。
TABLE_KINDS = frozenset({"fact", "card", "turn"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS text_ledger(
  kind TEXT NOT NULL, ref_id TEXT NOT NULL, rev INTEGER NOT NULL,
  sha256 TEXT NOT NULL, len INTEGER NOT NULL,
  recorded_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY(kind, ref_id, rev));
CREATE TABLE IF NOT EXISTS text_ledger_retired(
  kind TEXT NOT NULL, ref_id TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '', replaced_by TEXT NOT NULL DEFAULT '',
  by TEXT NOT NULL DEFAULT '', snippet TEXT NOT NULL DEFAULT '',
  retired_at TEXT DEFAULT (datetime('now')),
  PRIMARY KEY(kind, ref_id));
CREATE INDEX IF NOT EXISTS idx_text_ledger_ref ON text_ledger(kind, ref_id);
"""


def sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------- 当前正文

def _current_rows(conn: sqlite3.Connection):
    """产出 (kind, ref_id, text, revision)；缺少的表直接跳过。"""
    queries = {
        "fact": "SELECT id, fact, revision FROM durable_facts",
        "card": "SELECT event_id, summary, revision FROM event_cards",
        "turn": "SELECT id, content, 1 FROM turns",
    }
    for kind, sql in queries.items():
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            yield kind, str(row[0]), row[1] or "", int(row[2] or 1)


def _latest(conn: sqlite3.Connection, kind: str, ref_id: str):
    return conn.execute(
        "SELECT rev, sha256, len FROM text_ledger WHERE kind=? AND ref_id=? "
        "ORDER BY rev DESC LIMIT 1", (kind, ref_id)).fetchone()


def _retired(conn: sqlite3.Connection) -> set:
    return {(r["kind"], r["ref_id"])
            for r in conn.execute("SELECT kind, ref_id FROM text_ledger_retired")}


# ---------------------------------------------------------------- 写入

def record(conn: sqlite3.Connection, kind: str, ref_id: str, text: str,
           rev: int | None = None) -> int:
    """记一版正文；同一内容不会重复记。返回写入的 rev。"""
    ensure(conn)
    text = text or ""
    last = _latest(conn, kind, str(ref_id))
    if last and last["sha256"] == sha(text):
        return int(last["rev"])
    if rev is None:
        rev = int(last["rev"]) + 1 if last else 1
    conn.execute(
        "INSERT OR IGNORE INTO text_ledger(kind, ref_id, rev, sha256, len) "
        "VALUES(?,?,?,?,?)",
        (kind, str(ref_id), int(rev), sha(text), len(text)))
    conn.commit()
    return int(rev)


def sync(conn: sqlite3.Connection) -> int:
    """把库里的当前正文补进账本；旧版本保留，不改任何正文。"""
    ensure(conn)
    added = 0
    for kind, ref_id, text, revision in _current_rows(conn):
        last = _latest(conn, kind, ref_id)
        if last and last["sha256"] == sha(text):
            continue
        rev = max(int(last["rev"]) + 1, revision) if last else max(1, revision)
        cur = conn.execute(
            "INSERT OR IGNORE INTO text_ledger(kind, ref_id, rev, sha256, len) "
            "VALUES(?,?,?,?,?)", (kind, ref_id, rev, sha(text), len(text)))
        added += cur.rowcount
    conn.commit()
    return added


def retire(conn: sqlite3.Connection, kind: str, ref_id: str, reason: str = "",
           replaced_by: str = "", by: str = "user", snippet: str = "") -> None:
    """登记「合法消失」：删除、合并、撤回。记录留在账本里，不再算丢。"""
    ensure(conn)
    conn.execute(
        "INSERT INTO text_ledger_retired(kind, ref_id, reason, replaced_by, by, snippet) "
        "VALUES(?,?,?,?,?,?) ON CONFLICT(kind, ref_id) DO UPDATE SET "
        "reason=excluded.reason, replaced_by=excluded.replaced_by, "
        "by=excluded.by, snippet=excluded.snippet",
        (kind, str(ref_id), reason, str(replaced_by or ""), by, (snippet or "")[:120]))
    conn.commit()


# ---------------------------------------------------------------- 校验

def verify(conn: sqlite3.Connection) -> dict:
    ensure(conn)
    current = {}
    for kind, ref_id, text, _rev in _current_rows(conn):
        current[(kind, ref_id)] = text
    retired = _retired(conn)
    entries = {}
    for row in conn.execute(
            "SELECT kind, ref_id, rev, sha256, len FROM text_ledger "
            "ORDER BY kind, ref_id, rev"):
        entries[(row["kind"], row["ref_id"])] = row

    missing, changed, fresh = [], [], []
    for key, rec in entries.items():
        if key in retired or key[0] not in TABLE_KINDS:
            continue
        text = current.get(key)
        if text is None:
            missing.append(key)
        elif sha(text) != rec["sha256"]:
            changed.append(key)
    for key in current:
        if key not in entries and key not in retired:
            fresh.append(key)

    ok = not missing and not changed
    return {"ok": ok, "entries": len(entries), "current": len(current),
            "retired": len(retired), "missing": missing,
            "changed": changed, "new": fresh}


def report(conn: sqlite3.Connection) -> dict:
    result = verify(conn)
    print(f"账本 {result['entries']} 条｜库内 {result['current']} 条｜"
          f"已登记作废 {result['retired']} 条")
    if result["missing"]:
        print("丢了:", result["missing"][:10])
    if result["changed"]:
        print("正文变了（跑 --sync 记新版本）:", result["changed"][:10])
    if result["new"]:
        print("新增未记账（跑 --sync 补上）:", len(result["new"]))
    print("校验通过" if result["ok"] else "校验未通过")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("RP_DB") or "data/rp_memory.db")
    ap.add_argument("--sync", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--retire", nargs=2, metavar=("KIND", "REF_ID"))
    ap.add_argument("--reason", default="")
    ap.add_argument("--replaced-by", default="")
    ap.add_argument("--by", default="user")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        if args.retire:
            kind, ref_id = args.retire
            retire(conn, kind, ref_id, args.reason, args.replaced_by, args.by)
            print(f"已登记作废 {kind}:{ref_id}")
            return 0
        if args.sync:
            print(f"补记 {sync(conn)} 个版本")
        if args.verify:
            return 0 if verify(conn)["ok"] else 1
        report(conn)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
