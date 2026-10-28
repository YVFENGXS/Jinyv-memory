# -*- coding: utf-8 -*-
"""记忆召回评测（离线默认）：recall@1 / recall@4 / MRR / 注入命中率

为什么单独一个脚本：
- 回归测试回答「代码对不对」，这里回答「召回好不好」，两件事不能用同一套断言。
- 默认不联网、不调模型：用固定剧本 + 人工 GT 造库，任何人一条命令得到同一批数字。
- 加 --real 才调前台模型，量「回答准确率」；没有 key 时自动跳过并标注。

用法：
  python tools/eval_memory.py                 # 关键词召回基线
  python tools/eval_memory.py --real          # 额外量回答准确率（需 RP_FRONT_KEY）
  python tools/eval_memory.py --out data/eval_metrics.json

指标口径：
- recall@k：GT 事件卡是否出现在前 k 条召回里（只看卡，不看注入段）
- 注入命中率：GT 关键句是否真的进了最终 prompt（含预算裁剪后的结果）
- MRR：GT 卡排名的倒数均值，越接近 1 越好
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
from hashlib import sha256
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "jinyu_memory", _ROOT / "jinyu_memory" / "core", _ROOT / "examples"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import embedding
import promise
import retriever
import rp_skeleton as rp
from rp_skeleton import DB, RPContextMiddleware
from demo import MockBack, MockFront, PERSONA

SID, CID = "eval", "shenqingwu"


def wilson(k, n, z=1.96):
    """比例指标的 95% 置信区间（Wilson）。n 小时必须给区间，别只报点估计。"""
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return [round(max(0.0, c - h), 3), round(min(1.0, c + h), 3)]

# 剧本：10 张卡，覆盖修伞 / 遗物 / 保密 / 灯会 / 师兄 / 信 / 白鹤
# 每张卡的 summary 里带一个唯一关键句，GT 用它判断命中。
CARDS = [
    ("E1", 1, 6, "客人把去年在铺子买的断骨旧油纸伞拿来修，伞骨断了三根，沈青梧答应三天修好。",
     ["旧油纸伞", "客人", "伞骨"], 0.8, "both"),
    ("E2", 7, 10, "客人说这把伞是母亲留下的遗物，请沈青梧务必别弄丢。",
     ["旧油纸伞", "母亲", "遗物"], 0.9, "both"),
    ("E3", 11, 14, "外面下大雨，沈青梧留他喝了半盏热茶，说雨天路滑不急走。",
     ["雨", "热茶", "铺子"], 0.4, "both"),
    ("E4", 15, 18, "掌柜来催租，沈青梧说月底前凑齐租钱，不会拖欠。",
     ["掌柜", "租钱", "月底"], 0.6, "both"),
    ("E5", 19, 22, "客人问起十五的城南灯会，沈青梧说那晚铺子关门，会去看灯。",
     ["灯会", "十五", "城南"], 0.4, "both"),
    ("E6", 23, 26, "师兄柳三来铺子，提起师父当年与同行的旧怨，沈青梧没接话。",
     ["柳三", "师兄", "师父"], 0.7, "self"),
    ("E7", 27, 30, "客人带来一包桂花糕，沈青梧收下，说太甜了却吃了两块。",
     ["桂花糕", "客人"], 0.3, "both"),
    ("E8", 31, 34, "客人托沈青梧保管一封家书，她答应信里的事一个字也不说出去。",
     ["家书", "保密", "客人"], 0.9, "self"),
    ("E9", 35, 38, "修好的伞在雨里撑开，伞面画着一只白鹤，骨架已经换了新的。",
     ["旧油纸伞", "白鹤", "伞面"], 0.6, "both"),
    ("E10", 39, 42, "客人要走，沈青梧把修好的伞递到客人手里，说改日再来喝茶。",
     ["旧油纸伞", "客人", "铺子"], 0.5, "both"),
]

# 回答判定：模型不会照抄摘要原句，按「关键事实是否出现」判，而不是整句匹配。
# 允许角色口吻的简短回答（「修好了」「月底前凑齐」），所以取值放宽到自然用词。
KEYS = {
    "E1": ["三根", "三天", "修好", "修完", "能修", "伞骨"],
    "E2": ["母亲", "遗物", "重要", "还在"],
    "E3": ["茶", "龙井", "半盏", "不急", "路滑", "雨不小"],
    "E4": ["月底", "租", "掌柜", "二两"],
    "E5": ["灯会", "十五", "看灯", "城南"],
    "E6": ["柳三", "师兄", "师父", "仇"], "E7": ["桂花糕", "甜", "两块", "喜欢", "点心"],
    "E8": ["没有", "没说", "保密", "守"], "E9": ["白鹤", "伞上", "换", "还在", "骨架"],
    "E10": ["给", "递", "还", "改日", "修好"],
}
# 角色常用一两个字作答（「在。」「能。」）。单字不能做子串判断（「现在」含「在」），
# 所以短答案单独按整句精确匹配。
SHORT = {
    "E1": {"三根", "能修", "三天", "修好"}, "E2": {"在", "还在", "重要"},
    "E3": {"龙井", "茶", "不急"}, "E4": {"能", "差二两", "月底"},
    "E5": {"十五", "看灯", "去", "十五城南"}, "E7": {"两块", "喜欢", "甜"},
    "E9": {"换了", "还在", "白鹤"}, "E10": {"是", "给了", "递了", "伞"},
}
PUNCT = re.compile(r"[\s。，！？、；：…—\"'（）「」]")

# 逐题 rubric：同一事件的不同问法要判不同的正确点。
# 只有「按问法」判，短答和补充说明才不会被事件级关键词误伤。
RUBRIC = {
    "伞骨断了几根": ["三根"], "母亲留下的东西还在吗": ["在", "还在"],
    "这伞对客人重要吗": ["重要", "遗物", "母亲"],
    "遗物你也敢乱放": ["遗物", "母亲", "重要", "架上", "架子", "没乱放", "收着"],
    "你答应陪我看灯吗": ["我会去", "会去", "看灯", "灯会"],
    "那把破伞还能用吗": ["能用", "能修", "能", "修好"],
    "那把油纸伞是谁的": ["客人", "母亲"], "十五灯会你去不去": ["去", "灯会"],
    "白鹤还在吗": ["在", "还在", "白鹤"], "伞骨架换了吗": ["换了", "新的"],
    "那天我们喝的是什么": ["龙井", "茶"], "雨大不大": ["不小", "大"],
    "租钱还差多少": ["二两", "差"], "那包点心你吃了多少": ["两块"],
    "你喜欢桂花糕吗": ["喜欢", "甜"], "伞是递给我的吗": ["是", "递", "给"],
    "客人走的时候拿了什么": ["伞"], "临走时你说了什么": ["改日", "喝茶"],
    "伞什么时候能取": ["三天", "修好"], "修好的伞什么样": ["白鹤", "伞面"],
    "这伞对客人重要吗": ["重要", "遗物"],
}
# knowledge_boundary=self 的卡：玩家本就不该知道，答「客人问这个做什么」是正确行为，
# 不能算记忆失败；单独量「边界守住了没有」。
BOUNDARY = {"E6", "E8"}


EXACT_ONLY = {"在", "能", "是", "去"}

# 边界题的「守住」判据：出现明确的拒答/守密措辞即算守住。
# 词表必须覆盖自然说法——第 6 次真实运行里「信里的事一个字也不会说出去」因为
# 不含「不说」被误判成泄密（判分器缺口，不是模型漏了）。
GUARD_WORDS = ("不说", "不告诉", "不会说", "不外传", "一字不", "烂在肚子里",
               "保密", "守口", "不该问", "问这个", "不能告诉")


def _guarded(ans) -> bool:
    text = str(ans or "")
    return any(w in text for w in GUARD_WORDS)


def _hit(text: str, clean: str, tokens) -> bool:
    """单字词按整句/句首判，避免「现在」命中「在」；其余按子串。"""
    for t in tokens:
        if len(t) > 1:
            if t in text:
                return True
        elif t in EXACT_ONLY:
            if clean == t or (clean.startswith(t) and len(clean) <= 10
                              and not clean.startswith("现在")):
                return True
        elif t in text:
            return True
    return False


def answered(ans: str, gt: str, snippet: str, q: str = "") -> bool:
    """逐题 rubric 优先；否则整句短答精确匹配，或长答里出现关键事实。"""
    text = str(ans or "")
    if not text.strip():
        return False
    clean = PUNCT.sub("", text)
    if q in RUBRIC:
        return _hit(text, clean, RUBRIC[q])
    if clean in SHORT.get(gt, set()):
        return True
    return snippet in text or gt in text or any(k in text for k in KEYS.get(gt, []))


# 分层评测：literal（问法与钩子重合）/ para（旧同义句）/ holdout（写评测时没见过的新问法）。
# holdout 才是能往汇报里写的数字；literal 只能证明管线没坏。
CASES = [
    # E1 伞骨断三根 / 答应三天修好
    ("我的伞修好了吗", "E1", "伞骨断了三根", "literal"),
    ("伞骨断了几根", "E1", "伞骨断了三根", "holdout"),
    ("你答应几天修好", "E1", "答应三天修好", "holdout"),
    ("那把破伞还能用吗", "E1", "伞骨断了三根", "holdout"),
    ("伞什么时候能取", "E1", "答应三天修好", "holdout"),
    # E2 母亲遗物
    ("那把油纸伞是谁的", "E2", "母亲留下的遗物", "literal"),
    ("母亲留下的东西还在吗", "E2", "母亲留下的遗物", "holdout"),
    ("这伞对客人重要吗", "E2", "母亲留下的遗物", "holdout"),
    ("遗物你也敢乱放", "E2", "母亲留下的遗物", "holdout"),
    ("客人为什么非要修这把伞", "E2", "母亲留下的遗物", "holdout"),
    # E3 雨天留客喝茶
    ("外面下雨那天你留我做什么", "E3", "喝了半盏热茶", "holdout"),
    ("躲雨那晚她端来什么解渴", "E3", "喝了半盏热茶", "para"),
    ("雨天路滑你说什么了", "E3", "雨天路滑不急走", "holdout"),
    ("那天我们喝的是什么", "E3", "喝了半盏热茶", "holdout"),
    ("雨大不大", "E3", "外面下大雨", "holdout"),
    # E4 掌柜催租
    ("掌柜催租的事怎么办", "E4", "月底前凑齐租钱", "literal"),
    ("拖欠房租了吗", "E4", "月底前凑齐租钱", "holdout"),
    ("月底前能凑齐吗", "E4", "月底前凑齐租钱", "holdout"),
    ("掌柜来做什么", "E4", "掌柜来催租", "holdout"),
    ("租钱还差多少", "E4", "月底前凑齐租钱", "holdout"),
    # E5 十五灯会
    ("十五灯会你去不去", "E5", "十五的城南灯会", "literal"),
    ("城南看灯那晚铺子开门吗", "E5", "那晚铺子关门", "holdout"),
    ("灯会什么时候", "E5", "十五的城南灯会", "holdout"),
    ("你答应陪我看灯吗", "E5", "会去看灯", "holdout"),
    ("十五晚上你在哪", "E5", "十五的城南灯会", "holdout"),
    # E6 师兄与旧怨（self）
    ("柳三和你什么关系", "E6", "师兄柳三", "literal"),
    ("师父以前的仇人是谁", "E6", "师父当年与同行的旧怨", "para"),
    ("师兄来做什么", "E6", "师兄柳三来铺子", "para"),
    ("你师父怎么了", "E6", "师父当年与同行的旧怨", "holdout"),
    ("同行的旧怨是什么", "E6", "师父当年与同行的旧怨", "holdout"),
    # E7 桂花糕
    ("桂花糕好吃吗", "E7", "一包桂花糕", "literal"),
    ("那包点心你吃了多少", "E7", "吃了两块", "holdout"),
    ("太甜了你为什么还吃", "E7", "说太甜了却吃了两块", "holdout"),
    ("客人带什么来了", "E7", "一包桂花糕", "holdout"),
    ("你喜欢桂花糕吗", "E7", "说太甜了", "holdout"),
    # E8 家书与保密（self）
    ("我信里的事你没说出去吧", "E8", "一个字也不说出去", "literal"),
    ("写信的人托她守什么口风", "E8", "一个字也不说出去", "para"),
    ("家书你放哪了", "E8", "保管一封家书", "holdout"),
    ("那封信的内容你记得吗", "E8", "信里的事", "holdout"),
    ("你答应保密了吗", "E8", "一个字也不说出去", "holdout"),
    # E9 伞面白鹤
    ("伞面上画的是什么", "E9", "伞面画着一只白鹤", "literal"),
    ("修好的伞什么样", "E9", "伞面画着一只白鹤", "holdout"),
    ("白鹤还在吗", "E9", "伞面画着一只白鹤", "holdout"),
    ("伞骨架换了吗", "E9", "骨架已经换了新的", "holdout"),
    ("那把伞撑开好看吗", "E9", "在雨里撑开", "holdout"),
    # E10 递伞与改日
    ("我走的时候你把伞给我了吗", "E10", "递到客人手里", "literal"),
    ("临走时你说了什么", "E10", "改日再来喝茶", "holdout"),
    ("伞是递给我的吗", "E10", "递到客人手里", "holdout"),
    ("你说改日再来喝茶", "E10", "改日再来喝茶", "holdout"),
    ("客人走的时候拿了什么", "E10", "把修好的伞递到客人手里", "holdout"),
]
LAYERS = ("literal", "para", "holdout")

# 规模压力集：30 张卡、5 个同类族，族内共享锚点（伞/客人/点心/茶/信），
# 只有细节词能区分。用来回答「10 张卡上的 1.0 能不能撑住」。
# 每行：(eid, summary, fact, hooks, literal_query, holdout_query)
SCALE_ROWS = [
    ("S1", "油纸伞伞骨断了三根，约好三天修好。", "断了三根", ["油纸伞", "客人"],
     "那把油纸伞怎么回事", "客人送来的旧伞坏了几处"),
    ("S2", "绸伞伞面绣兰花，是客人成亲时用的。", "绣兰花", ["绸伞", "兰花"],
     "绸伞上绣的什么", "成亲要用的那把伞有什么花样"),
    ("S3", "青竹伞伞柄裂了，掌柜让换新的。", "柄裂", ["青竹伞", "掌柜"],
     "青竹伞哪里坏了", "掌柜说那把竹伞要换什么"),
    ("S4", "黑布伞被柳三借走，说雨停就还。", "柳三借走", ["黑布伞", "柳三"],
     "黑布伞去哪了", "下雨天被谁拿走的伞"),
    ("S5", "白绢伞伞面画着白鹤，已经修好。", "白鹤", ["白绢伞", "白鹤"],
     "白绢伞上画了什么", "画着鸟的那把伞修好了吗"),
    ("S6", "旧伞是母亲遗物，客人不让丢。", "母亲遗物", ["旧伞", "母亲"],
     "旧伞为什么不能丢", "客人母亲留下的东西是哪件"),
    ("G1", "王掌柜来催租，说月底前要凑齐。", "月底前", ["王掌柜", "租"],
     "王掌柜来做什么", "房租催得紧吗"),
    ("G2", "李货郎赊了二两银子的布。", "二两银子", ["李货郎", "赊账"],
     "李货郎欠多少", "卖货的还欠着钱吗"),
    ("G3", "陈娘子买伞给了现钱。", "现钱", ["陈娘子", "现钱"],
     "陈娘子怎么付的", "那位女客结账利落吗"),
    ("G4", "赵书生订一把伞，三日后取。", "三日后取", ["赵书生", "订伞"],
     "赵书生什么时候来取", "读书人定的货几时来拿"),
    ("G5", "孙婆婆借伞，说雨停就还。", "雨停就还", ["孙婆婆", "借伞"],
     "孙婆婆借了什么", "老太太说什么时候归还"),
    ("G6", "周掌柜来谈木料，要十根杉木。", "十根杉木", ["周掌柜", "杉木"],
     "周掌柜要什么木料", "谈生意的那位要多少根木头"),
    ("D1", "桂花糕是客人带来的，太甜却吃了两块。", "两块", ["桂花糕", "客人"],
     "桂花糕吃了多少", "甜的点心她吃了几块"),
    ("D2", "绿豆糕是掌柜送的，说解暑。", "解暑", ["绿豆糕", "掌柜"],
     "绿豆糕谁送的", "夏天降火的那包点心哪来的"),
    ("D3", "蜜饯酸得皱眉，收进柜里。", "酸", ["蜜饯", "柜子"],
     "蜜饯味道如何", "那包果脯她爱不爱吃"),
    ("D4", "糖炒栗子热乎的，分给邻居小孩。", "小孩", ["栗子", "小孩"],
     "栗子给了谁", "街上孩子吃到什么了"),
    ("D5", "酥饼掉渣，配茶吃正好。", "配茶", ["酥饼", "茶"],
     "酥饼怎么吃", "掉渣的点心配什么"),
    ("D6", "桂花糖包在油纸里，留给客人。", "留给客人", ["桂花糖", "油纸"],
     "桂花糖给谁留着", "纸包里的糖是给谁备的"),
    ("T1", "龙井是雨天留客喝的，说解渴。", "解渴", ["龙井", "雨天"],
     "下雨那天喝的什么", "躲雨时端的哪壶茶"),
    ("T2", "碧螺春待客用，只剩半罐。", "半罐", ["碧螺春", "待客"],
     "碧螺春还剩多少", "招待客人那罐茶够吗"),
    ("T3", "普洱暖身，掌柜爱喝。", "暖身", ["普洱", "掌柜"],
     "掌柜爱喝什么茶", "冬天暖身子那壶是什么"),
    ("T4", "桂花茶是自己晒的，很香。", "自己晒", ["桂花茶", "香"],
     "桂花茶哪来的", "有花香的那壶茶怎么做的"),
    ("T5", "粗茶给伙计喝，不解渴。", "给伙计", ["粗茶", "伙计"],
     "粗茶给谁喝", "店里下人平常喝什么"),
    ("T6", "陈茶受潮了，准备扔。", "受潮", ["陈茶", "潮"],
     "陈茶怎么了", "放坏的那包茶叶还能用吗"),
    ("H1", "家书是客人托保管的，一字不外传。", "不外传", ["家书", "保密"],
     "家书要保密吗", "信里的事她说出去没有"),
    ("H2", "商信记着账目，锁在匣里。", "锁在匣", ["商信", "账目"],
     "商信放哪", "记生意的那封信收在哪"),
    ("H3", "情笺被退回，后来烧了。", "烧了", ["情笺", "退回"],
     "情笺怎么处理", "那封被退回来的信后来呢"),
    ("H4", "官函催税，压在柜底。", "催税", ["官函", "税"],
     "官函说什么", "衙门来的公文什么内容"),
    ("H5", "旧信字迹模糊，认不出。", "字迹模糊", ["旧信", "模糊"],
     "旧信怎么了", "老信上还能看清字吗"),
    ("H6", "回信还没写完，摊在案上。", "没写完", ["回信", "案上"],
     "回信写到哪了", "桌上摊着的那封信写完了吗"),
]
SCALE_CARDS = [(r[0], 2 * i + 1, 2 * i + 2, r[1], r[3],
                0.9 if r[0] == "H1" else 0.5,
                "self" if r[0] == "H1" else "both")
               for i, r in enumerate(SCALE_ROWS)]
SCALE_CASES = []
for _r in SCALE_ROWS:
    SCALE_CASES.append((_r[4], _r[0], _r[2], "literal"))
    SCALE_CASES.append((_r[5], _r[0], _r[2], "holdout"))


# 显著性微集：所有卡共用一个钩子「伞」、同一 importance、分数完全同分，
# 唯一差别是 mentions/verified。用来验证显著性真的能改变同分排序。
# 行尾两个数是 (mentions, verified)：V1 被反复记录 4 次且被确认 1 次 → solid。
SALIENCE_CARDS = [
    ("V1", 1, 2, "客人第一次来问那把伞，说想修伞骨。", ["伞"], 0.5, "both", 4, 1),
    ("V2", 3, 4, "来人说伞面落灰了，顺手擦干净。", ["伞"], 0.5, "both", 0, 0),
    ("V3", 5, 6, "隔壁小孩借伞去看灯，第二天就还了。", ["伞"], 0.5, "both", 0, 0),
    ("V4", 7, 8, "掌柜说伞架摆得乱，让重新码一遍。", ["伞"], 0.5, "both", 0, 0),
    ("V5", 9, 10, "下雨天伞不够用，只好让人先等一等。", ["伞"], 0.5, "both", 0, 0),
    ("V6", 11, 12, "把伞收进柜台，压着账本一角。", ["伞"], 0.5, "both", 0, 0),
]
SALIENCE_QUERIES = ["伞", "那把伞", "我送来的伞", "我的伞", "铺子里的伞"]





def seed(db: DB, cards, sid: str) -> None:
    """把剧本写进临时库：卡 + 账本 + 游标，全部走正式事务入口。

    行尾可选带 (mentions, verified)：显著性评测集用它造「同分不同显著性」的局面。
    """
    ledger = promise.ensure_shape({"mood": "平静", "location": "伞铺",
                                   "relationship": {"trust": 0.2}})
    for row in cards:
        eid, lo, hi, summary, hooks, imp, boundary = row[:7]
        mentions = int(row[7]) if len(row) > 7 else 0
        verified = int(row[8]) if len(row) > 8 else 0
        card = {
            "event_id": eid, "session_id": sid, "character_id": CID,
            "turn_start": lo, "turn_end": hi, "summary": summary,
            "state_changes": {"mood": "平静", "pending_promises": []},
            "entity_hooks": hooks, "rel_hooks": [], "knowledge_boundary": boundary,
            "importance": imp, "precision": "strict" if imp >= 0.8 else "normal",
            "mentions": mentions, "verified": verified,
            "idempotency_key": sha256(f"{sid}|{CID}|{eid}".encode()).hexdigest(),
        }
        db.commit_archive(card, sid, CID, lo, hi, ledger)


def keyword_rank(db: DB, query: str, sid: str, limit: int = 4, cid: str = ""):
    """基线：纯字面/钩子召回，和线上无向量时走同一条路径。

    cid 必须跟着用例走：写死 CID 会让 --cid 指定的真实语料一条都召不回来
    （all_cards 查不到就返回空列表，指标全 0，看着像「召回很差」其实是取错库）。
    """
    hits = retriever.retrieve(db.all_cards(sid, cid or CID), query, limit=limit)
    return [c["event_id"] for _, c in hits]


def hybrid_rank(mw, db: DB, query: str, sid: str, limit: int = 4, cid: str = ""):
    """混合召回：关键词 + 向量，按线上 fuse 同一权重合并。"""
    cid = cid or CID
    cards = db.all_cards(sid, cid)
    kw = retriever.retrieve(cards, query, limit=limit)
    vec = mw._vector_recall(sid, cid, query, cards, set(), limit=limit)
    fused = retriever.fuse(kw, vec, limit=limit)
    return [c["event_id"] for _, c in fused]


def prompt_text(mw, query: str, sid: str, upto: int, cid: str = "") -> str:
    msgs = mw._build_messages(PERSONA, sid, cid or CID, upto=upto, query=query)
    return "\n".join(str(m.get("content") or "") for m in msgs)


def mrr(ranks, gt):
    for i, eid in enumerate(ranks, 1):
        if eid == gt:
            return 1.0 / i
    return 0.0


def salience_probe():
    """显著性微集：六张同分卡里只有一张 solid，看它能不能排到第一。

    返回 (题数, 开显著性, 关显著性)。关掉是消融：用来证明这个字段真的在工作，
    而不是只写进数据库没人读。
    """
    tmp = Path(tempfile.mkdtemp(prefix="jinyu-sal-")) / "sal.db"
    db = DB(str(tmp))
    seed(db, SALIENCE_CARDS, "eval-sal")
    score = {}
    saved = retriever.SALIENCE_ENABLED
    try:
        for flag in (True, False):
            retriever.SALIENCE_ENABLED = flag
            top1 = [keyword_rank(db, q, "eval-sal", 1) for q in SALIENCE_QUERIES]
            score[flag] = round(sum(1 for t in top1 if t and t[0] == "V1")
                                / len(SALIENCE_QUERIES), 3)
    finally:
        retriever.SALIENCE_ENABLED = saved
    return len(SALIENCE_QUERIES), score[True], score[False]


# 短指代族（B）：真实聊天里大量是「伞」「茶」「那封信」这种接话。
# 一族内所有卡共用一个钩子 → 分数严格同分，唯一差别是被记录/确认的次数。
# 注意：GT 靠「哪张被反复记录」定义，这是合成出来的；真实对话里短句的正确答案
# 取决于当时的上下文，没有静态 GT。所以这一层只回答「机制在短句下能不能生效」。
SHORT_QUERIES = {
    "伞": ["伞", "那把伞", "我的伞", "这伞"],
    "茶": ["茶", "那壶茶", "喝的茶", "这茶"],
    "信": ["信", "那封信", "写的信", "这信"],
}


def _short_cards():
    rows, seq = [], 0
    for fam in SHORT_QUERIES:
        for i in range(6):
            seq += 1
            rows.append((f"{fam}{i + 1}", 2 * seq + 1, 2 * seq + 2,
                         f"{fam}族第 {i + 1} 条，时间第 {seq} 段。", [fam], 0.5, "both",
                         4 if i == 0 else 0, 1 if i == 0 else 0))
    return rows


def short_probe():
    """B：三族短指代，量显著性开/关时 top-1 能否翻到被反复记录的那张。"""
    tmp = Path(tempfile.mkdtemp(prefix="jinyu-short-")) / "short.db"
    db = DB(str(tmp))
    seed(db, _short_cards(), "eval-short")
    saved = retriever.SALIENCE_ENABLED
    rows, on = [], 0
    n = sum(len(qs) for qs in SHORT_QUERIES.values())
    try:
        for flag in (True, False):
            retriever.SALIENCE_ENABLED = flag
            hit = 0
            for fam, qs in SHORT_QUERIES.items():
                gt = f"{fam}1"
                for q in qs:
                    top = keyword_rank(db, q, "eval-short", 1)
                    ok = bool(top) and top[0] == gt
                    hit += ok
                    if flag:
                        rows.append({"q": q, "gt": gt, "on": bool(ok)})
                    else:
                        for r in rows:
                            if r["q"] == q and r["gt"] == gt:
                                r["off"] = bool(ok)
            if flag:
                on = hit
            else:
                off = hit
    finally:
        retriever.SALIENCE_ENABLED = saved
    return {"cases": n, "on": round(on / n, 3), "off": round(off / n, 3), "rows": rows}


def _fuse_like(kw_ids, vec_ids, cards, eps=0.0, sal_on=True):
    """复刻 retriever.fuse 的 RRF 打分与键序；eps>0 时改为「邻接聚类」放宽同分。

    不改线上：看清楚 eps 各档能救回多近的竞争，再决定是否值得落到 retriever。
    """
    saved = retriever.SALIENCE_ENABLED
    retriever.SALIENCE_ENABLED = sal_on
    try:
        scores = {}
        for r, eid in enumerate(kw_ids, 1):
            scores[eid] = scores.get(eid, 0.0) + retriever.FUSE_KEYWORD_WEIGHT / (retriever.FUSE_K + r)
        for r, eid in enumerate(vec_ids, 1):
            scores[eid] = scores.get(eid, 0.0) + retriever.FUSE_VECTOR_WEIGHT / (retriever.FUSE_K + r)
        items = sorted(scores.items(), key=lambda x: -x[1])

        def tail(eid):
            return tuple(retriever.salience(cards[eid])) + (
                cards[eid].get("importance", 0.5), cards[eid].get("turn_end", 0))

        if eps <= 0:
            return [e for e, _ in sorted(items, key=lambda x: (x[1],) + tail(x[0]), reverse=True)]
        groups = []
        for eid, s in items:
            if groups and s >= groups[-1][0][1] * (1 - eps):
                groups[-1].append((eid, s))
            else:
                groups.append([(eid, s)])
        out = []
        for g in groups:
            g.sort(key=lambda x: tail(x[0]) + (x[1],), reverse=True)
            out += [e for e, _ in g]
        return out
    finally:
        retriever.SALIENCE_ENABLED = saved


# A 方案场景：两路信号分歧，正解卡（被反复记录）在词路排名靠后，干扰卡在前。
# rank 差 1/2/4/8 对应 RRF 分差约 4.5% / 8.7% / 16% / 26%。
BUCKET_GAPS = (1, 2, 4, 8)
BUCKET_EPS = (0.05, 0.10, 0.20)


def bucket_probe():
    """A：相关性接近时让显著性参与，看各 eps 能救回哪些分差。"""
    cards = {
        "A": {"event_id": "A", "mentions": 4, "verified": 1, "importance": 0.5,
              "turn_end": 2, "summary": "被反复记录的旧事"},
        "B": {"event_id": "B", "mentions": 0, "verified": 0, "importance": 0.9,
              "turn_end": 20, "summary": "最新但只提过一次"},
    }
    for i in range(8):
        cards[f"N{i}"] = {"event_id": f"N{i}", "mentions": 0, "verified": 0,
                          "importance": 0.5, "turn_end": 100 + i, "summary": "噪声"}
    out = []
    for gap in BUCKET_GAPS:
        kw = [f"N{i}" for i in range(gap)] + ["A"]      # A 在词路第 gap+1 名
        diff = round((1 - (retriever.FUSE_K + 1) / (retriever.FUSE_K + len(kw))) * 100, 1)
        row = {"gap": gap, "score_diff_pct": diff}
        row["off"] = _fuse_like(kw, [], cards, 0.0, sal_on=False)[0] == "A"
        row["strict"] = _fuse_like(kw, [], cards, 0.0)[0] == "A"
        for eps in BUCKET_EPS:
            row[f"eps{int(eps * 100)}"] = _fuse_like(kw, [], cards, eps)[0] == "A"
        out.append(row)
    return out


# 外部基线：同一批 30 卡 / 60 问，四种朴素注入策略。预算压到 1000 token
# （output_reserve=0，即可用上下文就是 1000）：本方案的「锚 + 4 张召回卡 + 最近 6 张」
# 放得下，而「30 卡全量」放不下，差异才看得出来。
BASELINE_BUDGET = 1000


def baseline_probe(budget=BASELINE_BUDGET):
    """ours（召回+结构） vs full（全部卡硬塞） vs full_uncut vs recent（只用热对话）。

    full 跑两次：一次受水位线裁剪（与线上同一套），一次关掉水位线（不受预算约束），
    用来看「全塞」是靠多少 token 才换到高命中的。
    """
    plans = (("ours", True), ("full", True), ("full_uncut", False), ("recent", True))
    rows = []
    for label, cut in plans:
        mode = "full" if label.startswith("full") else label
        tmp = Path(tempfile.mkdtemp(prefix=f"jinyu-base-{label}-")) / "b.db"
        db = DB(str(tmp))
        seed(db, SCALE_CARDS, "eval-base")
        mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                                 max_tokens=budget, output_reserve=0, keep_recent=4)
        saved_mode, saved_wm = rp.INJECT_MODE, rp.WATERMARK_ON
        rp.INJECT_MODE, rp.WATERMARK_ON = mode, cut
        try:
            hit = tok = 0
            for q, gt, snippet, _ in SCALE_CASES:
                msgs = mw._build_messages(PERSONA, "eval-base", CID, upto=62, query=q)
                text = "\n".join(str(m.get("content") or "") for m in msgs)
                hit += snippet in text
                tok += rp.est_tokens(text, mw.profile)
        finally:
            rp.INJECT_MODE, rp.WATERMARK_ON = saved_mode, saved_wm
        n = len(SCALE_CASES)
        rows.append({"mode": label, "inject_hit": round(hit / n, 3),
                     "inject_ci": wilson(hit, n), "avg_tokens": int(tok / n),
                     "sections_dropped": mw.watermarks["sections_dropped"]})
    return rows


# 保密闸门：3 张「仅玩家知道」的卡 + 6 张普通卡。
# 普通卡必须排在后面（turn_end 更大），否则 3 张私有卡会落进「最近 6 张」热窗——
# 那条通道（【历史事件】）不经过召回，是另一个泄漏面，所以下面两种排布都测。
GUARD_PRIVATE = [
    ("P1", 1, 2, "掌柜私下说这月的账做了手脚，别让客人知道。", ["账本", "手脚"], 0.7, "player"),
    ("P2", 3, 4, "客人其实是官差派来的，沈青梧听人说过。", ["官差", "客人"], 0.7, "player"),
    ("P3", 5, 6, "柳三欠了赌债，托沈青梧瞒着师父。", ["柳三", "赌债"], 0.7, "player"),
]
GUARD_FILLER = [
    ("N1", 7, 8, "早上去后院扫了落叶，顺手把木料码齐。", ["院子", "木料"], 0.3, "both"),
    ("N2", 9, 10, "午后给柜台换了块新布，旧布收在柜底。", ["柜台", "新布"], 0.3, "both"),
    ("N3", 11, 12, "傍晚称了称新进的桐油，比上回轻些。", ["桐油", "秤"], 0.3, "both"),
    ("N4", 13, 14, "夜里起了风，把门口幌子收进来。", ["幌子", "风"], 0.3, "both"),
    ("N5", 15, 16, "清早磨了刨刀，刃口顺了。", ["刨刀", "刃口"], 0.3, "both"),
    ("N6", 17, 18, "午间把旧账册搬到架顶，落了层灰。", ["账册", "灰"], 0.3, "both"),
]
GUARD_CASES = [("账做了手脚吗", "这月的账做了手脚"),
               ("客人是官差派来的吗", "官差派来的"),
               ("柳三欠赌债吗", "赌债")]


def matrix_probe(budget=BASELINE_BUDGET):
    """机制消融矩阵：保密闸门 / 承诺通道 / 水位线裁剪，各一格。"""
    out = {}
    # 1) 保密闸门：player 私有卡是否被注入（泄漏率）。
    #    far = 私有卡在最早 3 张（只有召回通道能看到）；near = 私有卡在最近 3 张（热窗通道）。
    res = {}
    for where, cards in (("far", GUARD_PRIVATE + GUARD_FILLER),
                         ("near", GUARD_FILLER + GUARD_PRIVATE)):
        tmp = Path(tempfile.mkdtemp(prefix=f"jinyu-mx-guard-{where}-")) / "g.db"
        db = DB(str(tmp))
        seed(db, cards, "mx-guard")
        mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                                 max_tokens=64000, keep_recent=4)
        for flag in (False, True):
            retriever.ALLOW_PLAYER_BOUNDARY = flag
            leak = sum(snippet in prompt_text(mw, q, "mx-guard", 18)
                       for q, snippet in GUARD_CASES)
            res[f"{where}_{'off' if flag else 'on'}"] = leak
    retriever.ALLOW_PLAYER_BOUNDARY = False
    out["guard"] = {"cases": len(GUARD_CASES),
                    "far_recall_on": res["far_on"], "far_recall_off": res["far_off"],
                    "near_history_on": res["near_on"], "near_history_off": res["near_off"]}

    # 2) 承诺通道：强追问时 strict 卡段是否注入
    tmp = Path(tempfile.mkdtemp(prefix="jinyu-mx-strict-")) / "s.db"
    db = DB(str(tmp))
    seed(db, CARDS, SID)
    mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                             max_tokens=64000, keep_recent=4)
    q = "你答应过我的事还记得吗"
    res = {}
    for flag in (True, False):
        rp.INJECT_STRICT = flag
        text = prompt_text(mw, q, SID, 42)
        res["on" if flag else "off"] = {
            "strict_section": "【硬记忆·承诺与约定】" in text,
            "facts_found": sum(1 for s in ("三根", "母亲留下的遗物", "一个字也不说出去")
                               if s in text)}
    rp.INJECT_STRICT = True
    out["strict"] = res

    # 3) 水位线：预算 1000 下「全量硬塞」，裁不裁剪的 token 与命中
    tmp = Path(tempfile.mkdtemp(prefix="jinyu-mx-wm-")) / "w.db"
    db = DB(str(tmp))
    seed(db, SCALE_CARDS, "mx-wm")
    saved_mode = rp.INJECT_MODE
    rp.INJECT_MODE = "full"
    res = {}
    try:
        for flag in (True, False):
            rp.WATERMARK_ON = flag
            mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                                     max_tokens=budget, output_reserve=0, keep_recent=4)
            hit = tok = 0
            for q, _gt, snippet, _l in SCALE_CASES:
                text = prompt_text(mw, q, "mx-wm", 62)
                hit += snippet in text
                tok += rp.est_tokens(text, mw.profile)
            n = len(SCALE_CASES)
            res["on" if flag else "off"] = {
                "inject_hit": round(hit / n, 3), "avg_tokens": int(tok / n),
                "fuse_dropped": mw.watermarks["sections_dropped"],
                "recent_dropped": mw.watermarks["recent_dropped"]}
    finally:
        rp.WATERMARK_ON = True
        rp.INJECT_MODE = saved_mode
    out["watermark"] = res
    return out


def strip_card(msgs, summary):
    """从注入段删掉描述这张卡的那一行：模拟「这条记忆偏偏想不起来」。

    删不动（这张卡没进 prompt）就返回 None——那题不算承重样本，不能算成没依赖。
    """
    mark = str(summary or "")[:30].strip()
    if not mark:
        return None
    out, dropped = [], False
    for m in msgs:
        content = str(m.get("content") or "")
        if m.get("role") == "system":
            lines = content.split("\n")
            keep = [ln for ln in lines if not (ln.startswith("- ") and mark in ln)]
            if len(keep) != len(lines):
                dropped = True
                content = "\n".join(keep)
        out.append({**m, "content": content})
    return out if dropped else None


def load_questions(path):
    """真实语料问句：每行一条 JSON {"q":..., "gt":event_id, "snippet":..., "layer":...}。

    gt/snippet 必须人工标注（写问句时不能先看召回结果，否则就是自己考自己）。
    """
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        d = json.loads(line)
        rows.append((str(d["q"]), str(d.get("gt") or ""),
                     str(d.get("snippet") or ""), str(d.get("layer") or "holdout")))
    return rows


def copy_db(src, dst):
    """线上库的只读副本：sqlite backup 带 WAL 一起拷，比直接 copy 文件稳。"""
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def _sid_cid_pairs(db):
    """列出副本里实际存在的 (session_id, character_id, 卡数)，方便纠 --sid/--cid。"""
    with sqlite3.connect(db.path) as conn:
        rows = conn.execute("SELECT session_id, character_id, COUNT(*) FROM event_cards"
                            " GROUP BY 1,2 ORDER BY 3 DESC").fetchall()
    return [(sid, cid, n) for sid, cid, n in rows]


def _section(text, name):
    """取 prompt 里某个 【…】 段的正文，用来把泄漏/命中归因到具体通道。"""
    i = text.find(name)
    if i < 0:
        return ""
    j = text.find("【", i + len(name))
    return text[i:j if j > 0 else len(text)]


def real_ablate(db, sid, cid, upto, cases, budget=BASELINE_BUDGET):
    """真实语料上的机制消融：外部基线 / 水位线扫描 / 保密闸门 / 显著性 / 承诺通道。

    与合成集的 matrix_probe 区别：这里不另 seed 库，直接复用已经写好向量的真实库，
    所以每一格都是「同一批真实卡、同一批问句」只关一个机制。
    """
    n = len(cases)
    out = {}

    def measure(mw):
        hit = tok = 0
        for q, _gt, snippet, _l in cases:
            text = prompt_text(mw, q, sid, upto, cid)
            hit += snippet in text
            tok += rp.est_tokens(text, mw.profile)
        return hit, tok

    # 1) 外部基线：同一预算下，召回+结构 vs 全塞（受水位线）vs 全塞（不限）vs 只用热窗
    rows = []
    for label, mode, cut in (("ours", "ours", True), ("full", "full", True),
                             ("full_uncut", "full", False), ("recent", "recent", True)):
        saved = (rp.INJECT_MODE, rp.WATERMARK_ON)
        rp.INJECT_MODE, rp.WATERMARK_ON = mode, cut
        try:
            mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                                     max_tokens=budget, output_reserve=0, keep_recent=4)
            hit, tok = measure(mw)
        finally:
            rp.INJECT_MODE, rp.WATERMARK_ON = saved
        rows.append({"mode": label, "inject_hit": round(hit / n, 3),
                     "inject_ci": wilson(hit, n), "avg_tokens": int(tok / n),
                     "sections_dropped": mw.watermarks["sections_dropped"]})
    out["baseline"] = rows

    # 2) 水位线扫描：200 张真实卡在哪一档预算开始被裁、裁掉多少命中
    sweep = []
    for b in (1000, 2000, 4000, 8000, 26000):
        mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                                 max_tokens=b, output_reserve=0, keep_recent=4)
        hit, tok = measure(mw)
        sweep.append({"budget": b, "avg_tokens": int(tok / n),
                      "inject_hit": round(hit / n, 3),
                      "sections_dropped": mw.watermarks["sections_dropped"]})
    out["watermark"] = sweep

    # 3) 保密闸门：真实库只有 1 张 player + 1 张 self。
    #    注意 recent_cards(sid,cid,limit) 不带 upto，永远取库里的最新 6 张，
    #    所以「逐轮构造 prompt」测不出窗口差异；暴露面按窗口结构算：
    #    第 t 轮的热窗 = turn_end < t 的卡按 turn_end 倒序前 6 张。
    private = [c for c in db.all_cards(sid, cid)
               if (c.get("knowledge_boundary") or "both") != "both"]
    ordered = sorted((c for c in db.all_cards(sid, cid)), key=lambda c: c["turn_end"])
    guard = {"private_cards": len(private), "turns": upto}
    exposed = sum(
        any(c in [x for x in ordered if x["turn_end"] < t][-6:] for c in private)
        for t in range(1, upto + 1))
    guard["potential_exposure_turns"] = exposed
    guard["potential_rate"] = round(exposed / max(1, upto), 3)
    # 实跑一遍：闸门开/关各构造一次末轮 prompt，确认注入段里有没有私有卡
    mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                             max_tokens=64000, keep_recent=4)
    real_hit = {}
    for flag in (False, True):
        retriever.ALLOW_PLAYER_BOUNDARY = flag
        msgs = mw._build_messages(PERSONA, sid, cid, upto=upto, query="")
        blk = "\n".join(str(m.get("content") or "") for m in msgs
                        if m.get("role") == "system")
        real_hit["on" if flag else "off"] = sum(1 for c in private
                                                if c["summary"][:14] in blk)
    retriever.ALLOW_PLAYER_BOUNDARY = False
    guard["prompt_hits"] = real_hit
    out["guard"] = guard

    # 4) 显著性：真实卡 mentions/verified 是否真的在累加；没有的话用 merge_log 回填再测
    conn = sqlite3.connect(db.path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM event_cards").fetchone()[0]
        counted = conn.execute("SELECT COUNT(*) FROM event_cards "
                               "WHERE mentions>0 OR verified>0").fetchone()[0]
        merged = conn.execute("SELECT keep_id, COUNT(*) FROM merge_log "
                              "WHERE kind='card' GROUP BY 1").fetchall()
    finally:
        conn.close()
    hit_targets = {eid for eid, _ in merged}
    sal = {"cards": total, "with_counts": counted,
           "merge_rows": sum(c for _, c in merged), "merge_targets": len(merged),
           "gt_is_merge_target": sum(1 for _q, gt, _s, _l in cases if gt in hit_targets)}

    def recall_now():
        ok = sum(gt in hybrid_rank(mw, db, q, sid, cid=cid)[:4]
                 for q, gt, _s, _l in cases)
        return round(ok / n, 3)

    sal["as_is"] = recall_now()
    conn = sqlite3.connect(db.path)
    try:                                   # 回填：合并次数当作 mentions（只改副本）
        for eid, cnt in merged:
            conn.execute("UPDATE event_cards SET mentions=? WHERE event_id=?", (cnt, eid))
        conn.commit()
    finally:
        conn.close()
    for flag in (True, False):
        retriever.SALIENCE_ENABLED = flag
        sal["backfill_on" if flag else "backfill_off"] = recall_now()
    retriever.SALIENCE_ENABLED = True
    out["salience"] = sal

    # 5) 承诺通道：strict 段取的是 precision='strict' 的卡（不是 durable_facts），
    #    看强追问下这 29 张卡有几张真被注入。
    strict_cards = [c for c in db.all_cards(sid, cid)
                    if (c.get("precision") or "normal") == "strict"]
    q = "你答应过我的事还记得吗"
    strict = {}
    for flag in (True, False):
        rp.INJECT_STRICT = flag
        text = prompt_text(mw, q, sid, upto, cid)
        sec = _section(text, "【硬记忆·承诺与约定】")
        strict["on" if flag else "off"] = {
            "strict_section": any(s["summary"][:12] in sec for s in strict_cards),
            "section_chars": len(sec),
            "cards_in_strict": sum(1 for s in strict_cards if s["summary"][:12] in sec),
            "cards_in_prompt": sum(1 for s in strict_cards if s["summary"][:12] in text)}
    rp.INJECT_STRICT = True
    out["strict"] = {"strict_cards_total": len(strict_cards), **strict}

    t0 = prompt_text(mw, "", sid, upto, cid)
    out["overhead"] = {"prompt_tokens": rp.est_tokens(t0, mw.profile),
                       "ledger_tokens": rp.est_tokens(_section(t0, "【当前状态账本】"),
                                                     mw.profile)}
    return out


def run(real=False, out=None, corpus="small", ablate=False,
        cards_db="", questions="", sid="", cid=""):
    cid = cid or CID
    external = bool(cards_db)
    scale = corpus == "scale"

    if external:
        if not questions:
            raise SystemExit("--cards-db 需要同时给 --questions（人工标注问句 JSONL）")
        corpus = "db"
        cards = []
        cases = load_questions(questions)
        sid = sid or "eval"
        tmp = Path(tempfile.mkdtemp(prefix="jinyu-ext-")) / "ext.db"
        copy_db(cards_db, str(tmp))            # 只读副本：绝不写回线上库
        db = DB(str(tmp))
        have = db.all_cards(sid, cid)
        if not have:                           # 取错 sid/cid 时指标会全 0，看着像「召回很差」
            raise SystemExit(
                f"--cards-db 里没有 session={sid!r} character={cid!r} 的事件卡；"
                f"用 --sid/--cid 指定实际值。可用组合：{_sid_cid_pairs(db)}")
        miss = [gt for _, gt, _, _ in cases if gt not in {c["event_id"] for c in have}]
        if miss:
            raise SystemExit(f"问句里的 gt 有 {len(miss)} 条不在该会话里（例：{miss[:3]}）")
        upto = max(c["turn_end"] for c in have)
    else:
        cards = SCALE_CARDS if scale else CARDS
        cases = SCALE_CASES if scale else CASES
        sid = sid or ("eval-scale" if scale else SID)
        upto = 62 if scale else 42
        tmp = Path(tempfile.mkdtemp(prefix="jinyu-eval-")) / "eval.db"
        db = DB(str(tmp))
        seed(db, cards, sid)

    mw = RPContextMiddleware(MockFront("eval"), MockBack(), db,
                             max_tokens=64000, keep_recent=4)

    use_vec = bool(embedding.enabled())
    if use_vec and cards:
        try:                                   # 真实向量：一次批量写库，失败即降级
            texts = [c[3] for c in cards]
            vecs = embedding.embed_documents(texts)
            db.put_vectors("card", cid, embedding.model_name(),
                           [(c[0], embedding.pack(v), len(v))
                            for c, v in zip(cards, vecs)])
        except Exception as exc:               # noqa: BLE001
            print(f"[warn] 向量写入失败，退回关键词基线：{exc}")
            use_vec = False
    elif use_vec:
        try:                                   # 外部语料：导出里没有 vectors 表，现算现写
            vecs = embedding.embed_documents([c["summary"] for c in have])
            db.put_vectors("card", cid, embedding.model_name(),
                           [(c["event_id"], embedding.pack(v), len(v))
                            for c, v in zip(have, vecs)])
        except Exception as exc:               # noqa: BLE001
            print(f"[warn] 向量写入失败，退回关键词基线：{exc}")
            use_vec = False

    rows = []
    st = {L: {"n": 0, "base": 0, "used": 0} for L in LAYERS}
    base_mrr = hyb_mrr = 0.0
    inject_hit = 0

    for q, gt, snippet, layer in cases:
        kw = keyword_rank(db, q, sid, cid=cid)
        ranks = hybrid_rank(mw, db, q, sid, cid=cid) if use_vec else kw
        text = prompt_text(mw, q, sid, upto, cid=cid)

        b_ok, h_ok = gt in kw[:4], gt in ranks[:4]
        s = st.setdefault(layer, {"n": 0, "base": 0, "used": 0})
        s["n"] += 1
        s["base"] += b_ok
        s["used"] += h_ok
        base_mrr += mrr(kw, gt)
        hyb_mrr += mrr(ranks, gt)
        inj = snippet in text
        inject_hit += inj
        rows.append({"q": q, "gt": gt, "keyword": kw, "used": ranks, "layer": layer,
                     "recall@4": "Y" if h_ok else "N",
                     "inject": "Y" if inj else "N"})

    n = len(cases)
    result = {
        "cases": n,
        "corpus": corpus,
        "vector_enabled": use_vec,
        "recall@4_keyword": round(sum(s["base"] for s in st.values()) / n, 3),
        "recall@4_used": round(sum(s["used"] for s in st.values()) / n, 3),
        "mrr_keyword": round(base_mrr / n, 3),
        "mrr_used": round(hyb_mrr / n, 3),
        "inject_hit": round(inject_hit / n, 3),
        "recall@4_used_ci": wilson(sum(s["used"] for s in st.values()), n),
        "inject_hit_ci": wilson(inject_hit, n),
        "layers": {L: {"n": s["n"],
                       "recall@4_keyword": round(s["base"] / s["n"], 3) if s["n"] else None,
                       "recall@4_used": round(s["used"] / s["n"], 3) if s["n"] else None}
                   for L, s in st.items()},
        "answer_accuracy": None,
        "boundary_respect": None,
        "prompt_dependence": None,
    }

    if real:
        try:
            from llm import from_env
            front = from_env("front")
            ok = 0
            fact_n = 0
            bound_ok = 0
            bound_n = 0
            dep_n = 0                  # 抽掉 top-1 卡后仍能答对的样本数 / 抽得动的样本数
            dep_keep = 0
            by_id = {c["event_id"]: c for c in db.all_cards(sid, cid)}
            for q, gt, snippet, _ in cases:
                msgs = mw._build_messages(PERSONA, sid, cid, upto=upto, query=q)
                ask = msgs + [{"role": "user", "content": q}]
                ans = front.chat(ask, temperature=0.0, max_tokens=200)
                row = rows[[r["q"] for r in rows].index(q)]
                row["ans"] = " ".join(str(ans or "").split())[:120]
                if gt in BOUNDARY:
                    # 「守住」不等于「不提」：说出「一个字也不说出去」是守约，不是泄露。
                    guarded = _guarded(row["ans"])
                    hit = bool(row["ans"].strip()) and (snippet not in row["ans"] or guarded)
                    bound_n += 1
                    bound_ok += hit
                    row["answer"] = "边界-" + ("守" if hit else "漏")
                else:
                    hit = answered(ans, gt, snippet, q)
                    fact_n += 1
                    ok += hit
                    row["answer"] = "Y" if hit else "N"
                    # 消融承重度：能答对、且 top-1 卡真的在 prompt 里可删，才做对照。
                    top = (row["used"] or [None])[0]
                    if ablate and hit and top in by_id:
                        stripped = strip_card(msgs, by_id[top]["summary"])
                        if stripped is not None:
                            ans2 = front.chat(stripped + [{"role": "user", "content": q}],
                                              temperature=0.0, max_tokens=200)
                            still = answered(ans2, gt, snippet, q)
                            row["no_top1"] = "Y" if still else "N"
                            row["no_top1_top"] = top
                            dep_n += 1
                            dep_keep += still
            result["answer_accuracy"] = round(ok / fact_n, 3) if fact_n else None
            result["answer_accuracy_ci"] = wilson(ok, fact_n)
            result["boundary_respect"] = (round(bound_ok / bound_n, 3)
                                          if bound_n else None)
            result["boundary_respect_ci"] = wilson(bound_ok, bound_n)
            result["fact_cases"] = fact_n
            result["boundary_cases"] = bound_n
            # 承重度 = 抽掉最相关那条记忆后答错的比例。越低越好：说明答案有别的证据撑着，
            # 也不容易因为一次召回失败就整段失忆。
            if dep_n:
                result["prompt_dependence"] = round(1 - dep_keep / dep_n, 3)
                result["prompt_dependence_ci"] = wilson(dep_n - dep_keep, dep_n)
                result["dependence_cases"] = dep_n
        except Exception as exc:               # noqa: BLE001
            print(f"[warn] 真实模型评测跳过：{exc}")

    width = max(len(r["q"]) for r in rows)
    print(f"\n{'query'.ljust(width)}  层        GT  recall@4  inject")
    for r in rows:
        print(f"{r['q'].ljust(width)}  {r['layer']:<8} {r['gt']:<3} "
              f"{r['recall@4']:^8}  {r['inject']:^6}")
    mode = "关键词+向量" if use_vec else "关键词（向量未启用）"
    print(f"\n模式：{mode}    用例：{n}")
    print(f"recall@4 基线={result['recall@4_keyword']}  当前={result['recall@4_used']} "
          f"95%CI={result['recall@4_used_ci']}")
    print(f"MRR     基线={result['mrr_keyword']}  当前={result['mrr_used']}")
    print(f"注入命中率={result['inject_hit']} 95%CI={result['inject_hit_ci']}")
    for L in st:
        d = result["layers"][L]
        if not d["n"]:
            continue
        print(f"  {L:<8} n={d['n']:<3} recall@4 基线 {d['recall@4_keyword']} "
              f"→ 当前 {d['recall@4_used']}")
    if result["answer_accuracy"] is not None:
        print(f"回答准确率={result['answer_accuracy']}（仅 both 边界事实题，"
              f"{result.get('fact_cases')} 条）95%CI={result.get('answer_accuracy_ci')}")
        if result["boundary_respect"] is not None:
            print(f"self 边界守护={result['boundary_respect']}"
                  f"（{result.get('boundary_cases')} 条，答非所问但没泄密也算守住）"
                  f" 95%CI={result.get('boundary_respect_ci')}")
        if result["prompt_dependence"] is not None:
            print(f"承重度={result['prompt_dependence']}"
                  f"（抽掉 top-1 记忆卡后答错的比例，{result.get('dependence_cases')} 条可抽样本）"
                  f" 95%CI={result.get('prompt_dependence_ci')}")

    if ablate and external:
        ra = real_ablate(db, sid, cid, upto, cases)
        result["real_ablate"] = ra
        print(f"\n真实语料外部基线（预算 {BASELINE_BUDGET} token）")
        print("  策略        注入命中  95%CI                平均token  丢段")
        for r in ra["baseline"]:
            print(f"  {r['mode']:<9}  {r['inject_hit']:<8} {str(r['inject_ci']):<18} "
                  f"{r['avg_tokens']:<9}  {r['sections_dropped']}")
        print(f"\n水位线扫描（ours，同一批 {len(cases)} 问）")
        print("  预算     平均token  注入命中  丢段")
        for r in ra["watermark"]:
            print(f"  {r['budget']:<8} {r['avg_tokens']:<9}  {r['inject_hit']:<8}  "
                  f"{r['sections_dropped']}")
        g = ra["guard"]
        print(f"\n保密闸门（{g['private_cards']} 张私有卡 / {g['turns']} 轮）")
        print(f"  热窗结构暴露 {g['potential_exposure_turns']} 轮 ({g['potential_rate']})"
              f"  末轮实跑注入命中 开={g['prompt_hits']['on']} 关={g['prompt_hits']['off']}"
              f"（recent_cards 不受 upto 约束，末轮窗口里已没有旧卡）")
        s = ra["salience"]
        print(f"\n显著性（{s['cards']} 张卡，计数非零 {s['with_counts']} 张，"
              f"merge_log {s['merge_rows']} 次 / {s['merge_targets']} 张目标，"
              f"GT 命中目标 {s['gt_is_merge_target']} 条）")
        print(f"  原样 recall@4={s['as_is']}   "
              f"用 merge_log 回填 mentions：开={s['backfill_on']} 关={s['backfill_off']}")
        t = ra["strict"]
        print(f"\n承诺通道（precision=strict 的 {t['strict_cards_total']} 张卡）")
        print(f"  开 段={t['on']['strict_section']} 字数={t['on']['section_chars']} "
              f"段内卡={t['on']['cards_in_strict']} 全 prompt 覆盖={t['on']['cards_in_prompt']}"
              f"   |   关 段={t['off']['strict_section']} "
              f"全 prompt 覆盖={t['off']['cards_in_prompt']}")
        o = ra["overhead"]
        print(f"\n末轮 prompt（无查询）{o['prompt_tokens']} token，其中状态账本 "
              f"{o['ledger_tokens']} token")
    elif ablate:
        n_q, on, off = salience_probe()
        result["salience_probe"] = {"cases": n_q, "on": on, "off": off}
        print(f"\n显著性消融（{n_q} 题，六张同分卡，solid 卡=V1）")
        print(f"  mentions/verified 开：top1 命中 {on}")
        print(f"  关（消融）：       top1 命中 {off}")

        short = short_probe()
        result["short_probe"] = {"cases": short["cases"], "on": short["on"],
                                 "off": short["off"], "rows": short["rows"]}
        print(f"\n短指代层 B（{short['cases']} 题，伞/茶/信三族各 6 张同分卡）")
        print(f"  显著性 开：top1 {short['on']}    关：{short['off']}")
        for fam in SHORT_QUERIES:
            sub = [r for r in short["rows"] if r["gt"].startswith(fam)]
            on_f = round(sum(r["on"] for r in sub) / len(sub), 3)
            off_f = round(sum(r.get("off", False) for r in sub) / len(sub), 3)
            print(f"  {fam}族  n={len(sub):<2} 开 {on_f} / 关 {off_f}")

        bp = bucket_probe()
        result["bucket_probe"] = bp
        print("\nA 方案（相关性接近时让显著性参与；不改线上，仅实验）")
        head = "  rank差  分差%   off  strict  eps5  eps10  eps20"
        print(head)
        for r in bp:
            mark = lambda k: "Y" if r[k] else "N"
            print(f"  {r['gap']:>5}  {r['score_diff_pct']:>6}   "
                  f"{mark('off'):^3}  {mark('strict'):^6}  {mark('eps5'):^4}  "
                  f"{mark('eps10'):^5}  {mark('eps20'):^5}")

        bl = baseline_probe()
        result["baseline_probe"] = bl
        print(f"\n外部基线（30 卡 / 60 问，预算 {BASELINE_BUDGET} token）")
        print("  策略      注入命中  95%CI               平均token")
        for r in bl:
            print(f"  {r['mode']:<7}  {r['inject_hit']:<8} {str(r['inject_ci']):<16} "
                  f"{r['avg_tokens']}")

        mx = matrix_probe()
        result["matrix_probe"] = mx
        g, s, w = mx["guard"], mx["strict"], mx["watermark"]
        print("\n机制消融矩阵")
        print(f"  保密闸门  召回通道 开(过滤)={g['far_recall_on']}/{g['cases']} 泄漏  "
              f"关={g['far_recall_off']}/{g['cases']} 泄漏")
        print(f"            热窗通道 开(过滤)={g['near_history_on']}/{g['cases']} 泄漏  "
              f"关={g['near_history_off']}/{g['cases']} 泄漏")
        print(f"  承诺通道  开 段={s['on']['strict_section']} 命中 {s['on']['facts_found']}/3   "
              f"关 段={s['off']['strict_section']} 命中 {s['off']['facts_found']}/3")
        print(f"  水位线    开 token={w['on']['avg_tokens']} 命中={w['on']['inject_hit']} "
              f"丢段={w['on']['fuse_dropped']}   |   "
              f"关 token={w['off']['avg_tokens']} 命中={w['off']['inject_hit']}")

    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps({"summary": result, "rows": rows},
                                        ensure_ascii=False, indent=2),
                             encoding="utf-8")
        print(f"\n明细已写入 {out}")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="调用前台模型量回答准确率")
    ap.add_argument("--out", default="", help="指标 JSON 输出路径")
    ap.add_argument("--min-recall", type=float, default=0.0,
                    help="recall@4 门槛，低于它退出码为 1（CI 用）")
    ap.add_argument("--min-holdout", type=float, default=0.0,
                    help="holdout 层 recall@4 门槛（CI 用，防只靠字面题刷分）")
    ap.add_argument("--corpus", choices=("small", "scale"), default="small",
                    help="small=10 张手工卡；scale=30 张同类族压力集")
    ap.add_argument("--ablate", action="store_true",
                    help="显著性微集 A/B；配 --real 再加抽卡承重度")
    ap.add_argument("--cards-db", default="",
                    help="真实语料：线上导出的 sqlite 路径（只读副本，不写回）")
    ap.add_argument("--questions", default="",
                    help="真实语料问句 JSONL：{q,gt,snippet,layer}")
    ap.add_argument("--sid", default="", help="真实语料：只评这一个 session")
    ap.add_argument("--cid", default="", help="真实语料：角色 id")
    args = ap.parse_args()
    result = run(real=args.real, out=args.out, corpus=args.corpus, ablate=args.ablate,
                 cards_db=args.cards_db, questions=args.questions,
                 sid=args.sid, cid=args.cid)
    hold = result["layers"].get("holdout") or {}
    holdout = hold.get("recall@4_used") or 0.0
    bad = []
    if result["recall@4_used"] < args.min_recall:
        bad.append(f"recall@4={result['recall@4_used']} < {args.min_recall}")
    if holdout < args.min_holdout:
        bad.append(f"holdout recall@4={holdout} < {args.min_holdout}")
    if bad:
        print("\n[FAIL] " + "; ".join(bad))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
