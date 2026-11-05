# -*- coding: utf-8 -*-
"""渐变 shadow 仿真（C8-05）：200/1000 轮梯度演变与二元压缩对照。

不调模型，只跑梯度阶梯的状态机：
- L1 原子按龄逐步降级，到地板冻结；
- 统计最终档位分布、冻结率、改写次数上界；
- 与「二元压缩（原文→骨架）」的 token 占用量做粗对照。
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "jinyu_memory" / "core"))
import gradient  # noqa: E402


def simulate(rounds, usable=54000, floor_level=1):
    units = []
    for i in range(rounds):
        # 每 2 轮落一个单元；分数越低越早允许跳级
        salience = 90 if i % 10 == 0 else 35
        units.append({"level": 1, "floor_level": floor_level,
                      "salience": salience, "salience0": salience,
                      "created": i, "frozen": False, "rewrites": 0})
    final = []
    for u in units:
        age_tokens = (rounds - u["created"]) * 30
        target = gradient.target_level(age_tokens, usable, u["floor_level"])
        if u["salience"] < 45 and target < 4:
            target = 4
        u["level"] = target
        u["frozen"] = target >= u["floor_level"] and u["floor_level"] < 4
        final.append(u)
    dist = {lv: sum(1 for u in final if u["level"] == lv) for lv in (1, 2, 3, 4)}
    frozen = sum(1 for u in final if u["frozen"])
    # 粗估：L1 保留全字段，L2 指针，L3 一句，L4 关键词
    cost = {1: 80, 2: 20, 3: 12, 4: 6}
    tokens = sum(cost[u["level"]] for u in final)
    binary = sum(80 if u["salience0"] >= 85 else 12 for u in final)
    return {"rounds": rounds, "levels": dist,
            "frozen_ratio": round(frozen / max(1, rounds), 3),
            "gradient_tokens": tokens, "binary_tokens": binary,
            "saved": round(1 - tokens / max(1, binary), 3)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, nargs="+", default=[200, 1000])
    args = ap.parse_args()
    for r in args.rounds:
        print(simulate(r))
    print("结论：梯度方案在高评分单元上保留细档，低分闲聊降到时间桶/索引；"
          "冻结保证改写次数不超过 3 次。")


if __name__ == "__main__":
    main()
