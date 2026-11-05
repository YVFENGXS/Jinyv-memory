# -*- coding: utf-8 -*-
"""梯度记忆（C8-02 / C8-03 / C8-04）：L1 原子 → L2 事件卡 → L3 时间桶 → L4 索引。

- 年龄主度量是 token 距离，不是墙钟；阈值按 USABLE 比例缩放。
- 只降不升；改写次数 ≤3；到地板即冻结，冻结后内容不再改写。
- L2 不新开表：`content_json` 只存 `card_id` 指针，正文仍在 `event_cards`。
- L4 永不进 prompt，只能靠 `query_memory` 回捞。
- open episode 的原文不因水位移出；超过上限先 checkpoint split。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import control

MAX_REWRITES = max(1, int(float(os.environ.get("RP_GRADIENT_MAX_REWRITES") or 3)))
FREEZE_ON_FLOOR = (os.environ.get("RP_GRADIENT_FREEZE_ON_FLOOR") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}
JUMP_IF_UNREAD = (os.environ.get("RP_GRADIENT_JUMP_IF_UNREAD") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}
L1_TO_L2 = max(0.0, float(os.environ.get("RP_GRADIENT_L1_TO_L2_RATIO") or 0.15))
L2_TO_L3 = max(0.0, float(os.environ.get("RP_GRADIENT_L2_TO_L3_RATIO") or 0.45))
L3_TO_L4 = max(0.0, float(os.environ.get("RP_GRADIENT_L3_TO_L4_RATIO") or 1.20))
HOT_MIN_TOKENS = max(1, int(float(os.environ.get("RP_HOT_MIN_TOKENS") or 12000)))
OPEN_EPISODE_MAX_TOKENS = max(1, int(float(os.environ.get("RP_OPEN_EPISODE_MAX_TOKENS")
                                           or 20000)))
BUCKET_NEAR_HOURS = max(1, int(float(os.environ.get("RP_BUCKET_NEAR_HOURS") or 4)))
BUCKET_MID_DAYS = max(1, int(float(os.environ.get("RP_BUCKET_MID_DAYS") or 1)))
BUCKET_FAR_DAYS = max(1, int(float(os.environ.get("RP_BUCKET_FAR_DAYS") or 7)))

_CJK = re.compile(r"[\u4e00-\u9fff]")


def enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get("RP_GRADIENT_ENABLED") or "1").strip().lower() not in {
        "0", "false", "no", "off"}


def shadow(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get("RP_GRADIENT_SHADOW") or "1").strip().lower() not in {
        "0", "false", "no", "off"}


def target_level(age_tokens: float, usable: int, floor_level: int) -> int:
    """按龄算目标档位；地板是「最粗允许」，不能更粗。"""
    lvl = 1
    if age_tokens >= L3_TO_L4 * usable:
        lvl = 4
    elif age_tokens >= L2_TO_L3 * usable:
        lvl = 3
    elif age_tokens >= L1_TO_L2 * usable:
        lvl = 2
    return min(lvl, max(1, int(floor_level)))


def _connect(db) -> sqlite3.Connection:
    conn = sqlite3.connect(getattr(db, "path", db), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _ctx(db):
    conn = _connect(db)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _parse(text):
    try:
        return datetime.fromisoformat(str(text or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _tokens(text) -> int:
    try:
        from rp_skeleton import est_tokens
        return est_tokens(str(text or ""), "cl100k")
    except Exception:                      # noqa: BLE001 离线估算兜底
        s = str(text or "")
        return len(s) // 2 + 1


def age_tokens(db, cid, source_turns, sid=None) -> int:
    """token 距离：从该单元之后到最新一轮的 token 总和。"""
    ends = [int(b) for _, b in (source_turns or []) if str(b).isdigit()] or [0]
    until = max(ends)
    sql = ("SELECT content FROM turns WHERE character_id=? AND turn_idx>?")
    args = [cid, until]
    if sid:
        sql += " AND session_id=?"
        args.append(sid)
    with _ctx(db) as c:
        rows = c.execute(sql, args).fetchall()
    return sum(_tokens(r["content"]) for r in rows)


def bucket_key(stamp, now=None) -> str:
    """≤3 天 4h 桶；3–30 天 1 天桶；>30 天 1 周桶。"""
    dt = _parse(stamp) or (now or datetime.now(timezone.utc))
    ref = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    delta = ref - dt
    if delta <= timedelta(days=3):
        hour = (dt.hour // BUCKET_NEAR_HOURS) * BUCKET_NEAR_HOURS
        return dt.strftime("%Y-%m-%d") + f"T{hour:02d}"
    if delta <= timedelta(days=30):
        return dt.strftime("%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    return "W" + monday.strftime("%Y-%m-%d")


def _topic(content) -> str:
    if isinstance(content, dict):
        for key in ("topic", "who", "what"):
            if content.get(key):
                return str(content[key])[:24]
        return str(content.get("conclusion") or "")[:24]
    return str(content or "")[:24]


def _entities(content, limit=4) -> list:
    if not isinstance(content, dict):
        return []
    out = []
    for key in ("entities", "entity", "who", "people"):
        val = content.get(key)
        if isinstance(val, list):
            out += [str(x) for x in val]
        elif val:
            out.append(str(val))
    return [x[:16] for x in dict.fromkeys(out)][:limit]


def _l2_content(unit) -> dict:
    return {"kind": "card_ref", "card_id": unit.get("episode_id") or "",
            "atom_id": unit["id"]}
    # 只存指针：正文仍在 event_cards（单源规则）


def _l3_content(unit) -> dict:
    old = control._json(unit.get("content_json"), {})
    return {"kind": "bucket", "topic": _topic(old),
            "conclusion": (old.get("what") or old.get("conclusion") or
                           old.get("result") or "")[:80],
            "entities": _entities(old),
            "pointers": [unit.get("episode_id") or ""]}


def _l4_content(unit) -> dict:
    old = control._json(unit.get("content_json"), {})
    text = json.dumps(old, ensure_ascii=False)
    words = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fff]{2,6}", text)))[:8]
    return {"kind": "index", "keywords": words,
            "entities": _entities(old),
            "pointers": [unit.get("episode_id") or ""]}


CONTENT_BUILDERS = {2: _l2_content, 3: _l3_content, 4: _l4_content}


def _hash(content) -> str:
    return hashlib.sha256(
        json.dumps(content, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def freeze(db, unit_id) -> bool:
    unit = control.get_unit(db, unit_id)
    if not unit:
        return False
    content = control._json(unit.get("content_json"), {})
    control.update_unit(db, unit_id, frozen=1, frozen_hash=_hash(content))
    return True


def demote(db, unit_id, target: int, reason="age", reviewer="auto") -> dict:
    """把单元降一档；返回 {changed, blocked, from, to, frozen}。"""
    unit = control.get_unit(db, unit_id)
    if not unit:
        return {"changed": False, "blocked": "missing"}
    out = {"changed": False, "blocked": "", "from": unit["level"],
           "to": unit["level"], "frozen": bool(unit.get("frozen"))}
    if unit.get("frozen"):
        out["blocked"] = "frozen"
        return out
    floor = int(unit.get("floor_level") or 1)
    target = min(4, max(1, int(target)))
    target = min(target, floor)                # 地板是「最粗允许」，不能更粗
    if target <= int(unit["level"]):
        out["blocked"] = "no_downgrade"
        return out
    if int(unit.get("rewrite_count") or 0) >= MAX_REWRITES:
        freeze(db, unit_id)
        out["blocked"] = "max_rewrites"
        out["frozen"] = True
        return out
    content = CONTENT_BUILDERS.get(target, _l3_content)(unit)
    skipped = [lvl for lvl in range(int(unit["level"]) + 1, target)]
    control.update_unit(db, unit_id, level=target, content_json=content,
                        rewrite_count=int(unit.get("rewrite_count") or 0) + 1,
                        skipped_levels=skipped,
                        bucket_key=bucket_key(unit.get("created_at")) if target >= 3 else "")
    control.add_rewrite(db, unit_id, unit["level"], target, reason, reviewer)
    out.update(changed=True, to=target)
    # 到地板即冻结
    if FREEZE_ON_FLOOR and target >= floor:
        freeze(db, unit_id)
        out["frozen"] = True
    return out


def episode_closed(db, episode_id) -> bool:
    """只压 CLOSED episode；查不到/OPEN 一律不压（宁可不压，不可压歪）。"""
    if not episode_id:
        return False
    with _ctx(db) as c:
        row = c.execute("SELECT meta_json FROM event_cards WHERE event_id=?",
                        (episode_id,)).fetchone()
    meta = control._json(row["meta_json"] if row else "{}", {})
    return str(meta.get("closure_state") or "OPEN") == "CLOSED"


def ladder(db, cid, usable, now=None) -> list[dict]:
    """扫描所有未冻结、且 episode 已闭环的单元，按龄降级；shadow 只报告不落库。"""
    reports = []
    for unit in control.list_units(db, cid, limit=5000):
        if unit.get("frozen"):
            continue
        if not episode_closed(db, unit.get("episode_id")):
            continue
        age = age_tokens(db, cid, unit.get("source_turns"), unit.get("session_id"))
        target = target_level(age, usable, unit.get("floor_level") or 1)
        jump = JUMP_IF_UNREAD and int(unit.get("retrieval_count") or 0) == 0 \
            and int(unit.get("salience") or 0) < 45 and target < 4
        if jump:
            target = 4
        if target <= int(unit["level"]):
            continue
        reports.append({"unit_id": unit["id"], "from": unit["level"],
                        "to": target, "age_tokens": age})
        if not shadow():
            demote(db, unit["id"], target,
                   reason="jump_unread" if jump else "age")
    return reports


def open_episode_start(db, sid, cid, default=1) -> int:
    sql = ("SELECT turn_start FROM event_cards WHERE session_id=? AND character_id=?"
           " ORDER BY turn_end DESC LIMIT 1")
    with _ctx(db) as c:
        row = c.execute(sql, (sid, cid)).fetchone()
    if not row:
        return int(default)
    return int(row["turn_start"])


def hot_lo(db, sid, cid, cursor, min_tokens=HOT_MIN_TOKENS) -> int:
    """热窗起点 = min(按 token 预算回推的轮次, open episode 起点)。"""
    open_start = open_episode_start(db, sid, cid, cursor + 1)
    with _ctx(db) as c:
        rows = c.execute("SELECT turn_idx, content FROM turns"
                         " WHERE session_id=? AND character_id=? AND turn_idx<=?"
                         " ORDER BY turn_idx DESC", (sid, cid, cursor)).fetchall()
    total, lo = 0, cursor + 1
    for r in rows:
        total += _tokens(r["content"])
        lo = int(r["turn_idx"])
        if total >= min_tokens:
            break
    return max(1, min(lo, open_start))


def checkpoint_split(db, sid, cid, start, end, max_tokens=OPEN_EPISODE_MAX_TOKENS):
    """open episode 超限时在自然断点切分；返回切割轮次（后半段起点）。

    前半段由调用方落 L1 原子；这里只负责选断点，不删原文、不压成整卡。
    """
    with _ctx(db) as c:
        rows = c.execute("SELECT turn_idx, role, content FROM turns"
                         " WHERE session_id=? AND character_id=? AND turn_idx>=?"
                         " AND turn_idx<=? ORDER BY turn_idx, id",
                         (sid, cid, int(start), int(end))).fetchall()
    total, cut = 0, int(start)
    for r in rows:
        total += _tokens(r["content"])
        if total >= max_tokens // 2 and r["role"] == "user":
            cut = int(r["turn_idx"])
            break
    return max(int(start) + 1, cut)


def select_for_prompt(db, cid, open_episode=None, now=None, limit=30) -> dict:
    """注入选层：open 全进；closed 评分 ≥85；L3 最近 7 日桶 + 4 周桶。"""
    units = control.list_units(db, cid, limit=2000)
    opened, strong, buckets = [], [], []
    cutoff_day = (now or datetime.now(timezone.utc)) - timedelta(days=7)
    cutoff_week = (now or datetime.now(timezone.utc)) - timedelta(days=28)
    day_keys, week_keys = [], []
    for u in units:
        level = int(u.get("level") or 1)
        if open_episode and u.get("episode_id") == open_episode:
            opened.append(u)
            continue
        if level == 1 and int(u.get("salience") or 0) >= 85:
            strong.append(u)
        if level == 3:
            key = str(u.get("bucket_key") or "")
            dt = _parse(key[1:] if key.startswith("W") else key)
            if key.startswith("W") and dt and dt >= cutoff_week and key not in week_keys:
                week_keys.append(key)
            elif dt and dt >= cutoff_day and key not in day_keys:
                day_keys.append(key)
    for u in units:
        if u.get("bucket_key") in day_keys + week_keys and u.get("bucket_key"):
            buckets.append(u)
    return {"open": opened[:limit], "strong": strong[:limit],
            "buckets": buckets[:7 + 4]}


def no_hole_audit(db, cid) -> list[dict]:
    """L0 到期但找不到对应 L1 原子的保留行（必须长期为 0）。"""
    l1 = control.list_units(db, cid, level=1, limit=5000)
    atoms = []
    for u in l1:
        for a, b in (u.get("source_turns") or []):
            atoms.append((int(a), int(b)))
    holes = []
    for r in control.list_retention(db, cid):
        idx = int(r["turn_idx"])
        if not any(a <= idx <= b for a, b in atoms):
            holes.append(r)
    return holes


def l1_sla_owed(db, cid, hours=None) -> list[dict]:
    """超过 L1_SLA 仍没有 L1 原子的原文：应 pin 并记 owed。"""
    hours = float(hours if hours is not None else
                  os.environ.get("RP_L1_SLA_HOURS") or 24)
    holes = no_hole_audit(db, cid)
    out = []
    for r in holes:
        dt = _parse(r.get("retention_until"))
        if dt and (dt - timedelta(days=7) + timedelta(hours=hours)) <= \
                datetime.now(timezone.utc):
            out.append(r)
    return out


def simulate(rounds=200, usable=54000, start_age=0, floor_level=1) -> dict:
    """离线仿真：给定 token 年龄序列，输出最终档位分布与冻结率。"""
    levels = {1: 0, 2: 0, 3: 0, 4: 0}
    frozen = 0
    for i in range(int(rounds)):
        age = start_age + i * usable / max(1, rounds // 4)
        lvl = target_level(age, usable, floor_level)
        levels[lvl] += 1
        if FREEZE_ON_FLOOR and lvl >= floor_level and floor_level < 4:
            frozen += 1
    return {"levels": levels, "frozen_ratio": frozen / max(1, int(rounds))}
