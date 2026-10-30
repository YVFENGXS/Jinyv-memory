# -*- coding: utf-8 -*-
"""后台摘要器（阶段二）：prompt + 解析 + 校验 clamp + 一次自修复重试

设计依据（四家评审收敛后的结论）：
- 单角色：character_perspective 只写 AI 角色怎么理解，不揣测玩家心理
- entity_hooks 必须有判定边界，否则抽出的抽象词全是噪声钩子
- knowledge_boundary 用三档可见性，而不是不存在的角色 ID
- confidence 按档位自评，禁止默认 0.9
- 校验失败先把错误回喂模型重试一次，仍失败才交给 archive_failures
"""
import json, re

import retriever

PROMPT = """你是角色扮演记忆整理员。只输出一个 JSON 对象，不要解释文字，不要 markdown 代码块。

规则：
1. 只基于给定对话，不推测未发生的事；玩家没说出口的心理、计划一律不写。
2. 本项目只有【一个 AI 角色】。summary 写"谁和谁发生了什么"，因果写进同一句；
   玩家的言行只作客观事实，禁止写"玩家心想/玩家打算"。
3. entity_hooks：只收具体可指代的对象（对玩家的称呼、物品、地点、专名），
   用对话里出现的原词，2-6 字；禁止情绪词（难过/开心）、动作词（告别/约定）、
   抽象概念；同一对象的多种称呼合并为最常用的那个；2-5 个，宁缺毋滥。
4. rel_hooks：关系模式 / 承诺 / 因果，2-3 个，不得与 entity_hooks 重复。
5. knowledge_boundary 只能取三档之一："self"（仅角色知道）、"player"（仅玩家知道）、
   "both"（双方都知道）。属于秘密且对方不知的，不得填 "both"。
6. precision：明确承诺、核心立场表态、保密约定 → "strict"，其余 "normal"。
   pending_promises 每条必须写清「答应/保证 + 给谁 + 做什么」，受益对象必须出现
   （主人 / 角色 / 某个 NPC 的名）；只写动作、只写「答应」的条目无效，不要写。
   保密类必须出现「保密」二字并写清为谁保密，例如「答应替主人保密，不把旧事说出去」；
   禁止改写成「保守秘密」「不告诉别人」这类同义说法（会让召回与 strict 判定漏掉）。
   角色确认玩家转述的旧承诺（「作数」「记得」「我答应过」）时，也要写入 pending_promises。
7. confidence 按档位自评，禁止默认 0.9：0.9-1.0 事实清楚；0.7-0.85 主干清楚细节模糊；
   <0.7 指代不明（会被转人工，宁可调低）。
8. claims：可核验的事实主张，0-3 条，字段固定为 subject/slot/value。
   slot 只能取六种之一："归属"（谁拥有/保管）、"立场"（对某人某事的态度）、
   "状态"（物品/设施的完好程度）、"位置"（所在处）、"关系"（两人关系的性质）、
   "知情"（谁知道某件事）。subject 必须原样取自 entity_hooks；value ≤10 字。
   矛盾判定只覆盖"归属/状态/知情"，取值必须落在下列集合内：
   __SLOT_VALUES__
   其他写法（如"客官"）请归一到最近的合法值，或改写为立场/关系；落不进集合的
   只作为记录，不参与矛盾判定。用世界视角称呼：AI 角色写"角色"、玩家写"主人"，
   不要写"我/他/她"。同 subject+slot 出现不同取值时只留较晚的一条。
9. durable_facts：值得长期记住的稳定事实，0-3 条，每条是一个对象：
   {"fact":"≤30 字的陈述句","tags":["2-4 个检索标签"],
    "category":"设定|事件|临时","importance":0~1,"ttl_hours":数字或 null,
    "emotion":0~1,"authority":0~1}
   - 设定：宠物/重要物品的名字与归属、主人的稳定偏好与设定、明确说「记住」的内容；
     importance ≥0.8，ttl_hours 填 null（永久有效）
   - 事件：三天后仍值得记得的经历；importance 0.4~0.7，ttl_hours 填 null
   - 临时：只在当下成立的临时状态（反复发送失败、正在忙之类）；
     importance ≤0.3，ttl_hours 填预计有效小时数（1~48）
   - tags 写以后会用来找它的名词（如 ["猫","团子","宠物"]），不要写情绪词
   - emotion：这条事实承载的情感强度。平常告知 0.3；明确的高兴 / 难过 / 生气 /
     害怕 / 珍视 0.8~1.0；拿不准填 0.5。
   - authority：这条事实的来源权威。主人明确说出的设定 / 偏好 / 承诺 0.9~1.0；
     角色自己的观察或推测 0.4~0.6；普通对话里的事实 0.7；拿不准填 0.7。
   不算：情绪、一次性的口气、本次对话才出现的临时指代。
10. subject：这张卡是「关于谁」的，只能取 "主人"、"角色"，或 entity_hooks 里的一个原词；
    判断不出就填空字符串。subject 不是「谁在说话」，不要把动作主语填进来。
11. durable_facts 每条再加 "speaker"：这条事实是谁提供/经历的，同样只能取
    "主人"、"角色" 或 entity_hooks 里的一个原词；不确定填空字符串。
12. durable_facts 和 claims 只写「世界里发生了什么」，不写「谁知道 / 谁不知道」的系统状态。
    角色自己「不知道、没记住、翻不出来、记忆里没有、没听说过某个词」是这次检索的结果，
    不是世界事实；写进长期记忆会让下一轮把「查不到」当成已确认的结论。这类内容
    一律不写进 durable_facts 或 claims。「知情」槽位只写对话里明确建立的知道 / 保密关系，
    拿不准对方是否知情就留空，不要用「不知道」占位。

输出格式：
{"summary":"一句话：谁因为什么做了什么",
 "state_changes":{"mood":"...","location":"...","relationship":{"trust":0.2},
                  "pending_promises":["..."],"resolved_promises":["P-003"],
                  "claims":[{"subject":"旧伞","slot":"归属","value":"主人"}]},
 "durable_facts":[{"fact":"主人养了一只猫，叫团子","tags":["猫","团子","宠物"],
                   "category":"设定","importance":0.9,"ttl_hours":null,
                   "emotion":0.8,"authority":0.9,"speaker":"主人"}],
 "entity_hooks":["..."],"rel_hooks":["..."],"knowledge_boundary":"both",
 "subject":"主人","importance":0.5,"precision":"normal","confidence":0.8}

{open_promises}
对话片段：
"""


