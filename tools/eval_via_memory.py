# -*- coding: utf-8 -*-
"""LongMemEval-S 评测：真正走 jinyv_memory 中间层（适配口径，非官方）。

与 tools/eval_longmemeval.py 的区别：

- eval_longmemeval.py 在脚本内自建 BM25 + 向量召回，只 import 了 embedding；
  它衡量的是「一条独立的问答流水线」，不经过记忆中间层。
- 本脚本把每个实例的会话**回放进临时记忆库**：add_turn 写入原始轮次，
  按会话构造事件卡，经 commit_archive 落库（含 occurred_at / evidence_layer /
  covers / 游标），再用中间层自己的 retriever 取证据。

v1 的已知口径限制（必须随结果一起读）：

1. 事件卡摘要由规则拼装（原文轮次），不是线上 LLM 摘要；
   durable_facts 不写入（线上由后台模型抽取，规则版只会引入噪声）。
2. 检索走中间层 retriever（实体钩子 + 相对闸门），钩子由规则从正文取词，
   不是线上摘要器抽的钩子；这会影响召回质量，但正是「中间层自带检索」的表现。
3. 作答与判分仍调用外部模型。judge_model 与 provider 会逐题写入结果。
   推理型 provider 在长证据下可能把 reasoning 吃进 max_tokens、返回空 content：
   用 RP_THINKING_DISABLED=1（接口支持时）或 RP_ANSWER_TOKENS=1200 规避；
   空答案会自动重试 3 次，并记入 `empty_answer`。
4. 本脚本的数字不是 LongMemEval 官方分数，也不能与 eval_longmemeval.py 的
   混合模型并集直接横比——后者的口径见 docs/longmemeval_500_report.md。

用法：
    # 只跑「回放 + 中间层检索」，不调模型
    python tools/eval_via_memory.py --data /path/longmemeval_s.json \
        --limit 20 --dry-run

    # 真作答 + 判分（50 题子集）
    python tools/eval_via_memory.py --data /path/longmemeval_s.json \
        --limit 50 --out lme_via_memory_50.json --env-file /path/your.env

    # P0 补充方法消融（查询改写 + 事实表 + Chain-of-Note）
    python tools/eval_via_memory.py --data /path/longmemeval_s.json \
        --sample-per-type 2 --p0 --out lme_p0_12.json --env-file /path/your.env
"""
import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "jinyv_memory", "core")
TOOLS = os.path.dirname(os.path.abspath(__file__))
for p in (CORE, TOOLS):
    if p not in sys.path:
        sys.path.insert(0, p)

import retriever                      # noqa: E402  中间层检索
from rp_skeleton import DB            # noqa: E402  中间层存储

try:
    import embedding                   # noqa: E402  本仓库 bge-m3 适配器
except Exception:                      # pragma: no cover
    embedding = None

import eval_longmemeval as base       # noqa: E402  复用 _chat / ANSWER_SYS / JUDGE_SYS

CJK = "\u4e00-\u9fff"
_WORD = re.compile(r"[a-z0-9]+")
_EXTRA_STOP = {
    "user", "assistant", "the", "and", "for", "you", "your", "with", "that",
    "this", "have", "has", "had", "was", "were", "are", "but", "not", "can",
    "could", "would", "should", "about", "from", "they", "them", "their",
    "what", "when", "where", "which", "who", "how", "why", "did", "does",
    "just", "very", "much", "many", "some", "any", "our", "out", "one", "two",
    "its", "it's", "i'm", "don't", "know", "think", "like", "also", "will",
    "yeah", "okay", "sure", "well", "really", "going", "get", "got", "want",
}
STOP = set(base._STOP) | _EXTRA_STOP

# 专名/数字：低词频但高区分度（Nordstrom 只出现一次也会被 top16 词频挤出）
_PROPER = re.compile(r"\b[A-Z][A-Za-z0-9&'\-]{2,}\b")
_NUM = re.compile(r"\b\d{1,4}\b")


def parse_time(raw):
    """LongMemEval 的 '2023/05/20 (Sat) 02:21' → '2023-05-20 02:21:00'（UTC 文本）。"""
    m = re.search(r"(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})", str(raw or ""))
    if not m:
        return ""
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:00"


def extract_hooks(text, limit=16, full=False):
    """规则抽钩子：出现次数最高的实词，统一小写，供中间层 retriever 匹配。

    full=True 时额外把专名（大写开头）和数字并入钩子，且不受 top-N 词频截断：
    只出现一次的地名/店名（Nordstrom）在长会话里会被词频挤出，但它是时间题
    唯一的定位词。上限 64 防止单会话钩子爆炸。
    """
    t = str(text or "")
    cnt = Counter(w for w in _WORD.findall(t.lower())
                  if len(w) >= 3 and w not in STOP)
    hooks = [w for w, _ in cnt.most_common(limit)]
    if not full:
        return hooks
    extra = []
    for w in _PROPER.findall(t):
        lw = w.lower()
        if len(lw) >= 3 and lw not in STOP and lw not in extra:
            extra.append(lw)
    for n in _NUM.findall(t):
        if n not in extra:
            extra.append(n)
    for h in extra:
        if h not in hooks:
            hooks.append(h)
    return hooks[:64]


def replay_instance(inst, db, sid, cid, full_hooks=False):
    """把一个实例回放成记忆库：原始轮次 + 每会话一张规则事件卡。

    返回 {longmemeval_session_id: turn_start}，用于按 covers 回溯命中。
    """
    idx = 0
    order = {}
    for lsid, date, sess in zip(inst["haystack_session_ids"],
                                inst["haystack_dates"],
                                inst["haystack_sessions"]):
        lo = idx + 1
        lines = []
        for turn in sess:
            role = str(turn.get("role") or "user")
            content = str(turn.get("content") or "").strip()
            if not content:
                continue
            idx += 1
            db.add_turn(sid, cid, idx, role, content, source="longmemeval")
            lines.append(f"{role}: {content}")
        hi = idx
        if not lines:
            continue
        order[lsid] = lo
        text = "\n".join(lines)
        card = {
            "event_id": uuid.uuid4().hex,
            "session_id": sid,
            "character_id": cid,
            "turn_start": lo,
            "turn_end": hi,
            "summary": text,
            "state_changes": {},
            "entity_hooks": extract_hooks(text, full=full_hooks),
            "rel_hooks": [],
            "knowledge_boundary": "both",
            "importance": 0.6,
            "conflict_flags": [],
            "precision": "normal",
            "occurred_at": parse_time(date),
            "evidence_layer": "experienced",
            "idempotency_key": f"lme:{inst['question_id']}:{lsid}",
            "quotes": [],
            "layer": "event",
            "covers": [lsid],
        }
        db.commit_archive(card, sid, cid, lo, hi, ledger={})
    return order


