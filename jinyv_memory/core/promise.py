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
from datetime import datetime, timedelta

MIN_SCORE = 0.25        # GLM 实测：改写对 0.25~0.71，无关对 0.00
MIN_MARGIN = 0.15       # ChatGPT：best 与 second 拉不开时弃权
TTL_TURNS = 40
MAX_ACTIVE = 8
MAX_STRICT = 12         # Grok Q2：strict 软上限，超出移入 dormant（不算违约）
RESOLVED_KEEP = 100     # Gemini A1：resolved 只留最近 100 条，账本 JSON 不无界增长

# 日期化：非 strict 承诺超过这么多天还没兑现就下沉，别让上星期的话继续挂在账本里
RETIRE_DAYS = 3
# 只对当天有效的指令：跨天即失效（「等下课」不该在第二天还装成待办）
TEMP_SAME_DAY = re.compile(r"(今天|今晚|今早|等下课|下课后|下完课|待会|等会|一会儿|马上|这会儿)")
TEMP_NEXT_DAY = re.compile(r"(明天|明早|明晚|明日)")

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
DELIVER = re.compile(r"你|主人|客人|他|她|对方|替你|帮你|给你|把.{1,4}(给|还|交|送)")
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
            p = ps[i]
        # 长期承诺生命周期字段（C2-03）：老条目就地补默认值，不迁移数据
        p.setdefault("status", "ACTIVE")
        p.setdefault("blocker", "")
        p.setdefault("master_stance", "")
        p.setdefault("next_action", "")
        p.setdefault("next_check_at", "")
        p.setdefault("last_discussed_at", "")
        p.setdefault("last_asked_at", "")
        p.setdefault("ask_count", 0)
        p.setdefault("source_turns", [])
        p.setdefault("review_policy", "trigger")
        # 日期化：老条目没有日期就留空，注入时显示「早期」，不做批量推断
        p.setdefault("created_day", "")
        p.setdefault("expire_day", "")
    lg["promises"] = ps
    return lg


PAUSE_WORDS = re.compile(r"(先搁着|先放放|先放着|先别管|先不提|以后再说|过阵子再说|"
                         r"再等等|等以后)")
NOPUSH_WORDS = re.compile(r"(别催|不用催|不要催|别推|不用推|别提醒|不用提醒|"
                          r"别老提|不要老提)")
LONG_TERM = re.compile(r"(以后|长期|将来|将来有一天|未来|计划|打算|等有空|"
                       r"有朝一日|慢慢来)")


def apply_stance(lg, text, turn):
    """从主人这句话更新承诺的主人态度（PAUSED / DORMANT / ACTIVE）。

    只认明确措辞；拿不准不动。长期承诺第二次「再等等」后转 DORMANT，
    避免每两天催一次（方案 6.5 / 10.4）。
    """
    ensure_shape(lg)
    t = str(text or "")
    pause = PAUSE_WORDS.search(t)
    nopush = NOPUSH_WORDS.search(t)
    longterm = LONG_TERM.search(t)
    for p in lg["promises"]:
        if not pause and not nopush:
            continue
        p["last_discussed_at"] = str(turn)
        p["master_stance"] = "先搁着" if pause else "不想被催"
        # 长期计划（承诺正文里就写着「以后/长期/计划」）按 30 天节奏
        long_plan = bool(longterm or LONG_TERM.search(p.get("text") or ""))
        if pause:
            if p.get("status") == "PAUSED" and (long_plan or p.get("review_policy") == "30d"):
                p["status"] = "DORMANT"
                p["review_policy"] = "30d"
            else:
                p["status"] = "PAUSED"
                p["review_policy"] = "30d" if (long_plan or p.get("strict")) else "trigger"
        elif nopush:
            p["status"] = "DORMANT"
            p["review_policy"] = "trigger"
    return lg


def set_lifecycle(lg, promise_id, status=None, blocker=None, next_action=None,
                  next_check_at=None, stance=None):
    """显式更新承诺生命周期字段（管理员/醒来动作可用）；返回是否命中。"""
    ensure_shape(lg)
    for p in lg["promises"]:
        if p.get("id") != promise_id:
            continue
        if status:
            p["status"] = status
        if blocker is not None:
            p["blocker"] = str(blocker)[:60]
        if next_action is not None:
            p["next_action"] = str(next_action)[:60]
        if next_check_at is not None:
            p["next_check_at"] = str(next_check_at)[:40]
        if stance is not None:
            p["master_stance"] = str(stance)[:20]
        return True
    return False


