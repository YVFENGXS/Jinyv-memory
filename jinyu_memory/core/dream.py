# -*- coding: utf-8 -*-
"""Dream 轻量整理（P1）：每天 03:30 收走过期临时事实、把同义重复词条并掉。

只做两件确定性的事，不做人格蒸馏、不做多跳联想：
- 过期临时事实：expires_at 已过 → 标 retracted（软删除，保留审计痕迹）
- 同义词条：复用已有的 merge_facts（后台判重 + 确定性护栏 + 二次复核）

幂等：dream_runs(character_id, day) 占坑，同一天只跑一次；跑失败就放开占坑，
当天还能补跑。任何异常都向上抛，由调度方记日志——后台整理坏了也不能拖住前台。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger("rp_memory")

DEFAULT_AT = (3, 30)          # 本地时间 03:30


def next_run(now=None, at=DEFAULT_AT):
    """下一次运行时间点（本地时区）；已经过了今天的目标时刻就顺延到明天。"""
    now = now or datetime.now()
    target = now.replace(hour=int(at[0]), minute=int(at[1]), second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def run(mw, cid, dry_run=False, day=None, min_facts=20, max_sessions=8, now=None):
    """执行一次整理，返回统计字典；dry_run 只统计、不写库也不调后台模型。

    min_facts：同一会话词条达到多少条才值得花后台模型合并（与自动合并同口径）。
    max_sessions：单次最多整理多少个会话，防止一次跑太久。
    """
    stamp = now or datetime.now()
    day = day or stamp.strftime("%Y-%m-%d")
    stats = {"day": day, "dry_run": bool(dry_run), "expired": [], "sessions": 0,
             "groups": 0, "skipped": ""}
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
    if not dry_run:
        mw.db.dream_note(cid, day,
                         f"expired={len(stats['expired'])} sessions={stats['sessions']}"
                         f" groups={stats['groups']}")
    logger.info("Dream 完成 day=%s dry_run=%s 过期=%d 会话=%d 合并组=%d",
                day, stats["dry_run"], len(stats["expired"]),
                stats["sessions"], stats["groups"])
    return stats