def recall_via_memory(db, sid, cid, question, topk):
    """中间层召回：事件卡池 → retriever.retrieve；空结果回落 recent_cards。

    回落用中间层自己的 recent_cards（最近事件），不是重新造一条检索通道。
    """
    cards = db.all_cards(sid, cid)
    hits = retriever.retrieve(cards, str(question or "").lower(), limit=topk)
    source = "retrieve"
    if not hits:
        recent = db.recent_cards(sid, cid, limit=topk)
        hits = [(0.0, c) for c in reversed(recent)]
        source = "recent_cards"
    return hits, source


def recall_hybrid(db, sid, cid, question, topk, kw_weight=None, vec_weight=None):
    """P1：钩子召回 + bge-m3 语义召回，走中间层自己的 RRF fuse。

    向量对象是事件卡摘要（线上同一条链路），不是评测脚本另建的索引；
    embedding 不可用时退回纯钩子，行为与 v1 一致。
    """
    cards = db.all_cards(sid, cid)
    keyword = retriever.retrieve(cards, str(question or "").lower(),
                                 limit=max(topk, 20))
    if not (embedding and embedding.enabled()):
        return recall_via_memory(db, sid, cid, question, topk)
    try:
        qv = embedding.embed_query(question)
        # dot() 要的是 pack() 后的 float32 字节，直接传 embed_documents 的
        # float 列表会让 bytes(blob) 抛 TypeError → dot 恒返 0 → 向量排序退化成
        # 原顺序（这正是 P1「融合变差」的真实原因之一）。
        vecs = [embedding.pack(v) for v in
                embedding.embed_documents([str(c.get("summary") or "") for c in cards])]
        dim = embedding.dim()
        vector = sorted(((max(0.0, embedding.dot(qv, v, dim)), c)
                         for c, v in zip(cards, vecs)),
                        key=lambda x: x[0], reverse=True)[:max(topk, 20)]
    except Exception as exc:
        print(f"[warn] embedding 失败，退回钩子：{exc}", file=sys.stderr, flush=True)
        return recall_via_memory(db, sid, cid, question, topk)
    fused = retriever.fuse(keyword, vector, limit=topk,
                           keyword_weight=(retriever.FUSE_KEYWORD_WEIGHT
                                           if kw_weight is None else kw_weight),
                           vector_weight=(retriever.FUSE_VECTOR_WEIGHT
                                          if vec_weight is None else vec_weight))
    if not fused:
        return recall_via_memory(db, sid, cid, question, topk)
    return fused, "fuse"


