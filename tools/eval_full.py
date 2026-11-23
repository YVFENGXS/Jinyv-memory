# -*- coding: utf-8 -*-
"""基于真实账号语料的 memory 中间件完整离线评测（不调任何模型）。

覆盖：语料结构 / 召回 / 注入 / 失败归因 / 预算水位 / 冗余证据 / 账本 /
      时间衰减 / 召回埋点 / 性能 / 隐私边界 / prompt 段结构

用法：
  python tools/eval_full.py --cards-db data/memory.db     --questions data/real_q.jsonl --sid qq-u10001 --cid jinyu

注意：--cards-db 给的是只读副本；脚本会复制到临时目录再跑，绝不写回原库。
"""
import json, logging, os, re, shutil, sqlite3, sys, tempfile, time, statistics

logging.disable(logging.WARNING)
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "jinyv_memory", ROOT / "jinyv_memory" / "core", ROOT / "examples"):
    sys.path.insert(0, str(p))

import ledger, recall_shadow, retriever, forgetting
import rp_skeleton as rp
from demo import MockBack, MockFront, PERSONA  # noqa: E402
from rp_skeleton import DB, RPContextMiddleware

DB_SRC = Path("")
Q_PATH = Path("")
SID, CID = "qq-u10001", "jinyu"
BUDGET = 26000
WORKDIR = Path(tempfile.mkdtemp(prefix="jinyv-eval-"))


def pct(k, n):
    return round(k / n, 3) if n else None


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]


def fresh_copy(tag):
    dst = WORKDIR / f"run_{tag}.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(DB_SRC, dst)
    return dst


def load_cases():
    rows = []
    for line in Q_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        rows.append((str(d["q"]), str(d["gt"]), str(d.get("snippet") or ""),
                     str(d.get("layer") or "holdout")))
    return rows


def make_mw(db, budget=BUDGET):
    return RPContextMiddleware(MockFront("eval"), MockBack(), db,
                               max_tokens=budget, output_reserve=0, keep_recent=4)


def prompt_text(mw, q, sid, cid, upto):
    msgs = mw._build_messages(PERSONA, sid, cid, upto=upto, query=q)
    return "\n".join(str(m.get("content") or "") for m in msgs)


def sections_of(text):
    out = {}
    for m in re.finditer(r"【([^】]{2,20})】", text):
        name = m.group(1)
        start = m.start()
        nxt = text.find("【", m.end())
        body = text[start:nxt if nxt > 0 else len(text)]
        out[name] = out.get(name, 0) + len(body)
    return out


def corpus_stats(db, cases, upto):
    con = sqlite3.connect(db.path)
    q = lambda sql, *a: con.execute(sql, a).fetchall()
    turns = q("SELECT COUNT(*) FROM turns WHERE session_id=? AND character_id=?", SID, CID)[0][0]
    cards = q("SELECT COUNT(*) FROM event_cards WHERE session_id=? AND character_id=?", SID, CID)[0][0]
    facts = q("SELECT COUNT(*) FROM durable_facts WHERE session_id=? AND character_id=?", SID, CID)[0][0]
    span = q("SELECT MIN(created_at), MAX(created_at) FROM turns WHERE session_id=?", SID)[0]
    imp = [r[0] for r in q("SELECT importance FROM event_cards WHERE session_id=?", SID)]
    fact_imp = [r[0] for r in q("SELECT importance FROM durable_facts WHERE session_id=?", SID)]
    strict = q("SELECT COUNT(*) FROM event_cards WHERE session_id=? AND precision='strict'", SID)[0][0]
    boundaries = dict(q("SELECT knowledge_boundary, COUNT(*) FROM event_cards WHERE session_id=? GROUP BY 1", SID))
    categories = dict(q("SELECT category, COUNT(*) FROM durable_facts WHERE session_id=? GROUP BY 1", SID))
    existing = {r[0] for r in q("SELECT event_id FROM event_cards WHERE session_id=?", SID)}
    total_gt = len({c[1] for c in cases})
    con.close()
    return {
        "turns": turns, "cards": cards, "facts": facts,
        "turns_until_eval": upto,
        "time_span": list(span),
        "card_importance": {"min": min(imp or [0]), "p50": statistics.median(imp or [0]),
                            "mean": round(sum(imp) / len(imp), 3) if imp else None,
                            "max": max(imp or [0])},
        "fact_importance": {"p50": statistics.median(fact_imp or [0]),
                            "mean": round(sum(fact_imp) / len(fact_imp), 3) if fact_imp else None},
        "strict_cards": strict,
        "boundaries": boundaries,
        "fact_categories": categories,
        "questions": len(cases),
        "unique_gt": total_gt,
        "gt_in_db": sum(1 for g in {c[1] for c in cases} if g in existing),
    }


