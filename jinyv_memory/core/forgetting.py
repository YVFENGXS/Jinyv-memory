# -*- coding: utf-8 -*-
"""记忆权重的拟人化衰减（可选）。

记忆默认走「长线」模式：权重不随时间下降，只在被重新想起时上调。
要启用随时间淡忘，加 `--decay` 或设 `RP_FORGET_DECAY=1`。

衰减规格（与具体实现无关的行为约定）：

    每日衰减率 = 基准 × 新鲜度(age) × 重要度(importance)
                       × 复述(hits, last_used) × 情绪(mood)
    衰减率上限 = 0.08
    日下降量   = min(importance × 衰减率, 0.03)
    新权重     = max(0.05, importance × (1 - 衰减率))
                 且不低于 importance - 0.03

豁免与加固：

    - 权重 >= 0.70：视为固化，不衰减
    - 形成不足 6 小时：不衰减
    - 被重新想起：每多命中一次 +0.02，单次最多 +0.06，上限 0.68
    - 三天内命中过：复述因子再乘 0.75
    - 情绪处于两端（>= 0.65 或 <= 0.35）：衰减率乘 0.85

只处理 durable_facts；默认只演算，`--apply` 才写库，写前做整库快照并校验。
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone

# 规格参数。名字描述「这个量在曲线上扮演什么角色」。
DECAY_BASE = 0.008          # 基准衰减率
RATE_CAP = 0.08             # 单日衰减率上限
DAILY_CAP = 0.03            # 单日权重下降上限
WEIGHT_FLOOR = 0.05         # 权重下限
GUARD_SCORE = 0.70          # 固化线：达到即完全豁免
GRACE_HOURS = 6.0           # 新生豁免期
BOOST_STEP = 0.02           # 每次命中的加固步长
BOOST_CEIL = 0.68           # 加固后的权重上限
BOOST_GAIN_CAP = 0.06       # 单次加固增量上限
REHEARSAL_DAYS = 3.0        # 复述折扣的观察窗口

# 表名保留 rhythm_* 前缀，只为兼容已存在的线上库；新库同样沿用，避免迁移。
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


@dataclass(frozen=True)
class Curve:
    """把衰减率拆成四个因子；每个因子只回答「这一项让衰减变快还是变慢」。"""

    base: float = DECAY_BASE
    rate_cap: float = RATE_CAP
    daily_cap: float = DAILY_CAP
    floor: float = WEIGHT_FLOOR
    guard: float = GUARD_SCORE
    grace: float = GRACE_HOURS / 24.0
    window: float = REHEARSAL_DAYS * 86400.0
    # 阶段阈值按分数从高到低排列，命中第一条即返回。
    stages: tuple = ((0.55, "记得挺清"), (0.45, "细节记不清"), (0.30, "快记不住了"))

    def recency(self, age_days: float) -> float:
        """越新越快；以 2 天为尺度回落，最终封顶 2.5。"""
        if age_days < self.grace:
            return 0.0
        return min(2.5, 2.5 - 1.5 * (1.0 - math.exp(-0.5 * age_days)))

    def importance(self, score: float) -> float:
        """越接近固化线越慢；线性落在 [0.55, 1.45]。"""
        closeness = min(1.0, max(0.0, score / self.guard))
        return 1.45 - 0.9 * closeness

    def rehearsal(self, hits: float, last_used: float) -> float:
        """命中越多越牢；最近三天内命中过，再打个七五折。"""
        factor = 1.0 / (1.0 + 0.5 * math.log1p(max(0.0, hits)))
        if last_used and (time.time() - last_used) < self.window:
            factor *= 0.75
        return factor

    def mood(self, value: float) -> float:
        """情绪处于两端时，记忆更黏，衰减再慢 15%。"""
        return 0.85 if not (0.35 < value < 0.65) else 1.0

    def evaluate(self, score: float, age_days: float, hits: float,
                 last_used: float, value: float) -> dict:
        """合成一次衰减所需的倍率与建议下降量。"""
        factors = {
            "recency": self.recency(age_days),
            "weight": self.importance(score),
            "rehearsal": self.rehearsal(hits, last_used),
            "mood": self.mood(value),
        }
        multiplier = math.prod(factors.values())
        rate = min(self.rate_cap, self.base * multiplier)
        factors["rate"] = rate
        factors["drop"] = min(score * rate, self.daily_cap)
        return factors

    def stage(self, score: float, age_days: float) -> str:
        """把分数与年龄翻译成一句可读的状态。"""
        if score >= self.guard:
            return "很难忘掉"
        if age_days < 1.0:
            return "刚记下"
        for threshold, label in self.stages:
            if score >= threshold:
                return label
        return "基本想不起"


CURVE = Curve()


def open_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def prepare_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def _epoch(value) -> float:
    """SQLite 的 datetime('now') 存的是 UTC；无时区字符串按 UTC 解释。"""
    if not value:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(value)[:19])
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _facts(conn: sqlite3.Connection, now: float):
    """读出尚未撤回的词条，并补上归一化后的年龄。"""
    try:
        rows = conn.execute(
            "SELECT id, fact, importance, emotion, attention_hits, last_used_at, "
            "created_at FROM durable_facts WHERE IFNULL(retracted_at,'')=''"
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    records = []
    for row in rows:
        born = _epoch(row["created_at"]) or now
        records.append({
            "id": str(row["id"]),
            "text": (row["fact"] or "")[:60],
            "importance": float(row["importance"] if row["importance"] is not None else 0.5),
            "emotion": float(row["emotion"] if row["emotion"] is not None else 0.5),
            "hits": float(row["attention_hits"] or 0),
            "last_used": _epoch(row["last_used_at"]),
            "age": max(0.0, (now - born) / 86400.0),
        })
    return records


def _decay_on(decay=None) -> bool:
    """默认关闭衰减；显式 True 或 RP_FORGET_DECAY=1 才开启。"""
    if decay is not None:
        return bool(decay)
    return (os.environ.get("RP_FORGET_DECAY") or "0").strip().lower() in {
        "1", "true", "yes", "on"}


def _exempt(item: dict) -> bool:
    """固化或仍在新生期：本轮不参与衰减。"""
    return item["importance"] >= CURVE.guard or item["age"] < CURVE.grace


def _boosted(item: dict, applied: dict) -> float:
    """按「自上次写库以来新增的命中数」加固权重。"""
    target = item["importance"]
    seen = applied.get(item["id"])
    if seen is None:
        return target
    previous = float(seen["hits"] or 0)
    if item["hits"] <= previous:
        return target
    gain = min(BOOST_STEP * (item["hits"] - previous), BOOST_GAIN_CAP)
    target = min(BOOST_CEIL, target + gain)
    item["reinforced"] = round(target - item["importance"], 4)
    return target


def _decayed(item: dict, target: float) -> float:
    """对目标权重施加时间衰减，并守住单日上限与绝对下限。"""
    dropped = max(CURVE.floor, target * (1.0 - item["rate"]))
    return max(item["importance"] - CURVE.daily_cap, dropped)


def _moved(before: float, after: float) -> bool:
    return abs(after - before) > 0.0005


def preview(conn: sqlite3.Connection, now: float | None = None, decay=None):
    """演算每条词条今天的变化，不写库。返回 (全部行, 有变化的行)。"""
    prepare_schema(conn)
    enabled = _decay_on(decay)
    stamp = time.time() if now is None else float(now)
    applied = {r["ref_id"]: r for r in conn.execute("SELECT * FROM rhythm_state")}

    rows, changed = [], []
    for item in _facts(conn, stamp):
        item["phase"] = CURVE.stage(item["importance"], item["age"])
        if _exempt(item):
            rows.append(item)
            continue

        target = _boosted(item, applied)
        item.update(CURVE.evaluate(
            item["importance"], item["age"], item["hits"],
            item["last_used"], item["emotion"]))

        if enabled:
            target = _decayed(item, target)
        else:
            # 长线模式：只保留加固，不因时间下降；审计里把率与量记 0。
            item["rate"] = 0.0
            item["drop"] = 0.0

        target = round(target, 4)
        item["new"] = target
        item["phase"] = CURVE.stage(target, item["age"])
        rows.append(item)
        if _moved(item["importance"], target):
            changed.append(item)

    return rows, changed


def snapshot(conn: sqlite3.Connection, path: str) -> str | None:
    """落库前整库快照，并校验快照可用。"""
    if path in ("", ":memory:") or not os.path.exists(path):
        return None
    dest = f"{path}.forgetting-{time.strftime('%Y%m%d-%H%M%S')}.bak"
    copy = sqlite3.connect(dest)
    with copy:
        conn.backup(copy)
    verdict = copy.execute("PRAGMA integrity_check").fetchone()[0]
    copy.close()
    if verdict != "ok":
        raise RuntimeError(f"衰减前快照校验失败: {verdict}")
    return dest


def commit(conn: sqlite3.Connection, changes, db_path: str = "",
           now: float | None = None) -> int:
    """把演算结果写回库，并记录审计。"""
    if not changes:
        return 0
    if db_path:
        print("已快照:", snapshot(conn, db_path) or "（内存库，跳过）")
    now = time.time() if now is None else float(now)

    state = []
    audit = []
    for item in changes:
        used = datetime.fromtimestamp(item["last_used"], tz=timezone.utc) \
            .strftime("%Y-%m-%d %H:%M:%S") if item["last_used"] else ""
        state.append((item["id"], item["hits"], used))
        audit.append((item["id"], item["importance"], item["new"],
                      round(item["rate"], 6), item.get("reinforced", 0.0),
                      item["phase"]))

    cur = conn.cursor()
    cur.executemany("UPDATE durable_facts SET importance=? WHERE id=?",
                    [(item["new"], int(item["id"])) for item in changes])
    cur.executemany(
        "INSERT INTO rhythm_state(ref_id, hits, last_used, applied_at) "
        "VALUES(?,?,?,datetime('now')) ON CONFLICT(ref_id) DO UPDATE SET "
        "hits=excluded.hits, last_used=excluded.last_used, "
        "applied_at=excluded.applied_at", state)
    cur.executemany(
        "INSERT INTO rhythm_audit(ref_id, old, new, rate, reinforced, phase) "
        "VALUES(?,?,?,?,?,?)", audit)
    conn.commit()
    return len(changes)


def show_curve() -> None:
    samples = [
        ("普通小事 0.45，从没想起过", 0.45, 0, 0.0),
        ("普通小事 0.45，想起过 5 次", 0.45, 5, time.time()),
        ("有点分量 0.60，想起过 2 次", 0.60, 2, time.time() - 5 * 86400),
        ("重要 0.69，常被想起", 0.69, 10, time.time()),
    ]
    days_list = [1, 3, 7, 30, 90, 365]
    print("== 拟人化曲线 vs 旧的「每天固定 1%」\n")
    for label, score, hits, last in samples:
        old = new = score
        rec_old, rec_new = {}, {}
        for day in range(1, 366):
            rate = CURVE.evaluate(new, day + 0.5, hits, last, 0.5)["rate"]
            new = max(CURVE.floor, new * (1.0 - rate))
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
        show_curve()
        return 0
    conn = open_db(args.db)
    try:
        rows, changes = preview(conn, decay=True if args.decay else None)
        if args.explain is not None:
            hit = next((r for r in rows if r["id"] == str(args.explain)), None)
            if not hit:
                print("没有这条记忆")
                return 1
            detail = CURVE.evaluate(hit["importance"], hit["age"], hit["hits"],
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
            print(f"已写入 {commit(conn, changes, args.db)} 条")
        else:
            print("（只演算，没写入。要生效加 --apply）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