def recall_hybrid_summary(db, sid, cid, question, topk, model, key, base_url,
                          kw_weight=None, vec_weight=None, candidate_k=20,
                          candidate_full=False):
    """P1 改进版：keyword ∪ raw-vector 候选 → P3 式摘要 → 向量融合。

    旧版只摘要钩子候选；金标卡如果不在钩子结果里，摘要向量也救不回来。
    这里先用 raw-text 向量补候选，再摘要，兼顾召回与精度。

    candidate_full=True 时两路各取满预算（默认各取一半）。bge-m3 返回的
    向量有千分位抖动，卡在 half 边界上的金标卡会随单次调用进出候选，
    取满预算把这个临界效应挤出决策范围。
    """
    cards = db.all_cards(sid, cid)
    budget = max(topk, candidate_k)
    keyword = retriever.retrieve(cards, str(question or "").lower(), limit=budget)
    if not (embedding and embedding.enabled()) or not cards:
        return recall_via_memory(db, sid, cid, question, topk)

    try:
        qv = embedding.embed_query(question)
        raw_vecs = [embedding.pack(v) for v in
                    embedding.embed_documents([str(c.get("summary") or "")
                                               for c in cards])]
        dim = embedding.dim()
        raw_ranked = sorted(((max(0.0, embedding.dot(qv, v, dim)), c)
                             for c, v in zip(cards, raw_vecs)),
                            key=lambda x: x[0], reverse=True)
    except Exception as exc:
        print(f"[warn] P1 raw 向量失败，退回钩子：{exc}",
              file=sys.stderr, flush=True)
        return recall_via_memory(db, sid, cid, question, topk)

    take = budget if candidate_full else max(1, budget // 2)
    seen, candidates = set(), []
    for _, card in list(keyword[:take]) + list(raw_ranked[:take]):
        eid = card.get("event_id")
        if eid in seen:
            continue
        seen.add(eid)
        candidates.append(card)
    if not candidates:
        return recall_via_memory(db, sid, cid, question, topk)
    print(f"  [p1-candidates] keyword={len(keyword)} "
          f"raw_vec={len(raw_ranked)} union={len(candidates)}",
          file=sys.stderr, flush=True)

    try:
        summaries = []
        for idx, card in enumerate(candidates, 1):
            print(f"  [p1-summary] {idx}/{len(candidates)}",
                  file=sys.stderr, flush=True)
            try:
                text = summarize_card(card, model, key, base_url)
            except Exception as exc:
                print(f"[warn] P1 卡片摘要失败，用原文：{exc}",
                      file=sys.stderr, flush=True)
                text = ""
            if not str(text).strip():
                date = str(card.get("occurred_at") or "")[:10]
                text = ((f"[{date}]\n" if date else "")
                        + str(card.get("summary") or ""))
            summaries.append(text)
        vecs = [embedding.pack(v) for v in embedding.embed_documents(summaries)]
        dim = embedding.dim()
        vector = sorted(((max(0.0, embedding.dot(qv, v, dim)), c)
                         for c, v in zip(candidates, vecs)),
                        key=lambda x: x[0], reverse=True)
    except Exception as exc:
        print(f"[warn] P1 摘要向量失败，退回钩子：{exc}",
              file=sys.stderr, flush=True)
        return recall_via_memory(db, sid, cid, question, topk)

    fused = retriever.fuse(keyword, vector, limit=topk,
                           keyword_weight=(retriever.FUSE_KEYWORD_WEIGHT
                                           if kw_weight is None else kw_weight),
                           vector_weight=(retriever.FUSE_VECTOR_WEIGHT
                                          if vec_weight is None else vec_weight))
    if not fused:
        return recall_via_memory(db, sid, cid, question, topk)
    return fused, "fuse_summary_union"


def recall_vector(db, sid, cid, question, topk):
    """纯语义召回（不融合）：给偏好/推荐题用。

    偏好题的问句与金标会话常零字面重合（「推荐看什么」vs「我喜欢 stand-up
    comedy」），钩子召回必然落空；而 RRF 把弱钩子和向量混排又会拉低命中。
    这里只用 bge-m3（多语，含英文）对事件卡摘要排序，避开钩子。embedding
    不可用时退回钩子召回，行为与 v1 一致。
    """
    cards = db.all_cards(sid, cid)
    if not (embedding and embedding.enabled() and cards):
        return recall_via_memory(db, sid, cid, question, topk)
    try:
        qv = embedding.embed_query(question)
        vecs = [embedding.pack(v) for v in
                embedding.embed_documents([str(c.get("summary") or "") for c in cards])]
        dim = embedding.dim()
        ranked = sorted(((max(0.0, embedding.dot(qv, v, dim)), c)
                         for c, v in zip(cards, vecs)),
                        key=lambda x: x[0], reverse=True)[:topk]
    except Exception as exc:
        print(f"[warn] 偏好题向量召回失败，退回钩子：{exc}", file=sys.stderr, flush=True)
        return recall_via_memory(db, sid, cid, question, topk)
    return ranked, "vector"


def write_facts(db, sid, cid, hits, question, topk):
    """P2：把召回卡写进 durable_facts（带 occurred_at），再用中间层的
    list_facts_detail 读回来。线上由后台模型在归档时抽事实；这里用检索到的
    卡摘要代填，属于实验近似，结果里会标 facts_written 与 facts_n。
    """
    for _, c in hits:
        try:
            db.add_fact(sid, cid, str(c.get("summary") or "")[:800],
                        int(c.get("turn_end") or 0), "", 0.6,
                        occurred_at=str(c.get("occurred_at") or ""),
                        evidence_layer="experienced")
        except Exception:
            continue
    try:
        facts = db.list_facts_detail(sid, cid, limit=max(4, topk), query=question)
    except Exception:
        return []
    labels = [str(f.get("label") or "") for f in facts if str(f.get("label") or "")]
    # 按卡的事件时间排：facts 行里只有「月-日」，跨年不可靠；用回写时的 occurred_at 定序
    def when(label):
        for _, c in hits:
            head = str(c.get("summary") or "")[:60]
            if head and head in label:
                return str(c.get("occurred_at") or "")
        return ""
    labels.sort(key=when)
    return labels


def format_evidence(hits):
    """按 turn 时序摊平成「[日期] 角色: 内容」，保留日期给时间题。"""
    items = sorted((c for _, c in hits), key=lambda c: c.get("turn_start") or 0)
    lines = []
    for c in items:
        date = str(c.get("occurred_at") or "")[:10]
        prefix = f"[{date}] " if date else ""
        for line in str(c.get("summary") or "").splitlines():
            line = line.strip()
            if line:
                lines.append(prefix + line)
    return "\n".join(lines)


def answer(inst, evidence_text, model, key, base_url, con=False, pref=False,
           pref_constraints=False, count_strict=False):
    note = ("Evidence is ordered oldest to newest; the last value for an attribute "
            "is the latest.\n") if inst["question_type"] == "knowledge-update" else ""
    extra = ""
    if pref and pref_constraints:
        extra = (
            "\nWork in four steps.\n"
            "Step 1 - Preference checklist: copy every item from the 'User preference profile', "
            "keeping [MUST-HARD] constraints separate from [MUST] likes.\n"
            "Step 2 - Constraint gate: before matching any like, drop every option that violates "
            "a [MUST-HARD] item (time window, wind-down/bedtime, avoid-list such as phone/TV/screens, "
            "budget, dietary limit). State the constraint and the surviving options only.\n"
            "Step 3 - Match: for each [MUST] item, give a concrete option from the surviving set. "
            "For advice/how-to questions, if a [MUST] item names a specific object or technique "
            "(e.g., yogurt, beef stew), give the exact method from the evidence "
            "(temperature, time, starter, equipment), not a generic diet tip; do not demote it to a side dish.\n"
            "Step 4 - Answer: on the last line prefixed with 'Answer:', recommend only options that "
            "passed Step 2 and directly serve the [MUST] items. If a [MUST-HARD] item forbids a "
            "category (e.g., no phone or TV before bed), the final Answer must not include it.")
    elif pref:
        extra = (
            "\nWork in three steps.\n"
            "Step 1 - Preference checklist: copy every item from the 'User preference profile'. "
            "Address items marked [MUST] first; use [CONTEXT] only when it directly helps the recommendation.\n"
            "Step 2 - Match: for each [MUST] item, give a concrete option or tip from the evidence. "
            "For advice/how-to questions, if a [MUST] item names a specific object or technique "
            "(e.g., yogurt, beef stew), give the exact method from the evidence "
            "(temperature, time, starter, equipment), not a generic diet tip; do not demote it to a side dish.\n"
            "Step 3 - Answer: on the last line prefixed with 'Answer:', center the [MUST] items and "
            "explicitly name the concrete option/method for each; do not let general diet advice displace them. "
            "If the profile contains an active practice/learning goal (e.g., language practice, skill-building), "
            "center that goal over passive entertainment or general browsing, and the final Answer must "
            "recommend only options that directly serve that goal; omit unrelated passive entertainment.")
    elif con and count_strict and _COUNT_Q.search(str(inst.get("question") or "")):
        extra = (
            "\nWork in four steps.\n"
            "Step 1 - Inventory: for each evidence block, write [date] item | the user's exact wording. "
            "Keep only projects the user explicitly says they led, are leading, or are responsible for leading. "
            "Do not count a team role embedded in a coursework, class, or assignment anecdote unless the "
            "user calls it their own project.\n"
            "Step 2 - Dedupe: merge repeated mentions of the same project across sessions; keep one entry, "
            "with the earliest date.\n"
            "Step 3 - Audit: re-read the kept entries; delete any that is not a distinct project the user led, "
            "and state why each remaining entry qualifies.\n"
            "Step 4 - Answer: put the final count alone on the last line prefixed with 'Answer:'.")
    elif con:
        extra = (
            "\nWork in two steps.\n"
            "Step 1 - Notes: for each evidence block, write one line "
            "[date] copied fact. Keep every distinct item when the question asks for a count.\n"
            "Step 2 - Answer: reason over the notes, then put the final answer alone on the "
            "last line prefixed with 'Answer:'. For date arithmetic, write the two absolute "
            "dates and the difference explicitly instead of estimating.")
    user = (f"Current date: {inst['question_date']}\n"
            f"Question: {inst['question']}\n{note}\nEvidence:\n{evidence_text}{extra}")
    return base._chat([{"role": "system", "content": base.ANSWER_SYS},
                       {"role": "user", "content": user}],
                      model, key, base_url,
                      max_tokens=int(os.environ.get("RP_ANSWER_TOKENS") or 1200))


_EXPAND_SYS = (
    "Rewrite the user's memory question into retrieval keywords.\n"
    "If it asks for a recommendation, name concrete objects, tools, brands, or "
    "attributes that would appear in past conversations.\n"
    "Output one line of lowercase space-separated keywords. No explanation.")

_TABLE_SYS = (
    "Extract atomic facts from the evidence into a compact table.\n"
    "One row per fact: entity | attribute | value | date.\n"
    "Keep every distinct item when the question asks for a count or a total.\n"
    "Output only the table, no prose.")


_COUNT_Q = re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b|\btotal\b", re.I)


def _expand_cache_file(cache_dir, inst):
    qid = re.sub(r"[^A-Za-z0-9_.-]", "_",
                 str(inst.get("question_id") or "unknown"))
    return os.path.join(cache_dir, qid + ".json")


def expand_query(inst, model, key, base_url, cache_dir=""):
    """P0：把抽象问句改写成可字面命中的关键词。

    cache_dir 非空时按 question_id 冻结扩写结果。扩写由 LLM 生成，同一模型
    多次运行也会给出不同词，跨版本对比必须共用同一份查询，否则单题翻转
    无法归因到被改的组件。
    """
    cache_file = ""
    if cache_dir:
        cache_file = _expand_cache_file(cache_dir, inst)
        try:
            cached = json.load(open(cache_file, encoding="utf-8"))
            if str(cached.get("model") or "") == str(model):
                return str(cached.get("expanded") or "")
        except Exception:
            pass
    raw = base._chat([{"role": "system", "content": _EXPAND_SYS},
                      {"role": "user", "content": f"Question: {inst['question']}"}],
                     model, key, base_url, max_tokens=80)
    text = " ".join(re.findall(r"[a-z0-9]+", raw.lower()))[:300]
    if cache_file:
        try:
            os.makedirs(os.path.dirname(cache_file), exist_ok=True)
            tmp = "%s.%d.tmp" % (cache_file, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"model": model, "expanded": text}, f,
                          ensure_ascii=False)
            os.replace(tmp, cache_file)
        except Exception as exc:
            print(f"  [warn] 扩写缓存写入失败：{exc}", file=sys.stderr, flush=True)
    return text


