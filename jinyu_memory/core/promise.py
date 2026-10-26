# -*- coding: utf-8 -*-
"""承诺生命周期（阶段二 Q1）：状态机 + 中间件签发 ID + bigram 匹配 + 淘汰

权威源是账本（Grok / ChatGPT / GLM 三家一致）：
- 后台模型只能"提议"，这里做确定性提交，模型无权直接改状态
- ID 由中间件签发；模型只在候选清单里选，选错由这里的校验兜住
- 匹配 best>=0.25 且 best-second>=0.15 才唯一命中；否则弃权，承诺保持 open
- 兑现出账进 resolved；非 strict 承诺 TTL 40 轮 / 活跃至多 8 条，strict 豁免

状态迁移（ChatGPT 的迁移表，收敛为四个终态）：
  created --兑现--> fulfilled
  created --超龄--> expired（时间规则触发，不等于违约）
  created --超量--> dormant（移出 prompt，可被后续召回，不算违约）
  created --显式替换/明确违约--> superseded / broken（保留接口，当前由人审触发）
普通角色内对话只是"证据"，永远不直接改状态。
"""
import re

MIN_SCORE = 0.25        # GLM 实测：改写对 0.25~0.71，无关对 0.00
MIN_MARGIN = 0.15       # ChatGPT：best 与 second 拉不开时弃权
TTL_TURNS = 40
MAX_ACTIVE = 8
MAX_STRICT = 12         # Grok Q2：strict 软上限，超出移入 dormant（不算违约）
RESOLVED_KEEP = 100     # Gemini A1：resolved 只留最近 100 条，账本 JSON 不无界增长

# 豆包词表：强承诺标记（命中且剥离后仍有动作对象才判 strict）
STRONG = (r"答应", r"保证", r"发誓", r"一定(要|会|给你|还你|回来|到)", r"说好了?", r"一言为定",
          r"拉钩", r"说到做到", r"说话算话", r"绝不(反悔|食言|告诉别人)", r"我欠你",
          r"替你保密", r"这事不外传", r"烂在肚子里", r"我站你这边")
# 豆包词表：客套 / 弱承诺，命中即禁止升 strict
POLITE = (r"改天", r"有空(再说|聚|聊)", r"下次一定", r"回头(聊|说|看)", r"过两天",
          r"以后再说", r"到时候看", r"尽量吧", r"争取(吧|能)", r"看吧",
          r"下次再也不", r"别后悔")
# 豆包 Q4：纯自我约束不算对他人承诺
SELF_ONLY = re.compile(r"再也不|别后悔|再犯|下次再也不|再不(迟到|弄丢|犯)")
# 豆包 Q4：strict 必须带交付对象或保密对象，否则只是自我表态
DELIVER = re.compile(r"你|客人|他|她|对方|替你|帮你|给你|把.{1,4}(给|还|交|送)")
# 保密类：模型常把「保密」改写成「保守秘密」「守住旧事」，只认「保密」会漏 strict。
# 这里按语义族收，不要求固定措辞；提示词仍要求写「保密」，两条路径互为兜底。
SECRET = (r"保密", r"不外传", r"烂在肚子里", r"守口如瓶", r"不说出去",
          r"不告诉(别人|第三个人)", r"不(往外|对外)(说|讲|提)",
          r"(保守|守住|保住).{0,6}秘密", r"秘密.{0,8}(不说|不告诉|不外传|烂在)",
          r"不提(这事|那事|旧事|此事)")

# GLM 实测：句首承诺动词是样板不是身份。剥掉后「答应帮掌柜修伞」不再错关「答应带伞」，
# 且「带伞的事我一直记着」对「答应带伞」从 0.333 升到 1.0。
PROMISE_VERB = re.compile(r"^(答应|承诺|保证|说好了?|替你|帮你|给你|一定)+")

# 豆包 Q1：同批「既建又兑」只有在有完成态证据时才允许关闭；否则只是口头重申
BROKEN = (r"没做到", r"食言", r"办砸了", r"没能(兑现|做到|还)", r"辜负(了你|我)?",
          r"答应了却", r"爽约", r"说好的事我没")
