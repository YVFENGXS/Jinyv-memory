# -*- coding: utf-8 -*-
"""Dream 轻量整理（P1）：每天 03:30 收走过期临时事实、把同义重复词条并掉。

P2 追加章节卡：把 30 天前的明细卡按车道/受众/知识边界合成一张 chapter 卡，
明细卡保留不删，章节卡只做「更粗的一层索引」，方便跨月回忆时先看章节。
"""
from __future__ import annotations

import hashlib, logging, os, sqlite3, uuid
from datetime import datetime, timedelta, timezone

from rp_skeleton import lane_base

logger = logging.getLogger("rp_memory")

DEFAULT_AT = (3, 30)          # 本地时间 03:30
CHAPTER_DAYS = max(1, int(os.environ.get("RP_CHAPTER_DAYS") or 30))
CHAPTER_MIN = max(2, int(os.environ.get("RP_CHAPTER_MIN") or 3))
CHAPTER_MAX = max(1, int(os.environ.get("RP_CHAPTER_MAX") or 4))
# 一章最多覆盖多少张明细卡：8-12 张时摘要还能把首/中/尾都写进去；
# 一整组几十上百张时不切分，章节正文只能反映最早几张。
CHAPTER_GROUP_MAX = max(2, int(os.environ.get("RP_CHAPTER_GROUP_MAX") or 10))
# 单页取 400 张；已覆盖的卡由 SQL 直接排除，offset 只用于「未覆盖卡仍然很多」时翻页。
# 扫描上限只防一次整理把整库翻一遍；触顶会记 scan_capped 并留到下次。
CHAPTER_LIMIT = max(1, int(os.environ.get("RP_CHAPTER_LIMIT") or 400))
CHAPTER_SCAN = max(CHAPTER_LIMIT, int(os.environ.get("RP_CHAPTER_SCAN") or 8000))


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
    """章节正文只做「日期 + 各明细首句」的确定性拼接，不调模型、不新增信息。

    预算按卡数均分，并且先把日期前缀「MM-DD」（7 字）、分隔符「；」和可能的
    省略号扣掉：切章后一般 8-12 张，每张都能分到一段正文，不会因为前缀占位
    把最后几张挤掉。
    """
    rows = []
    for card in cards:
        text = " ".join(str(card.get("summary") or "").split())
        if text:
            rows.append((_mmdd(card.get("created_at")), text))
    if not rows:
        return ""
    overhead = sum(7 for when, _ in rows if when) + len(rows) - 1 + 1
    share = max(8, (limit - overhead) // len(rows))
    parts = [f"「{when}」{text[:share]}" if when else text[:share]
             for when, text in rows]
    text = "；".join(parts)
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    return text


def _utc_stamp(stamp=None):
    """统一成 UTC：外部传 naive 时间按本机时区理解，避免和 UTC 的 created_at 差 8 小时。"""
    if stamp is None:
        return datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        return stamp.astimezone(timezone.utc)
    return stamp.astimezone(timezone.utc)


def _chapter_cards_sql(db, cid, cutoff):
    """SQL 已排除覆盖卡，这里只负责翻页和扫描上限。"""
    cards, offset, capped = [], 0, False
    while True:
        page = db.chapter_candidates(cid, cutoff, limit=CHAPTER_LIMIT, offset=offset)
        if not page:
            break
        cards.extend(page)
        if len(page) < CHAPTER_LIMIT:
            break
        offset += CHAPTER_LIMIT
        if offset >= CHAPTER_SCAN:
            capped = True
            break
    return cards, capped


def _chapter_cards_py(db, cid, cutoff):
    """老 SQLite 没有 JSON1 时的回退：用 keyset 往后翻，在 Python 里跳过 covers。

    不能按 offset 翻页：如果前几千张都已覆盖，offset 会永远停在同一段覆盖区。
    keyset 用上一页最后一行的 (session_id, turn_end, event_id) 作游标，
    被过滤掉的覆盖卡不会挡住后面的新卡；上限按「收集到的未覆盖卡数」计。
    """
    covers = db.chapter_covers(cid)
    cards, after, capped = [], None, False
    while True:
        page = db.chapter_candidates(cid, cutoff, limit=CHAPTER_LIMIT, offset=0,
                                     skip_covered=False, after=after)
        if not page:
            break
        cards.extend(c for c in page if c["event_id"] not in covers)
        if len(cards) >= CHAPTER_SCAN:
            cards = cards[:CHAPTER_SCAN]
            capped = True
            break
        if len(page) < CHAPTER_LIMIT:
            break
        last = page[-1]
        after = (last.get("session_id"), int(last.get("turn_end") or 0),
                 last.get("event_id"))
    return cards, capped


def _chapter_cards(db, cid, cutoff):
    """取章节原料：优先用 SQL 排除已覆盖卡，老库回退到 Python 过滤。"""
    try:
        return _chapter_cards_sql(db, cid, cutoff)
    except sqlite3.OperationalError:
        logger.warning("章节候选不能用 SQL 排除已覆盖卡（SQLite 缺 JSON1），回退 Python 过滤")
        return _chapter_cards_py(db, cid, cutoff)


def chapterize(db, cid, days=None, min_cards=None, max_chapters=None,
               dry_run=False, now=None):
    """把足够老的明细卡合成章节卡（T2）。

    安全边界：只合并同一 (车道, audience, bot_id, knowledge_boundary) 的卡，
    不同受众/边界绝不混章；明细卡保留不删（可继续展开原文），章节只做粗索引。
    分组按车道基键（lane_base）而不是带 #epoch 的会话 id：48 小时轮换不会把
    一段旧事切成十几组。章节卡的 created_at 取被覆盖卡的最大时间，渲染日期
    跟着「事件发生的那段」走，而不是「生成本身的那天」。
    幂等：covers 记录已覆盖的明细卡，重复跑不会重复合并。
    """
    days = CHAPTER_DAYS if days is None else max(1, int(days))
    min_cards = CHAPTER_MIN if min_cards is None else max(2, int(min_cards))
    max_chapters = CHAPTER_MAX if max_chapters is None else max(1, int(max_chapters))
    stamp = _utc_stamp(now)
    cutoff = (stamp - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    cards, capped = _chapter_cards(db, cid, cutoff)
    groups = {}
    for card in cards:
        key = (lane_base(card.get("session_id")), card.get("audience") or "",
               card.get("bot_id") or "", card.get("knowledge_boundary") or "both")
        groups.setdefault(key, []).append(card)
    stats = {"chapters": 0, "cards": 0, "scan_capped": bool(capped)}
    if capped:
        logger.warning("Dream 章节候选达到扫描上限 %d 张（已覆盖卡已由 SQL 排除），"
                       "剩余留到下次整理", CHAPTER_SCAN)
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    for (lane, audience, bot_id, boundary), group in ordered:
        if stats["chapters"] >= max_chapters:
            break
        if len(group) < min_cards:
            continue
        group = sorted(group, key=lambda c: (c.get("turn_start") or 0, c.get("turn_end") or 0))
        # 大组切章：一整段 30 天的旧事不塞进一张 240 字的摘要，
        # 每章最多 CHAPTER_GROUP_MAX 张，保证摘要能覆盖到这一段的头尾。
        chunks = [group[i:i + CHAPTER_GROUP_MAX]
                  for i in range(0, len(group), CHAPTER_GROUP_MAX)]
        # 尾块不足 min_cards（比如 11 张切成 10+1）时并进上一章，
        # 否则最后那 1-2 张每轮都凑不够数，永远成不了章节。
        if len(chunks) > 1 and len(chunks[-1]) < min_cards:
            chunks[-2].extend(chunks.pop())
        for chunk in chunks:
            if stats["chapters"] >= max_chapters:
                break
            if len(chunk) < min_cards:
                continue
            ids = [c["event_id"] for c in chunk]
            summary = _chapter_summary(chunk)
            if not summary:
                continue
            subjects = [str(c.get("subject") or "").strip() for c in chunk]
            subjects = [s for s in subjects if s]
            subject = max(set(subjects), key=subjects.count) if subjects else ""
            # 章节落在组里最新的真实会话（epoch）上，轮换后仍和普通卡走同一条读路径；
            # 日期取被覆盖卡的最大时间，而不是插入时的「今天」。
            newest = max(chunk, key=lambda c: (str(c.get("created_at") or ""),
                                               int(c.get("turn_end") or 0)))
            stamps = [str(c.get("created_at") or "") for c in chunk if c.get("created_at")]
            card = {
                "event_id": f"E-{uuid.uuid4().hex[:8]}",
                "session_id": newest.get("session_id") or lane, "character_id": cid,
                "turn_start": min(int(c.get("turn_start") or 0) for c in chunk),
                "turn_end": max(int(c.get("turn_end") or 0) for c in chunk),
                "summary": summary, "state_changes": {}, "quotes": [],
                "entity_hooks": _union(chunk, "entity_hooks", 8),
                "rel_hooks": _union(chunk, "rel_hooks", 4),
                "knowledge_boundary": boundary, "subject": subject,
                "importance": max(float(c.get("importance") or 0.5) for c in chunk),
                "precision": "normal", "conflict_flags": [],
                "audience": audience, "bot_id": bot_id,
                "layer": "chapter", "covers": ids,
                "created_at": max(stamps) if stamps else "",
                "idempotency_key": "chapter:" + hashlib.sha1(
                    "|".join(sorted(ids)).encode()).hexdigest()[:24],
            }
            if dry_run:
                stats["chapters"] += 1
                stats["cards"] += len(chunk)
                continue
            if db.insert_card(card):
                stats["chapters"] += 1
                stats["cards"] += len(chunk)
                logger.info("Dream 章节卡 lane=%s 覆盖=%d 区间=%d-%d 日期=%s",
                            lane, len(chunk), card["turn_start"], card["turn_end"],
                            card["created_at"] or "now")
    return stats


def run(mw, cid, dry_run=False, day=None, min_facts=20, max_sessions=8, now=None):
    """执行一次整理，返回统计字典；dry_run 只统计、不写库也不调后台模型。

    min_facts：同一会话词条达到多少条才值得花后台模型合并（与自动合并同口径）。
    max_sessions：单次最多整理多少个会话，防止一次跑太久。
    """
    stamp = now or datetime.now()
    day = day or stamp.strftime("%Y-%m-%d")
    stats = {"day": day, "dry_run": bool(dry_run), "expired": [], "sessions": 0,
             "groups": 0, "chapters": 0, "chapter_cards": 0, "scan_capped": False,
             "skipped": ""}
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
        # 章节生成与词条合并在同一个 try 里：chapterize 抛异常同样要放开当日占坑，
        # 否则「失败放开、当天还能补跑」的承诺在章节这一步就不成立。
        chapter = chapterize(mw.db, cid, dry_run=dry_run, now=now)
        stats["chapters"] = int(chapter.get("chapters") or 0)
        stats["chapter_cards"] = int(chapter.get("cards") or 0)
        stats["scan_capped"] = bool(chapter.get("scan_capped"))
    except Exception:
        if not dry_run:
            mw.db.dream_release(cid, day)       # 失败放开占坑，当天还能再试
        raise
    if not dry_run:
        mw.db.dream_note(cid, day,
                         f"expired={len(stats['expired'])} sessions={stats['sessions']}"
                         f" groups={stats['groups']} chapters={stats['chapters']}")
    logger.info("Dream 完成 day=%s dry_run=%s 过期=%d 会话=%d 合并组=%d 章节=%d",
                day, stats["dry_run"], len(stats["expired"]),
                stats["sessions"], stats["groups"], stats["chapters"])
    return stats