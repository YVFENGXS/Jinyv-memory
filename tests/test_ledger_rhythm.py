# -*- coding: utf-8 -*-
"""文本账本 / 重要性衰减 / 召回埋点 回归。

运行：python tests/test_ledger_rhythm.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_CORE = _ROOT / "jinyv_memory" / "core"
if str(_CORE) not in sys.path:
    sys.path.insert(0, str(_CORE))

import ledger
import recall_shadow
import rhythm

MIN_SCHEMA = """
CREATE TABLE durable_facts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, character_id TEXT,
  fact TEXT, importance REAL DEFAULT 0.5, emotion REAL DEFAULT 0.5,
  attention_hits INTEGER DEFAULT 0, last_used_at TEXT DEFAULT '',
  revision INTEGER DEFAULT 1, retracted_at TEXT DEFAULT NULL,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE event_cards(
  event_id TEXT PRIMARY KEY, summary TEXT, revision INTEGER DEFAULT 1,
  created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE turns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT,
  created_at TEXT DEFAULT (datetime('now')));
"""


def _db():
    path = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(MIN_SCHEMA)
    conn.commit()
    return path, conn


def test_ledger_roundtrip():
    path, conn = _db()
    conn.execute("INSERT INTO durable_facts(fact) VALUES('主人喜欢蓝色')")
    conn.execute("INSERT INTO event_cards(event_id, summary) VALUES('e1','第一次见面')")
    conn.execute("INSERT INTO turns(content) VALUES('在吗')")
    conn.commit()
    assert ledger.sync(conn) == 3
    assert ledger.verify(conn)["ok"]

    # 正文被改：先报 changed，再 sync 记新版本
    conn.execute("UPDATE durable_facts SET fact='主人喜欢深蓝' WHERE id=1")
    conn.commit()
    out = ledger.verify(conn)
    assert not out["ok"] and ("fact", "1") in out["changed"], out
    assert ledger.sync(conn) == 1
    assert ledger.verify(conn)["ok"], ledger.verify(conn)

    # 删除：报 missing；登记作废后通过
    conn.execute("DELETE FROM durable_facts WHERE id=1")
    conn.commit()
    assert ("fact", "1") in ledger.verify(conn)["missing"]
    ledger.retire(conn, "fact", 1, reason="合并到 2", replaced_by="2",
                  by="test", snippet="主人喜欢蓝色")
    assert ledger.verify(conn)["ok"]
    conn.close()
    os.unlink(path)
    print("L1 账本 sync/verify/retire: OK")


def test_ledger_promise():
    path, conn = _db()
    ledger.record(conn, "promise", "P-003", "周末陪主人看海")
    assert ledger.verify(conn)["ok"]
    ledger.retire(conn, "promise", "P-003", reason="合并到 P-001",
                  replaced_by="P-001", by="promise.merge", snippet="周末陪主人看海")
    assert ledger.verify(conn)["ok"]
    row = conn.execute("SELECT * FROM text_ledger_retired WHERE kind='promise'").fetchone()
    assert row["replaced_by"] == "P-001" and row["reason"].startswith("合并")
    conn.close()
    os.unlink(path)
    print("L2 承诺合并留痕: OK")


def test_rhythm_fresh_protected():
    path, conn = _db()
    conn.execute("INSERT INTO durable_facts(fact, importance, created_at)"
                 " VALUES('刚发生的事', 0.5, datetime('now'))")
    conn.execute("INSERT INTO durable_facts(fact, importance, created_at)"
                 " VALUES('刻在心里的事', 0.72, datetime('now','-30 day'))")
    conn.commit()
    _rows, changes = rhythm.plan(conn)
    ids = {c["id"] for c in changes}
    assert "1" not in ids, "6 小时内不该衰减"
    assert "2" not in ids, "0.70 以上受保护"
    conn.close()
    os.unlink(path)
    print("L3 新鲜期与保护阈值: OK")


def test_rhythm_decay_and_cap():
    path, conn = _db()
    conn.execute("INSERT INTO durable_facts(fact, importance, created_at)"
                 " VALUES('八天前的小事', 0.5, datetime('now','-8 day'))")
    conn.commit()
    _rows, changes = rhythm.plan(conn, decay=True)
    assert len(changes) == 1
    item = changes[0]
    assert item["new"] < 0.5
    assert item["importance"] - item["new"] <= rhythm.MAX_DAILY_DROP + 1e-9
    assert item["new"] >= rhythm.FLOOR
    assert item["phase"] in ("记得清楚", "有点模糊", "快忘了")
    assert rhythm.apply(conn, changes, path) == 1
    got = conn.execute("SELECT importance FROM durable_facts WHERE id=1").fetchone()[0]
    assert abs(got - item["new"]) < 1e-6
    assert conn.execute("SELECT COUNT(*) FROM rhythm_audit").fetchone()[0] == 1
    conn.close()
    os.unlink(path)
    print("L4 衰减写库与审计: OK")


def test_rhythm_longterm_no_drop():
    """默认长线记忆：放再久也不降低，只有被想起时才加固。"""
    path, conn = _db()
    conn.execute("INSERT INTO durable_facts(fact, importance, created_at)"
                 " VALUES('很久以前的小事', 0.5, datetime('now','-90 day'))")
    conn.commit()
    rows, changes = rhythm.plan(conn, decay=False)
    assert not changes, changes
    assert rows[0]["new"] == 0.5 and rows[0]["drop"] == 0.0, rows[0]
    conn.close()
    os.unlink(path)
    print("L4b 长线不衰减: OK")


def test_rhythm_reinforce():
    path, conn = _db()
    conn.execute("INSERT INTO durable_facts(fact, importance, attention_hits,"
                 " created_at) VALUES('被想起的事', 0.5, 1, datetime('now','-10 day'))")
    conn.commit()
    _rows, changes = rhythm.plan(conn, decay=False)
    rhythm.ensure(conn)
    conn.execute("INSERT OR REPLACE INTO rhythm_state(ref_id, hits, last_used)"
                 " VALUES('1', 1, '')")
    conn.commit()
    conn.execute("UPDATE durable_facts SET attention_hits=4 WHERE id=1")
    conn.commit()
    _rows, changes = rhythm.plan(conn, decay=False)
    item = changes[0]
    assert item.get("reinforced", 0) > 0, item
    assert item["new"] <= rhythm.REINFORCE_CAP
    conn.close()
    os.unlink(path)
    print("L5 回忆加固: OK")


def test_shadow():
    path, conn = _db()
    f = recall_shadow.features("你昨天答应过我什么？")
    assert f["has_past"] and f["has_cue"] and f["has_question"]
    recall_shadow.log(conn, "recall", "s1", "jinyu", "你昨天答应过我什么？",
                      hits=2, dropped=0, channels=["kw", "vec"])
    rows = recall_shadow.stats(conn, days=1)
    assert rows and rows[0]["n"] == 1 and rows[0]["avg_hits"] == 2.0, rows
    conn.close()
    os.unlink(path)
    print("L6 召回埋点: OK")


if __name__ == "__main__":
    test_ledger_roundtrip()
    test_ledger_promise()
    test_rhythm_fresh_protected()
    test_rhythm_decay_and_cap()
    test_rhythm_longterm_no_drop()
    test_rhythm_reinforce()
    test_shadow()
    print("\nledger / rhythm / shadow tests passed")