def add_pending(lg, texts, turn, today=None):
    """新增承诺。近似重复（overlap>=0.7）不重复入账。返回新增 ID。

    已知特性（不修）：去重只看字面 bigram overlap，跨批次语义复述一旦措辞差异
    大于阈值就会各入一条，兑现侧也跟着重复计数（实测 50 轮：1 条剧本意图 →
    5 条文本、closed_total 4 > 意图 2）。这是「宁多勿丢」方向的设计内误差，
    上界由 MAX_ACTIVE / MAX_STRICT / TTL / RESOLVED_KEEP 兜住，指标按趋势读。
    真要收窄得做跨批次语义去重，优先级低。

    today 是本地日期（datetime.date）；登记时落 created_day，当日指令再落 expire_day。
    """
    today = today or datetime.now().date()
    added = []
    for t in texts or []:
        t = sanitize(t)
        if not t:
            continue
        if any(overlap(t, p["text"]) >= 0.7 for p in lg["promises"]):
            continue
        lg["pseq"] += 1
        p = {"id": f"P-{lg['pseq']:03d}", "text": t,
             "created_turn": turn, "strict": is_strict(t),
             "created_day": today.strftime("%Y-%m-%d")}
        # 时间措辞与 strict 无关：哪怕郑重答应过，说了「等下课」也只在当天有效
        if TEMP_SAME_DAY.search(t):
            p["expire_day"] = today.strftime("%Y-%m-%d")
        elif TEMP_NEXT_DAY.search(t):
            p["expire_day"] = (today + timedelta(days=1)).strftime("%Y-%m-%d")
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


def _parse_day(s):
    try:
        return datetime.strptime(str(s or ""), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _retire(lg, p, reason):
    """移出活跃池但保留内容：进 dormant，能被后续召回，不算违约。"""
    lg["promises"].remove(p)
    lg["dormant"].append({**p, "dormant_reason": reason})
    return p["id"]


def evict(lg, turn, ttl=TTL_TURNS, cap=MAX_ACTIVE, strict_cap=MAX_STRICT, today=None):
    """淘汰：非 strict 超龄 → expired；超量 → dormant（移出 prompt，不算违约）。

    日期规则（解决「昨天的指令今天还在执行」）：
      ① 当日/次日指令过了 expire_day 立刻下沉；
      ② 非 strict 承诺超过 RETIRE_DAYS 天未兑现也下沉；
    两条都进 dormant，不删内容，后续需要时仍可召回。

    strict 豁免 TTL，但按 Grok Q2 设软上限 12 条：超出的最旧项进 dormant，
    可被规则召回重新激活；resolved 只留最近 100 条，避免账本 JSON 无界增长。
    """
    ensure_shape(lg)
    today = today or datetime.now().date()
    moved = []
    for p in list(lg["promises"]):
        exp = _parse_day(p.get("expire_day"))
        if exp and today > exp:
            moved.append(_retire(lg, p, "当日指令已过期"))
            continue
        if p.get("strict"):
            continue
        made = _parse_day(p.get("created_day"))
        if made and (today - made).days > RETIRE_DAYS:
            moved.append(_retire(lg, p, "超过3天未兑现"))
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


def inject_lines(lg, days=RETIRE_DAYS, limit=6):
    """给前台的承诺行：带登记日期与编号，别让旧指令冒充今天。

    strict 不受天数限制（保密、长期约定本来就该一直记着）；非 strict 只给
    最近 days 天、最多 limit 条，按日期从新到旧。没有日期的老条目显示
    「早期」并排在最后——不猜日期，也不隐藏。
    编号 P-xxx 是给 manage_promises 工具用的：模型要自己整理承诺时靠它定位。
    """
    ensure_shape(lg)
    today = datetime.now().date()
    rows = []
    for p in lg["promises"]:
        made = _parse_day(p.get("created_day"))
        if not p.get("strict") and made and (today - made).days > days:
            continue
        rows.append((made or datetime.min.date(), p))
    rows.sort(key=lambda x: x[0], reverse=True)
    out = []
    for made, p in rows[:limit]:
        tag = "早期" if made == datetime.min.date() else f"{made:%m-%d}"
        out.append(f"[{tag} {p.get('id')}] {sanitize(p.get('text'), 40)}")
    return out


def manage(lg, action, ids, note="", turn=0):
    """角色自己整理承诺（keep / drop / done / goal）。

    只动本账本里存在的编号，最多 8 条；drop/done 都保留原文（进 dormant /
    resolved），不物理删除。返回一句可读结果，交给工具回填。
    """
    ensure_shape(lg)
    action = str(action or "").strip().lower()
    want = {str(i).strip().upper() for i in (ids or []) if str(i).strip()}
    if action not in ("keep", "drop", "done", "goal"):
        return "action 只能是 keep / drop / done / goal。"
    if not want:
        return "缺少 ids：填账本里方括号中的承诺编号（如 P-003）。"
    hit = [p for p in lg["promises"] if str(p.get("id") or "").upper() in want][:8]
    if not hit:
        return "没有找到对应编号的承诺，未改动。"
    done = []
    for p in hit:
        if action == "keep":
            p["expire_day"] = ""
            p["master_stance"] = "长期，不用催"
            p["review_policy"] = "30d"
        elif action == "goal":
            p["next_action"] = sanitize(note, 60)
            p["master_stance"] = "有明确目标"
            p["expire_day"] = ""
        elif action == "drop":
            lg["promises"].remove(p)
            lg["dormant"].append({**p, "dormant_reason": "角色自行收起"})
        else:                                   # done
            lg["promises"].remove(p)
            lg["resolved"].append({**p, "status": "fulfilled", "resolved_turn": turn,
                                   "evidence": sanitize(note or "角色自述已完成", 40),
                                   "matched_by": "self"})
        done.append(p["id"])
    left = len(lg["promises"])
    return f"已处理 {action}：{'、'.join(done)}；账本剩余 {left} 条。"
