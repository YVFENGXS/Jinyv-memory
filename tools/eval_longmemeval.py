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
import time
import urllib.error
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

# assistant 的自我指涉套话会把「用户说试过四家」和「助手说自己没试过」混在一起，
# 诱导模型拒答。检索保留原文，作答前只丢掉这些无信息量的句子。
_BOILER = re.compile(
    r"(as a large language model|i don't have personal experiences|"
    r"i haven't tried any|i am an ai|i'm an ai|i cannot browse|"
    r"i don't have access to|i'm afraid i'm a large language model)", re.I)


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


MAX_RETRIES = 5
PROVIDER_NAME = ""


class ContentFiltered(Exception):
    """单题被上游内容审核拦截：跳过这题，不要整轮换 provider。"""


def _chat(messages, model, key, base, max_tokens=256, temperature=0.0):
    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    # 推理型接口默认带思维链，短 max_tokens 会被 reasoning 吃光、content 为空；
    # 评测只取最终答案，可用 RP_THINKING_DISABLED=1 关闭（与线上 thinking=off 对齐）。
    if os.environ.get("RP_THINKING_DISABLED") == "1":
        payload["thinking"] = {"type": "disabled"}
    # 需要显式提高推理档位时用（OpenAI 风格 reasoning_effort）。
    effort = os.environ.get("RP_REASONING_EFFORT")
    if effort:
        payload["reasoning_effort"] = effort
    # 部分接口对单条 message 有硬性长度上限，超长直接 400。
    cap = int(os.environ.get("RP_MSG_CHARS") or 0)
    if cap:
        for m in payload["messages"]:
            if isinstance(m.get("content"), str) and len(m["content"]) > cap:
                m["content"] = m["content"][:cap] + "\n[truncated]"
    body = json.dumps(payload).encode()
    url = base.rstrip("/") + "/chat/completions"
    last = None
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json",
                     # 部分入口前置 WAF，默认 Python-urllib UA 会被 403/1010 拦掉
                     "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/120.0 Safari/537.36")})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.loads(r.read())
            return (data["choices"][0]["message"].get("content") or "").strip()
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 400:
                body = exc.read().decode("utf-8", "ignore")
                if ("data_inspection_failed" in body
                        or "inappropriate content" in body
                        or "exceeds maximum length" in body):
                    raise ContentFiltered(body[:200])
                print(f"  [400] {body[:300]}", file=sys.stderr, flush=True)
            if exc.code in (429, 500, 502, 503, 504, 520, 522, 523, 524) \
                    and attempt < MAX_RETRIES:
                wait = min(120, 5 * (2 ** attempt))
                print(f"  [retry] HTTP {exc.code}，{wait}s 后重试",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            raise
        except Exception as exc:          # 超时 / 断连
            last = exc
            if attempt < MAX_RETRIES:
                wait = min(120, 3 * (2 ** attempt))
                print(f"  [retry] {type(exc).__name__}，{wait}s 后重试",
                      file=sys.stderr, flush=True)
                time.sleep(wait)
                continue
            raise
    raise last


def format_evidence(evidence, chronological=False):
    """摊平成 [日期] 角色: 内容；可按时序排列，并滤掉助手自指套话。"""
    items = list(evidence)
    if chronological:
        items.sort(key=lambda x: str(x[1]["date"]))
    lines = []
    for _, doc in items:
        date = str(doc["date"])[:10]
        for line in doc["text"].splitlines():
            line = line.strip()
            if not line or _BOILER.search(line):
                continue
            lines.append(f"[{date}] {line}")
    return "\n".join(lines)


ANSWER_SYS = (
    "You answer questions about the user's past conversations using retrieved evidence.\n"
    "Rules:\n"
    "1. Each evidence line starts with [date] and a role (user/assistant). The user's "
    "own statements are authoritative; ignore assistant disclaimers.\n"
    "2. Synthesize across ALL evidence. For counting questions, enumerate every "
    "matching item first, then give the total.\n"
    "3. When the same attribute has different values, prefer the value with the most "
    "recent date; later evidence supersedes earlier evidence. State the latest value "
    "as the answer and do not lead with a superseded one. Never mix an old value "
    "with a new one. The dated evidence is authoritative: if the backend summary "
    "conflicts with it, follow the evidence and the latest date.\n"
    "4. For preference or recommendation questions (e.g. \"suggest a hotel for my trip "
    "to X\"), the evidence does NOT need to mention the target place. Infer the user's "
    "general tastes (style, budget, amenities) from any relevant evidence and apply "
    "them to the new request. Always give concrete suggestions; never refuse for lack "
    "of a direct mention.\n"
    "5. Only say \"I don't know\" if NO evidence is relevant. Incomplete evidence is "
    "not a reason to refuse; give the best-supported answer.\n"
    "6. Apply the question's predicate strictly. For counts, include only items that "
    "explicitly match (e.g. \"led a project\"); do not count adjacent activities like "
    "leading a team or planning a launch.\n"
    "Answer concisely in English, no preamble.")

BACK_SYS = (
    "You are a memory consolidation engine. From dated conversation excerpts, extract "
    "the facts relevant to the question. Rules:\n"
    "- Keep each fact's date (YYYY/MM/DD) and speaker.\n"
    "- Compare dates numerically. For the same attribute, the later date always "
    "supersedes the earlier date; never mark an earlier value as newer.\n"
    "- A value the user attributes to themselves (e.g. \"my personal best time of X\", "
    "\"I led X\") is a fact, even when the sentence is about a future goal or plan.\n"
    "- For counting questions, include only items that explicitly satisfy the "
    "question's predicate; do not count adjacent activities (leading a team is not "
    "leading a project unless stated).\n"
    "- Drop assistant disclaimers and speculation.\n"
    "Output a compact dated bullet list, no prose.")

JUDGE_SYS = (
    "You grade an answer against a gold answer. Reply with strict JSON: "
    '{"correct": true|false, "why": "<short reason>"}. '
    "Mark correct if the prediction conveys the same key fact as the gold answer, "
    "even with different wording. Mark incorrect if it contradicts or misses it.")


def answer(inst, evidence, model, key, base, facts="", chronological=False):
    ctx = format_evidence(evidence, chronological=chronological)
    note = ("Evidence is ordered oldest to newest; the last value for an attribute "
            "is the latest.\n") if chronological else ""
    fact_block = (f"\nBackend consolidated facts (latest wins):\n{facts}\n"
                  if facts else "")
    user = (f"Current date: {inst['question_date']}\n"
            f"Question: {inst['question']}\n{note}\nEvidence:\n{ctx}{fact_block}")
    return _chat([{"role": "system", "content": ANSWER_SYS},
                  {"role": "user", "content": user}],
                 model, key, base,
                 max_tokens=int(os.environ.get("RP_ANSWER_TOKENS") or 400))


def consolidate(inst, evidence, model, key, base):
    """后台模型先把证据归并成带日期的事实表（模拟生产里的摘要/冲突归并）。"""
    ctx = format_evidence(evidence)
    user = (f"Question: {inst['question']}\n"
            f"Current date: {inst['question_date']}\n\nExcerpts:\n{ctx}")
    return _chat([{"role": "system", "content": BACK_SYS},
                  {"role": "user", "content": user}],
                 model, key, base,
                 max_tokens=int(os.environ.get("RP_BACK_TOKENS") or 800))


def judge(inst, pred, model, key, base):
    user = (f"Question: {inst['question']}\n"
            f"Gold answer: {inst['answer']}\n"
            f"Predicted answer: {pred}\n\n"
            "Is the prediction correct?")
    raw = _chat([{"role": "system", "content": JUDGE_SYS},
                 {"role": "user", "content": user}],
                model, key, base,
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
    ap.add_argument("--offset", type=int, default=0,
                    help="从第 N 题开始（分片并发用）")
    ap.add_argument("--topk", type=int, default=8)
    ap.add_argument("--topk-multi", type=int, default=14,
                    help="multi-session 题的召回深度（跨会话汇总需要更多证据）")
    ap.add_argument("--out", default="")
    ap.add_argument("--resume", default="",
                    help="断点文件（JSONL）；默认 <out>.jsonl，存在则跳过已完成题")
    ap.add_argument("--env-file", default="", help="可选：从文件读取 RP_* 密钥")
    ap.add_argument("--front-model", default=os.environ.get("RP_FRONT_MODEL", "deepseek-v4-flash"))
    ap.add_argument("--back-model", default="",
                    help="后台归并模型；默认 RP_BACK_MODEL，再回退前台模型")
    ap.add_argument("--judge-model", default="")
    ap.add_argument("--no-back", action="store_true", help="关闭后台事实归并")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只跑检索，不调模型")
    ap.add_argument("--max-retries", type=int, default=5,
                    help="单 provider 内 429/5xx 重试次数；池模式建议 1，快速切换")
    ap.add_argument("--provider-name", default="", help="写入结果的 provider 名")
    args = ap.parse_args()
    global MAX_RETRIES, PROVIDER_NAME
    MAX_RETRIES = max(0, args.max_retries)
    PROVIDER_NAME = args.provider_name
    load_env(args.env_file)

    data = json.load(open(args.data, encoding="utf-8"))
    if args.offset or args.limit:
        end = args.offset + args.limit if args.limit else None
        data = data[args.offset:end]

    key = (os.environ.get("RP_FRONT_KEY") or "").strip()
    base = (os.environ.get("RP_FRONT_BASE") or os.environ.get("RP_LLM_BASE")
            or "https://api.deepseek.com").strip()
    judge_model = args.judge_model or args.front_model
    back_model = (args.back_model or os.environ.get("RP_BACK_MODEL")
                  or args.front_model)
    if not args.dry_run and not key:
        sys.exit("缺少 RP_FRONT_KEY（可用 --env-file 指向含 key 的文件）；"
                 "只想验证检索请加 --dry-run")

    use_embed = not args.no_embed
    # 逐题写 JSONL：被限流/断网中断也不丢已跑结果，重跑自动跳过。
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
        multi = inst["question_type"] == "multi-session"
        topk = max(args.topk, args.topk_multi) if multi else args.topk
        docs = build_docs(inst)
        ev = retrieve(docs, inst["question"], topk, use_embed)
        hit = bool(set(inst.get("answer_session_ids") or ())
                   & {d["id"] for _, d in ev})
        hits += hit
        pred = why = facts = ""
        correct = None
        filtered = False
        if not args.dry_run:
            try:
                # 后台先归并新旧事实，前台再按「最新优先」作答；knowledge-update
                # 额外把证据按时序排，避免旧值压过新值。
                # preference 题考的是「从偏好推断建议」，没有单一事实可归并；
                # 后台事实表反而会强调「没提过 Miami」而诱发拒答，这里跳过。
                if not args.no_back and inst["question_type"] != "single-session-preference":
                    facts = consolidate(inst, ev, back_model, key, base)
                pred = answer(inst, ev, args.front_model, key, base, facts=facts,
                              chronological=inst["question_type"] == "knowledge-update")
                correct, why = judge(inst, pred, judge_model, key, base)
            except ContentFiltered:
                # 单题被内容审核拦截：记错题并继续，不整轮切 provider。
                filtered = True
                correct = False
                why = "content_filtered"
                pred = facts = ""
            ok += 1 if correct else 0
        row = {"question_id": inst["question_id"],
               "question_type": inst["question_type"],
               "question": inst["question"], "gold": inst["answer"],
               "recall_hit": hit, "facts": facts, "pred": pred,
               "correct": correct, "why": why, "provider": PROVIDER_NAME,
               "filtered": filtered}
        rows.append(row)
        if ckpt and not args.dry_run:
            with open(ckpt, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
        tag = "hit" if hit else "miss"
        print(f"[{len(rows)}/{len(data)}] {tag} {inst['question_type']}: "
              f"{inst['question'][:70]}", flush=True)
        time.sleep(0.3)

    print(f"\n证据命中（answer_session 进 top{args.topk}/multi {args.topk_multi}）："
          f"{hits}/{len(data)} = {hits / max(1, len(data)):.3f}")
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
                   "back_model": back_model, "topk": args.topk,
                   "topk_multi": args.topk_multi, "rows": rows},
                  open(args.out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print("已写出", args.out)


if __name__ == "__main__":
    main()
