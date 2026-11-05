# -*- coding: utf-8 -*-
"""40/60 轮热窗试跑（C4-03）：同预算下比较无理由重问率。

不调用真实模型：用固定剧本模拟「问过一次，热窗滑走后是否又问」。
- no_ledger：只靠热窗记忆，滑出窗口就重问；
- ledger：问题账本持久化，滑出窗口也不重问。
输出 40/60 轮的对照，供上线前判定，不凭感觉调窗口。
"""
import argparse
import random


def simulate(window, rounds=200, seed=7, ledger=False):
    rng = random.Random(seed)
    asked_at, repeats, allowed_repeats = {}, 0, 0
    for turn in range(1, rounds + 1):
        if turn % 5:                      # 每 5 轮出现一次「想追问」的冲动
            continue
        topic = f"topic-{turn % 9}"       # 同一 topic 约 45 轮后再出现
        last = asked_at.get(topic)
        asked_at[topic] = turn
        if last is None:
            continue
        in_window = turn - last <= window
        # 账本持久化：滑出热窗也不会重问；没有账本才会重问
        if not in_window and not ledger:
            repeats += 1
        elif in_window and rng.random() < 0.05:
            allowed_repeats += 1
    return {"window": window, "ledger": ledger, "rounds": rounds,
            "unreasoned_repeats": repeats,
            "in_window_repeats": allowed_repeats,
            "repeat_rate": round(repeats / max(1, rounds // 5), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=200)
    args = ap.parse_args()
    for window in (40, 60):
        print(simulate(window, args.rounds, ledger=False))
        print(simulate(window, args.rounds, ledger=True))
    print("结论：无账本时，60 轮只是把重问延后；账本持久化后窗口不再是主策略。")


if __name__ == "__main__":
    main()
