# -*- coding: utf-8 -*-
"""证据闸门与时钟短路（阶段 1）

思路参考 SeekMemory 的 evidence_floor 与 is_clock_query，独立实现、未复制代码：
- 相对闸门：候选分数必须 ≥ max(MIN_ABS, 最高分 × MIN_REL)。
  retriever 现在只要 s > 0 就注入，最坏的情况是「所有卡都很弱」时仍硬凑 top-N，
  把一批和当前对话没关系的旧事塞进上下文。
- 时钟短路：纯问时间/日期的句子不启动检索。当前时间由状态账本锚提供，
  检索只会拉进无关旧事（「今天几号」不需要回忆起灯会）。

两个常量要用 tools/eval_memory.py 在自有语料上标定；这里的默认值取自
score_card 的分数刻度（单字核心词 0.6 是最低的有效命中，整词约 3.4~3.8），
不是外来魔数。GATE_ENABLED / CLOCK_SHORTCUT 只用于评测消融，线上不要关。
"""
import os
import re


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


MIN_ABS = _env_float("RP_GATE_MIN_ABS", 0.6)   # 绝对下限：至少一个核心词命中
MIN_REL = _env_float("RP_GATE_MIN_REL", 0.3)   # 相对下限：不到最高分三成算噪声

GATE_ENABLED = True        # False：退回「命中即注入」，评测消融用
CLOCK_SHORTCUT = True      # False：时间问句也照常检索

# 时间标记：命中才可能是纯问时间。剥掉标记后还要再看剩多少实义词。
_CLOCK_MARK = re.compile(
    r"几点(了|钟)?|几号|几月几(日|号)?|星期几|周几|什么(时候|日期)|"
    r"现在(是)?(几点|什么时候|几号)|今天(是)?(几号|周几|星期几)")
_FILLER = re.compile(r"请问|告诉我|知道|现在|今天|明天|昨天|是|的|了|啊|呀|呢|吧|哦|嗯|"
                     r"[\s，。？！、,.?!~～]")


def evidence_floor(top_evidence: float) -> float:
    """最高分 → 本轮的过闸线：强命中按比例，全部很弱时按绝对下限。"""
    top = max(0.0, float(top_evidence or 0.0))
    return max(MIN_ABS, top * MIN_REL)


def apply(scored: list, exempt=()) -> list:
    """scored 必须已按分数降序；返回过闸后的列表（保持原顺序）。

    exempt 是「只降权、不排除」的卡 id（带强冲突标记）：它们已经被降过权，
    不能再被相对闸门二次处罚，否则「只标注不拦截」就变成了拦截（R6 的语义）。
    """
    if not GATE_ENABLED or not scored:
        return scored
    floor = evidence_floor(scored[0][0])
    ex = set(exempt or ())
    return [(s, c) for s, c in scored if s >= floor or c.get("event_id") in ex]


def is_clock_query(text: str) -> bool:
    """纯问时间/日期：剥掉时间标记与语气填充后几乎不剩实义词。

    「今天几号」→ True；「今天几号去灯会」→ False（还剩「去灯会」）。
    """
    t = str(text or "")
    if not _CLOCK_MARK.search(t):
        return False
    rest = _FILLER.sub("", _CLOCK_MARK.sub("", t))
    return len(rest) <= 1