def retrieval_eval(db, cases, cards):
    out = {}
    for limit in (1, 2, 4, 8, 12, 20):
        ranks = {}
        for q, gt, _s, _l in cases:
            ids = [c["event_id"] for _, c in retriever.retrieve(cards, q, limit=limit)]
            ranks[(q, gt)] = ids
        def rec(rows):
            return pct(sum(1 for q, gt, _s, _l in rows if gt in ranks[(q, gt)][:4]), len(rows))
        out[f"recall@{limit}"] = rec(cases)
    # MRR@4 与分层
    rr, by_layer = [], {}
    for q, gt, _s, layer in cases:
        rank = 0
        for i, eid in enumerate([c["event_id"] for _, c in retriever.retrieve(cards, q, limit=4)], 1):
            if eid == gt:
                rank = i
                break
        rr.append(1.0 / rank if rank else 0.0)
        by_layer.setdefault(layer, []).append(rank)
    out["mrr@4"] = round(sum(rr) / len(rr), 3)
    out["layers"] = {
        layer: {"n": len(rs), "recall@4": pct(sum(1 for r in rs if r), len(rs)),
                "ci": wilson(sum(1 for r in rs if r), len(rs))}
        for layer, rs in by_layer.items()}
    out["recall@4_ci"] = wilson(sum(1 for q, gt, _s, _l in cases
                                    if gt in [c["event_id"] for _, c in retriever.retrieve(cards, q, limit=4)]),
                                len(cases))
    return out


def injection_eval(db, cases, upto):
    mw = make_mw(db)
    hits, toks, per_layer = [], [], {}
    prompt_cache = {}
    for q, gt, snippet, layer in cases:
        text = prompt_text(mw, q, SID, CID, upto)
        prompt_cache[q] = text
        ok = bool(snippet) and snippet in text
        hits.append(ok)
        toks.append(rp.est_tokens(text, mw.profile))
        per_layer.setdefault(layer, []).append(ok)
    return {
        "inject_hit": pct(sum(hits), len(hits)),
        "inject_ci": wilson(sum(hits), len(hits)),
        "layers": {k: {"n": len(v), "hit": pct(sum(v), len(v))} for k, v in per_layer.items()},
        "tokens": {"p50": int(statistics.median(toks)), "max": max(toks),
                   "used_pct_of_budget": round(statistics.mean(toks) / BUDGET, 3)},
        "prompts": prompt_cache,
    }


def failure_eval(db, cases, prompts):
    cards = db.all_cards(SID, CID)
    by_id = {c["event_id"]: c for c in cards}
    mw = make_mw(db)
    ok, bad = [], []
    for q, gt, snippet, layer in cases:
        hit = gt in [c["event_id"] for _, c in retriever.retrieve(cards, q, limit=4)]
        (ok if hit else bad).append((q, gt, snippet, layer))
    reasons = {}
    for q, gt, snippet, _l in bad:
        card = by_id.get(gt)
        if card is None:
            r = "gt_not_in_db"
        elif (card.get("knowledge_boundary") or "both") == "player":
            r = "blocked_player_boundary"
        elif retriever.suspicious(card.get("summary") or ""):
            r = "blocked_suspicious"
        else:
            ids = [c["event_id"] for _, c in retriever.retrieve(cards, q, limit=100)]
            r = "rank_beyond_100" if gt not in ids else "rank_low"
            if snippet and snippet in prompts.get(q, ""):
                r = "in_prompt_via_recent"
        reasons[r] = reasons.get(r, 0) + 1
    return {"n": len(cases), "recalled": len(ok), "missed": len(bad), "reasons": reasons,
            "missed_samples": [{"q": q, "gt": gt, "layer": l}
                               for q, gt, _s, l in bad[:10]]}


def budget_eval(db, cases, upto):
    rows = []
    for budget in (1000, 2000, 4000, 8000, 16000, 26000):
        mw = make_mw(db, budget)
        hit = tok = over = 0
        for q, _gt, snippet, _l in cases:
            text = prompt_text(mw, q, SID, CID, upto)
            hit += bool(snippet) and snippet in text
            t = rp.est_tokens(text, mw.profile)
            tok += t
            over += t > budget
        rows.append({"budget": budget, "inject_hit": pct(hit, len(cases)),
                     "avg_tokens": int(tok / len(cases)), "over_budget": pct(over, len(cases)),
                     "sections_dropped": mw.watermarks["sections_dropped"],
                     "recent_dropped": mw.watermarks["recent_dropped"]})
    return rows


