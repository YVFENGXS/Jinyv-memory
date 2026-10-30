# -*- coding: utf-8 -*-
"""合成式问句生成：从事件卡写「事后回忆会怎么问」，用于协议对照评测。

与 tools/eval_memory.py 的「原话问句」协议互补：
- 原话协议测「一条没头没尾的真实消息能否找回对应卡」；
- 本协议测「问句带足卡内具体信息时能否找回」。
两者数字不可混用：本协议的问句由 GT 卡写成，天然带线索。

用法：
  python tools/gen_synth_questions.py --cards-db /tmp/snap.db \
      --sid qq-u10001 --n 60 --out /tmp/sq_main.jsonl
"""
import argparse
import json
import random
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jinyu_memory" / "core"))
import llm                                                    # noqa: E402

PROMPT = """你是回忆评测的提问者。下面是一张「记忆卡」的摘要。请写成一句用户事后回忆时
会问伙伴的话（中文，15~45 字，口语）。
要求：
1. 必须带上摘要里最具体的实体或事件（人名、物品、地点、动作），但不要照抄整句；
2. 不要出现「记忆卡」「摘要」「评测」这类词，也不要提编号；
3. 只输出这一句话，不要引号、不要解释。
摘要："""

# 半合成：只留一个实体，其余全部模糊化，用来测「线索减少时召回怎么衰减」。
HALF_PROMPT = """你是回忆评测的提问者。下面是一张「记忆卡」的摘要。请写成一句用户事后
回忆时会问伙伴的话（中文，15~45 字，口语）。
要求：
1. 只保留摘要里**一个**最具体的实体（人名、物品或地点，任选其一）；
2. 其余信息改成模糊指代：那个、那次、那天、后来、那个东西、那件事……
3. 不许照抄摘要句子；不许出现「记忆卡」「摘要」「评测」；不许提编号；
4. 只输出这一句话，不要引号、不要解释。
摘要："""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards-db", required=True)
    ap.add_argument("--sid", required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20261030)
    ap.add_argument("--mode", choices=("full", "half"), default="full",
                    help="full=带全部具体信息；half=只留一个实体、其余模糊化")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    back = llm.from_env("back")
    prompt = HALF_PROMPT if args.mode == "half" else PROMPT
    con = sqlite3.connect("file:%s?mode=ro" % args.cards_db, uri=True)
    cards = con.execute(
        "select event_id, summary from event_cards where session_id=? "
        "order by turn_end desc", (args.sid,)).fetchall()
    random.Random("%d:%s:synth" % (args.seed, args.sid)).shuffle(cards)
    out = []
    for eid, summary in cards:
        if len(out) >= args.n:
            break
        try:
            q = back.chat([{"role": "user", "content": prompt + str(summary or "")[:160]}],
                          temperature=0.7, max_tokens=80)
        except Exception as e:                                 # noqa: BLE001
            print("skip", type(e).__name__, e)
            continue
        q = re.sub(r"[\"'“”‘’\n]", "", q).strip()
        if not (8 <= len(q) <= 70) or "记忆卡" in q or "摘要" in q:
            continue
        out.append({"q": q, "gt": eid, "snippet": str(summary or "")[:40],
                    "layer": "synth"})
    with open(args.out, "w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(args.sid, "cards", len(cards), "questions", len(out))


if __name__ == "__main__":
    main()
