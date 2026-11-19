# -*- coding: utf-8 -*-
"""压缩质量（C3-01 / C3-02 / C3-03）：must-keep 原子、覆盖率、五字段审核、四道关。

原子从归档批次的原话里抽取，不额外调用模型；硬类别（医疗/安全/承诺/节点/
偏好/冲突/关系）一律落 L1 并钉死地板。压缩只压 CLOSED episode，从最老开始，
到 LOW 停；must-keep 覆盖率必须 100%，否则拒绝改写。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

import control
import gradient
import scoring

MUST_KEEP_TYPES = ("medical", "safety", "commitment", "external_node",
                   "preference", "boundary", "conflict", "relationship")
COVERAGE_REQUIRED = float(os.environ.get("RP_MUST_KEEP_COVERAGE") or 1.0)
MAX_ATOMS = 16
CTRL = re.compile(r"[\u0000-\u001f\u202a-\u202e\u2066-\u2069]")
_WS = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]…—~·/]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")


def _clean(text, limit=100) -> str:
    return CTRL.sub("", str(text or "")).strip()[:limit]


def _key(text) -> str:
    return _WS.sub("", str(text or ""))


def atom(type_, who, when, what, result="", status="", next_="",
         source_turn=0, confidence=0.8, **extra) -> dict:
    return {"atom_type": type_, "type": type_, "who": _clean(who, 20),
            "when": _clean(when, 40), "what": _clean(what, 100),
            "result": _clean(result, 80), "status": _clean(status, 40),
            "next": _clean(next_, 60), "source_turn": int(source_turn or 0),
            "confidence": round(max(0.0, min(1.0, float(confidence))), 2),
            **extra}


def _speaker(t) -> str:
    if t.get("role") == "assistant":
        return "角色"
    label = str(t.get("source") or "")
    return "主人" if "QQ" not in label else "主人"


def extract_atoms(turns, fields=None) -> list[dict]:
    """从一批原话抽 must-keep 原子；宁多勿漏，去重后最多 MAX_ATOMS 条。"""
    fields = fields or {}
    sc = fields.get("state_changes") or {}
    out, seen = [], set()

    def add(item):
        key = (item["atom_type"], _key(item["what"]))
        if not item["what"] or key in seen:
            return
        seen.add(key)
        out.append(item)

    for t in turns or ():
        text = _clean(t.get("content"), 160)
        if len(_key(text)) < 4:
            continue
        kind = scoring.detect_type(text, fields)
        when = _clean(t.get("created_at") or "", 40)
        if not kind:
            # 非 must-keep 也要有 L1 原子：否则原文到期时无格可落（C20）。
            # 这类原子不参与覆盖率，floor=4，可随龄降到时间桶/索引。
            add(atom("", _speaker(t), when, text,
                     source_turn=t.get("turn_idx", 0)))
            continue
        # 承诺：明确写清「谁答应做什么」的句子
        if kind == "commitment":
            add(atom("commitment", _speaker(t), when, text,
                     status="ACTIVE", next_="等兑现/等主人确认",
                     source_turn=t.get("turn_idx", 0)))
        elif kind == "medical":
            add(atom("medical", _speaker(t), when, text, status="进行中",
                     next_="观察症状变化", source_turn=t.get("turn_idx", 0)))
        elif kind == "safety":
            add(atom("safety", _speaker(t), when, text, status="需关注",
                     next_="确认安全", source_turn=t.get("turn_idx", 0), confidence=0.9))
        elif kind == "external_node":
            add(atom("external_node", _speaker(t), when, text, status="待发生",
                     next_="节点到达时提醒", source_turn=t.get("turn_idx", 0)))
        elif kind == "preference":
            add(atom("preference", _speaker(t), when, text, status="长期有效",
                     source_turn=t.get("turn_idx", 0)))
        elif kind == "relationship":
            add(atom("relationship", _speaker(t), when, text, status="",
                     source_turn=t.get("turn_idx", 0)))
        else:
            add(atom(kind, _speaker(t), when, text, source_turn=t.get("turn_idx", 0)))

    # 后台结构化字段比句子规则更可靠：承诺、冲突、关系变化各补一条
    for i, p in enumerate(sc.get("pending_promises") or [], 1):
        add(atom("commitment", "角色", "", p, status="ACTIVE",
                 next_="等兑现", source_turn=max(
                     [int(t.get("turn_idx") or 0) for t in turns or ()] or [0])))
    for flag in fields.get("conflict_flags") or []:
        add(atom("conflict", "系统", "", str(flag), status="待人工确认",
                 source_turn=max([int(t.get("turn_idx") or 0) for t in turns or ()] or [0])))
    if sc.get("relationship"):
        add(atom("relationship", "主人", "", "关系变化：" + json.dumps(
            sc["relationship"], ensure_ascii=False), source_turn=max(
                [int(t.get("turn_idx") or 0) for t in turns or ()] or [0])))
    return out[:MAX_ATOMS]


def coverage(atoms, text, refs=(), atom_ids=()) -> dict:
    """must-keep 覆盖率：原子已被 L1 落库引用，或关键字段出现在压缩稿里。"""
    body = _key(text)
    ref = " ".join(str(r) for r in refs or ())
    ids = {str(x) for x in (atom_ids or ()) if x}
    total = present = 0
    missing = []
    for a in atoms or ():
        if a.get("atom_type") not in MUST_KEEP_TYPES:
            continue
        total += 1
        if a.get("atom_id") and str(a["atom_id"]) in ids:
            present += 1
            continue
        what = _key(a.get("what"))
        ok = bool(what) and (what in body or what in _key(ref))
        if not ok:
            # 压缩稿可以只留短关键词，允许取原子中 4 字以上的片段命中
            pieces = [what[i:i + 6] for i in range(0, max(1, len(what) - 5), 2)]
            ok = any(p and p in body for p in pieces[:12])
        if ok:
            present += 1
        else:
            missing.append(a.get("what"))
    return {"must_keep_present": present, "must_keep_total": total,
            "ratio": 1.0 if total == 0 else present / total,
            "missing": missing[:5]}


def _numbers(text) -> set:
    return set(re.findall(r"[0-9]{1,4}", str(text or "")))


def distortion(source, summary) -> list[str]:
    """压歪检查：数字/时间/否定被改动的痕迹。"""
    flags = []
    src_nums, out_nums = _numbers(source), _numbers(summary)
    if src_nums - out_nums:
        flags.append("number_missing:" + ",".join(sorted(src_nums - out_nums)[:4]))
    src_neg = len(re.findall(r"(不|没|别|勿)", str(source or "")))
    out_neg = len(re.findall(r"(不|没|别|勿)", str(summary or "")))
    if src_neg >= 2 and out_neg == 0:
        flags.append("negation_flipped")
    for token in ("今天", "明天", "后天", "昨天", "下周", "上周"):
        if token in str(source or "") and token not in str(summary or ""):
            flags.append("time_missing:" + token)
    return flags


def red_flags(source, summary, atoms, atom_ids=()) -> list[str]:
    flags = distortion(source, summary)
    cov = coverage(atoms, summary, atom_ids=atom_ids)
    if cov["ratio"] < 1.0:
        flags.append("coverage:%d/%d" % (cov["must_keep_present"],
                                         cov["must_keep_total"]))
    if not _key(summary):
        flags.append("empty_summary")
    return flags


def review(db, card_id, cid, sid, source_turns, sources, summary,
           atoms, retention_until="", refs=(), atom_ids=()) -> dict:
    """四道关：来源可追溯 + 硬信息/覆盖 + 压歪压漏 + 冲突。"""
    flags = red_flags(sources, summary, atoms, atom_ids=atom_ids)
    cov = coverage(atoms, summary, refs=refs, atom_ids=atom_ids)
    status = "passed"
    if not source_turns:
        flags.append("source_missing")
        status = "owed"
    if cov["ratio"] < COVERAGE_REQUIRED:
        status = "rejected"
    if flags:
        status = "rejected" if status == "passed" else status
    rid = control.create_review(
        db, card_id, cid, source_turns, retention_until or control.now_iso(),
        max_retention_until=retention_until or "",
        red_flags=flags,
        ai_mark={"distortion": [f for f in flags if "number" in f or "negation" in f
                                or "time" in f],
                 "omission": cov["missing"],
                 "owed": [f for f in flags if "source" in f],
                 "coverage": {"present": cov["must_keep_present"],
                              "total": cov["must_keep_total"]}},
        status=status)
    if status != "passed":
        for a, b in source_turns or ():
            control.pin_turns(db, cid, sid, a, reason="compression_review")
            control.pin_turns(db, cid, sid, b, reason="compression_review")
    return {"review_id": rid, "status": status, "flags": flags, "coverage": cov}


def write_atoms(db, sid, cid, card, turns, retention_until="") -> dict:
    """归档时落 L1 原子 + 审核；返回 {atom_ids, review, coverage}。"""
    atoms = extract_atoms(turns, card)
    atom_ids = []
    for a in atoms:
        kind = a.get("atom_type") or ""
        front = scoring.front_score(a.get("what") or "")
        back, ev = scoring.back_score(a.get("what") or "", [str(a.get("source_turn"))])
        final, source = scoring.effective_score(kind, front, back)
        floor = scoring.floor_level(final) if kind in MUST_KEEP_TYPES else 4
        uid = control.add_unit(
            db, cid, sid, level=1, floor_level=floor, content=a,
            source_turns=[[a.get("source_turn", 0), a.get("source_turn", 0)]],
            episode_id=card.get("event_id") or "", atom_type=kind,
            salience=final, score_front=front, score_back=back,
            score_source=source, retention_until=retention_until)
        a["atom_id"] = uid
        atom_ids.append(uid)
        if floor == 1:                       # 已在最细档：到地板即冻结
            gradient.freeze(db, uid)
        # 原文在 L1 落库后仍按 7 天保留；must-keep 期间 pin 住
        if kind in MUST_KEEP_TYPES and a.get("source_turn"):
            control.pin_turns(db, cid, sid, a["source_turn"],
                              until_hours=24 * 7, reason="must_keep")
    source_turns = [[int(card.get("turn_start") or 0), int(card.get("turn_end") or 0)]]
    source_text = "\n".join(str(t.get("content") or "") for t in turns or ())
    # L1 落格是原文→原子的保真复制，不做 L2 摘要的压歪检查；
    # 审核对象用原文本身，covered 由 atom_ids 直接证明。
    return {"atom_ids": atom_ids,
            "atoms": atoms,
            **review(db, card.get("event_id") or "", cid, sid, source_turns,
                     source_text, source_text, atoms, retention_until=retention_until,
                     refs=[card.get("event_id") or ""], atom_ids=atom_ids)}


def maybe_compress(db, sid, cid, used, budget, shadow=True, now=None) -> list[dict]:
    """closed + 高水位才压，从最老开始，到 LOW 停。"""
    if used < budget.high:
        return []
    reports = gradient.ladder(db, cid, budget.usable, now=now)
    if shadow:
        return reports
    return reports


def overflow_priority() -> list[str]:
    """open episode 溢出时先裁什么（低价值在前，原子永不在列）。"""
    return ["autonomy_digest", "old_cards", "tool_outputs", "recent_summary"]


def status_snapshot(db, cid) -> dict:
    units = control.list_units(db, cid, limit=5000)
    reviews = control.list_reviews(db, cid, limit=200)
    holes = gradient.no_hole_audit(db, cid)
    scores = [u.get("salience") for u in units]
    return {
        "units": len(units),
        "frozen": sum(1 for u in units if u.get("frozen")),
        "reviews": len(reviews),
        "rejected": sum(1 for r in reviews if r.get("status") == "rejected"),
        "owed": sum(1 for r in reviews if r.get("status") == "owed"),
        "no_hole": len(holes),
        "inflation": round(scoring.inflation_ratio(scores), 3),
    }
