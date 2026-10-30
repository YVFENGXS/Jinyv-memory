# -*- coding: utf-8 -*-
"""四级披露（阶段 2）

接在保密硬门之后：硬门（player / 可疑文本 / 红队）决定「能不能进候选池」，
这一层只决定「已经进了池的卡，在当前场景能说到什么程度」。

纪律：
- 低等级只返回写死的固定句，绝不对 summary 做截断或摘要——截断就是泄漏。
- 低等级不输出 entity_hooks / tags / state_changes，钩子词本身就是信息。
- 任何异常都落到 silence，而不是 full——宁可少说，不能说漏。
- 三档阈值已用 tools/eval_memory.py 在自有语料上标定（见下方注释），
  不是 SeekMemory 的 0.55/0.18/0.03。
"""
import os
import re

import retriever

LEVELS = ("full", "paraphrase", "hint", "silence")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


# 标定记录（scale 60 题 × 私聊/群聊/直播，RP_DISC_ON=1，相关度用 score/top1）：
# FULL 0.60：普通卡 top-1 在三场景都够（最低是直播 0.85）；self 0.35、敏感 0.2 永远够不到。
#        例外是已验证主人的私聊：信任场景不乘 boundary/sensitivity，self 与保密卡同样 full。
# PARA 0.30：夹在私聊/群聊 self（0.35 / 0.315）与直播 self（0.2975）之间——
#            非主人私聊与群聊能听「记得这事但细节说不清」，直播只到「隐约觉得」。
# HINT 0.15：敏感卡 0.17~0.20 落到 hint，不再整张消失。
# 实测：三场景注入命中都回到 0.767（基线），标准答案卡 0 降级；每场景只剩 6 张次要卡进低等级。
FULL_MIN = _env_float("RP_DISC_FULL", 0.60)
PARA_MIN = _env_float("RP_DISC_PARA", 0.30)
HINT_MIN = _env_float("RP_DISC_HINT", 0.15)

DISCLOSURE_ENABLED = True    # 阶段 3 定标 + 相对分修复 + 18 条样例复核通过，灰度开启

# rel 由调用方按「同一问句内 score / top1」归一化到 0~1。
# 为什么不用绝对分：scale 语料里 4 道题的正确答案靠单字回退命中（0.6 / 3.8 = 0.158），
# 曾被误判成「几乎不相关」而静音。相关性交给证据闸门，披露层只比较
# 「这张卡在本轮召回里是不是最好的那一张」，再叠加场合与边界。

BOUNDARY_SELF = 0.35         # knowledge_boundary == "self"：角色自己知道，不能主动说破
SENSITIVE = 0.2              # 命中 SECRET_MARK：即使相关也只能点到为止
SUBJECT_BONUS = 0.2          # 卡的主体就是当前说话人：可以多说一点
# 2026-10-29 起后台按提示词产出 card.subject（主人/角色/钩子词），当前说话人
# 只在「已验证主人的私聊」能确定（speaker="主人"），所以这条加成目前只在主人
# 自己的车道上可能触发；群聊的逐条说话人身份还没透传，等 P1-2 再补。
# 注意：主人私聊已是信任场景（exposure 恒为相关度 =1 时封顶），加成不会带来额外披露；
# 它影响的是非主人私聊里「主体是主人」的卡，让它们不至于被压到静音。

# 场合因子：真正的压制来自 boundary / sensitivity；场合只作用于非 top-1 的次要卡。
# 群聊 0.90 / 直播 0.85：直播比群聊再收一档，但 top-1 普通卡在三场景都是 full。
PRIVATE_FACTOR = 1.0
OWNER_FACTOR = 1.0
GROUP_FACTOR = _env_float("RP_DISC_AUD_GROUP", 0.90)
LIVE_FACTOR = _env_float("RP_DISC_AUD_LIVE", 0.85)

PARAPHRASE_SENTENCE = "（你记得有件相关的事，但细节一时想不起来。）"
HINT_SENTENCE = "（你隐约觉得有件相关的事。）"

