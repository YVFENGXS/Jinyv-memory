# -*- coding: utf-8 -*-
"""LongMemEval-S 适配评测（非官方口径）。

官方 LongMemEval 的做法：把整段历史一次性塞进长上下文，答案与参考做
GPT-4o 判分。本脚本是**记忆中间层**的适配版：

1. 把每个实例的 haystack 按 session 切成文档（保留日期）；
2. 用本仓库的检索通道选证据：BM25 +（可选）bge-m3 向量，两路归一取大；
3. 让前台模型只依据选出的证据回答；
4. 用 judge 模型对「预测 vs 参考」做二元判分。

因此本脚本的数字**不是** LongMemEval-S 官方分数，只能与官方趋势对照。
真正要对齐官方口径，需要 OpenAI GPT-4o 的判分脚本（仓库未内置）。

用法：
    # 只跑检索、不调模型（不需要任何 key）
    python tools/eval_longmemeval.py --data /path/longmemeval_s.json --limit 20 --dry-run

    # 真作答 + 判分（需要 RP_FRONT_KEY；有 RP_EMBED_KEY 时自动开向量）
    python tools/eval_longmemeval.py --data /path/longmemeval_s.json --limit 50 --out lme.json

环境变量（与仓库其它工具一致）：
    RP_FRONT_KEY / RP_FRONT_BASE / RP_FRONT_MODEL
    RP_EMBED / RP_EMBED_KEY（回退 RP_ASR_KEY）/ RP_EMBED_BASE / RP_EMBED_MODEL
"""
import argparse
import json
import math
import os
import re
import sys
import urllib.request
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE = os.path.join(ROOT, "jinyv_memory", "core")
if CORE not in sys.path:
    sys.path.insert(0, CORE)

try:
    import embedding  # noqa: E402  本仓库的 bge-m3 适配器
except Exception:       # pragma: no cover - 无依赖时降级为纯词法
    embedding = None

_WORD = re.compile(r"[a-z0-9]+")
_STOP = set("""a an the is are was were be been being do does did doing have has had
having i you he she it we they me him her us them my your his its our their this
that these those of to in on at by for with about as and or but if then so than
what when where which who whom how why not no yes can could would should may might
will just also very much many some any""".split())


def load_env(path):
    """可选：从 --env-file 读 KEY=VALUE，已存在的环境变量优先。"""
    if not path or not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def terms(text):
    return {w for w in _WORD.findall(str(text or "").lower())
            if w not in _STOP and len(w) > 1}


def bm25(docs, query, k1=1.2, b=0.75):
    """对一组英文文档算 BM25，返回 0~1 归一后的分数。"""
    if not docs:
        return []
    q = terms(query)
    if not q:
        return [0.0] * len(docs)
    toks = [re.findall(r"[a-z0-9]+", d["text"].lower()) for d in docs]
    n = len(docs)
    avg = sum(len(t) for t in toks) / n or 1.0
    df = {t: sum(1 for tk in toks if t in tk) for t in q}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q}
    raw = []
    for tk in toks:
        tf = Counter(tk)
        s = sum(idf[t] * tf[t] * (k1 + 1) /
                (tf[t] + k1 * (1 - b + b * len(tk) / avg)) for t in q if tf[t])
        raw.append(s)
    peak = max(raw) or 1.0
    return [x / peak for x in raw]


def build_docs(inst):
    """一个 session 一篇文档；标题带日期，正文按轮拼接。"""
    docs = []
    for sid, date, sess in zip(inst["haystack_session_ids"],
                               inst["haystack_dates"], inst["haystack_sessions"]):
        lines = []
        for turn in sess:
            role = turn.get("role", "user")
            content = str(turn.get("content", "")).strip()
            if content:
                lines.append(f"{role}: {content}")
        docs.append({"id": sid, "date": date, "text": "\n".join(lines)})
    return docs


def retrieve(docs, query, topk, use_embed):
    """BM25（+ 向量）两路归一取大，返回 [(score, doc)]。"""
    lex = bm25(docs, query)
    scores = list(lex)
    if use_embed and embedding and embedding.enabled():
        try:
            qv = embedding.embed_query(query)
            blobs = embedding.embed_documents([d["text"] for d in docs])
            dims = embedding.dim()
            vec = [max(0.0, embedding.dot(qv, blob, dims)) for blob in blobs]
            peak = max(vec) or 1.0
            scores = [max(a, v / peak) for a, v in zip(scores, vec)]
        except Exception as exc:       # 网络/额度失败就退回纯词法
            print(f"[warn] embedding 失败，退回 BM25：{exc}", file=sys.stderr)
    order = sorted(range(len(docs)), key=lambda i: scores[i], reverse=True)
    return [(scores[i], docs[i]) for i in order[:topk]]


