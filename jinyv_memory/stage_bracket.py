# -*- coding: utf-8 -*-
"""把回复里的括号动作描写翻译成一次身体动作。

只做三件事：抽括号、按动作词表预筛、把命中的描写拼成一句给后台解码器的意图。
真正选工具与参数仍走 jinyv_stage.intent（与 body_intent 同一条链路），
这里不连桥、不执行、不新增协议。

保守原则：一条回复最多一个动作；没有明确动作词、或移动/坐躺没有明确目标的
括号一律丢弃，交由上层记日志。
"""
from __future__ import annotations

import re
from typing import NamedTuple

# 全角/半角括号；不匹配嵌套。
_BRACKET_RE = re.compile(r"[（(]([^（）()]{1,60})[）)]")

# 语气词、停顿与叙述性补充：不是身体动作，命中就丢。
_IGNORE = {
    "笑", "笑了笑", "轻笑", "微笑", "苦笑", "叹气", "沉默", "沉默片刻", "停顿",
    "想了想", "顿了顿", "认真", "无奈", "小声", "轻声", "低声", "喃喃", "犹豫",
    "沉默了一会儿",
}

# 动作词 -> 类别。顺序即优先级：同一条里既有坐又有走时按「坐」处理。
_VERBS: list[tuple[str, tuple[str, ...]]] = [
    ("sit", ("坐下", "坐到", "坐回", "坐在", "坐好", "躺下", "躺到", "躺回",
             "躺在", "趴下", "趴到")),
    ("move", ("走过去", "走到", "走近", "过去", "挪", "退开", "退后", "靠过去",
              "蹲下")),
    ("gesture", ("转身", "起身", "站起", "站起来", "挥手", "摆手", "伸手",
                 "抱住", "抱你", "牵手", "摸了摸", "戳了戳", "拍了拍",
                 "点头", "摇头")),
    ("look", ("看向", "望向", "看着", "看你", "盯着", "转头", "回头", "抬头",
              "低头", "歪头", "侧头", "斜眼")),
    ("face", ("脸红", "皱眉", "眨眼", "嘟嘴", "别过头", "笑一下")),
    ("loop", ("呼吸", "尾巴", "甩尾", "摇尾")),
]

# 移动/坐躺必须有落点；没有就丢，避免「凭空乱走」。
_PLACES = ("床", "沙发", "椅", "凳", "窗", "门", "桌", "厨房", "客厅", "卧室",
           "浴室", "阳台", "冰箱", "躺椅", "地上", "你", "主人", "身边", "面前",
           "这边", "那边", "过来")
# 视线可以靠方向词成立，否则必须有一个明确对象。
_LOOK_TARGETS = ("你", "主人", "窗", "门", "床", "沙发", "桌", "屏幕", "手机", "外面")
_LOOK_DIRECT = ("抬头", "低头", "转头", "回头", "歪头", "侧头", "斜眼")

# 多分句/列举说明它不是一个动作；引号、英文、数字也不碰。
_MULTI = ("，", "。", "；", ";", ",", "、")
_QUOTES = ("“", "”", '"', "「", "」")

_MAX_LEN = 24


class BracketAction(NamedTuple):
    raw: str
    kind: str
    intent: str


def _norm(text: str) -> str:
    return " ".join(str(text or "").replace("\u3000", " ").split()).strip()


def _kind(text: str) -> str | None:
    for kind, words in _VERBS:
        if any(word in text for word in words):
            return kind
    return None


def _has_place(text: str) -> bool:
    return any(word in text for word in _PLACES)


def extract(reply: str) -> BracketAction | None:
    """返回第一个可执行的动作；没有就返回 None（调用方只记日志）。"""
    for match in _BRACKET_RE.finditer(str(reply or "")):
        raw = _norm(match.group(1))
        if not raw or len(raw) > _MAX_LEN:
            continue
        if raw in _IGNORE:
            continue
        if any(ch in raw for ch in _MULTI) or any(ch in raw for ch in _QUOTES):
            continue
        if re.search(r"[A-Za-z]|\d|https?://", raw):
            continue
        kind = _kind(raw)
        if kind is None:
            continue
        if kind in {"move", "sit"} and not _has_place(raw):
            continue
        if kind == "look" and not (
            _has_place(raw) or any(word in raw for word in _LOOK_DIRECT)
        ):
            continue
        return BracketAction(raw, kind, f"只执行这一个动作：{raw}")
    return None
