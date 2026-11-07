# -*- coding: utf-8 -*-
"""回复分气泡：一句一个，括号动作单独成泡。

QQ 里一条消息就是一个气泡，角色扮演常见写法是「（动作）台词。台词。」。规则：

1. 先保护富消息标记 [[img:..]] 和链接，切句时不把它们拆开；
2. 括号（全角/半角）整体保护。括号出现在句子开头或结尾时单独成一个气泡，
   「（歪头）主人今天好累啊。」→「（歪头）」+「主人今天好累啊。」；
   句中的括号留在原句里，「我（小声）跟你说个事。」不切三截；
3. 按换行和 。！？!? 切，标点留在前一句；
4. 超过上限时按句数均分成 limit 条，不把剩下的全塞进最后一条——
   那样长回复会变成「9 条 + 一大坨」，等于没拆。
"""
import re

RICH_TOKEN = re.compile(r"\[\[[^\[\]]*\]\]|https?://[^\s\[\]（）]+")
ACTION = re.compile(r"（[^（）]*）|\([^()]*\)")
_SENT = re.compile(r"\n+|(?<=[。！？!?])")
_MARK = re.compile(r"\x00(\d+)\x00")
_MARK_HEAD = re.compile(r"\x00(\d+)\x00")
_MARK_TAIL = re.compile(r"\x00(\d+)\x00$")


def _is_action(mark: str) -> bool:
    return bool(mark) and mark[0] in "（("


def _peel(part: str, marks: list) -> list:
    """把句子首尾的括号动作剥成独立气泡；句中的括号不动。"""
    out, rest = [], part
    while True:
        m = _MARK_HEAD.match(rest)
        if not m or not _is_action(marks[int(m.group(1))]):
            break
        out.append(m.group(0))
        rest = rest[m.end():].strip(" \t")
    tail = []
    while True:
        rest = rest.rstrip(" \t")
        m = _MARK_TAIL.search(rest)
        if not m or not _is_action(marks[int(m.group(1))]):
            break
        tail.insert(0, m.group(0))
        rest = rest[:m.start()].rstrip(" \t")
    if rest:
        out.append(rest)
    return out + tail


def split(reply: str, limit: int = 10) -> list:
    """把一条回复拆成多条气泡（纯函数，不依赖 NoneBot）。"""
    limit = max(1, int(limit or 10))
    marks = []

    def _keep(m):
        marks.append(m.group(0))
        return f"\x00{len(marks) - 1}\x00"

    text = RICH_TOKEN.sub(_keep, reply or "")
    text = ACTION.sub(_keep, text)
    out = []
    for part in _SENT.split(text):
        part = part.strip(" \t")
        if part:
            out.extend(_peel(part, marks))
    if len(out) > limit:
        size = -(-len(out) // limit)                   # 均分，余数进前面的气泡
        out = ["\n".join(out[i:i + size]) for i in range(0, len(out), size)]
    return [_MARK.sub(lambda m: marks[int(m.group(1))], b) for b in out]