FULFILL = (r"已经(把|给|替)?", r"把.{1,6}(收|递|带|交|还|修|办)好?", r"收好了", r"递过去",
           r"带来了", r"交到(他|你|客人)手上", r"还清了", r"修好了", r"办妥了",
           r"当场(就)?(给|把|替)", r"这就(给|把)", r"说完就(给|把|收|递)")
# 问句不是兑现证据：「用不用带伞来」不能关掉「带伞」
QUESTION = re.compile(r"吗|？|用不用|是不是|要不要|会不会|能不能|有没有")

# 注入前清控制字符与 bidi 覆盖符（ChatGPT Q1 的确定性边界）
CTRL = re.compile(r"[\u0000-\u001f\u202a-\u202e\u2066-\u2069]")


def sanitize(s, limit=40):
    return CTRL.sub("", str(s or "")).strip()[:limit]


def _has(pats, text):
    return any(re.search(p, text or "") for p in pats)



def _clean(s):
    return re.sub(r"[\s，。！？、；：…—\"'（）「」]", "", str(s or ""))


def bigrams(s):
    s = _clean(s)
    return {s[i:i + 2] for i in range(len(s) - 1)} or ({s} if s else set())


def overlap(a, b):
    """剥句首承诺动词后取 bigram 重叠系数（分母取短侧）。

    一侧剥空（宾语省略，如兑现只写「答应」）只认全等，避免把「答应」当万能键。
    """
    ca = PROMISE_VERB.sub("", _clean(a))
    cb = PROMISE_VERB.sub("", _clean(b))
    if not ca or not cb:
        return 1.0 if ca == cb else 0.0
    A, B = bigrams(ca), bigrams(cb)
    return len(A & B) / min(len(A), len(B))


def is_strict(text):
    """强标记 ∧ 有交付/保密对象 ∧ 非客套 ∧ 非纯自我约束。宁可漏判，不可误判。"""
    t = str(text or "")
    if any(re.search(p, t) for p in POLITE) or SELF_ONLY.search(t):
        return False
    if not any(re.search(p, t) for p in STRONG):
        return False
    if not (DELIVER.search(t) or any(re.search(p, t) for p in SECRET)):
        return False
    return len(_clean(t)) >= 4


def ensure_shape(lg):
    """旧账本的字符串承诺就地升级为 dict，补齐 pseq/resolved/dormant/unmatched（幂等）。"""
    lg.setdefault("pseq", 0)
    lg.setdefault("resolved", [])
    lg.setdefault("dormant", [])
    lg.setdefault("unmatched", [])
    ps = lg.get("promises") or []
    for i, p in enumerate(list(ps)):
        if isinstance(p, str):
            lg["pseq"] += 1
            ps[i] = {"id": f"P-{lg['pseq']:03d}", "text": p,
                     "created_turn": 0, "strict": is_strict(p)}
    lg["promises"] = ps
    return lg


def add_pending(lg, texts, turn):
    """新增承诺。近似重复（overlap>=0.7）不重复入账。返回新增 ID。

    已知特性（不修）：去重只看字面 bigram overlap，跨批次语义复述一旦措辞差异
    大于阈值就会各入一条，兑现侧也跟着重复计数（实测 50 轮：1 条剧本意图 →
    5 条文本、closed_total 4 > 意图 2）。这是「宁多勿丢」方向的设计内误差，
    上界由 MAX_ACTIVE / MAX_STRICT / TTL / RESOLVED_KEEP 兜住，指标按趋势读。
    真要收窄得做跨批次语义去重，优先级低。
    """
    added = []
    for t in texts or []:
        t = sanitize(t)
        if not t:
            continue
        if any(overlap(t, p["text"]) >= 0.7 for p in lg["promises"]):
            continue
        lg["pseq"] += 1
        p = {"id": f"P-{lg['pseq']:03d}", "text": t,
             "created_turn": turn, "strict": is_strict(t)}
        lg["promises"].append(p)
        added.append(p["id"])
    return added


