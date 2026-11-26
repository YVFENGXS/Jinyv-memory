# -*- coding: utf-8 -*-
"""计数/汇总类问题的两遍结构化（灰度，默认关闭）。

开关：RP_COUNT_TWOPASS=1。只对命中计数问句的请求生效，其余请求完全走原链路。

第一遍：只从原始事实 + 原始对话轮次抽「日期|对象|动作|原话」清单，
        不读压缩摘要，避免摘要合并导致漏项或并项。
第二遍：把清单与三条口径注入正常回复生成，由同一人设完成回答。

三条口径：
1) 换货拆分：取到替换件与退回原件算两件。
2) 跨会话补全：同一对象跨会话只算一次；只出现一次的不同对象要保留，
   亲友赠予的也算。
3) 领导/资格判定：只算主人明确说过“我在带/我负责/我领导”的项目本身；
   课程作业、仅计划中、仅作为团队成员的不算；有未来发布时间但当前在带的算。
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("jinyu.count")

TRUE_VALUES = {"1", "true", "on", "yes", "y"}

COUNT_RE = re.compile(
    r"(几(?:个|次|条|件|项|回|种|位|台|本|张|份|家|只|瓶|双|套)|"
    r"多少(?:个|次|条|件|项|回|种|位|台|本|张|份|家|只|瓶|双|套)?|"
    r"一共|总共|加起来|累计|how many|number of|count of)", re.I)

TEMPORAL_RE = re.compile(
    r"(?:几(?:个)?(?:月|天|年|周|星期|小时|分钟|秒)|多久|多长时间|"
    r"how many (?:months|days|years|weeks|hours|minutes|seconds))", re.I)

EXTRACT_SYS = (
    "You build a candidate inventory for a counting question. Do not count or filter relevance.\n"
    "For every evidence line, output one line per distinct object in the format:\n"
    "[date] object | action | exact user quote (<=25 words).\n"
    "Actions must be one of: pickup, return, exchange, acquire, receive, lead, plan, attend.\n"
    "Rules:\n"
    "- Never merge multiple objects into one line: split each project, plant, item, or event into its "
    "own line, even when several appear in the same sentence or the same summary line.\n"
    "- Split an exchange into two lines: picking up the replacement and returning the original.\n"
    "- Include items mentioned only once and items received from family or friends.\n"
    "- Copy the user's own wording; do not paraphrase.\n"
    "End with a line containing only END.")

INJECT_TMPL = (
    "\n\n[计数口径]\n"
    "本轮是计数/汇总类问题。下面是从原始记忆抽出的候选清单（未计数）。"
    "先逐条核对证据，再按三条口径判重、判资格，最后回答：\n"
    "1. 换货拆分：取到替换件与退回原件算两件。\n"
    "2. 跨会话补全：同一对象跨会话只算一次；只出现一次的不同对象要保留，亲友赠予的也算。\n"
    "3. 领导/资格判定：只算主人明确说过“我在带/我负责/我领导”的项目本身；"
    "课程作业、仅计划中、仅作为团队成员的不算；有未来发布时间但当前在带的算。\n"
    "候选清单：\n%s\n"
    "回答要自然、像平时说话，不要提到“清单/规则/口径”；最后用一句话点明总数。")


def enabled() -> bool:
    return str(os.environ.get("RP_COUNT_TWOPASS") or "0").strip().lower() in TRUE_VALUES


def is_count_question(text) -> bool:
    """命中计数问句；纯时长问句（几个月/多久）不算。"""
    t = str(text or "")
    if not t or not COUNT_RE.search(t):
        return False
    mt = TEMPORAL_RE.search(t)
    if mt:
        rest = t[:mt.start()] + t[mt.end():]
        if not COUNT_RE.search(rest):
            return False
    return True


def should_run(text) -> bool:
    return enabled() and is_count_question(text)


def _fact_text(fact) -> str:
    label = fact.get("label") or fact.get("fact") or ""
    day = str(fact.get("occurred_at") or "")[:10]
    return ("[%s] %s" % (day, label)) if day else str(label)


def build_evidence(db, sid, cid, turn, facts_limit=80, turn_window=400,
                   max_chars=28000) -> str:
    """原始证据：长期事实 + 未压缩的对话轮次（含已归档原文）。"""
    blocks = []
    try:
        facts = db.list_facts_detail(sid, cid, limit=facts_limit) or []
        lines = [_fact_text(f) for f in facts if _fact_text(f).strip()]
        if lines:
            blocks.append("长期事实：\n" + "\n".join(lines))
    except Exception as exc:                       # noqa: BLE001 证据缺失不拖垮对话
        logger.warning("计数证据：读事实失败 sid=%s cid=%s：%s", sid, cid, exc)
    try:
        hi = int(turn or 0) or None
        lo = max(1, (int(turn or 1) - int(turn_window)))
        turns = db.get_turns(sid, cid, lo=lo, hi=hi, include_archived=True) or []
        lines = []
        for t in turns:
            content = str(t.get("content") or "").strip()
            if not content:
                continue
            day = str(t.get("created_at") or "")[:16]
            role = "主人" if t.get("role") == "user" else "瑾瑜"
            lines.append("[%s] %s: %s" % (day, role, content))
        if lines:
            blocks.append("原始对话：\n" + "\n".join(lines))
    except Exception as exc:                       # noqa: BLE001
        logger.warning("计数证据：读轮次失败 sid=%s cid=%s：%s", sid, cid, exc)
    text = "\n\n".join(blocks)
    return text[-max_chars:] if len(text) > max_chars else text


def extract_inventory(llm, question, evidence) -> str:
    if llm is None or not str(evidence or "").strip():
        return ""
    raw = llm.chat(
        [{"role": "system", "content": EXTRACT_SYS},
         {"role": "user",
          "content": "Question: %s\nEvidence:\n%s" % (question, evidence)}],
        temperature=0.0,
        max_tokens=int(os.environ.get("RP_COUNT_EXTRACT_TOKENS") or 1200),
    )
    return str(raw or "").strip()


def inject(msgs, inventory):
    """把清单与口径挂到现有 system 消息尾部，保持消息角色结构不变。"""
    note = INJECT_TMPL % inventory
    for m in msgs:
        if m.get("role") == "system":
            m["content"] = str(m.get("content") or "") + note
            return msgs
    msgs.insert(0, {"role": "system", "content": note.strip()})
    return msgs


logger.info("[feature] count_twopass=%s", "on" if enabled() else "off")
