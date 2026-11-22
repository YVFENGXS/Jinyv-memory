# -*- coding: utf-8 -*-
"""亲密记事本 / 亲密时间线（框架，内容后填）。

设计约束：
- 只存「一行中性记录」：日期、谁主动、方式标签、发生了什么。不存露骨原文。
- 只读注入：后台在主人明确提到亲密时，把最近几条拼进本轮 prompt。
- 不自动写入：内容由人显式添加，避免压缩摘要把细节自动搬进来。
- 群聊不注入。

用法（服务器上）：
    python3 intimacy.py add --db /opt/qqbot/data/rp_memory.db \
        --sid qq-u3521544885 --cid jinyu --day 2026-11-06 \
        --actor master --tag first "第一次，主人主动"

    python3 intimacy.py list --db ... --sid ... --cid ...
"""
import argparse
import re
import sqlite3
import sys

DDL = """
CREATE TABLE IF NOT EXISTS intimacy_notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  event_day TEXT NOT NULL,
  actor TEXT NOT NULL DEFAULT 'unknown',
  tag TEXT NOT NULL DEFAULT 'other',
  note TEXT NOT NULL,
  turn_start INTEGER,
  turn_end INTEGER,
  card_id TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  UNIQUE(session_id, character_id, seq));
CREATE INDEX IF NOT EXISTS idx_intimacy_notes
  ON intimacy_notes(session_id, character_id, seq);
"""

# 触发词只用于「私聊 + 明确提到」，宁严不宽；命中后也只是多一段参考
TRIGGER = re.compile(
    r"亲密|亲热|想做|想要你|要你|抱你|摸你|用手|用尾巴|进来|进去|"
    r"床上|一起睡|洗澡|脱掉|舔|含着|夹紧|湿了|亲热")

HEAD = "【亲密记事本·仅本轮】\n（私下记录，只作参考；不要逐条复述，也不要拿它当筹码）"
RULE = ("规则：跟着主人当下的意愿走；不主动催；沿用你们说好的——"
        "慢一点、难受就说、不愿意就停。"
        "描写用动作和留白带过，不要出戏解释自己能写什么、不能写什么。")

ACTORS = {"master": "主人主动", "jinyu": "瑾瑜主动", "mutual": "彼此"}


def ensure_schema(con: sqlite3.Connection) -> None:
    con.executescript(DDL)


def is_private(sid: str) -> bool:
    return "#" not in str(sid or "")


def triggered(text: str) -> bool:
    return bool(TRIGGER.search(str(text or "")))


def _rows(con, sid, cid, limit):
    return con.execute(
        "SELECT seq, event_day, actor, tag, note FROM intimacy_notes"
        " WHERE session_id=? AND character_id=? ORDER BY seq DESC LIMIT ?",
        (sid, cid, int(limit))).fetchall()


def block(con, sid, cid, limit: int = 8, max_chars: int = 700) -> str:
    """没有记录时返回空串——框架先空着，不硬编内容。"""
    if not is_private(sid):
        return ""
    rows = list(reversed(_rows(con, sid, cid, limit)))
    if not rows:
        return ""
    lines = ["#%s %s %s：%s" % (r[0], r[1], ACTORS.get(r[2], r[2]), r[4])
             for r in rows]
    return (HEAD + "\n" + "\n".join(lines) + "\n" + RULE)[:max_chars]


def add_note(con, sid, cid, event_day, actor, tag, note,
             turn_start=None, turn_end=None, card_id=None) -> int:
    ensure_schema(con)
    seq = con.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM intimacy_notes"
        " WHERE session_id=? AND character_id=?", (sid, cid)).fetchone()[0]
    con.execute(
        "INSERT INTO intimacy_notes(session_id, character_id, seq, event_day,"
        " actor, tag, note, turn_start, turn_end, card_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (sid, cid, seq, event_day, actor, tag, note,
         turn_start, turn_end, card_id))
    con.commit()
    return seq


def timeline(con, sid, cid) -> list:
    ensure_schema(con)
    cols = ("seq", "event_day", "actor", "tag", "note")
    return [dict(zip(cols, r)) for r in con.execute(
        "SELECT seq, event_day, actor, tag, note FROM intimacy_notes"
        " WHERE session_id=? AND character_id=? ORDER BY seq", (sid, cid))]


def _main(argv) -> int:
    ap = argparse.ArgumentParser(description="亲密记事本")
    ap.add_argument("action", choices=["add", "list"])
    ap.add_argument("--db", required=True)
    ap.add_argument("--sid", required=True)
    ap.add_argument("--cid", default="jinyu")
    ap.add_argument("--day", default="")
    ap.add_argument("--actor", default="unknown")
    ap.add_argument("--tag", default="other")
    ap.add_argument("--note", nargs="*", default=[])
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    try:
        if args.action == "add":
            note = " ".join(args.note).strip()
            if not (args.day and note):
                print("需要 --day 和一句话说明")
                return 2
            seq = add_note(con, args.sid, args.cid, args.day,
                           args.actor, args.tag, note)
            print("added #%d" % seq)
        else:
            for r in timeline(con, args.sid, args.cid):
                print("#%s %s %s [%s] %s" % (r["seq"], r["event_day"],
                                             r["actor"], r["tag"], r["note"]))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
