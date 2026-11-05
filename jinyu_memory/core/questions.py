# -*- coding: utf-8 -*-
"""防重复提问（C4-01 / C4-02）：问题账本 + 服务端硬门禁。

重复不是「时间久了」，而是「事有没有变」。只有三类算新理由：
症状变化、外部固定节点出现、主人自己说想做；安全/急症与主人直接提问例外。

账本持久化在 SQLite，脱离热上下文；门禁在生成后拦截，不靠模型自觉。
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timedelta, timezone

import control
import sealing

MIN_INTERVAL_HOURS = float(os.environ.get("RP_ASK_MIN_INTERVAL_HOURS") or 72)
JITTER_HOURS = float(os.environ.get("RP_ASK_MIN_INTERVAL_JITTER_HOURS") or 24)
TOPIC_COOLDOWN_HOURS = float(os.environ.get("RP_ASK_TOPIC_COOLDOWN_HOURS") or 24)
SAFETY_BYPASS = (os.environ.get("RP_ASK_SAFETY_BYPASS") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}
DIRECT_BYPASS = (os.environ.get("RP_ASK_DIRECT_QUESTION_BYPASS") or "1").strip().lower() \
    not in {"0", "false", "no", "off"}

CTRL = re.compile(r"[\u0000-\u001f\u202a-\u202e\u2066-\u2069]")
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]…—~·/]+")
_Q_TAIL = re.compile(r"(吗|呢|吧|么|没有|有没有|是不是|好不好|行不行|要不要|"
                     r"会不会|能不能|怎么样|如何|为什么|怎么了|多少|几天|多久)")
_Q_SPLIT = re.compile(r"(?<=[。！？!?" + "\n" + r"])")
_POLITE = re.compile(r"(请问|帮我|能不能|可以|麻烦|我想知道|告诉我|查一下|搜一下|"
                     r"那个|这个|主人问的|主人说的|主人说)")

# 语义归一：同义说法先压到同一个词，再做指纹（不做模型调用）
SYN = {
    "有没有": "是否", "好些了吗": "是否好转", "好点了吗": "是否好转",
    "好一点了吗": "是否好转", "还疼吗": "是否疼痛", "疼不疼": "是否疼痛",
    "吃药了吗": "是否服药", "吃了吗": "是否服药", "睡了吗": "是否睡眠",
    "睡得怎么样": "睡眠情况", "去医院了吗": "是否就诊", "去了吗": "是否就诊",
    "还需要吗": "是否继续", "还要吗": "是否继续", "忙吗": "是否忙碌",
    "累不累": "是否疲惫", "心情怎么样": "心情情况",
}

TOPIC_RULES = (
    ("医疗", re.compile(r"(症状|头疼|头痛|发烧|咳嗽|过敏|吃药|用药|医生|医院|"
                       r"复诊|检查|体检|手术|副作用|疼|痛|不舒服|失眠|睡眠)")),
    ("旅行", re.compile(r"(旅行|旅游|出行|机票|酒店|行程|假期|景点)")),
    ("工作", re.compile(r"(工作|上班|加班|项目|会议|面试|汇报|老板|同事)")),
    ("学习", re.compile(r"(学习|考试|作业|课程|论文|复习|成绩)")),
    ("财务", re.compile(r"(钱|预算|工资|房租|账单|还款|花销)")),
    ("关系", re.compile(r"(关系|吵架|和好|道歉|喜欢|讨厌|朋友|家人)")),
    ("情绪", re.compile(r"(心情|难过|焦虑|压力|崩溃|害怕|开心)")),
)
_TOPIC_OBJ = re.compile(r"(?:关于|关于那个|那个)?([\u4e00-\u9fff]{2,8})的事")


def normalize(text) -> str:
    t = CTRL.sub("", str(text or ""))
    t = _POLITE.sub("", t)
    for src, dst in SYN.items():
        t = t.replace(src, dst)
    t = _Q_TAIL.sub("", t)
    t = _PUNCT.sub("", t)
    return t.strip()


def fingerprint(text) -> str:
    """语义指纹 = 归一化意图 + 对象 + 动作；不是原句字符串。"""
    norm = normalize(text)
    if not norm:
        return ""
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def detect_questions(reply) -> list[str]:
    """从回复里拆出问题句；问号句与语气词结尾都算。"""
    out = []
    for part in _Q_SPLIT.split(str(reply or "")):
        s = part.strip()
        if not s:
            continue
        if s.endswith(("?", "？")) or re.search(r"(吗|呢|吧|么)[。！!？?]?$", s):
            out.append(s)
    return out


def topic_of(text, default="general") -> str:
    t = str(text or "")
    for topic, pat in TOPIC_RULES:
        if pat.search(t):
            return topic
    m = _TOPIC_OBJ.search(t)
    if m:
        return m.group(1)
    return default


def classify_reason(user_input, question="") -> str:
    """五类：safety / direct_question / symptom_change / external_node / master_request。"""
    u = str(user_input or "")
    if sealing.is_safety(u):
        return "safety"
    if re.search(r"(吗|呢|？|\?|怎么|如何|为什么|多少|多久|要不要)", u):
        return "direct_question"
    if re.search(r"(更疼|加重|好点|好转|又疼|又开始|副作用|不舒服|睡不着|发烧)", u):
        return "symptom_change"
    if re.search(r"(明天|后天|下周|预约|挂号|复诊|检查结果|截止|面试|考试|到达|见面)", u):
        return "external_node"
    if re.search(r"(我想|我要|我打算|计划|准备|想去做|想去)", u):
        return "master_request"
    return "master_request"


def action_level(question) -> int:
    q = str(question or "")
    if sealing.is_safety(q):
        return 4
    if re.search(r"(去医院|看医生|急诊|挂号|马上|立刻|严重)", q):
        return 3
    if re.search(r"(要不要|是不是该|建议|怎么办|需不需要)", q):
        return 2
    if re.search(r"(吗|呢|？|\?)", q):
        return 1
    return 0


def min_gap_hours(reason, evidence_strength=0.0) -> float:
    """弹性间隔：短期医疗约 2 天，一般 3 天（48–96h），长期规划 30 天。"""
    if reason == "safety":
        return 0.0
    base = MIN_INTERVAL_HOURS
    try:
        strength = max(0.0, min(1.0, float(evidence_strength)))
    except (TypeError, ValueError):
        strength = 0.0
    jitter = max(0.0, JITTER_HOURS) * (1.0 - strength)
    return max(1.0, base - jitter)


def evidence_for(db, cid, text, topic=None) -> str:
    """把问题落到具体 atom/card/commitment；落不了返回空串（记 owed）。"""
    topic = topic or topic_of(text)
    try:
        units = control.list_units(db, cid, limit=500)
    except Exception:                           # noqa: BLE001
        units = []
    for u in units:
        content = u.get("content_json") or {}
        body = json.dumps(content, ensure_ascii=False)
        if topic and topic in body:
            return u["id"]
        if normalize(content.get("what") or "") and \
                normalize(content.get("what")) in normalize(text):
            return u["id"]
    return ""


def gate(db, cid, reply, user_input, evidence_id="", topic=None,
         now=None) -> dict:
    """服务端门禁：同指纹同理由、无新证据、最小间隔内 → 拦下。"""
    stamp = now or datetime.now(timezone.utc)
    questions = detect_questions(reply)
    topic = topic or topic_of(user_input or reply)
    if not questions:
        return {"allowed": True, "questions": [], "blocked": [], "owed": [],
                "topic": topic, "reason": ""}
    reason = classify_reason(user_input, questions[0])
    blocked, owed, allowed = [], [], []
    for q in questions:
        fp = fingerprint(q)
        if not fp:
            allowed.append(q)
            continue
        if reason == "safety" and SAFETY_BYPASS:
            allowed.append(q)
            continue
        if reason == "direct_question" and DIRECT_BYPASS:
            allowed.append(q)
            continue
        row = control.get_question(db, cid, fp, reason)
        if row is None:
            # 第一次问这个主题：允许，不要求 evidence；只有「重问」才要新证据
            allowed.append(q)
            continue
        if not evidence_id:
            owed.append(q)
            blocked.append(q)
            continue
        nxt = control.parse_iso(row.get("next_eligible_at"))
        if nxt and stamp < nxt:
            blocked.append(q)
            continue
        if int(row.get("action_level") or 0) <= 0:
            blocked.append(q)             # 到 0 级且无新理由，停止
            continue
        if int(row.get("ask_count") or 0) > 0 and action_level(q) > int(row["action_level"]):
            blocked.append(q)             # 重复提问必须带更小的动作
            continue
        # topic 冷却：同 topic 物流询问 24h 内最多一次
        try:
            same = [r for r in control.list_questions(db, cid, topic=topic, limit=20)
                    if r.get("topic") == topic and r.get("last_asked_at")]
        except Exception:                 # noqa: BLE001
            same = []
        if same and TOPIC_COOLDOWN_HOURS > 0:
            last = max((control.parse_iso(r["last_asked_at"]) for r in same
                        if control.parse_iso(r["last_asked_at"])), default=None)
            if last and (stamp - last) < timedelta(hours=TOPIC_COOLDOWN_HOURS):
                blocked.append(q)
                continue
        allowed.append(q)
    return {"allowed": not blocked, "questions": questions, "blocked": blocked,
            "owed": owed, "reason": reason, "topic": topic}


def remember(db, cid, questions, user_input, evidence_id="", topic=None,
             reason=None) -> list[str]:
    """把本轮真正问出口的问题写进账本。"""
    topic = topic or topic_of(user_input or "")
    reason = reason or classify_reason(user_input, (questions or [""])[0])
    ids = []
    for q in questions or ():
        fp = fingerprint(q)
        if not fp:
            continue
        level = action_level(q)
        gap = min_gap_hours(reason, 0.8 if evidence_id else 0.0)
        qid = control.record_question(
            db, cid, topic, fp, reason, evidence_id=evidence_id,
            action_level=level, min_gap_hours=gap,
            status="active")
        control.mark_asked(db, qid, min_gap_hours=gap, status="active",
                           evidence_id=evidence_id or None)
        ids.append(qid)
    return ids


def prompt_note(db, cid, limit=10, chars=300) -> str:
    """每轮注入「最近问过，勿重复」；只放活跃条目。"""
    try:
        rows = [r for r in control.list_questions(db, cid, limit=limit)
                if r.get("status") in ("active", "unanswered", "owed")
                and r.get("last_asked_at")]
    except Exception:                     # noqa: BLE001
        return ""
    if not rows:
        return ""
    lines, used = [], 0
    for r in rows:
        # 账本不存原句，只给指纹对应的主题与次数，避免二次复制正文
        line = f"- {r.get('topic') or '话题'}（问过 {int(r.get('ask_count') or 0)} 次）"
        if used + len(line) > chars:
            break
        lines.append(line)
        used += len(line)
    return ("\n【最近问过·勿重复】" + "；".join(x[2:] for x in lines)
            if lines else "")[:chars + 20]


def gate_reply(reply, db, cid, user_input, rewriter=None, evidence_id="",
               topic=None) -> tuple[str, dict]:
    """生成后门禁；被拦下时优先用 rewriter 重写一次，失败则删掉问题句。"""
    verdict = gate(db, cid, reply, user_input, evidence_id=evidence_id, topic=topic)
    if verdict["allowed"]:
        remember(db, cid, verdict["questions"], user_input,
                 evidence_id=evidence_id, topic=verdict["topic"],
                 reason=verdict.get("reason"))
        return reply, verdict
    if rewriter is not None and verdict["blocked"]:
        try:
            new = rewriter(reply, verdict["blocked"])
            if new and new != reply:
                v2 = gate(db, cid, new, user_input, evidence_id=evidence_id,
                          topic=verdict["topic"])
                if v2["allowed"]:
                    remember(db, cid, v2["questions"], user_input,
                             evidence_id=evidence_id, topic=verdict["topic"],
                             reason=v2.get("reason"))
                    return new, v2
                # 重写后仍被拦：对重写稿再做一次剔除，绝不把旧问法原样发出去
                reply, verdict = new, v2
        except Exception:                 # noqa: BLE001 重写失败不阻断对话
            pass
    # 最后兜底：删掉被拦下的问题句，保留陈述
    keep = reply
    for q in verdict["blocked"]:
        keep = keep.replace(q, "").strip()
    return (keep or "……"), verdict
