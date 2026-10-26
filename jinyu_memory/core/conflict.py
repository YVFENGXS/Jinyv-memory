# -*- coding: utf-8 -*-
"""记忆层冲突检测（阶段二）：只标注，不拦截

命中只写 conflict_flags，事件卡照常入库。理由：角色故意撒谎、剧情反转都会
产生"看似矛盾"的事件，让后台模型有删卡权限比留一张可疑卡危险得多。

阈值按 GLM 的假阳性账收窄（每条都在代码里复现过）：
- R1 0.3 → 0.5：RP 里 -0.4/+0.3 的常态起伏不该算立场反转
- R2 改为 bigram 判定，但只有"唯一命中"才算合法兑现，其余照报
- R3 加通用称呼停用表：PROMPT 把"对玩家的称呼"列为钩子，'客人'几乎每张卡都有
"""
import re

from promise import overlap

STOP_HOOKS = {"客人", "姑娘", "公子", "大人", "兄台", "阁下", "那人", "这家",
              "东西", "事情", "地方", "他们", "我们", "你们"}


def _norm(s):
    return re.sub(r"\s+", "", str(s or ""))


def _hook_ok(h):
    return isinstance(h, str) and len(h) >= 2 and h not in STOP_HOOKS


def check(summary, state_changes, entity_hooks, boundary, ledger, existing_cards,
          pending_promises=()):
    """返回 flags 列表。任一规则命中即追加一个可读标记。"""
    flags = []
    sc = state_changes or {}

    # R1 信任值反号且幅度不小：可能前后立场矛盾
    rel = sc.get("relationship") or {}
    if isinstance(rel, dict):
        prev_map = (ledger or {}).get("relationship") or {}
        for k, v in rel.items():
            try:
                delta = float(v)
                prev = float(prev_map.get(k, 0.0))
            except (TypeError, ValueError):
                continue
            if prev and delta and prev * delta < 0 and abs(delta) >= 0.5:
                flags.append(f"trust_sign_flip:{k}")

    # R2 记为已兑现、但账本里既没有这个 ID，也没有能唯一对上的文本
    # 同批新建的承诺还没入账，必须一并纳入，否则「本批既建又兑」会被误报
    prem = [p for p in ((ledger or {}).get("promises") or []) if isinstance(p, dict)]
    ids = {p.get("id") for p in prem}
    texts = [p.get("text") or "" for p in prem]
    texts += [t for t in (str(x).strip()[:40] for x in (pending_promises or [])) if t]
    for r in (sc.get("resolved_promises") or []):
        r = str(r).strip()
        if r in ids:
            continue
        scored = sorted((overlap(r, t) for t in texts), reverse=True)
        best = scored[0] if scored else 0.0
        second = scored[1] if len(scored) > 1 else 0.0
        if not (best >= 0.25 and best - second >= 0.15):
            flags.append(f"resolve_unknown_promise:{r[:20]}")

    # R3 同一实体此前是「仅角色知道」，这张卡却写成双方都知道：可能提前说破
    if boundary == "both" and entity_hooks:
        mine = {h for h in entity_hooks if _hook_ok(h)}
        for c in existing_cards:
            if (c.get("knowledge_boundary") or "both") != "self":
                continue
            if mine & {h for h in (c.get("entity_hooks") or []) if _hook_ok(h)}:
                flags.append("reveal_without_consent")
                break

    # R4 摘要与本区间已有卡完全重复：归档批次重叠或后台模型偷懒
    for c in existing_cards:
        if _norm(c.get("summary")) == _norm(summary):
            flags.append("duplicate_summary")
            break

    return flags
