# -*- coding: utf-8 -*-
"""合并候选生成：向量近邻召回，模型只负责判定。

把 80 条卡 / 400 条事实排成一列交给生成式模型「自己找重复」，等于让它隐式
做 O(n²) 配对，实测一屏 200 张卡时整组漏判。这里换分工：

- 召回：embedding 余弦，只输出「可能是同一件事」的对子；
- 过滤：同一说话人（卡片另加时间邻近）才允许成对；
- 裁决：模型后面只看这些对子，判是/不是，不再扫全表。

纯函数，不依赖数据库和 NoneBot。
"""
import math


def normalize(vec) -> list:
    norm = math.sqrt(sum(float(x) * float(x) for x in vec or []))
    return [float(x) / norm for x in vec] if norm else []


def _speaker(row) -> str:
    return str(row.get("speaker") or "").strip()


def _turn(row) -> int:
    try:
        return int(row.get("turn_end") or 0)
    except (TypeError, ValueError):
        return 0


def pairs(rows, vectors, threshold=0.72, top_k=4, same_speaker=True,
          max_turn_gap=None):
    """返回相似对：[{"a": id, "b": id, "sim": 余弦}]，按相似度降序。

    rows: [{"id", "fact"/"summary", "speaker", "turn_end"}]
    vectors: {id: 归一化向量}
    同一个条目最多进 top_k 个对子，避免一张泛卡连上十几条。
    """
    items = [(r, vectors.get(r.get("id"))) for r in rows]
    items = [(r, v) for r, v in items if v]
    found = []
    for i in range(len(items)):
        ri, vi = items[i]
        for j in range(i + 1, len(items)):
            rj, vj = items[j]
            si, sj = _speaker(ri), _speaker(rj)
            if same_speaker and si and sj and si != sj:
                continue
            if max_turn_gap is not None:
                ti, tj = _turn(ri), _turn(rj)
                if ti and tj and abs(ti - tj) > max_turn_gap:
                    continue
            sim = sum(float(a) * float(b) for a, b in zip(vi, vj))
            if sim >= threshold:
                found.append((sim, i, j))
    found.sort(reverse=True)
    out, degree = [], {}
    for sim, i, j in found:
        if degree.get(i, 0) >= top_k or degree.get(j, 0) >= top_k:
            continue
        degree[i] = degree.get(i, 0) + 1
        degree[j] = degree.get(j, 0) + 1
        out.append({"a": items[i][0]["id"], "b": items[j][0]["id"],
                    "sim": round(sim, 4)})
    return out


def compare(found, groups) -> dict:
    """shadow 对比：向量对里有多少被模型组覆盖。

    groups 是 merge_facts / merge_cards 的返回值，sources 里带原始 id。
    """
    sets = [set(int(s["id"]) for s in (g.get("sources") or [])
                if s.get("id") is not None)
            for g in groups]
    covered, ann_only = 0, []
    for p in found:
        if any(p["a"] in s and p["b"] in s for s in sets):
            covered += 1
        else:
            ann_only.append(p)
    missed = sum(1 for s in sets
                 if not any(p["a"] in s and p["b"] in s for p in found))
    return {"pairs": len(found), "covered": covered, "ann_only": ann_only,
            "missed_groups": missed}