def _slot_values_line():
    """prompt 里的合法值清单由 retriever.SLOT_VALUES 生成，扩值集只改常量一处。"""
    return "；".join(f"{slot} = {'/'.join(sorted(retriever.SLOT_VALUES[slot]))}"
                    for slot in ("归属", "状态", "知情"))


PROMPT = PROMPT.replace("__SLOT_VALUES__", _slot_values_line())

REPAIR = "上一条回复不是合法的事件卡 JSON，问题：{err}。请只输出符合格式的 JSON，不要任何其他文字。"

# 社交圈模式：多人共用一份记忆时，事实必须标清属于谁，否则「喜欢猫」会串人
SOCIAL_RULE = """
13. 这是多人社交圈：每条用户消息带「[群号]/[私聊] 昵称(QQ)：」前缀。durable_facts 每条
    必须加 "about" 字段，填冒号前的说话人标签原词（形如「小明(12345)」，含括号里的 QQ，
    不要带群号或「私聊」）；与具体的人无关的公共事实填 ""。不要把两个人的事实合成一条。
"""

BOUNDARIES = ("self", "player", "both")
PRECISIONS = ("strict", "normal")
# 主体档位：卡/事实「关于谁」。这两个是固定世界视角，其余只能来自 entity_hooks。
SUBJECT_ROLES = ("主人", "角色")

# 封闭 claims 词表：槽位由代码定死，subject 必须来自 entity_hooks。
# 好处是同一事实在两次摘要里必然落在同一个 key 上，矛盾才可能被确定性检出。
CLAIM_SLOTS = ("归属", "立场", "状态", "位置", "关系", "知情")
CLAIM_ALIASES = {"所有权": "归属", "拥有": "归属", "归属方": "归属",
                 "态度": "立场", "好感": "立场", "倾向": "立场",
                 "状况": "状态", "完好度": "状态", "生死": "状态",
                 "所在地": "位置", "地点": "位置",
                 "身份关系": "关系", "称谓": "关系",
                 "知情范围": "知情", "知道的人": "知情"}
_LEAD_OBJ = re.compile(r"^(那|这|该|一)?(把|个|只|件|位|张|间|条)?")

WORD = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")          # 至少一个实义字符，排除「。。。」「???」
CTRL = re.compile(r"[\u0000-\u001f\u202a-\u202e\u2066-\u2069]")   # ChatGPT Q1：控制符/bidi

# 防污染：这些词都是「这次没翻到」的检索状态，不是世界里发生的事。
# 线上出现过「瑾瑜的记忆里没有『鲸八股』这个词」落进 durable_facts（#361），
# 下一轮就把它当成已确认结论。检索词类不看主语，一律丢；普通否定只在角色自己身上丢
# （「主人还不知道真相」可以是剧情事实，不能一刀切）。
SELF_MISS = re.compile(r"(记忆(里|中)?(没有|没|不存在)|脑子里没有|翻不出来|没翻到|查不到|"
                       r"想不起来|没记住|没印象|没听说过)")
