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


def parse_time(raw):
    """LongMemEval 的 '2023/05/20 (Sat) 02:21' → '2023-05-20 02:21:00'（UTC 文本）。"""
    m = re.search(r"(\d{4})/(\d{2})/(\d{2}).*?(\d{2}):(\d{2})", str(raw or ""))
    if not m:
        return ""
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:00"


def extract_hooks(text, limit=16):
    """规则抽钩子：出现次数最高的实词，统一小写，供中间层 retriever 匹配。"""
    cnt = Counter(w for w in _WORD.findall(str(text or "").lower())
                  if len(w) >= 3 and w not in STOP)
    return [w for w, _ in cnt.most_common(limit)]


def replay_instance(inst, db, sid, cid):
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
            "entity_hooks": extract_hooks(text),
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


def answer(inst, evidence_text, model, key, base_url, con=False):
    note = ("Evidence is ordered oldest to newest; the last value for an attribute "
            "is the latest.\n") if inst["question_type"] == "knowledge-update" else ""
    extra = ""
    if con:
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


def expand_query(inst, model, key, base_url):
    """P0：把抽象问句改写成可字面命中的关键词。"""
    raw = base._chat([{"role": "system", "content": _EXPAND_SYS},
                      {"role": "user", "content": f"Question: {inst['question']}"}],
                     model, key, base_url, max_tokens=80)
    return " ".join(re.findall(r"[a-z0-9]+", raw.lower()))[:300]


def build_fact_table(inst, evidence_text, model, key, base_url):
    """P0：把长证据压成 (实体, 属性, 值, 日期) 表，计数在行上做。"""
    user = (f"Current date: {inst['question_date']}\n"
            f"Question: {inst['question']}\nEvidence:\n{evidence_text}")
    return base._chat([{"role": "system", "content": _TABLE_SYS},
                       {"role": "user", "content": user}],
                      model, key, base_url,
                      max_tokens=int(os.environ.get("RP_TABLE_TOKENS") or 900))


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
    ap.add_argument("--sample-per-type", type=int, default=0,
                    help="每题型取前 N 题（跨题型分层抽样，优先于 --limit）")
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
    ap.add_argument("--query-expand", action="store_true", help="P0：检索前改写问句")
    ap.add_argument("--fact-table", action="store_true", help="P0：作答前把证据压成事实表")
    ap.add_argument("--con", action="store_true", help="P0：Chain-of-Note 两步作答")
    args = ap.parse_args()
    if args.p0:
        args.query_expand = args.fact_table = args.con = True
    base.PROVIDER_NAME = args.provider_name
    base.load_env(args.env_file)
    front_model = (args.front_model or os.environ.get("RP_FRONT_MODEL")
                   or "deepseek-v4-flash")

    data = json.load(open(args.data, encoding="utf-8"))
    if args.only_type:
        data = [x for x in data if x.get("question_type") == args.only_type]
    if args.sample_per_type:
        picked, seen = [], Counter()
        for x in data:
            t = x.get("question_type") or ""
            if seen[t] < args.sample_per_type:
                picked.append(x)
                seen[t] += 1
        data = picked
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
        tmpdir = tempfile.mkdtemp(prefix="lme-via-")
        db_path = os.path.join(tmpdir, "mem.db")
        db = DB(db_path)
        sid = "lme-" + inst["question_id"]
        cid = "eval"
        try:
            order = replay_instance(inst, db, sid, cid)
            query = inst["question"]
            expanded = ""
            if args.query_expand and not args.dry_run:
                try:
                    expanded = expand_query(inst, front_model, key, base_url)
                    if expanded:
                        query = query + " " + expanded
                except base.ContentFiltered:
                    expanded = ""
            hits_list, source = recall_via_memory(db, sid, cid, query, topk)
            got = {lsid for _, c in hits_list for lsid in (c.get("covers") or [])}
            hit = bool(set(inst.get("answer_session_ids") or ()) & got)
            hits += hit
            evidence_text = format_evidence(hits_list)
            pred = why = ""
            correct = None
            filtered = False
            raw_answer = ""
            table = ""
            if not args.dry_run:
                try:
                    if args.fact_table and evidence_text:
                        table = build_fact_table(inst, evidence_text,
                                                 front_model, key, base_url)
                    if table:
                        evidence_text = ("Extracted fact table:\n" + table
                                         + "\n\nRaw evidence:\n" + evidence_text)
                    for attempt in range(3):
                        raw_answer = answer(inst, evidence_text, front_model, key,
                                            base_url, con=args.con)
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
                ok += 1 if correct else 0
            row = {"question_id": inst["question_id"],
                   "question_type": inst["question_type"],
                   "question": inst["question"], "gold": inst["answer"],
                   "recall_hit": hit, "evidence_n": len(hits_list),
                   "evidence_source": source, "pred": pred,
                   "correct": correct, "why": why,
                   "provider": base.PROVIDER_NAME,
                   "judge_model": judge_model, "judge_self": judge_self,
                   "via_memory": True, "filtered": filtered,
                   "empty_answer": not str(pred).strip(),
                   "sessions": len(order),
                   "query_expanded": expanded,
                   "fact_table": bool(table),
                   "con": args.con,
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
    if not args.dry_run:
        print(f"回答准确率（judge={judge_model}，via_memory，非官方口径）："
              f"{ok}/{len(data)} = {ok / max(1, len(data)):.3f}")
        by = {}
        for r in rows:
            t = by.setdefault(r["question_type"], [0, 0])
            t[0] += 1
            t[1] += 1 if r["correct"] else 0
        for t, (n, c) in sorted(by.items()):
            print(f"  {t:26s} {c}/{n} = {c / n:.3f}")

    if args.out:
        json.dump({"n": len(data), "recall_hit": hits, "ok": ok,
                   "official": False, "via_memory": True,
                   "front_model": front_model,
                   "judge_model": judge_model, "judge_self": judge_self,
                   "provider": base.PROVIDER_NAME,
                   "topk": args.topk, "topk_multi": args.topk_multi,
                   "cards_rule_built": True, "facts_written": False,
                   "p0": {"query_expand": args.query_expand,
                          "fact_table": args.fact_table, "con": args.con},
                   "rows": rows},
                  open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("已写出", args.out)


if __name__ == "__main__":
    main()