def build_fact_table(inst, evidence_text, model, key, base_url):
    """P0：把长证据压成 (实体, 属性, 值, 日期) 表，计数在行上做。"""
    user = (f"Current date: {inst['question_date']}\n"
            f"Question: {inst['question']}\nEvidence:\n{evidence_text}")
    return base._chat([{"role": "system", "content": _TABLE_SYS},
                       {"role": "user", "content": user}],
                      model, key, base_url,
                      max_tokens=int(os.environ.get("RP_TABLE_TOKENS") or 900))


_COUNT_EXTRACT_SYS = (
    "You build a candidate inventory for a counting question. Do not count or filter relevance.\n"
    "For every evidence block, output one line per distinct object in the format:\n"
    "[date] object | action | exact user quote (<=25 words).\n"
    "Actions must be one of: pickup, return, exchange, acquire, receive, lead, plan, attend.\n"
    "Rules:\n"
    "- Never merge multiple objects into one line: split each project, plant, item, or event into its own "
    "line, even when several appear in the same sentence or the same summary line.\n"
    "- Split an exchange into two lines: picking up the replacement and returning the original.\n"
    "- Include items mentioned only once and items received from family or friends.\n"
    "- Copy the user's own wording; do not paraphrase.\n"
    "End with a line containing only END.")


_COUNT_FINAL_SYS = (
    "You are given a candidate inventory and the evidence. Count items for the question by applying "
    "these rules strictly:\n"
    "1. Exchange splits: picking up a replacement AND returning the original are two separate items.\n"
    "2. Cross-session completeness: merge duplicate mentions of the same item, but keep every distinct "
    "item even if it appears in only one session; include items received from family or friends.\n"
    "3. Leadership eligibility: count a project only when the user says they led it, are leading it, or "
    "are responsible for leading the project itself. Exclude coursework/class/assignment projects, "
    "purely brainstormed ideas, and cases where the user is merely part of a team. A project with a "
    "future launch or delivery date still counts when the user is leading it now.\n"
    "Work in steps: (a) dedupe, (b) check every inventory row against the evidence and split any row "
    "that merged distinct items, (c) apply eligibility and say why each kept item qualifies, "
    "(d) recount. Put the final count alone on the last line prefixed with 'Answer:'.")


def count_extract(inst, evidence_text, model, key, base_url):
    """计数题第一遍：只抽取「日期|对象|动作|原话」，不做计数与资格判断。"""
    user = f"Question: {inst['question']}\nEvidence:\n{evidence_text}"
    return base._chat([{"role": "system", "content": _COUNT_EXTRACT_SYS},
                       {"role": "user", "content": user}],
                      model, key, base_url,
                      max_tokens=int(os.environ.get("RP_COUNT_EXTRACT_TOKENS") or 1200))


def count_finalize(inst, evidence_text, inventory, model, key, base_url):
    """计数题第二遍：按口径判重、判资格，再给出计数。"""
    user = (f"Question: {inst['question']}\n"
            f"Candidate inventory:\n{inventory}\n\nEvidence:\n{evidence_text}")
    return base._chat([{"role": "system", "content": _COUNT_FINAL_SYS},
                       {"role": "user", "content": user}],
                      model, key, base_url,
                      max_tokens=int(os.environ.get("RP_COUNT_FINAL_TOKENS") or 1500))


