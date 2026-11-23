# -*- coding: utf-8 -*-
"""把线上导出的 JSON 还原成只读评测用的 sqlite（按当前 schema 建表，只填导出的列）。

用法：
  python tools/eval_db_from_export.py --src export.json --dst data/memory.db

导出 JSON 结构：{"tables": {"turns": [...], "event_cards": [...], ...}}
缺的列按当前 schema 的默认值处理，不会写回线上库。
"""
import json, sqlite3, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "jinyv_memory" / "core"))
import rp_skeleton as rp

import argparse
_ap = argparse.ArgumentParser(description="从线上导出 JSON 重建评测库")
_ap.add_argument("--src", required=True, help="导出的 JSON")
_ap.add_argument("--dst", required=True, help="输出 sqlite 路径")
_args = _ap.parse_args()
SRC, DST = Path(_args.src), Path(_args.dst)
if DST.exists():
    DST.unlink()

data = json.loads(SRC.read_text(encoding="utf-8"))
db = rp.DB(str(DST))
con = sqlite3.connect(str(DST))
con.execute("PRAGMA journal_mode=WAL")

for table, rows in data["tables"].items():
    if not rows:
        continue
    cols = list(rows[0].keys())
    # 只保留目标表真实存在的列
    have = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    cols = [c for c in cols if c in have]
    sql = 'INSERT OR REPLACE INTO "%s" (%s) VALUES (%s)' % (
        table, ",".join(cols), ",".join("?" * len(cols)))
    con.executemany(sql, [[r.get(c) for c in cols] for r in rows])
con.commit()

for table, rows in data["tables"].items():
    n = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    print(f"{table:18s} export={len(rows):5d} db={n:5d}")
con.close()
print("OK ->", DST, DST.stat().st_size, "bytes")
