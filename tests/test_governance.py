# -*- coding: utf-8 -*-
"""治理层回归：python tests/test_governance.py

覆盖：事件时间 occurred_at、类型化撤回与失效传播、证据分层晋升闸门、
矛盾并列留痕、单一写入者锁。
"""
import os
import sys
import time
import json
import shutil
import sqlite3
import tempfile
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "jinyv_memory", _ROOT / "jinyv_memory" / "core", _ROOT / "examples"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

os.environ.setdefault("RP_STYLE_MIDDLE", "0")

import control
import governance
import writer_lock
from rp_skeleton import DB


def fresh_db(tmp):
    path = os.path.join(tmp, "gov.db")
    return path, DB(path)


def test_schema_migration():
    tmp = tempfile.mkdtemp()
    try:
        path, db = fresh_db(tmp)
        con = sqlite3.connect(path)
        for table in ("event_cards", "durable_facts"):
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            for name in ("occurred_at", "retract_kind", "evidence_layer"):
                assert name in cols, f"{table} 缺列 {name}"
        ucols = {r[1] for r in con.execute("PRAGMA table_info(memory_units)")}
        assert "invalidated_by" in ucols
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"memory_invalidations", "memory_contradictions"} <= tables
        con.close()
        print("OK 迁移：新列与治理表就位")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_occurred_at():
    tmp = tempfile.mkdtemp()
    try:
        path, db = fresh_db(tmp)
        fid = db.add_fact("s1", "c1", "去年三月去过杭州", turn=3, speaker="用户",
                          occurred_at="2025-03-04 10:00:00",
                          evidence_layer="experienced")
        assert fid
        facts = db.list_facts_detail("s1", "c1", limit=5)
        row = [f for f in facts if f["id"] == fid][0]
        assert row["date"] == "03-04", row
        assert row["occurred_at"] == "2025-03-04 10:00:00"
        assert row["evidence_layer"] == "experienced"
        print("OK occurred_at：展示用发生时间而非记录时间")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_archive_stamps_event_time():
    tmp = tempfile.mkdtemp()
    try:
        path, db = fresh_db(tmp)
        with sqlite3.connect(path) as con:
            con.execute("INSERT INTO turns(session_id,character_id,turn_idx,role,content,"
                        "created_at) VALUES('s1','c1',4,'user','我们去了杭州','2026-05-01 09:30:00')")
        eid = db.add_card("s1", "c1", "一起去了杭州", turn=4)
        fid = db.add_fact("s1", "c1", "去过杭州", turn=4, speaker="用户")
        with sqlite3.connect(path) as con:
            card_t = con.execute("SELECT occurred_at FROM event_cards WHERE event_id=?",
                                 (eid,)).fetchone()[0]
            fact_t = con.execute("SELECT occurred_at FROM durable_facts WHERE id=?",
                                 (fid,)).fetchone()[0]
        assert card_t == "2026-05-01 09:30:00", card_t
        assert fact_t == "2026-05-01 09:30:00", fact_t

        # 合并：留存卡已有事件时间，不能被合并区间的终点改写
        e2 = db.add_card("s1", "c1", "又聊到杭州", turn=4)
        db.apply_card_merge("c1", eid, [e2], "去杭州并聊到", 4, 4)
        with sqlite3.connect(path) as con:
            merged_t = con.execute("SELECT occurred_at FROM event_cards WHERE event_id=?",
                                   (eid,)).fetchone()[0]
        assert merged_t == "2026-05-01 09:30:00", merged_t

        # 显式传入的事件时间优先于轮次记录时间
        fid2 = db.add_fact("s1", "c1", "去年三月去过杭州", turn=4,
                           occurred_at="2025-03-04 10:00:00")
        with sqlite3.connect(path) as con:
            explicit = con.execute("SELECT occurred_at FROM durable_facts WHERE id=?",
                                   (fid2,)).fetchone()[0]
        assert explicit == "2025-03-04 10:00:00", explicit
        print("OK 归档链路：自动补事件时间，合并不覆盖，显式值优先")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_typed_retraction_and_propagation():
    tmp = tempfile.mkdtemp()
    try:
        path, db = fresh_db(tmp)
        eid = db.add_card("s1", "c1", "她说周五交报告", turn=5)
        uid = control.add_unit(db, "c1", "s1", 2, 1, {"text": "周五交报告"},
                               [5, 6], salience=60)
        # 先修一条「基于它」的新事实，验证 supersedes 传播
        fid = db.add_fact("s1", "c1", "周五交报告", turn=5, speaker="用户")
        with sqlite3.connect(path) as con:
            con.execute(
                "INSERT INTO durable_facts(session_id,character_id,turn_end,fact,speaker,"
                "supersedes_id) VALUES('s1','c1',5,'改期到周一','用户',?)",
                (str(fid),))

        out = governance.retract(db, "event", eid, "misunderstanding", by="test")
        assert out["retracted"] == 1, out
        assert out["invalidated_units"] == 1, out
        units = control.list_units(db, "c1")
        assert all(u["id"] != uid for u in units), "失效单元默认不应出现在召回列表"
        units = control.list_units(db, "c1", include_invalidated=True)
        assert [u for u in units if u["id"] == uid][0]["invalidated_by"] == str(eid)

        with sqlite3.connect(path) as con:
            kind = con.execute("SELECT retract_kind FROM event_cards WHERE event_id=?",
                               (eid,)).fetchone()[0]
        assert kind == "misunderstanding"

        out2 = governance.retract(db, "fact", fid, "belief_correction", by="test")
        assert out2["retracted"] == 1
        with sqlite3.connect(path) as con:
            invalid = con.execute(
                "SELECT COUNT(*) FROM memory_invalidations WHERE source_id=?",
                (str(fid),)).fetchone()[0]
        assert invalid >= 1, "supersedes 派生行应被登记失效"

        try:
            governance.retract(db, "event", eid, "not-a-kind")
        except ValueError:
            pass
        else:
            raise AssertionError("非法撤回类型应被拒绝")
        print("OK 撤回：类型留痕 + 单元/派生失效，原行保留")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_evidence_gate():
    tmp = tempfile.mkdtemp()
    try:
        path, db = fresh_db(tmp)
        fid = db.add_fact("s1", "c1", "他猜明天会下雨", turn=1,
                          evidence_layer="inferred")
        assert governance.can_verify("inferred") is False
        assert governance.can_verify("imagined") is False
        assert governance.can_verify("experienced") is True
        assert governance.set_verified(db, "fact", fid) is False
        with sqlite3.connect(path) as con:
            v = con.execute("SELECT IFNULL(verified,0) FROM durable_facts WHERE id=?",
                            (fid,)).fetchone()[0]
        assert int(v) == 0
        fid2 = db.add_fact("s1", "c1", "他亲眼看到雨停", turn=2,
                           evidence_layer="experienced")
        assert governance.set_verified(db, "fact", fid2) is True
        print("OK 证据分层：推测/想象不能晋升为已验证")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_contradiction_kept():
    tmp = tempfile.mkdtemp()
    try:
        path, db = fresh_db(tmp)
        governance.record_contradiction(db, "fact", "1", "fact", "2",
                                        cid="c1", reason="时间冲突")
        governance.record_contradiction(db, "fact", "2", "fact", "1",
                                        cid="c1", reason="重复登记")
        rows = governance.contradictions(db, "fact", "1")
        assert len(rows) == 1, rows
        with sqlite3.connect(path) as con:
            n = con.execute("SELECT COUNT(*) FROM memory_contradictions").fetchone()[0]
        assert n == 1
        print("OK 矛盾留痕：并列登记、幂等")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_writer_lock():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "lock.db")
        core = str(_ROOT / "jinyv_memory" / "core")
        code = ("import sys,time;sys.path.insert(0,%r);import writer_lock;"
                "print(writer_lock.acquire(%r));time.sleep(1.2)" % (core, path))
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.5)
        try:
            writer_lock.acquire(path)
        except RuntimeError:
            pass
        else:
            raise AssertionError("第二个进程应抢锁失败")
        proc.wait(timeout=5)
        assert proc.returncode == 0, proc.stderr.read().decode()
        assert writer_lock.acquire(path) is True
        writer_lock.release(path)
        writer_lock.release(path)
        print("OK 写入者锁：跨进程互斥、释放后可再获取")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_schema_migration()
    test_occurred_at()
    test_archive_stamps_event_time()
    test_typed_retraction_and_propagation()
    test_evidence_gate()
    test_contradiction_kept()
    test_writer_lock()
    print("治理层回归全部通过")
