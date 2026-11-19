# -*- coding: utf-8 -*-
"""出站 AI 味审计：只记日志，不改回复。

规则来源：维基百科《Signs of AI writing》+ humanizer 的常见模式，剔掉所有
「文章专用」的（段落长度、小标题层级），只留中文聊天里也会露馅的。
刻意不抓：破折号以外的角色文风、动作描写（尾巴拍水）、口语重复——那是人设，
清掉就 OOC 了。所以这里永远只观察，不做任何改写。

环境变量：
  RP_SLOP_AUDIT  1/0 开关，默认 1（只写日志）
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("rp_memory")

# (名字, 正则)。宁缺毋滥：只收「助手腔」确信度高的，角色文风一律不碰。
RULES = (
    ("翻案腔", re.compile(r"不是[^。！？\n]{0,18}?而是|不只是[^。！？\n]{0,18}?更是"
                          r"|不仅[^。！？\n]{0,18}?还|与其说[^。！？\n]{0,18}?不如说")),
    ("收尾总结", re.compile(r"(总而言之|说到底|归根结底|总的来说|总之，)")),
    ("假深刻", re.compile(r"(真正重要的是|本质上|某种意义上|从某种角度|这才是最关键的)")),
    ("铺垫起手", re.compile(r"^(说句实话|老实说|实话说|其实吧|事情是这样的|值得一提的是"
                            r"|说实话，)")),
    ("空气辩论", re.compile(r"我不是说[^。！？\n]{0,20}?但是|并不是说[^。！？\n]{0,20}?而是")),
    ("破折号", re.compile(r"——")),
    ("分号", re.compile(r"[；;]")),
    ("加粗标题", re.compile(r"(\*\*|^#{1,4}\s)", re.M)),
    ("列点", re.compile(r"^\s*(\d+[.、)]|[-*•])\s", re.M)),
    ("三段式", re.compile(r"首先[^。！？\n]{0,30}?其次|其次[^。！？\n]{0,30}?最后")),
    ("助手口癖", re.compile(r"(希望(这|以上)[^。！？\n]{0,10}?(帮助|有用)"
                            r"|帮到你|帮助到你|如果你愿意|我可以帮你"
                            r"|需要我[^。！？\n]{0,6}吗|还有什么[^。！？\n]{0,6}吗)")),
    ("值得注意", re.compile(r"(值得注意的是|不难看出|可以看出|需要注意的是)")),
    ("超长气泡", re.compile(r"^[^\n]{160,}$", re.M)),
)

# 开头一个装饰性 emoji（✨🌟…）也算，但正文里的普通表情不算
_LEAD_EMOJI = re.compile(r"^[\s]*([✨🌟💡🎯🔥💫🌸🎉])")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]")


def enabled() -> bool:
    return (os.environ.get("RP_SLOP_AUDIT") or "1").strip().lower() not in {
        "0", "false", "no", "off"}


def audit(text: str) -> list:
    """返回命中的规则名列表（同一规则只算一次）。"""
    t = str(text or "")
    if not t:
        return []
    hits = []
    for name, rx in RULES:
        if rx.search(t):
            hits.append(name)
    if _LEAD_EMOJI.search(t) or len(_EMOJI.findall(t)) >= 4:
        hits.append("装饰emoji")
    return hits


def log(text: str, sid: str = "", where: str = "") -> list:
    """只在命中时写一行日志；永远不改 text。"""
    if not enabled():
        return []
    hits = audit(text)
    if not hits:
        return []
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    logger.info("出站AI味 命中=%s 字数=%d 来源=%s sid=%s 原文=%s",
                ",".join(hits), len(body), where or "-", sid or "-", body[:80])
    return hits