_CRITICAL = tuple(re.compile(p) for p in retriever.SECRET_MARK)


def sensitive(card: dict) -> bool:
    """命中保密词：这类卡即使相关，也只能点到为止。"""
    return any(p.search(str(card.get("summary") or "")) for p in _CRITICAL)


class Scene:
    """当前场景：私聊 / 群聊 / 直播，以及说话人是否为已验证主人。"""

    __slots__ = ("is_private", "is_owner", "is_live", "speaker")

    def __init__(self, is_private=False, is_owner=False, is_live=False,
                 speaker=""):
        self.is_private = bool(is_private)
        self.is_owner = bool(is_owner)
        self.is_live = bool(is_live)
        self.speaker = str(speaker or "")


def scene_of(sid: str, speaker: str = "", is_owner: bool = False) -> Scene:
    """按车道键推断场景：qq-u 私聊 / bili-l 直播 / 其余按群聊。"""
    head = str(sid or "")
    return Scene(is_private=head.startswith("qq-u"),
                 is_owner=is_owner,
                 is_live=head.startswith("bili-l"),
                 speaker=speaker)


def audience_factor(scene: Scene) -> float:
    if scene.is_private:
        return PRIVATE_FACTOR
    if scene.is_owner:
        return OWNER_FACTOR
    if scene.is_live:            # 直播间是公开场合，收敛得更狠
        return LIVE_FACTOR
    return GROUP_FACTOR


def _same_person(subject: str, speaker: str) -> bool:
    """卡主体与当前说话人是不是同一个人：群名片 / 昵称可能带 QQ，用包含判断。

    只在两边都不少于 2 字时才做包含：单字昵称（「白」「安」）做子串会把
    「白桦」「安和」全算成同一个人，宁可漏判也不要误判。
    """
    a, b = str(subject or "").strip(), str(speaker or "").strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 2 and len(b) >= 2 and (a in b or b in a)


def exposure(card: dict, scene: Scene, rel: float) -> float:
    """exposure = 边界 × 场合 × 相对相关度 × 敏感度（+ 主体加成）。

    已验证主人的私聊是「信任场景」：卡本来就是主人自己说过的话，边界与保密词
    不再压制，只保留相关度——对主人记得越多越好。群聊 / 直播 / 非主人私聊
    仍按 boundary 与 SECRET_MARK 压制（对那些场合才需要保密）。
    """
    trusted = bool(scene.is_private and scene.is_owner)
    boundary = 1.0 if trusted else (
        BOUNDARY_SELF if (card.get("knowledge_boundary") or "both") == "self" else 1.0)
    topic = max(0.0, min(1.0, float(rel or 0.0)))
    sens = 1.0 if trusted else (SENSITIVE if sensitive(card) else 1.0)
    score = boundary * audience_factor(scene) * topic * sens
    if _same_person(card.get("subject"), scene.speaker):
        score = min(1.0, score + SUBJECT_BONUS)
    return score


def decide(card: dict, scene: Scene, rel: float) -> tuple:
    """返回 (等级, exposure)；异常一律 silence，绝不放成 full。"""
    try:
        value = exposure(card, scene, rel)
    except Exception:
        return "silence", 0.0
    if value >= FULL_MIN:
        return "full", value
    if value >= PARA_MIN:
        return "paraphrase", value
    if value >= HINT_MIN:
        return "hint", value
    return "silence", value


def render(card: dict, level: str) -> str:
    """full → 原文；para/hint → 写死的固定句；silence → 空串。

    低等级分支不读卡里的任何文本、钩子或标签，保证输出与卡内容无交集。
    """
    if level == "full":
        return retriever.clean_text(card.get("summary") or "", 200)
    if level == "paraphrase":
        return PARAPHRASE_SENTENCE
    if level == "hint":
        return HINT_SENTENCE
    if level == "silence":
        return ""
    raise ValueError(f"unknown disclosure level: {level!r}")