def redundancy_eval(db, cases):
    con = sqlite3.connect(db.path)
    facts = [r[0] or "" for r in con.execute(
        "SELECT fact FROM durable_facts WHERE session_id=? AND character_id=?", (SID, CID))]
    summaries = [r[0] or "" for r in con.execute(
        "SELECT summary FROM event_cards WHERE session_id=? AND character_id=?", (SID, CID))]
    con.close()

    def cover(snippet):
        s = (snippet or "").strip()
        if len(s) < 4:
            return False, False
        n = min(6, len(s))
        grams = {s[i:i + n] for i in range(0, max(1, len(s) - n + 1))}
        f = any(g in fact for fact in facts for g in grams)
        c = any(g in summary for summary in summaries for g in grams)
        return f, c

    fact_hit = card_hit = both = 0
    for _q, _gt, snippet, _l in cases:
        f, c = cover(snippet)
        fact_hit += f
        card_hit += c
        both += f and c
    return {"cases": len(cases), "snippet_in_facts": pct(fact_hit, len(cases)),
            "snippet_in_cards": pct(card_hit, len(cases)),
            "second_source_available": pct(both, len(cases)),
            "facts_total": len(facts), "cards_total": len(summaries)}


def rowconn(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def ledger_eval(db):
    con = rowconn(db.path)
    ledger.ensure(con)
    added = ledger.sync(con)
    first = ledger.verify(con)
    row = con.execute("SELECT id, content FROM turns WHERE session_id=? ORDER BY id LIMIT 1",
                      (SID,)).fetchone()
    con.execute("UPDATE turns SET content=content||' ' WHERE id=?", (row[0],))
    con.commit()
    tampered = ledger.verify(con)
    con.close()
    return {"texts_added": added, "entries": first["entries"], "current": first["current"],
            "missing_after_sync": len(first["missing"]), "changed_after_sync": len(first["changed"]),
            "tamper_detected": len(tampered["changed"]), "tampered_ref": str(row[0]),
            "detect_ok": len(tampered["changed"]) == 1}


def forgetting_eval(db_src):
    path = fresh_copy("forgetting")
    con = rowconn(path)
    forgetting.prepare_schema(con)
    rows, changes = forgetting.preview(con)
    before = con.execute("SELECT AVG(importance) FROM durable_facts WHERE session_id=?", (SID,)).fetchone()[0]
    applied = forgetting.commit(con, changes)
    after = con.execute("SELECT AVG(importance) FROM durable_facts WHERE session_id=?", (SID,)).fetchone()[0]
    audit = con.execute("SELECT COUNT(*) FROM rhythm_audit").fetchone()[0]
    phases = {}
    for r in rows:
        phases[r["phase"]] = phases.get(r["phase"], 0) + 1
    drops = [round(r.get("drop", 0), 4) for r in rows]
    con.close()
    db = DB(str(path))
    mw = make_mw(db)
    cases = load_cases()
    upto = db.max_turn(SID, CID)
    hit = sum(1 for q, _g, s, _l in cases
              if s and s in prompt_text(mw, q, SID, CID, upto))
    return {"facts": len(rows), "changed": len(changes), "applied": applied,
            "avg_importance_before": round(before, 4), "avg_importance_after": round(after, 4),
            "max_daily_drop": max(drops) if drops else 0, "audit_rows": audit,
            "phases": phases,
            "inject_hit_after_decay": pct(hit, len(cases)),
            "note": "同一批库、同一批问句，衰减只改 importance，不改卡正文"}


def shadow_eval(db):
    path = fresh_copy("shadow")
    con = rowconn(path)
    cases = load_cases()
    logged = 0
    for q, _gt, _s, _l in cases:
        try:
            recall_shadow.log(con, "recall", SID, CID, q, hits=1, dropped=0,
                              channels=["kw"])
            logged += 1
        except Exception:
            pass
    stats = recall_shadow.stats(con, days=30)
    total = con.execute("SELECT COUNT(*) FROM recall_shadow").fetchone()[0]
    con.close()
    return {"logged": logged, "rows": total, "stats": stats}


def perf_eval(db, cases, upto):
    cards = db.all_cards(SID, CID)
    mw = make_mw(db)
    rt, bt = [], []
    for _ in range(3):
        for q, _g, _s, _l in cases:
            t0 = time.perf_counter()
            retriever.retrieve(cards, q, limit=4)
            rt.append((time.perf_counter() - t0) * 1000)
    for _ in range(3):
        for q, _g, _s, _l in cases:
            t0 = time.perf_counter()
            mw._build_messages(PERSONA, SID, CID, upto=upto, query=q)
            bt.append((time.perf_counter() - t0) * 1000)
    def q(v, p):
        v = sorted(v)
        return round(v[min(len(v) - 1, int(len(v) * p))], 2)
    return {"retrieve_ms": {"p50": q(rt, .5), "p95": q(rt, .95)},
            "build_messages_ms": {"p50": q(bt, .5), "p95": q(bt, .95)},
            "n_retrieve": len(rt), "n_build": len(bt)}


def privacy_eval(db, prompts, upto):
    cards = db.all_cards(SID, CID)
    secret = [c for c in cards if (c.get("knowledge_boundary") or "both") in ("player", "self")]
    suspicious = [c for c in cards if retriever.suspicious(c.get("summary") or "")]
    leaks = []
    for c in secret:
        mark = (c.get("summary") or "")[:20]
        where = [q for q, t in prompts.items() if mark and mark in t]
        leaks.append({"event_id": c["event_id"], "boundary": c["knowledge_boundary"],
                      "in_prompts": len(where)})
    mw = make_mw(db)
    text = prompt_text(mw, "你答应过我的事还记得吗", SID, CID, upto)
    return {"secret_cards": len(secret), "suspicious_cards": len(suspicious),
            "secret_leaks": leaks,
            "leak_rate": pct(sum(1 for x in leaks if x["in_prompts"]), len(leaks)) if leaks else None}


def prompt_structure(db, upto):
    mw = make_mw(db)
    text = prompt_text(mw, "家里那只猫叫什么名字", SID, CID, upto)
    secs = sections_of(text)
    total = rp.est_tokens(text, mw.profile)
    return {"total_tokens": total, "total_chars": len(text),
            "sections": [{"name": k, "chars": v, "share": round(v / len(text), 3)}
                         for k, v in sorted(secs.items(), key=lambda kv: -kv[1])]}


def main():
    global DB_SRC, Q_PATH, SID, CID, BUDGET
    import argparse
    ap = argparse.ArgumentParser(description="memory 中间件完整离线评测")
    ap.add_argument("--cards-db", required=True, help="线上库的只读副本（不会被写回）")
    ap.add_argument("--questions", required=True, help="人工标注问句 JSONL")
    ap.add_argument("--sid", default=SID)
    ap.add_argument("--cid", default=CID)
    ap.add_argument("--budget", type=int, default=BUDGET)
    ap.add_argument("--out", default="", help="指标 JSON 输出路径")
    args = ap.parse_args()
    DB_SRC, Q_PATH = Path(args.cards_db), Path(args.questions)
    SID, CID, BUDGET = args.sid, args.cid, args.budget
    cases = load_cases()
    db = DB(str(fresh_copy("main")))
    upto = db.max_turn(SID, CID)
    cards = db.all_cards(SID, CID)
    out = {"meta": {"sid": SID, "cid": CID, "budget": BUDGET,
                    "vector": "off (no RP_EMBED_KEY)", "model": "off (offline)",
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}}
    out["corpus"] = corpus_stats(db, cases, upto)
    out["retrieval"] = retrieval_eval(db, cases, cards)
    inj = injection_eval(db, cases, upto)
    out["injection"] = {k: v for k, v in inj.items() if k != "prompts"}
    out["failures"] = failure_eval(db, cases, inj["prompts"])
    out["budget"] = budget_eval(db, cases, upto)
    out["redundancy"] = redundancy_eval(db, cases)
    out["ledger"] = ledger_eval(db)
    out["forgetting"] = forgetting_eval(DB_SRC)
    out["shadow"] = shadow_eval(DB_SRC)
    out["perf"] = perf_eval(db, cases, upto)
    out["privacy"] = privacy_eval(db, inj["prompts"], upto)
    out["prompt_structure"] = prompt_structure(db, upto)
    dst = Path(args.out) if args.out else WORKDIR / "eval_full.json"
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "meta"},
                     ensure_ascii=False, indent=1))
    print("\n->", dst)


if __name__ == "__main__":
    main()
