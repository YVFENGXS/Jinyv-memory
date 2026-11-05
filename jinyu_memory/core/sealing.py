# -*- coding: utf-8 -*-
"""封口权（C5-01 / C5-02）：按 topic 独立的状态机 + topic×mode 矩阵。

OPEN / PAUSE_2D / NO_PUSH / TOTAL_SILENCE / SAFETY
- 「先搁着」→ PAUSE_2D 48h：不催不推不建议，物流询问可问；
- 「别催」→ NO_PUSH：可提及/物流，建议最多一次；
- 「一个字不提」→ TOTAL_SILENCE：连暗示都禁止，只有主人能解除；
- 安全/急症可越过封口。
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import control

DEFAULT_HOURS = float(os.environ.get("RP_SEAL_DEFAULT_HOURS") or 48)
TOTAL_PHRASE = os.environ.get("RP_SEAL_TOTAL_SILENCE_PHRASE") or "一个字不提"

ACTIONS = ("mention", "logistics", "advice", "push", "hint")

_MATRIX = {
    "OPEN": {"mention": True, "logistics": True, "advice": True,
             "push": True, "hint": True},
    "PAUSE_2D": {"mention": False, "logistics": True, "advice": False,
                 "push": False, "hint": False},
    "NO_PUSH": {"mention": True, "logistics": True, "advice": True,
                "push": False, "hint": False},
    "TOTAL_SILENCE": {a: False for a in ACTIONS},
    "SAFETY": {a: True for a in ACTIONS},
}

_SAFE = re.compile(r"(自杀|自残|轻生|不想活|危机|急症|喘不上气|胸痛|晕倒|"
                   r"受伤|流血|报警|撑不住)")
_UNSEAL = re.compile(r"(不用|不必|可以|允许|别瞒).{0,6}(一个字不提|不提|说|提)|"
                     r"可以提|说吧|能提")
_TOTAL = re.compile(r"(一个字不提|一个字都别提|一个字也别提|全都别提|"
                    r"永远别提|再也不提)")
_PAUSE = re.compile(r"(先搁着|先放放|先放着|先别管|先不提|先别聊|缓一缓|"
                    r"以后再说|过阵子再说|等以后)")
_NOPUSH = re.compile(r"(别催|不用催|不要催|别推|不用推|别提醒|不用提醒|"
                     r"别老提|不要老提|别问)")
_TOPIC = re.compile(r"(?:关于|那个)?([\u4e00-\u9fff]{2,8})(?:的事|这事儿|那事儿)")


def is_safety(text) -> bool:
    return bool(_SAFE.search(str(text or "")))


def topic_of(text, default="general") -> str:
    t = str(text or "")
    m = _TOPIC.search(t)
    if m:
        topic = m.group(1)
        if topic not in ("一个字", "这事", "那事"):
            return topic
    try:
        import questions
        return questions.topic_of(t, default=default)
    except Exception:                     # noqa: BLE001 循环导入兜底
        return default


def detect(text, default_topic="general"):
    """识别封口语义；返回 (topic, status) 或 None。"""
    t = re.sub(r"[\s，。！？、；：,.!?;:]+", "", str(text or ""))
    if not t:
        return None
    topic = topic_of(text, default_topic)
    if _UNSEAL.search(t):
        return topic, "OPEN"
    if TOTAL_PHRASE in t or _TOTAL.search(t):
        return topic, "TOTAL_SILENCE"
    if _NOPUSH.search(t):
        return topic, "NO_PUSH"
    if _PAUSE.search(t):
        return topic, "PAUSE_2D"
    if is_safety(t):
        return topic, "SAFETY"
    return None


def observe(db, cid, text, default_topic="general") -> dict | None:
    """主人说话时更新封口状态；返回生效的 (topic,status)。"""
    hit = detect(text, default_topic)
    if not hit:
        return None
    topic, status = hit
    control.set_seal(db, cid, topic, status, hours=DEFAULT_HOURS)
    return {"topic": topic, "status": status}


def observe_safety(db, cid, text, topic="safety") -> None:
    if is_safety(text):
        control.set_seal(db, cid, topic, "SAFETY")


def current(db, cid, topic) -> str:
    row = control.get_seal(db, cid, topic)
    if not row:
        return "OPEN"
    status = str(row.get("status") or "OPEN")
    if status == "PAUSE_2D":
        until = control.parse_iso(row.get("until"))
        if until and datetime.now(timezone.utc) >= until:
            # 到点允许问一次「要不要现在动」，但不自动升级为 OPEN
            return "PAUSE_2D_DUE"
    return status


def allowed(mode, action, advice_count=0) -> bool:
    """topic×mode 矩阵；PAUSE_2D_DUE 只在 push 上放开一次。"""
    if mode == "PAUSE_2D_DUE":
        return action in ("logistics", "push")
    rule = _MATRIX.get(mode or "OPEN", _MATRIX["OPEN"])
    if action == "advice" and mode == "NO_PUSH":
        return int(advice_count or 0) < 1
    return bool(rule.get(action, False))


def can_act(db, cid, topic, action, advice_count=0) -> bool:
    if action not in ACTIONS:
        return False
    if is_safety(topic):
        return True
    return allowed(current(db, cid, topic), action, advice_count)


def prompt_note(db, cid, limit=6) -> str:
    """把封口状态注入上下文：只写 topic 与禁止项，不写原因正文。"""
    rows = control.list_seals(db, cid)[:limit]
    if not rows:
        return ""
    parts = []
    for r in rows:
        mode = current(db, cid, r.get("topic"))
        if mode == "OPEN":
            continue
        forbid = [a for a in ACTIONS if not allowed(mode, a)]
        parts.append(f"{r.get('topic')}：{mode}（禁止{'/'.join(forbid) or '无'}）")
    return ("\n【封口】" + "；".join(parts)) if parts else ""


def due(db, cid) -> list[dict]:
    out = []
    for r in control.list_seals(db, cid):
        if r.get("status") == "PAUSE_2D":
            until = control.parse_iso(r.get("until"))
            if until and datetime.now(timezone.utc) >= until:
                out.append(r)
    return out


def clear(db, cid, topic) -> bool:
    return control.clear_seal(db, cid, topic)
