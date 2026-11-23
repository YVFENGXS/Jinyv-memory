# -*- coding: utf-8 -*-
"""重要性衰减：可选的时间淡忘；默认长线记忆，只加固不降低。

产品定位是「长线记忆」：默认不遗忘，`importance` 只增不减——
被召回一次仍会加固（attention_hits 增加 +0.02，封顶 0.68），
但不会因为「很久没被想起」自己变淡。需要旧行为时显式加 `--decay`
或设 `RP_RHYTHM_DECAY=1`。

衰减模式（--decay）的曲线：
    rate = 0.008 × 新鲜度 × 重要性 × 回忆加固 × 情绪
    新值 = 旧值 × (1 - rate)，单日最多掉 0.03，最低 0.05

- 6 小时内刚形成的词条不衰减；
- importance >= 0.70 的词条完全保护（刻在心里的东西）；
- 被召回一次（attention_hits 增加）加固 +0.02，封顶 0.68；
- 带情绪的词条掉得慢 15%。

只作用于 `durable_facts`，不动事件卡与原文；默认只演算，`--apply` 才写库，
写之前先做整库备份并校验。

用法：
    python -m jinyv_memory.core.rhythm --db data/rp_memory.db
    python -m jinyv_memory.core.rhythm --db data/rp_memory.db --apply
    python -m jinyv_memory.core.rhythm --db data/rp_memory.db --decay --apply
    python -m jinyv_memory.core.rhythm --db data/rp_memory.db --explain 12
    python -m jinyv_memory.core.rhythm --db data/rp_memory.db --curve
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

BASE_RATE = 0.008
MAX_DAILY_DROP = 0.03
FLOOR = 0.05
PROTECTED = 0.70
FRESH_HOURS = 6.0
REINFORCE = 0.02
REINFORCE_CAP = 0.68

SCHEMA = """
CREATE TABLE IF NOT EXISTS rhythm_state(
  ref_id TEXT PRIMARY KEY, hits REAL NOT NULL DEFAULT 0,
  last_used TEXT NOT NULL DEFAULT '', applied_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS rhythm_audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref_id TEXT NOT NULL,
  old REAL NOT NULL, new REAL NOT NULL, rate REAL NOT NULL,
  reinforced REAL NOT NULL DEFAULT 0, phase TEXT NOT NULL DEFAULT '',
  at TEXT DEFAULT (datetime('now')));
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _ts(value) -> float:
    """SQLite 的 datetime('now') 是 UTC；无时区的字符串按 UTC 解释。"""
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def freshness(age_days: float) -> float:
    if age_days < FRESH_HOURS / 24.0:
        return 0.0
    return min(2.5, 1.0 + 1.5 * math.exp(-age_days / 2.0))


def salience(importance: float) -> float:
    ratio = min(1.0, max(0.0, importance / PROTECTED))
    return max(0.55, min(1.45, 0.55 + 0.9 * (1.0 - ratio)))


def review_factor(hits: float, last_used: float) -> float:
    base = 1.0 / (1.0 + 0.5 * math.log(1.0 + max(0.0, hits)))
    if last_used and (time.time() - last_used) < 3 * 86400:
        base *= 0.75
    return base


def emotion_factor(emotion: float) -> float:
    return 0.85 if (emotion >= 0.65 or emotion <= 0.35) else 1.0


def explain(importance: float, age_days: float, hits: float,
            last_used: float, emotion: float) -> dict:
    f = freshness(age_days)
    s = salience(importance)
    r = review_factor(hits, last_used)
    e = emotion_factor(emotion)
    rate = min(0.08, BASE_RATE * f * s * r * e)
    drop = min(importance * rate, MAX_DAILY_DROP)
    return {"freshness": f, "salience": s, "review": r, "emotion": e,
            "rate": rate, "drop": drop}


def phase_of(importance: float, age_days: float) -> str:
    if importance >= PROTECTED:
        return "刻在心里"
    if age_days < 1:
        return "还新鲜"
    if importance >= 0.55:
        return "记得清楚"
    if importance >= 0.45:
        return "有点模糊"
    if importance >= 0.3:
        return "快忘了"
    return "几乎想不起"


