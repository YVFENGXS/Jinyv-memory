# -*- coding: utf-8 -*-
"""查看召回留痕：这一轮注入了哪些卡/词条，以及「为什么想起这个」。

  python tools/why.py --db /opt/qqbot/data/rp_memory.db --sid qq-u10001 --limit 3
  python tools/why.py --db data/rp_memory.db --sid s1 --cid jinyu --json

只读打开（mode=ro），不会写库、不会触发迁移；可直接指向线上库。
证据来自 recall_trace.facts / recall_trace.evidence：
  evidence = [{id, via, rank, score, cue?}]，via=kw/tag/vec/assoc/cache/graph
  facts    = [{id, turn, fact, cue?}]
"""
import argparse
import json
import sqlite3
import sys


def _json(raw, default):
    try:
        val = json.loads(raw or "")
        return val if isinstance(val, list) else default
    except (TypeError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser(description="召回留痕 / 证据链查看")
    ap.add_argument("--db", required=True, help="rp_memory.db（只读打开）")
    ap.add_argument("--sid", required=True, help="会话 id，如 qq-u10001 或 s1")
    ap.add_argument("--cid", default="jinyu")
    ap.add_argument("--limit", type=int, default=3, help="看最近几轮，默认 3")
    ap.add_argument("--json", action="store_true", help="输出原始 JSON")
    args = ap.parse_args()

    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        sys.exit(f"打不开数据库（只读）：{e}")
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recall_trace)")}
        if not cols:
            sys.exit("这个库里没有 recall_trace 表（还没跑过新版 bot）")
        rows = conn.execute(
            "SELECT * FROM recall_trace WHERE session_id=? AND character_id=?"
            " ORDER BY id DESC LIMIT ?", (args.sid, args.cid, max(1, args.limit))).fetchall()
        if not rows:
            sys.exit(f"没有留痕：session={args.sid!r} character={args.cid!r}")
        cards = {r["event_id"]: dict(r) for r in conn.execute(
            "SELECT event_id, turn_start, turn_end, summary, knowledge_boundary"
            " FROM event_cards WHERE character_id=?", (args.cid,))}
        facts = {r["id"]: dict(r) for r in conn.execute(
            "SELECT id, fact, category, turn_end, retracted_at, retracted_by"
            " FROM durable_facts WHERE character_id=?", (args.cid,))}
    finally:
        conn.close()

    out = []
    for row in rows:
        d = dict(row)
        evidence = _json(d.get("evidence") if "evidence" in d else "[]", [])
        fact_rows = _json(d.get("facts") if "facts" in d else "[]", [])
        if not evidence:                      # 老留痕：至少把 recalled 翻成卡片
            evidence = [{"id": eid, "via": "?", "rank": i}
                        for i, eid in enumerate(_json(d.get("recalled"), []), 1)]
        item = {"created_at": d.get("created_at"), "turn": d.get("turn_idx"),
                "version": d.get("version"), "query": d.get("query"),
                "dropped": d.get("dropped"),
                "evidence": evidence, "facts": fact_rows,
                "excluded": len(_json(d.get("excluded"), []))}
        out.append(item)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    for t in out:
        print(f"\n=== {t['created_at']}  t{t['turn']}  {t['version']}")
        print(f"问：{t['query']}")
        print(f"卡片证据（按注入顺序，排除 {t['excluded']} 张，裁剪 {t['dropped']} 轮）：")
        if not t["evidence"]:
            print("  （无）")
        for e in t["evidence"]:
            c = cards.get(e.get("id")) or {}
            rng = (f"t{c['turn_start']}-{c['turn_end']}" if c else "卡已不存在")
            why = e.get("via", "?")
            if e.get("cue"):
                why += f"({e['cue']})"
            if e.get("score") is not None:
                why += f" {e['score']}"
            summary = str((c or {}).get("summary") or "")[:60]
            secret = "（self）" if (c or {}).get("knowledge_boundary") == "self" else ""
            print(f"  {e.get('rank', '?'):>2}. [{e.get('id')}] {rng} {why} {summary}{secret}")
        print("词条证据：")
        if not t["facts"]:
            print("  （无）")
        for f in t["facts"]:
            row = facts.get(f.get("id")) or {}
            mark = "（已退场）" if row.get("retracted_at") else ""
            cue = f" cue={f['cue']}" if f.get("cue") else ""
            print(f"  #{f.get('id')} t{f.get('turn', '?')}{cue} {f.get('fact', '')}{mark}")


if __name__ == "__main__":
    main()
