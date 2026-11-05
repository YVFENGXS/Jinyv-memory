# -*- coding: utf-8 -*-
"""观测与日报（C7-01）：覆盖率、压歪/压漏、重问率、pin 数、归档数、token 预算。"""
from __future__ import annotations

import context_budget
import compression
import control
import gradient
import questions
import retention
import scoring


def snapshot(db, cid, budget=None) -> dict:
    budget = budget or context_budget.load_budget()
    units = control.list_units(db, cid, limit=5000)
    reviews = control.list_reviews(db, cid, limit=500)
    qs = control.list_questions(db, cid, limit=500)
    holes = gradient.no_hole_audit(db, cid)
    scores = [u.get("salience") for u in units]
    passed = sum(1 for r in reviews if r.get("status") == "passed")
    rejected = [r for r in reviews if r.get("status") == "rejected"]
    omitted = sum(1 for r in rejected
                  if any("coverage" in str(f) for f in
                         (r.get("backend_red_flags") or [])))
    ret = retention.audit(db, cid)
    return {
        "units": len(units),
        "levels": {lv: sum(1 for u in units if int(u.get("level") or 1) == lv)
                   for lv in (1, 2, 3, 4)},
        "frozen": sum(1 for u in units if u.get("frozen")),
        "reviews": len(reviews), "reviews_passed": passed,
        "reviews_rejected": len(rejected), "omission": omitted,
        "questions": len(qs),
        "questions_asked": sum(int(q.get("ask_count") or 0) for q in qs),
        "no_hole": len(holes),
        "retention": ret,
        "inflation": round(scoring.inflation_ratio(scores), 3),
        "budget": {"work": budget.work_budget, "usable": budget.usable,
                   "high": budget.high, "low": budget.low},
    }


def report_text(db, cid) -> str:
    s = snapshot(db, cid)
    b = s["budget"]
    lines = [
        "【压缩状态】",
        f"记忆单元：{s['units']}（L1 {s['levels'][1]} / L2 {s['levels'][2]} / "
        f"L3 {s['levels'][3]} / L4 {s['levels'][4]}），冻结 {s['frozen']}",
        f"压缩审核：{s['reviews']}（通过 {s['reviews_passed']} / 拒绝 "
        f"{s['reviews_rejected']} / 压漏 {s['omission']}）",
        f"无洞过期：{s['no_hole']}，到期未 pin：{s['retention']['expired_unpinned']}，"
        f"软归档：{s['retention']['archived_turns']} 轮",
        f"问题账本：{s['questions']} 条（累计问 {s['questions_asked']} 次）",
        f"评分膨胀（>80 占比）：{s['inflation'] * 100:.1f}%",
        f"预算：WORK {b['work']} / USABLE {b['usable']} / "
        f"HIGH {b['high']} / LOW {b['low']}",
    ]
    return "\n".join(lines)


def question_metrics(db, cid) -> dict:
    qs = control.list_questions(db, cid, limit=1000)
    total = len(qs)
    repeated = [q for q in qs if int(q.get("ask_count") or 0) > 1]
    with_ev = [q for q in qs if q.get("evidence_id")]
    return {"total": total,
            "repeated": len(repeated),
            "repeat_rate": round(len(repeated) / total, 3) if total else 0.0,
            "with_evidence": len(with_ev),
            "owed": sum(1 for q in qs if q.get("status") == "owed")}