SELF_UNKNOWN = re.compile(r"(不知道|不记得)")

PROMISE_HINT = ("当前未兑现承诺（本批若兑现其中某条，resolved_promises 只填它的 ID；"
                "都不涉及则填空数组）：\n")


def _messages(dialogue, open_promises=(), social=False):
    hint = PROMISE_HINT + "\n".join(open_promises) + "\n" if open_promises else ""
    prompt = PROMPT + (SOCIAL_RULE if social else "")
    return [{"role": "user", "content": prompt.replace("{open_promises}", hint) + dialogue}]


def parse(text):
    """三级兜底：裸 JSON → ```json 围栏 → 首尾花括号切片。"""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    lo, hi = text.find("{"), text.rfind("}")
    if lo != -1 and hi > lo:
        try:
            return json.loads(text[lo:hi + 1])
        except json.JSONDecodeError:
            pass
    return None


def _clamp(x, lo=0.0, hi=1.0, default=0.5):
    try:
        return round(max(lo, min(hi, float(x))), 3)
    except (TypeError, ValueError):
        return default


def _terms(values, limit):
    if not isinstance(values, list):
        return None
    out = []
    for v in values:
        if isinstance(v, str):
            t = CTRL.sub("", v).strip()
            if 1 <= len(t) <= 8 and t not in out:
                out.append(t)
    return out[:limit]


def _subject(raw, hooks):
    """主体归一：只接受两个固定世界视角或本卡已有的钩子，其余一律空。

    不猜、不模糊匹配：写不出可核验的主体就当没有，避免把「他」这类代词落库后
    在跨会话里被误当成某个具体的人。
    """
    s = _LEAD_OBJ.sub("", CTRL.sub("", str(raw or "")).strip()).strip("的")[:6]
    return s if (s in SUBJECT_ROLES or s in hooks) else ""


