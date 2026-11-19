"""平台能力表 + 元素降级。

只做两件事：
1. 声明每个平台支持哪些消息元素（阶段一只用到 text）；
2. 把收到的元素序列压平成纯文本，不支持的元素退化成可读标记。

这一层不依赖任何平台 SDK，Koishi / NoneBot / 以后的适配器都能复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable


@dataclass(frozen=True)
class Capability:
    """一个平台能做什么。"""

    name: str
    text: bool = True
    image: bool = False
    audio: bool = False
    video: bool = False
    file: bool = False
    quote: bool = False
    mention: bool = False
    markdown: bool = False
    max_text_chars: int = 4000
    notes: str = ""


# 阶段一只跑 KOOK：私聊文本；图片/语音等先降级成占位符，不猜内容。
CAPABILITIES: dict[str, Capability] = {
    "kook": Capability(
        name="kook",
        image=True,
        file=True,
        quote=True,
        mention=True,
        max_text_chars=4000,
        notes="官方机器人；私聊需先加入同一服务器。语音消息不支持。",
    ),
    "telegram": Capability(
        name="telegram",
        image=True,
        audio=True,
        video=True,
        file=True,
        quote=True,
        mention=True,
        markdown=True,
        max_text_chars=4096,
        notes="阶段二；服务器侧需先解决出网。",
    ),
    "qq": Capability(
        name="qq",
        image=True,
        audio=True,
        video=True,
        file=True,
        quote=True,
        mention=True,
        max_text_chars=2000,
        notes="现有 NapCat 链路，不经本侧车。",
    ),
}

# 元素名 -> 纯文本占位符；不在表里的元素直接丢弃。
DEGRADE = {
    "img": "[图片]",
    "image": "[图片]",
    "audio": "[语音]",
    "record": "[语音]",
    "video": "[视频]",
    "file": "[文件]",
    "emoji": "[表情]",
    "sharp": "[表情]",
    "quote": "",
    "at": "",
    "referrer": "",
}


def get(platform: str) -> Capability:
    """取平台能力；未登记的平台按纯文本处理。"""
    return CAPABILITIES.get(str(platform or "").lower(), Capability(name=platform or "unknown"))


def _walk(node: Any) -> Iterable[str]:
    """深度优先压平 satori Element。不依赖具体元素类，只认 text / 占位符。"""
    text = getattr(node, "text", None)
    if isinstance(text, str):
        yield text
        return
    # satori-python 的 MessageObject.content 直接是字符串（KOOK 侧实测如此）
    content = getattr(node, "content", None)
    if isinstance(content, str):
        yield content
        return
    children = getattr(node, "_children", None)
    if children:
        for child in children:
            yield from _walk(child)
        return
    yield DEGRADE.get(type(node).__name__.lower(), "")


def to_text(message: Any, platform: str = "kook") -> str:
    """把一条消息压成纯文本；超长按平台上限截断。"""
    if message is None:
        return ""
    if isinstance(message, str):
        raw = message
    else:
        raw = "".join(_walk(message))
    # 连续空白折叠成单个空格，避免把换行排版带进记忆库
    text = " ".join(raw.split())
    cap = get(platform)
    if cap.max_text_chars > 0:
        text = text[: cap.max_text_chars]
    return text.strip()


def clamp_outbound(text: str, platform: str = "kook") -> list[str]:
    """把回复按平台上限切成若干段；阶段一不处理富文本。"""
    cap = get(platform)
    limit = max(1, cap.max_text_chars)
    body = str(text or "")
    if not body:
        return []
    return [body[i : i + limit] for i in range(0, len(body), limit)]