def _chat(messages, model, key, base, max_tokens=256, temperature=0.0):
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens, "temperature": temperature}).encode()
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.loads(r.read())
    return (data["choices"][0]["message"].get("content") or "").strip()


ANSWER_SYS = (
    "You answer questions about the user's past conversations. "
    "Use ONLY the evidence below; if it is insufficient, say you don't know. "
    "Answer concisely, in English, no preamble.")

JUDGE_SYS = (
    "You grade an answer against a gold answer. Reply with strict JSON: "
    '{"correct": true|false, "why": "<short reason>"}. '
    "Mark correct if the prediction conveys the same key fact as the gold answer, "
    "even with different wording. Mark incorrect if it contradicts or misses it.")


def answer(inst, evidence, model, key, base):
    ctx = "\n\n".join(f"[{d['date']}] {d['text']}" for _, d in evidence)
    user = (f"Current date: {inst['question_date']}\n"
            f"Question: {inst['question']}\n\nEvidence:\n{ctx}")
    return _chat([{"role": "system", "content": ANSWER_SYS},
                  {"role": "user", "content": user}],
                 model, key, base, max_tokens=300)


def judge(inst, pred, model, key, base):
    user = (f"Question: {inst['question']}\n"
            f"Gold answer: {inst['answer']}\n"
            f"Predicted answer: {pred}\n\n"
            "Is the prediction correct?")
    raw = _chat([{"role": "system", "content": JUDGE_SYS},
                 {"role": "user", "content": user}],
                model, key, base, max_tokens=120)
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
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--out", default="")
    ap.add_argument("--env-file", default="", help="可选：从文件读取 RP_* 密钥")
    ap.add_argument("--front-model", default=os.environ.get("RP_FRONT_MODEL", "deepseek-v4-flash"))
    ap.add_argument("--judge-model", default="")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只跑检索，不调模型")
    args = ap.parse_args()
    load_env(args.env_file)

    data = json.load(open(args.data, encoding="utf-8"))
    if args.limit:
        data = data[:args.limit]

    key = (os.environ.get("RP_FRONT_KEY") or "").strip()
    base = (os.environ.get("RP_FRONT_BASE") or os.environ.get("RP_LLM_BASE")
            or "https://api.deepseek.com").strip()
    judge_model = args.judge_model or args.front_model
    if not args.dry_run and not key:
        sys.exit("缺少 RP_FRONT_KEY（可用 --env-file 指向含 key 的文件）；"
                 "只想验证检索请加 --dry-run")

    use_embed = not args.no_embed
    rows, hits, ok = [], 0, 0
    for inst in data:
        docs = build_docs(inst)
        ev = retrieve(docs, inst["question"], args.topk, use_embed)
        hit = bool(set(inst.get("answer_session_ids") or ())
                   & {d["id"] for _, d in ev})
        hits += hit
        pred = why = ""
        correct = None
        if not args.dry_run:
            pred = answer(inst, ev, args.front_model, key, base)
            correct, why = judge(inst, pred, judge_model, key, base)
            ok += 1 if correct else 0
        rows.append({"question_id": inst["question_id"],
                     "question_type": inst["question_type"],
                     "question": inst["question"], "gold": inst["answer"],
                     "recall_hit": hit, "pred": pred,
                     "correct": correct, "why": why})
        tag = "hit" if hit else "miss"
        print(f"[{len(rows)}/{len(data)}] {tag} {inst['question_type']}: "
              f"{inst['question'][:70]}")

    print(f"\n证据命中（answer_session 进 top{args.topk}）：{hits}/{len(data)}"
          f" = {hits / max(1, len(data)):.3f}")
    if not args.dry_run:
        print(f"回答准确率（judge={judge_model}，非官方口径）：{ok}/{len(data)}"
              f" = {ok / max(1, len(data)):.3f}")
        by = {}
        for r in rows:
            t = by.setdefault(r["question_type"], [0, 0])
            t[0] += 1
            t[1] += 1 if r["correct"] else 0
        for t, (n, c) in sorted(by.items()):
            print(f"  {t:26s} {c}/{n} = {c / n:.3f}")

    if args.out:
        json.dump({"n": len(data), "recall_hit": hits, "ok": ok,
                   "official": False, "judge_model": judge_model,
                   "rows": rows}, open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("已写出", args.out)


if __name__ == "__main__":
    main()
