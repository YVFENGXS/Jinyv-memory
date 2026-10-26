#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读健康检查：python3 check_health.py [/path/to/rp_memory.db]

看四件事：每个会话归档到第几轮、事件卡够不够、有没有失败区间、账本是否隔离。
有失败区间时退出码 1，可直接接监控。
"""
import os
import sqlite3
import sys

path = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RP_DB", "data/rp_memory.db")
if not os.path.exists(path):
    print(f"[FAIL] 数据库不存在：{path}")
    sys.exit(2)

conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def one(sql, *args):
    return conn.execute(sql, args).fetchone()[0]


print(f"DB: {path}")
sessions = conn.execute(
    "SELECT session_id, character_id, summarized_until_turn FROM archive_cursor"
    " ORDER BY session_id").fetchall()
if not sessions:
    print("尚无会话")
for s in sessions:
    sid, cid, cur = s["session_id"], s["character_id"], s["summarized_until_turn"]
    turns = one("SELECT COUNT(*) FROM turns WHERE session_id=? AND character_id=?", sid, cid)
    last = one("SELECT COALESCE(MAX(turn_idx),0) FROM turns WHERE session_id=? AND character_id=?",
               sid, cid) or 0
    cards = one("SELECT COUNT(*) FROM event_cards WHERE session_id=? AND character_id=?", sid, cid)
    lag = last - cur
    print(f"  {sid}/{cid}: 轮次 {last}（{turns} 条消息）｜已归档至 t{cur}｜"
          f"热尾 {lag} 轮｜事件卡 {cards} 张")

fails = conn.execute(
    "SELECT session_id, character_id, turn_start, turn_end, attempts, last_error"
    " FROM archive_failures ORDER BY last_failed_at DESC").fetchall()
if fails:
    print(f"[WARN] 归档失败区间 {len(fails)} 个：")
    for f in fails:
        print(f"  {f['session_id']}/{f['character_id']} t{f['turn_start']}-{f['turn_end']}"
              f" ×{f['attempts']}：{f['last_error']}")
else:
    print("归档失败区间：无")

cards_with_flags = one("SELECT COUNT(*) FROM event_cards WHERE conflict_flags != '[]'")
print(f"带冲突标记的卡：{cards_with_flags}")
conn.close()
sys.exit(1 if fails else 0)