def validate(data):
    """返回 (fields, None) 或 (None, 错误原因)。错误原因会回喂模型做自修复。"""
    if not isinstance(data, dict):
        return None, "not_object"
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None, "missing_or_empty:summary"
    if not WORD.search(summary):
        return None, "summary_has_no_content"        # Gemini B1：合法 JSON 但语义为空
    sc = data.get("state_changes", {})
    if not isinstance(sc, dict):
        return None, "state_changes_not_object"
    hooks = _terms(data.get("entity_hooks"), 8)
    if hooks is None:
        return None, "entity_hooks_not_list"
    rel = _terms(data.get("rel_hooks", []), 5)
    if rel is None:
        return None, "rel_hooks_not_list"
    boundary = data.get("knowledge_boundary", "both")
    if boundary not in BOUNDARIES:
        return None, f"knowledge_boundary_must_be:{'/'.join(BOUNDARIES)}"
    if not isinstance(sc.get("relationship", {}), dict):
        return None, "relationship_not_object"
    for key in ("pending_promises", "resolved_promises"):
        v = sc.get(key)
        if v is not None and not isinstance(v, list):
            return None, f"{key}_not_list"
    # claims：跨会话矛盾消解的唯一确定性依据。槽位走封闭词表，subject 必须与
    # entity_hooks 同名，否则丢弃该条（不整卡判失败，避免一条噪声卡住整批）。
    card_subject = _subject(data.get("subject"), hooks)
    claims = sc.get("claims")
    if claims is not None and not isinstance(claims, list):
        return None, "claims_not_list"
    clean_claims = []
    for cl in claims or []:
        if not isinstance(cl, dict):
            continue
        subject = _LEAD_OBJ.sub("", CTRL.sub("", str(cl.get("subject") or "")).strip()).strip("的")[:6]
        slot = CLAIM_ALIASES.get(CTRL.sub("", str(cl.get("slot") or "")).strip(),
                                 CTRL.sub("", str(cl.get("slot") or "")).strip())
        value = CTRL.sub("", str(cl.get("value") or "")).strip()[:12]
        if not subject or not value or slot not in CLAIM_SLOTS or subject not in hooks:
            continue
        clean_claims.append({"subject": subject, "slot": slot, "value": value})
    if clean_claims:
        sc["claims"] = clean_claims[:3]
    else:
        sc.pop("claims", None)
    # durable_facts：后台判定并打标签（用户 2026-10-26：事实要带标签/热力/时效）。
    # 老格式的纯字符串仍然接受，统一归一成对象，下游只处理一种形状。
    facts = data.get("durable_facts")
    if facts is not None and not isinstance(facts, list):
        return None, "durable_facts_not_list"
    clean_facts, blank = [], []
    for f in facts or []:
        item = f if isinstance(f, dict) else {"fact": f}
        t = CTRL.sub("", str(item.get("fact") or "")).strip()[:40]
        if not (2 <= len(t) <= 40 and WORD.search(t)):
            continue
        spk = _subject(item.get("speaker"), hooks)
        if SELF_MISS.search(t) or (SELF_UNKNOWN.search(t) and "角色" in (spk, card_subject)):
            blank.append(t)            # 检索状态不是世界事实：丢弃，并记账供下面清 claims
            continue
        about = CTRL.sub("", str(item.get("about") or "")).strip()[:32]
        if any(t == x["fact"] and about == x["about"] for x in clean_facts):
            continue
        tags = []
        for raw in item.get("tags") or []:
            tag = CTRL.sub("", str(raw)).strip()[:12]
            if tag and tag not in tags:
                tags.append(tag)
        cat = CTRL.sub("", str(item.get("category") or "")).strip()
        ttl = item.get("ttl_hours")
        try:
            hours = float(ttl) if ttl is not None else 0.0
        except (TypeError, ValueError):
            hours = 0.0
        clean_facts.append({
            "fact": t,
            "about": about,
            "speaker": spk,
            "tags": tags[:4],
            "category": cat if cat in ("设定", "事件", "临时") else "",
            "importance": _clamp(item.get("importance", 0.5)),
            # 四因子里的 E / A：模型没写或写坏时退回默认值（中等情感、归档摘要权威），
            # 与老行默认值一致，不会因为这一批模型偷懒就打乱原有排序。
            "emotion": _clamp(item.get("emotion", 0.5)),
            "authority": _clamp(item.get("authority", 0.7), default=0.7),
            "ttl_hours": round(max(0.0, min(72.0, hours)), 2) or None})
    # 过滤后为空也要覆写：留着原列表等于让被丢弃的事实从 out 里原样漏出去。
    data["durable_facts"] = clean_facts[:3]
    # 同一条对话常常同时写着「记忆里没有X」和 claim「X·知情=不知道」：
    # 前者已判为检索状态，后者同样是这次没翻到的产物，一起清掉（#361 / E-e9deb63a）。
    if blank and sc.get("claims"):
        sc["claims"] = [cl for cl in sc["claims"]
                        if not (cl["slot"] == "知情" and cl["value"] == "不知道"
                                and any(cl["subject"] in b for b in blank))]
        if not sc["claims"]:
            sc.pop("claims", None)
    # ChatGPT Q1：所有会进账本/前台的字符串先清控制符并截断
    for key in ("pending_promises", "resolved_promises"):
        if isinstance(sc.get(key), list):
            sc[key] = [CTRL.sub("", str(x)).strip()[:40] for x in sc[key]
                       if CTRL.sub("", str(x)).strip()]
    precision = data.get("precision", data.get("precision_requirement", "normal"))
    out = {
        "summary": CTRL.sub("", summary).strip()[:200],
        "state_changes": sc,
        "entity_hooks": hooks,
        "rel_hooks": rel,
        "knowledge_boundary": boundary,
        "subject": card_subject,
        "importance": _clamp(data.get("importance", 0.5)),
        "precision": precision if precision in PRECISIONS else "normal",
        "confidence": _clamp(data.get("confidence", 0.8), default=0.8),
    }
    if data.get("durable_facts"):
        out["durable_facts"] = data["durable_facts"]
    return out, None


def summarize(back_llm, dialogue, open_promises=(), max_repair=1, social=False):
    """调后台模型产出事件卡字段。校验失败时回喂错误重试一次（默认 1 次）。

    open_promises：账本当前未兑现承诺的候选清单（"P-003 答应带伞"），
    让模型做选择题而不是问答题；它仍只是"提议"，最终由 promise.resolve 提交。

    返回 (fields, None) 或 (None, err)。异常（网络/限流）直接抛出，
    由调用方按归档失败处理，因为这不是格式问题，重打也没用。
    """
    msg = _messages(dialogue, open_promises, social)
    raw = back_llm.chat(msg)
    err = None
    for attempt in range(max_repair + 1):
        data = parse(raw)
        if data is None:
            err = "parse_fail"
        else:
            fields, err = validate(data)
            if err is None:
                return fields, None
        if attempt >= max_repair:
            break
        raw = back_llm.chat(msg + [
            {"role": "assistant", "content": (raw or "")[:1500]},
            {"role": "user", "content": REPAIR.format(err=err)},
        ])
    return None, err