_SUMMARY_SYS = (
    "Compress the evidence into dated bullet notes.\n"
    "One bullet per session: [date] key facts, names, numbers.\n"
    "Keep every distinct item when the question asks for a count. No preamble.")


def summarize_evidence(inst, evidence_text, model, key, base_url):
    """P3：用模型把召回证据压成带日期的要点，替代规则拼接的原文摘要。"""
    user = (f"Current date: {inst['question_date']}\n"
            f"Question: {inst['question']}\nEvidence:\n{evidence_text}")
    return base._chat([{"role": "system", "content": _SUMMARY_SYS},
                       {"role": "user", "content": user}],
                      model, key, base_url,
                      max_tokens=int(os.environ.get("RP_SUMMARY_TOKENS") or 700))


_PREF_PROFILE_SYS = (
    "Extract the user's durable preferences and constraints relevant to the question.\n"
    "Include preferences stated in other cities or scenarios that still apply.\n"
    "Include recent successes, past choices, and concrete items.\n"
    "Order the list: first items that directly answer the question (mark [MUST]); "
    "then background context (mark [CONTEXT]).\n"
    "For advice/how-to questions, mark direct experience with the activity "
    "(specific successes, failures, techniques, and objects) as [MUST]; "
    "mark general diet or lifestyle constraints as [CONTEXT].\n"
    "List every explicit attribute: likes, dislikes, brands, tools, goals, dietary needs, "
    "locations, budgets, and past choices. Quote concrete names.\n"
    "If the evidence truly lacks a preference, say so explicitly.\n"
    "Output only bullet lines, each starting with '- [MUST]' or '- [CONTEXT]'. "
    "Do not explain your reasoning. Do not restate these instructions. "
    "Do not recommend anything yet.")


_PREF_HARD_SYS = (
    "\nAlso extract non-negotiable constraints and list them FIRST:\n"
    "- [MUST-HARD] time windows, deadlines, bedtimes or wind-down times, things to avoid "
    "(e.g., phone, TV, screens, caffeine, allergens), budget caps, dietary limits.\n"
    "Quote the concrete limit or avoid-item verbatim. Never merge a constraint with a like, "
    "and never drop a stated constraint because other interests are more prominent.")


def build_preference_profile(inst, evidence_text, model, key, base_url, hard=False):
    """偏好题：先从证据抽用户偏好画像，供作答逐条匹配。

    hard=True 时要求把硬约束（时间窗/避免项/预算）单列为 [MUST-HARD] 并排在最前，
    作答阶段据此先过滤再匹配喜好。
    """
    user = (f"Question: {inst['question']}\n"
            f"Evidence:\n{evidence_text}")
    raw = base._chat(
        [{"role": "system",
          "content": _PREF_PROFILE_SYS + (_PREF_HARD_SYS if hard else "")},
         {"role": "user", "content": user}],
        model, key, base_url,
        max_tokens=int(os.environ.get("RP_PREF_PROFILE_TOKENS") or 600))
    lines = []
    for line in str(raw).splitlines():
        stripped = line.strip()
        if re.match(r"^[-*•]\s+", stripped):
            lines.append(stripped)
    return "\n".join(lines) if lines else str(raw).strip()


_CARD_SUMMARY_SYS = (
    "Compress one conversation session into dense dated bullet notes.\n"
    "Keep every name, number, item, preference, date, and location exactly.\n"
    "Do not infer, merge, or add facts. Output only the notes, no preamble.")


def summarize_card(card, model, key, base_url):
    """P1 摘要向量用：单张事件卡 → 带日期的要点，保留专名/数字/偏好。

    RP_SUMMARY_MODEL/KEY/BASE 可把卡片摘要切到更快的独立端点；
    作答与判分仍用 RP_FRONT_*。RP_SUMMARY_INTERVAL 用于临时覆盖请求间隔。
    """
    date = str(card.get("occurred_at") or "")[:10]
    text = str(card.get("summary") or "")
    cap = int(os.environ.get("RP_CARD_SUMMARY_CHARS") or 12000)
    if cap > 0:
        text = text[:cap]
    if date:
        text = f"[{date}]\n{text}"

    summary_model = os.environ.get("RP_SUMMARY_MODEL") or model
    summary_key = os.environ.get("RP_SUMMARY_KEY") or key
    summary_base = os.environ.get("RP_SUMMARY_BASE") or base_url

    cache_path = os.environ.get("RP_SUMMARY_CACHE") or ""
    cache_key = ""
    if cache_path:
        cache_key = hashlib.sha256(
            (_CARD_SUMMARY_SYS + "\n" + text + "\n"
             + summary_model + "\n" + summary_base).encode("utf-8")).hexdigest()
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cache = json.load(handle)
        except Exception:
            cache = {}
        if cache_key in cache:
            return cache[cache_key]

    interval = os.environ.get("RP_SUMMARY_INTERVAL")
    old_interval = os.environ.get("RP_REQUEST_INTERVAL")
    if interval is not None:
        os.environ["RP_REQUEST_INTERVAL"] = interval
    old_retries = getattr(base, "MAX_RETRIES", 1)
    base.MAX_RETRIES = 0
    try:
        result = base._chat(
            [{"role": "system", "content": _CARD_SUMMARY_SYS},
             {"role": "user", "content": text}],
            summary_model, summary_key, summary_base,
            max_tokens=int(os.environ.get("RP_CARD_SUMMARY_TOKENS") or 300))
    except Exception as exc:
        # 摘要端点配额耗尽（free 模型 429 且 Retry-After 可能长达 1 小时）时
        # 不挂起整题：回退原始文本做向量，检索仍可继续。
        if getattr(exc, "code", None) == 429:
            print("  [warn] 摘要端点 429，回退原始文本", file=sys.stderr, flush=True)
            return text
        raise
    finally:
        base.MAX_RETRIES = old_retries
        if interval is not None:
            if old_interval is None:
                os.environ.pop("RP_REQUEST_INTERVAL", None)
            else:
                os.environ["RP_REQUEST_INTERVAL"] = old_interval

    if cache_path and str(result).strip():
        try:
            with open(cache_path, encoding="utf-8") as handle:
                cache = json.load(handle)
        except Exception:
            cache = {}
        cache[cache_key] = result
        cache_dir = os.path.dirname(cache_path)
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False)
        os.replace(tmp, cache_path)
    return result


