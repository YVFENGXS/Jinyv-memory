# -*- coding: utf-8 -*-
"""评分器（C8-01）：前台快评 + 后台细评 + 硬地板。

    S_raw = 0.4 × S_front + 0.6 × S_back      # 只有一边时权重归一
    S     = max(S_raw, hard_floor(type))      # 硬类别不交给模型打分

前台快评不额外发请求：能从回复正文/结构化字段读到的信号（数字、时间、否定、
承诺、外部节点、情绪）直接算分；如果调用方已经带了 `salience`（0-100），优先用它。
后台细评在归档时从原文重算，并返回引用了哪几条 turn 作为 evidence。
"""
from __future__ import annotations

import re

# 硬地板：模型打低分也按地板处理（C18）
FLOORS = {
    "safety": 95,
    "medical": 95,
    "commitment": 90,
    "external_node": 90,
    "preference": 88,
    "boundary": 88,
    "conflict": 85,
    "relationship": 85,
}

# 分数 → 最低保留形态（地板级别，越大越细）
FLOOR_LEVEL = ((85, 1), (65, 2), (45, 3), (0, 4))

_EMO = re.compile(r"(开心|高兴|难过|伤心|生气|害怕|焦虑|紧张|喜欢|讨厌|舍不得|"
                  r"委屈|放心|担心|心疼|感动|崩溃)")
_TIME = re.compile(r"(今天|明天|后天|昨天|下周|上周|这周|月底|年初|"
                   r"[0-9]{1,2}[点:：][0-9]{0,2}|[0-9]{1,2}月[0-9]{1,2}日|"
                   r"[0-9]{4}年|[一二三四五六七八九十]+点)")
_NEG = re.compile(r"(不|没|别|勿|拒绝|取消|算了|不要|不用)")
_NUM = re.compile(r"[0-9]+")
_MED = re.compile(r"(症状|头疼|头痛|发烧|咳嗽|过敏|吃药|用药|药量|布洛芬|"
                  r"医院|复诊|检查|体检|手术|副作用|不舒服|疼|痛|呕吐|失眠)")
_NODE = re.compile(r"(预约|挂号|复诊|考试|面试|截止|交付|出差|航班|车票|"
                   r"见面|开会|报到|入职|复查)")
_PREF = re.compile(r"(不喜欢|不想|别问|别提|不要提|不许|讨厌|害怕|"
                   r"称呼|叫我|禁忌|保密|不能说)")
_SAFE = re.compile(r"(自杀|自残|轻生|不想活|危机|急症|喘不上气|胸痛|晕倒|"
                   r"受伤|流血|报警)")
_PROMISE = re.compile(r"(答应|保证|承诺|说好|一定|拉钩|欠你|替你保密)")
_REL = re.compile(r"(主人|客人|朋友|家人|称呼|关系|信任|好感|道歉|原谅|"
                  r"和好|吵架)")


def _clamp(x, lo=0, hi=100):
    return int(max(lo, min(hi, round(float(x)))))


def hard_floor(atom_type: str) -> int:
    return int(FLOORS.get(str(atom_type or ""), 0))


def detect_type(text: str, fields: dict | None = None) -> str:
    """按内容猜 must-keep 类别；多类命中取地板最高的。"""
    t = str(text or "")
    fields = fields or {}
    sc = fields.get("state_changes") or {}
    found = []
    if _SAFE.search(t):
        found.append("safety")
    if _MED.search(t):
        found.append("medical")
    if sc.get("pending_promises") or sc.get("resolved_promises") or _PROMISE.search(t):
        found.append("commitment")
    if _NODE.search(t):
        found.append("external_node")
    if _PREF.search(t):
        found.append("preference")
    if fields.get("conflict_flags"):
        found.append("conflict")
    if sc.get("relationship") or fields.get("rel_hooks") and _REL.search(t):
        found.append("relationship")
    if not found:
        return ""
    return max(found, key=hard_floor)


def front_score(text: str, salience=None, mentions=0, verified=0) -> int:
    """前台快评：默认 35，按事实密度/未决/情绪/节点/强调递增。"""
    if salience is not None:
        try:
            return _clamp(float(salience))
        except (TypeError, ValueError):
            pass
    t = str(text or "")
    score = 35
    score += 10 if _NUM.search(t) else 0
    score += 8 if _TIME.search(t) else 0
    score += 6 if _NEG.search(t) else 0
    score += 10 if _PROMISE.search(t) else 0
    score += 8 if _NODE.search(t) else 0
    score += 8 if _EMO.search(t) else 0
    score += 6 if _REL.search(t) else 0
    score += min(12, 4 * int(max(0, mentions or 0)))
    score += min(8, 4 * int(max(0, verified or 0)))
    if _SAFE.search(t):
        score = max(score, 95)
    if _MED.search(t):
        score = max(score, 90)
    return _clamp(score)


def back_score(text: str, evidence=(), fields: dict | None = None) -> tuple[int, list]:
    """后台细评：必须带 evidence（哪几条 turn 命中了哪类事实）。"""
    t = str(text or "")
    ev = [str(e) for e in (evidence or ()) if str(e).strip()]
    score = front_score(t)
    if _MED.search(t):
        score = max(score, 92)
    if _SAFE.search(t):
        score = max(score, 95)
    if _NODE.search(t):
        score = max(score, 88)
    if not ev:
        score = min(score, 75)          # 没有来源的细评不给高分
    return _clamp(score), ev[:6]


def combine(front, back, front_w=0.4, back_w=0.6) -> tuple[int, str]:
    """加权；只有一边时权重归一，并返回 score_source。"""
    f = None if front is None else _clamp(front)
    b = None if back is None else _clamp(back)
    if f is not None and b is not None:
        return _clamp(float(f) * front_w + float(b) * back_w), "both"
    if f is not None:
        return f, "front"
    if b is not None:
        return b, "back"
    return 60, "rule"


def effective_score(atom_type: str, front, back, front_w=0.4, back_w=0.6
                    ) -> tuple[int, str]:
    """最终分 = max(加权分, 硬地板)，并返回来源。"""
    raw, source = combine(front, back, front_w, back_w)
    floor = hard_floor(atom_type)
    if floor > raw:
        return floor, "rule"
    return raw, source


def floor_level(score) -> int:
    try:
        s = float(score)
    except (TypeError, ValueError):
        s = 60.0
    for threshold, level in FLOOR_LEVEL:
        if s >= threshold:
            return level
    return 4


def inflation_ratio(scores) -> float:
    """高分段（>80）占比；>0.30 视为分数膨胀。"""
    vals = [float(s) for s in (scores or ()) if s is not None]
    if not vals:
        return 0.0
    return sum(1 for v in vals if v > 80) / len(vals)


def inflation_guard(scores, threshold=0.30) -> bool:
    return inflation_ratio(scores) > float(threshold)
