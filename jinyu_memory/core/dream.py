# -*- coding: utf-8 -*-
"""Dream 轻量整理（P1）：每天 03:30 收走过期临时事实、把同义重复词条并掉。

P2 追加章节卡：把 30 天前的明细卡按车道/受众/知识边界合成一张 chapter 卡，
明细卡保留不删，章节卡只做「更粗的一层索引」，方便跨月回忆时先看章节。
"""
from __future__ import annotations

import hashlib, logging, os, uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("rp_memory")

DEFAULT_AT = (3, 30)          # 本地时间 03:30
CHAPTER_DAYS = max(1, int(os.environ.get("RP_CHAPTER_DAYS") or 30))
CHAPTER_MIN = max(2, int(os.environ.get("RP_CHAPTER_MIN") or 3))
CHAPTER_MAX = max(1, int(os.environ.get("RP_CHAPTER_MAX") or 4))


def next_run(now=None, at=DEFAULT_AT):
    """下一次运行时间点（本地时区）；已经过了今天的目标时刻就顺延到明天。"""
    now = now or datetime.now()
    target = now.replace(hour=int(at[0]), minute=int(at[1]), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def _mmdd(ts):
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S").strftime("%m-%d")
    except (TypeError, ValueError):
        return ""


def _union(cards, key, limit):
    out = []
    for card in cards:
        for raw in card.get(key) or []:
            value = str(raw).strip()
            if value and value not in out:
                out.append(value)
    return out[:limit]


def _chapter_summary(cards, limit=240):
    """章节正文只做「日期 + 各明细首句」的确定性拼接，不调模型、不新增信息。"""
    parts = []
    for card in cards:
        text = " ".join(str(card.get("summary") or "").split())
        if not text:
            continue
        when = _mmdd(card.get("created_at"))
        parts.append(f"「{when}」{text[:60]}" if when else text[:60])
    text = "；".join(parts)
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def chapterize(db, cid, days=None, min_cards=None, max_chapters=None,
               dry_run=False, now=None):
    """把足够老的明细卡合成章节卡（T2）。

    安全边界：只合并同一 (session_id, audience, bot_id, knowledge_boundary) 的卡，
    不同受众/边界绝不混章；明细卡保留不删（可继续展开原文），章节只做粗索引。
    幂等：covers 记录已覆盖的明细卡，重复跑不会重复合并。
    """
    days = CHAPTER_DAYS if days is None else max(1, int(days))
    min_cards = CHAPTER_MIN if min_cards is None else max(2, int(min_cards))
    max_chapters = CHAPTER_MAX if max_chapters is None else max(1, int(max_chapters))
    stamp = now or datetime.now(timezone.utc)
    cutoff = (stamp - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cards = [c for c in db.chapter_candidates(cid, cutoff)
             if c["event_id"] not in db.chapter_covers(cid)]
    groups = {}
    for card in cards:
        key = (card.get("session_id") or "", card.get("audience") or "",
               card.get("bot_id") or "", card.get("knowledge_boundary") or "both")
        groups.setdefault(key, []).append(card)
    stats = {"chapters": 0, "cards": 0}
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for (sid, audience, bot_id, boundary), group in ordered:
        if stats["chapters"] >= max_chapters:
            break
        if len(group) < min_cards:
            continue
        group = sorted(group, key=lambda c: (c.get("turn_start") or 0, c.get("turn_end") or 0))
        ids = [c["event_id"] for c in group]
        summary = _chapter_summary(group)
        if not summary:
            continue
        subjects = [str(c.get("subject") or "").strip() for c in group]
        subjects = [s for s in subjects if s]
        subject = max(set(subjects), key=subjects.count) if subjects else ""
        card = {
            "event_id": f"E-{uuid.uuid4().hex[:8]}",
            "session_id": sid, "character_id": cid,
            "turn_start": min(int(c.get("turn_start") or 0) for c in group),
            "turn_end": max(int(c.get("turn_end") or 0) for c in group),
            "summary": summary, "state_changes": {}, "quotes": [],
            "entity_hooks": _union(group, "entity_hooks", 8),
            "rel_hooks": _union(group, "rel_hooks", 4),
            "knowledge_boundary": boundary, "subject": subject,
            "importance": max(float(c.get("importance") or 0.5) for c in group),
            "precision": "normal", "conflict_flags": [],
            "audience": audience, "bot_id": bot_id,
            "layer": "chapter", "covers": ids,
            "idempotency_key": "chapter:" + hashlib.sha1(
                "|".join(sorted(ids)).encode()).hexdigest()[:24],
        }
        if dry_run:
            stats["chapters"] += 1
            stats["cards"] += len(group)
            continue
        if db.insert_card(card):
            stats["chapters"] += 1
            stats["cards"] += len(group)
            logger.info("Dream 章节卡 sid=%s 覆盖=%d 区间=%d-%d",
                        sid, len(group), card["turn_start"], card["turn_end"])
    return stats


def run(mw, cid, dry_run=False, day=None, min_facts=20, max_sessions=8, now=None):
    """执行一次整理，返回统计字典；dry_run 只统计、不写库也不调后台模型。

    min_facts：同一会话词条达到多少条才值得花后台模型合并（与自动合并同口径）。
    max_sessions：单次最多整理多少个会话，防止一次跑太久。
    """
    stamp = now or datetime.now()
    day = day or stamp.strftime("%Y-%m-%d")
    stats = {"day": day, "dry_run": bool(dry_run), "expired": [], "sessions": 0,
             "groups": 0, "chapters": 0, "chapter_cards": 0, "skipped": ""}
    if not dry_run and not mw.db.dream_claim(cid, day):
        stats["skipped"] = "already_done"      # 今天已经整理过：不重复跑
        return stats
    try:
        stats["expired"] = mw.db.expire_temp_facts(cid, dry_run=dry_run)
        owners = sorted(mw.db.fact_owners(cid), key=lambda o: int(o["n"] or 0), reverse=True)
        for owner in owners:
            if stats["sessions"] >= max(0, int(max_sessions)):
                break
            if int(owner["n"] or 0) < max(2, int(min_facts)):
                continue                        # 条数太少：合并没有收益，省一次模型调用
            stats["sessions"] += 1
            if dry_run:
                continue                        # 预览只看候选，不为预览花模型调用
            sid = mw.db.latest_sid(owner["base"], cid)
            groups = mw.merge_facts(owner["base"], cid, True)
            stats["groups"] += len(groups or [])
            if groups:
                logger.info("Dream 词条合并 base=%s 组=%d：%s", owner["base"], len(groups),
                            "；".join(f"{len(g['ids'])}→{str(g['fact'])[:16]}"
                                     for g in groups[:6]))
    except Exception:
        if not dry_run:
            mw.db.dream_release(cid, day)       # 失败放开占坑，当天还能再试
        raise
    chapter = chapterize(mw.db, cid, dry_run=dry_run, now=now)
    stats["chapters"] = int(chapter.get("chapters") or 0)
    stats["chapter_cards"] = int(chapter.get("cards") or 0)
    if not dry_run:
        mw.db.dream_note(cid, day,
                         f"expired={len(stats['expired'])} sessions={stats['sessions']}"
                         f" groups={stats['groups']} chapters={stats['chapters']}")
    logger.info("Dream 完成 day=%s dry_run=%s 过期=%d 会话=%d 合并组=%d 章节=%d",
                day, stats["dry_run"], len(stats["expired"]),
                stats["sessions"], stats["groups"], stats["chapters"])
    return stats