def _rows(conn: sqlite3.Connection, now: float):
    try:
        rows = conn.execute(
            "SELECT id, fact, importance, emotion, attention_hits, last_used_at, "
            "created_at FROM durable_facts WHERE IFNULL(retracted_at,'')=''"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for row in rows:
        created = _ts(row["created_at"]) or now
        out.append({
            "id": str(row["id"]),
            "text": (row["fact"] or "")[:60],
            "importance": float(row["importance"] if row["importance"] is not None else 0.5),
            "emotion": float(row["emotion"] if row["emotion"] is not None else 0.5),
            "hits": float(row["attention_hits"] or 0),
            "last_used": _ts(row["last_used_at"]),
            "age": max(0.0, (now - created) / 86400.0),
        })
    return out


def _decay_enabled(decay=None) -> bool:
    """默认关闭衰减（长线记忆）；显式 True 或 RP_RHYTHM_DECAY=1 才开启。"""
    if decay is not None:
        return bool(decay)
    return (os.environ.get("RP_RHYTHM_DECAY") or "0").strip().lower() in {
        "1", "true", "yes", "on"}


def plan(conn: sqlite3.Connection, now: float | None = None, decay=None):
    """算出每条词条今天该变成多少；不写库。decay=None 时读 RP_RHYTHM_DECAY。"""
    ensure(conn)
    decay = _decay_enabled(decay)
    now = time.time() if now is None else float(now)
    prev = {r["ref_id"]: r for r in conn.execute("SELECT * FROM rhythm_state")}
    rows, changes = [], []
    for item in _rows(conn, now):
        item["phase"] = phase_of(item["importance"], item["age"])
        if item["importance"] >= PROTECTED or item["age"] < FRESH_HOURS / 24.0:
            rows.append(item)
            continue
        new = item["importance"]
        before = prev.get(item["id"])
        if before is not None and item["hits"] > float(before["hits"] or 0):
            gained = min(REINFORCE * (item["hits"] - float(before["hits"] or 0)), 0.06)
            new = min(REINFORCE_CAP, new + gained)
            item["reinforced"] = round(new - item["importance"], 4)
        detail = explain(item["importance"], item["age"], item["hits"],
                         item["last_used"], item["emotion"])
        item.update(detail)
        if decay:
            new = max(FLOOR, new * (1.0 - detail["rate"]))
            new = max(item["importance"] - MAX_DAILY_DROP, new)
        else:
            # 长线模式：只保留加固，不因时间下降；审计里把 rate/drop 记 0
            item["rate"] = 0.0
            item["drop"] = 0.0
        new = round(new, 4)
        item["new"] = new
        item["phase"] = phase_of(new, item["age"])
        rows.append(item)
        if abs(new - item["importance"]) > 0.0005:
            changes.append(item)
    return rows, changes


def backup(conn: sqlite3.Connection, path: str) -> str | None:
    if path in ("", ":memory:") or not os.path.exists(path):
        return None
    dest = f"{path}.rhythm-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    dst = sqlite3.connect(dest)
    with dst:
        conn.backup(dst)
    ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
    dst.close()
    if ok != "ok":
        raise RuntimeError(f"衰减前备份校验失败: {ok}")
    return dest


def apply(conn: sqlite3.Connection, changes, db_path: str = "", now: float | None = None) -> int:
    if not changes:
        return 0
    if db_path:
        print("已备份:", backup(conn, db_path) or "（内存库，跳过）")
    now = time.time() if now is None else float(now)
    cur = conn.cursor()
    for item in changes:
        cur.execute("UPDATE durable_facts SET importance=? WHERE id=?",
                    (item["new"], int(item["id"])))
        cur.execute(
            "INSERT INTO rhythm_state(ref_id, hits, last_used, applied_at) "
            "VALUES(?,?,?,datetime('now')) ON CONFLICT(ref_id) DO UPDATE SET "
            "hits=excluded.hits, last_used=excluded.last_used, "
            "applied_at=excluded.applied_at",
            (item["id"], item["hits"],
             datetime.fromtimestamp(item["last_used"], tz=timezone.utc)
             .strftime("%Y-%m-%d %H:%M:%S") if item["last_used"] else ""))
        cur.execute(
            "INSERT INTO rhythm_audit(ref_id, old, new, rate, reinforced, phase) "
            "VALUES(?,?,?,?,?,?)",
            (item["id"], item["importance"], item["new"], round(item["rate"], 6),
             item.get("reinforced", 0.0), item["phase"]))
    conn.commit()
    return len(changes)


def curve() -> None:
    samples = [
        ("普通小事 0.45，从没想起过", 0.45, 0, 0.0),
        ("普通小事 0.45，想起过 5 次", 0.45, 5, time.time()),
        ("有点分量 0.60，想起过 2 次", 0.60, 2, time.time() - 5 * 86400),
        ("重要 0.69，常被想起", 0.69, 10, time.time()),
    ]
    days_list = [1, 3, 7, 30, 90, 365]
    print("== 拟人化曲线 vs 旧的「每天固定 1%」\n")
    for label, imp, hits, last in samples:
        old = new = imp
        rec_old, rec_new = {}, {}
        for day in range(1, 366):
            rate = explain(new, day + 0.5, hits, last, 0.5)["rate"]
            new = max(FLOOR, new * (1.0 - rate))
            old = old * 0.99
            if day in days_list:
                rec_old[day] = old
                rec_new[day] = new
        print("  " + label)
        print("      " + "  ".join(f"{d:>5}天" for d in days_list))
        print("    旧 " + "  ".join(f"{rec_old[d]:.3f}" for d in days_list))
        print("    新 " + "  ".join(f"{rec_new[d]:.3f}" for d in days_list))
        print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("RP_DB") or "data/rp_memory.db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--decay", action="store_true",
                    help="启用时间衰减（默认长线记忆：只加固、不降低）")
    ap.add_argument("--explain", type=int, metavar="ID")
    ap.add_argument("--curve", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.curve:
        curve()
        return 0
    conn = connect(args.db)
    try:
        rows, changes = plan(conn, decay=True if args.decay else None)
        if args.explain is not None:
            hit = next((r for r in rows if r["id"] == str(args.explain)), None)
            if not hit:
                print("没有这条记忆")
                return 1
            detail = explain(hit["importance"], hit["age"], hit["hits"],
                             hit["last_used"], hit["emotion"])
            print({**hit, **detail})
            return 0
        if args.json:
            print(__import__("json").dumps(changes, ensure_ascii=False, indent=2))
        else:
            print(f"共 {len(rows)} 条词条，今天会变 {len(changes)} 条")
            if not args.decay:
                print("（长线模式：只加固、不淡忘；要启用旧衰减加 --decay）")
            for item in sorted(changes, key=lambda x: -x["drop"])[:20]:
                print(f"  #{item['id']:<5} {item['importance']:.3f} → {item['new']:.3f}"
                      f"  掉 {item['drop']:.4f}  {item['phase']}  {item['text']}")
        if args.apply:
            print(f"已写入 {apply(conn, changes, args.db)} 条")
        else:
            print("（只演算，没写入。要生效加 --apply）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