def resolve(lg, items, turn, evidence=""):
    """兑现：ID 直配优先，否则文本匹配；歧义/无匹配一律弃权（不删、不猜）。

    evidence 是本批摘要。豆包 Q1：
    - 有违约词 → 本批不允许关闭任何承诺（宁漏勿错）
    - 同批新建的承诺必须有完成态证据才算 fulfilled，否则只是口头重申
    - 问句永远不是兑现证据（「用不用带伞来」不能关掉「带伞」）

    返回 (fulfilled_ids, unmatched_texts, restated_ids)。
    """
    fulfilled, unmatched, restated = [], [], []
    broken = _has(BROKEN, evidence)
    fulfilled_ev = _has(FULFILL, evidence)
    for raw in items or []:
        r = sanitize(str(raw))
        if not r:
            continue
        if broken or (QUESTION.search(r) and not fulfilled_ev):
            unmatched.append(r)
            continue
        hit = next((p for p in lg["promises"] if p["id"] == r), None)
        matched_by = "id" if hit is not None else ""
        if hit is None:
            scored = sorted(((overlap(r, p["text"]), p) for p in lg["promises"]),
                            key=lambda x: x[0], reverse=True)
            best = scored[0][0] if scored else 0.0
            second = scored[1][0] if len(scored) > 1 else 0.0
            if best >= MIN_SCORE and best - second >= MIN_MARGIN:
                hit = scored[0][1]
                matched_by = "text"
        if hit is None:
            unmatched.append(r)
            continue
        if hit.get("created_turn") == turn and not fulfilled_ev:
            restated.append(hit["id"])          # 同批建兑但只有应允语：保持 open
            continue
        lg["promises"].remove(hit)
        lg["resolved"].append({**hit, "status": "fulfilled",
                               "resolved_turn": turn,
                               "evidence": hit["text"][:40],   # 存承诺原文，不存提交的 ID
                               "matched_by": matched_by})      # id / text：可回溯匹配路径
        fulfilled.append(hit["id"])
    if unmatched:
        lg["unmatched"].extend({"text": t, "turn": turn} for t in unmatched)
        overflow = max(0, len(lg["unmatched"]) - 20)
        if overflow:                                   # 被截断的待人工条目必须可计数
            lg["unmatched_dropped"] = lg.get("unmatched_dropped", 0) + overflow
        del lg["unmatched"][:-20]          # 只留最近 20 条待人工
    return fulfilled, unmatched, restated


def evict(lg, turn, ttl=TTL_TURNS, cap=MAX_ACTIVE, strict_cap=MAX_STRICT):
    """淘汰：非 strict 超龄 → expired；超量 → dormant（移出 prompt，不算违约）。

    strict 豁免 TTL，但按 Grok Q2 设软上限 12 条：超出的最旧项进 dormant，
    可被规则召回重新激活；resolved 只留最近 100 条，避免账本 JSON 无界增长。
    """
    moved = []
    for p in list(lg["promises"]):
        if p.get("strict"):
            continue
        if turn - p.get("created_turn", 0) > ttl:
            lg["promises"].remove(p)
            lg["resolved"].append({**p, "status": "expired", "resolved_turn": turn})
            moved.append(p["id"])
    soft = [p for p in lg["promises"] if not p.get("strict")]
    if len(soft) > cap:
        for p in sorted(soft, key=lambda q: q.get("created_turn", 0))[:len(soft) - cap]:
            lg["promises"].remove(p)
            lg["dormant"].append(p)
            moved.append(p["id"])
    hard = [p for p in lg["promises"] if p.get("strict")]
    if len(hard) > strict_cap:
        for p in sorted(hard, key=lambda q: q.get("created_turn", 0))[:len(hard) - strict_cap]:
            lg["promises"].remove(p)
            lg["dormant"].append({**p, "dormant_reason": "strict_cap"})
            moved.append(p["id"])
    if len(lg["resolved"]) > RESOLVED_KEEP:
        lg["resolved_dropped"] = lg.get("resolved_dropped", 0) + len(lg["resolved"]) - RESOLVED_KEEP
        del lg["resolved"][:-RESOLVED_KEEP]
    return moved


def prompt_lines(lg, limit=None):
    """给后台模型的候选清单：把自由文本配对变成选择题。

    默认不截断：前台注入的是全部活跃承诺，候选清单必须与之同集合，
    否则第 7 条之后的承诺模型看得见却兑不了。
    """
    ps = lg["promises"] if limit is None else lg["promises"][:limit]
    return [f"- {p['id']} {p['text']}" for p in ps]


def texts(lg):
    return [p["text"] for p in lg["promises"]]