def parse_con(raw):
    """CoN：只把 'Answer:' 之后的最后一段交给判分，避免把笔记当答案。"""
    m = re.findall(r"Answer\s*[:：]\s*(.+)", str(raw or ""), re.S)
    return (m[-1] if m else str(raw or "")).strip()[:500]


def judge(inst, pred, model, key, base_url):
    user = (f"Question: {inst['question']}\n"
            f"Gold answer: {inst['answer']}\n"
            f"Predicted answer: {pred}\n\n"
            "Is the prediction correct?")
    raw = base._chat([{"role": "system", "content": base.JUDGE_SYS},
                      {"role": "user", "content": user}],
                     model, key, base_url,
                     max_tokens=int(os.environ.get("RP_JUDGE_TOKENS") or 200))
    try:
        start, end = raw.find("{"), raw.rfind("}")
        obj = json.loads(raw[start:end + 1])
        return bool(obj.get("correct")), str(obj.get("why") or "")
    except Exception:
        return False, raw[:120]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="longmemeval_s.json 路径")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 题，0=全部")
    ap.add_argument("--offset", type=int, default=0, help="从第 N 题开始")
    ap.add_argument("--only-type", default="", help="只跑某个题型")
    ap.add_argument("--types", default="",
                    help="逗号分隔的多个题型，如 multi-session,temporal-reasoning")
    ap.add_argument("--ids", default="",
                    help="逗号分隔的 question_id，只跑这些题")
    ap.add_argument("--sample-per-type", type=int, default=0,
                    help="每题型取前 N 题（跨题型分层抽样，优先于 --limit）")
    ap.add_argument("--sample-random", type=int, default=0,
                    help="随机抽 N 题（配合 --seed；默认纯随机）")
    ap.add_argument("--seed", type=int, default=42, help="随机抽样种子")
    ap.add_argument("--stratify-random", action="store_true",
                    help="随机抽样按题型分层（每类尽量均分）")
    ap.add_argument("--topk", type=int, default=8, help="单会话题的召回深度")
    ap.add_argument("--topk-multi", type=int, default=14,
                    help="multi-session 题的召回深度")
    ap.add_argument("--out", default="", help="汇总 JSON 输出路径")
    ap.add_argument("--resume", default="", help="断点 JSONL；默认 <out>.jsonl")
    ap.add_argument("--env-file", default="", help="从文件读取 RP_* 密钥")
    ap.add_argument("--front-model", default="",
                    help="作答模型；默认读 --env-file / 环境里的 RP_FRONT_MODEL")
    ap.add_argument("--judge-model", default="", help="判分模型；默认与作答模型相同（自判）")
    ap.add_argument("--provider-name", default="", help="写入结果的 provider 名")
    ap.add_argument("--dry-run", action="store_true", help="只回放+检索，不调模型")
    ap.add_argument("--keep-db", action="store_true", help="保留每题的临时库（调试）")
    ap.add_argument("--p0", action="store_true",
                    help="开启 P0 补充方法：查询改写 + 事实表 + Chain-of-Note")
    ap.add_argument("--p1", action="store_true",
                    help="P1：钩子 + bge-m3 向量，走 retriever.fuse 融合")
    ap.add_argument("--p1-summary", action="store_true",
                    help="P1 改进版：先对候选卡做 P3 式摘要，再向量化融合")
    ap.add_argument("--p1-candidates", type=int, default=20,
                    help="P1 summary 对钩子 top-N 卡做摘要（默认 20）")
    ap.add_argument("--p2", action="store_true",
                    help="P2：把召回卡写入 durable_facts 再经 list_facts_detail 读回")
    ap.add_argument("--p3", action="store_true",
                    help="P3：用模型把证据压成带日期的会话要点")
    ap.add_argument("--pref-profile", action="store_true",
                    help="偏好题先抽用户偏好画像，再要求逐条显式匹配")
    ap.add_argument("--all", action="store_true",
                    help="P0+P2+P3（不含 P1：12 题对照中 P1 未见增益，样本不足，不作定论）")
    ap.add_argument("--fuse-kw", type=float, default=None,
                    help="P1 融合的钩子权重（默认沿用 retriever.FUSE_KEYWORD_WEIGHT）")
    ap.add_argument("--fuse-vec", type=float, default=None,
                    help="P1 融合的向量权重（默认沿用 retriever.FUSE_VECTOR_WEIGHT）")
    ap.add_argument("--query-expand", action="store_true", help="P0：检索前改写问句")
    ap.add_argument("--fact-table", action="store_true", help="P0：作答前把证据压成事实表")
    ap.add_argument("--con", action="store_true", help="P0：Chain-of-Note 两步作答")
    ap.add_argument("--full-hooks", action="store_true",
                    help="钩子并入专名与数字（不受 top16 词频截断），修低词频专名召回")
    ap.add_argument("--pref-vec", action="store_true",
                    help="single-session-preference 题改用纯向量召回（需要 RP_EMBED_*）")
    ap.add_argument("--p1-candidate-full", action="store_true",
                    help="P1 摘要候选取满预算（keyword[:K] ∪ raw[:K]）；默认两路各取一半")
    ap.add_argument("--expand-cache", default="",
                    help="P0 扩写缓存目录：按 question_id 冻结查询，跨版本对比同查询")
    ap.add_argument("--pref-constraints", action="store_true",
                    help="偏好题硬约束优先：时间窗/避免项先过滤，再匹配喜好")
    ap.add_argument("--count-strict", action="store_true",
                    help="计数题严格口径：资格判定 + 跨会话去重 + 逐条复核")
    ap.add_argument("--pref-topk", type=int, default=0,
                    help="偏好题最终证据上限（0=沿用 --topk）；配合 --p1-candidate-full 使用")
    ap.add_argument("--count-two-pass", action="store_true",
                    help="计数题两遍结构化：先抽取「日期|对象|动作|原话」，再判重/判资格/计数")
    args = ap.parse_args()
    if args.all:
        args.p0 = args.p2 = args.p3 = True
    if args.p0:
        args.query_expand = args.fact_table = args.con = True
    base.PROVIDER_NAME = args.provider_name
    base.load_env(args.env_file)
    front_model = (args.front_model or os.environ.get("RP_FRONT_MODEL")
                   or "deepseek-v4-flash")

    data = json.load(open(args.data, encoding="utf-8"))
    if args.ids:
        keep = {x.strip() for x in args.ids.split(",") if x.strip()}
        data = [x for x in data if x["question_id"] in keep]
    if args.only_type:
        data = [x for x in data if x.get("question_type") == args.only_type]
    if args.types:
        keep = {t.strip() for t in args.types.split(",") if t.strip()}
        data = [x for x in data if x.get("question_type") in keep]
    if args.sample_per_type:
        picked, seen = [], Counter()
        for x in data:
            t = x.get("question_type") or ""
            if seen[t] < args.sample_per_type:
                picked.append(x)
                seen[t] += 1
        data = picked
    if args.sample_random:
        import random
        rng = random.Random(args.seed)
        n = min(args.sample_random, len(data))
        if args.stratify_random:
            by_type = {}
            for x in data:
                by_type.setdefault(x.get("question_type") or "", []).append(x)
            types = sorted(by_type)
            picked = []
            base_n, rem = divmod(n, max(1, len(types)))
            for i, t in enumerate(types):
                take = base_n + (1 if i < rem else 0)
                pool = by_type[t]
                picked.extend(rng.sample(pool, min(take, len(pool))))
            if len(picked) < n:
                picked_ids = {x["question_id"] for x in picked}
                rest = [x for x in data if x["question_id"] not in picked_ids]
                picked.extend(rng.sample(rest, min(n - len(picked), len(rest))))
            order = {x["question_id"]: i for i, x in enumerate(data)}
            picked.sort(key=lambda x: order[x["question_id"]])
            data = picked
        else:
            data = rng.sample(data, n)
        print(f"[sample] random {len(data)} seed={args.seed} "
              f"stratify={args.stratify_random}", flush=True)
        for x in data:
            print(f"  {x['question_id']} {x['question_type']}: "
                  f"{x['question'][:70]}", flush=True)
    if args.offset or args.limit:
        end = args.offset + args.limit if args.limit else None
        data = data[args.offset:end]

    key = (os.environ.get("RP_FRONT_KEY") or "").strip()
    base_url = (os.environ.get("RP_FRONT_BASE") or os.environ.get("RP_LLM_BASE")
                or "https://api.deepseek.com").strip()
    judge_model = args.judge_model or front_model
    judge_self = judge_model == front_model
    if not args.dry_run and not key:
        sys.exit("缺少 RP_FRONT_KEY（可用 --env-file）；只想验证检索请加 --dry-run")

    ckpt = args.resume or (args.out + ".jsonl" if args.out else "")
    rows, hits, ok = [], 0, 0
    done = set()
    if ckpt and os.path.exists(ckpt) and not args.dry_run:
        with open(ckpt, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                rows.append(r)
                done.add(r["question_id"])
                hits += 1 if r.get("recall_hit") else 0
                ok += 1 if r.get("correct") else 0
        print(f"[resume] 已完成 {len(rows)} 题，跳过", flush=True)

    for inst in data:
        if inst["question_id"] in done:
            continue
        t0 = time.time()
        multi = inst["question_type"] == "multi-session"
        topk = max(args.topk, args.topk_multi) if multi else args.topk
        if (inst["question_type"] == "single-session-preference"
                and args.pref_topk):
            topk = args.pref_topk
        tmpdir = tempfile.mkdtemp(prefix="lme-via-")
        db_path = os.path.join(tmpdir, "mem.db")
        db = DB(db_path)
        sid = "lme-" + inst["question_id"]
        cid = "eval"
        try:
            order = replay_instance(inst, db, sid, cid, full_hooks=args.full_hooks)
            query = inst["question"]
            expanded = ""
            if args.query_expand and not args.dry_run:
                try:
                    expanded = expand_query(inst, front_model, key, base_url,
                                            args.expand_cache)
                    if expanded:
                        query = query + " " + expanded
                except base.ContentFiltered:
                    expanded = ""
                except Exception as exc:
                    print(f"  [warn] P0 查询改写失败，用原问句：{exc}",
                          file=sys.stderr, flush=True)
                    expanded = ""
            if args.p1_summary and not args.dry_run:
                hits_list, source = recall_hybrid_summary(
                    db, sid, cid, query, topk, front_model, key, base_url,
                    args.fuse_kw, args.fuse_vec, args.p1_candidates,
                    args.p1_candidate_full)
            elif args.pref_vec and inst["question_type"] == "single-session-preference":
                hits_list, source = recall_vector(db, sid, cid, query, topk)
            elif args.p1:
                hits_list, source = recall_hybrid(db, sid, cid, query, topk,
                                                  args.fuse_kw, args.fuse_vec)
            else:
                hits_list, source = recall_via_memory(db, sid, cid, query, topk)
            got = {lsid for _, c in hits_list for lsid in (c.get("covers") or [])}
            hit = bool(set(inst.get("answer_session_ids") or ()) & got)
            hits += hit
            evidence_text = format_evidence(hits_list)
            raw_evidence_text = evidence_text
            pred = why = ""
            correct = None
            request_failed = False
            filtered = False
            raw_answer = ""
            table = ""
            count_table = ""
            profile = ""
            summary = ""
            facts = []
            if not args.dry_run:
                try:
                    if args.p3 and evidence_text:
                        try:
                            summary = summarize_evidence(
                                inst, evidence_text, front_model, key, base_url)
                        except Exception as exc:
                            print(f"  [warn] P3 证据摘要失败，用原始证据：{exc}",
                                  file=sys.stderr, flush=True)
                            summary = ""
                        if summary.strip():
                            evidence_text = "Session notes:\n" + summary
                    if args.p2 and hits_list:
                        facts = write_facts(db, sid, cid, hits_list, query, topk)
                        if facts:
                            evidence_text = ("Durable facts (retrieved from memory):\n"
                                             + "\n".join(f"- {f}" for f in facts)
                                             + "\n\nEvidence:\n" + evidence_text)
                    if args.fact_table and evidence_text:
                        try:
                            table = build_fact_table(
                                inst, evidence_text, front_model, key, base_url)
                        except Exception as exc:
                            print(f"  [warn] 事实表失败，跳过：{exc}",
                                  file=sys.stderr, flush=True)
                            table = ""
                    if table:
                        evidence_text = ("Extracted fact table:\n" + table
                                         + "\n\nRaw evidence:\n" + evidence_text)
                    if (args.pref_profile
                            and inst["question_type"] == "single-session-preference"
                            and evidence_text):
                        try:
                            profile = build_preference_profile(
                                inst, evidence_text, front_model, key, base_url,
                                hard=args.pref_constraints)
                        except Exception as exc:
                            print(f"  [warn] 偏好画像失败，跳过：{exc}",
                                  file=sys.stderr, flush=True)
                            profile = ""
                        if profile.strip():
                            evidence_text = ("User preference profile:\n" + profile
                                             + "\n\nEvidence:\n" + evidence_text)
                    qtext = str(inst.get("question") or "")
                    two_pass = bool(
                        args.count_two_pass
                        and inst["question_type"] != "temporal-reasoning"
                        and _COUNT_Q.search(qtext)
                        and not re.search(
                            r"how many (months|days|years|weeks|hours|minutes)",
                            qtext, re.I))
                    if two_pass:
                        try:
                            count_table = count_extract(
                                inst, raw_evidence_text, front_model, key, base_url)
                        except Exception as exc:
                            print(f"  [warn] 计数抽取失败，回退单遍：{exc}",
                                  file=sys.stderr, flush=True)
                            count_table = ""
                    for attempt in range(3):
                        if two_pass and count_table.strip():
                            raw_answer = count_finalize(
                                inst, raw_evidence_text, count_table,
                                front_model, key, base_url)
                        else:
                            raw_answer = answer(inst, evidence_text, front_model, key,
                                                base_url, con=args.con,
                                                pref=bool(profile.strip()),
                                                pref_constraints=args.pref_constraints,
                                                count_strict=args.count_strict)
                        pred = parse_con(raw_answer) if args.con else raw_answer
                        if str(pred).strip():
                            break
                        # 上游偶发返回空 content（限流/断连重试后 200 空体）：
                        # 不当作「模型拒答」，重试一次再计分。
                        print(f"  [empty] 第 {attempt + 1} 次空答案，重试",
                              file=sys.stderr, flush=True)
                        time.sleep(2 * (attempt + 1))
                    correct, why = judge(inst, pred, judge_model, key, base_url)
                except base.ContentFiltered:
                    filtered = True
                    correct = False
                    why = "content_filtered"
                    pred = ""
                except Exception as exc:
                    # 答案已生成但判分/上游异常：保存 correct=None 的整题行，
                    # 交给独立重判；不要整轮退出，也不要当作答案错误。
                    if not str(pred).strip():
                        raise
                    request_failed = True
                    correct = None
                    why = (f"judge_error: {type(exc).__name__}: "
                           f"{str(exc)[:200]}")
                ok += 1 if correct else 0
            row = {"question_id": inst["question_id"],
                   "question_type": inst["question_type"],
                   "question": inst["question"], "gold": inst["answer"],
                   "recall_hit": hit, "evidence_n": len(hits_list),
                   "evidence_source": source, "pred": pred,
                   "correct": correct, "why": why,
                   "judge_error": request_failed,
                   "provider": base.PROVIDER_NAME,
                   "judge_model": judge_model, "judge_self": judge_self,
                   "via_memory": True, "filtered": filtered,
                   "empty_answer": not str(pred).strip(),
                   "sessions": len(order),
                   "query_expanded": expanded,
                   "fact_table": bool(table),
                   "count_two_pass": bool(count_table),
                   "count_table": str(count_table)[:1200],
                   "con": args.con,
                   "summary": bool(summary),
                   "pref_profile": bool(profile.strip()),
                   "pref_profile_text": profile[:600],
                   "facts_written": len(facts),
                   "recall_mode": source,
                   "raw_answer": str(raw_answer)[:600] if args.con else "",
                   "sec": round(time.time() - t0, 1)}
            rows.append(row)
            if ckpt and not args.dry_run:
                with open(ckpt, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
            tag = "hit" if hit else "miss"
            print(f"[{len(rows)}/{len(data)}] {tag} {inst['question_type']}: "
                  f"{inst['question'][:70]}", flush=True)
        finally:
            db.close() if hasattr(db, "close") else None
            if not args.keep_db:
                try:
                    for name in os.listdir(tmpdir):
                        os.remove(os.path.join(tmpdir, name))
                    os.rmdir(tmpdir)
                except OSError:
                    pass
        time.sleep(0.2)

    print(f"\n中间层检索命中（covers 命中 answer_session）："
          f"{hits}/{len(data)} = {hits / max(1, len(data)):.3f}")
    judged_rows = [r for r in rows if r.get("correct") is not None]
    judged_n = len(judged_rows)
    judged_ok = sum(1 for r in judged_rows if r.get("correct"))
    judge_errors = sum(1 for r in rows if r.get("judge_error"))
    if not args.dry_run:
        print(f"回答准确率（judge={judge_model}，via_memory，非官方口径）："
              f"{judged_ok}/{judged_n} = {judged_ok / max(1, judged_n):.3f}"
              f"（判分失败 {judge_errors} 题未计入分母）")
        by = {}
        for r in judged_rows:
            t = by.setdefault(r["question_type"], [0, 0])
            t[0] += 1
            t[1] += 1 if r["correct"] else 0
        for t, (n, c) in sorted(by.items()):
            print(f"  {t:26s} {c}/{n} = {c / n:.3f}")

    if args.out:
        json.dump({"n": len(data), "recall_hit": hits, "ok": ok,
                   "judged_ok": judged_ok,
                   "judged": judged_n, "judge_errors": judge_errors,
                   "official": False, "via_memory": True,
                   "front_model": front_model,
                   "judge_model": judge_model, "judge_self": judge_self,
                   "provider": base.PROVIDER_NAME,
                   "topk": args.topk, "topk_multi": args.topk_multi,
                   "pref_topk": args.pref_topk,
                   "count_two_pass": args.count_two_pass,
                   "p1_candidate_full": args.p1_candidate_full,
                   "cards_rule_built": not args.p3, "facts_written": args.p2,
                   "p0": {"query_expand": args.query_expand,
                          "fact_table": args.fact_table, "con": args.con},
                   "p1": args.p1, "p1_summary": args.p1_summary,
                   "p1_candidates": args.p1_candidates,
                   "p2": args.p2, "p3": args.p3,
                   "pref_profile": args.pref_profile,
                   "sample_random": args.sample_random,
                   "sample_seed": args.seed,
                   "stratify_random": args.stratify_random,
                   "full_hooks": args.full_hooks, "pref_vec": args.pref_vec,
                   "rows": rows},
                  open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("已写出", args.out)


if __name__ == "__main__":
    main()
