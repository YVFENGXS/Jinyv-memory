# -*- coding: utf-8 -*-
"""离线回填四因子里的 emotion / authority（默认只预览）。

背景：四因子上线前，所有长期词条的 emotion=0.5、authority=0.7 都是列默认值，
E/A 实际上不参与排序。这里用后台模型按与 summarizer 同一套口径给存量词条重新打分，
让四因子在历史词条上也产生区分度。

  python tools/backfill_factors.py --db data/rp_memory.db            # 预览
  python tools/backfill_factors.py --db data/rp_memory.db --apply    # 写库

安全边界：
- authority>=0.95 的词条视为「人工/高权威」，一律不动；
- 已退场（retracted）的词条不动；
- 模型没给或给坏的值一律回退默认值，不会写入脏数据；
- 只 UPDATE emotion/authority 两列，不改正文、标签、热度、注意力。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# 仓库里是 jinyu_memory/core，线上插件目录是 core 平铺，两种都挂上。
_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "jinyu_memory", _ROOT / "core",
           _ROOT / "jinyu_memory" / "core", _ROOT / "examples"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

BATCH = 40

PROMPT = """给下面这些长期记忆打分，只输出 JSON：{{"items":[{{"id":序号,"emotion":0~1,"authority":0~1}}]}}

口径（与写入时的 summarizer 一致，不要自创）：
- emotion（情感强度）：平常告知 0.3；明确的高兴/难过/生气/害怕/珍视 0.8~1.0；拿不准 0.5。
- authority（来源权威）：主人明确说出的设定/偏好/承诺 0.9~1.0；
  角色自己的观察或推测 0.4~0.6；普通对话里的事实 0.7；拿不准 0.7。
- 只依据这条事实本身能看出的信息，不要脑补剧情。
- 每条都必须给分，不要漏。

事实列表（序号｜类别｜说话人｜重要度｜正文）：
{rows}"""


def _clamp(v, default):
    try:
        return round(max(0.0, min(1.0, float(v))), 2)
    except (TypeError, ValueError):
        return default


def _load(db_path, cid, limit):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, fact, category, speaker, importance, emotion, authority"
        " FROM durable_facts WHERE character_id=? AND retracted_at IS NULL"
        " AND authority<0.95 ORDER BY id LIMIT ?", (cid, limit)).fetchall()
    con.close()
    return [dict(r) for r in rows]


def _score(back, chunk):
    lines = "\n".join(
        f"{i}｜{r['category'] or '未分类'}｜{r['speaker'] or '通用'}"
        f"｜{float(r['importance'] or 0.5):.2f}｜{r['fact']}"
        for i, r in enumerate(chunk, 1))
    # 40 条 × 每条两个 0~1 分数，1200 token 会在尾部截断成非法 JSON：
    # 预算给足，双倍重试由 LLM 适配器的 finish=length 逻辑兜底。
    raw = back.chat([{"role": "user", "content": PROMPT.format(rows=lines)}],
                    temperature=0.2, max_tokens=2600, json_mode=True)
    raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    items = data.get("items") if isinstance(data, dict) else data
    out = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        try:
            idx = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        if 1 <= idx <= len(chunk):
            out[idx] = (_clamp(it.get("emotion"), 0.5), _clamp(it.get("authority"), 0.7))
    return out


def main():
    ap = argparse.ArgumentParser(description="回填长期事实的 emotion/authority")
    ap.add_argument("--db", default=str(_ROOT / "data" / "rp_memory.db"))
    ap.add_argument("--cid", default="jinyu")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--apply", action="store_true", help="不加则只预览分布")
    args = ap.parse_args()

    key = os.environ.get("RP_BACK_KEY") or ""
    if not key:
        sys.exit("缺少 RP_BACK_KEY：回填打分需要后台模型")
    from llm import LLM
    back = LLM(key, os.environ.get("RP_BACK_MODEL") or "deepseek-flash",
               os.environ.get("RP_LLM_BASE") or None, role="back")

    rows = _load(args.db, args.cid, max(1, args.limit))
    print(f"待回填 {len(rows)} 条（authority>=0.95 的人工词条已跳过），模式="
          f"{'执行' if args.apply else '预览'}")
    scored, updates = 0, []
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        got = _score(back, chunk)
        if not got:                          # 模型偶发返回非 JSON：同一批再问一次
            got = _score(back, chunk)
        for i, r in enumerate(chunk, 1):
            if i not in got:
                continue
            emo, auth = got[i]
            scored += 1
            if (round(float(r["emotion"]), 2), round(float(r["authority"]), 2)) != (emo, auth):
                updates.append((r["id"], emo, auth, r["fact"]))
        print(f"  批 {start // BATCH + 1}: {len(chunk)} 条，评分 {len(got)} 条")

    print(f"评分成功 {scored}/{len(rows)}，产生变更 {len(updates)} 条")
    for row in updates[:15]:
        print(f"  {row[0]:>4} E={row[1]:.2f} A={row[2]:.2f} {row[3][:40]}")
    if not args.apply:
        print("预览结束，未写库。确认后加 --apply。")
        return
    con = sqlite3.connect(args.db)
    con.executemany("UPDATE durable_facts SET emotion=?, authority=? WHERE id=?",
                    [(emo, auth, fid) for fid, emo, auth, _ in updates])
    con.commit()
    con.close()
    print(f"已写入 {len(updates)} 条")


if __name__ == "__main__":
    main()
