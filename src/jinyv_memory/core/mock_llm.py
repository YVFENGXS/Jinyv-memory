# -*- coding: utf-8 -*-
"""离线 Mock 模型：自检与测试用，不访问网络、不需要密钥。

行为对齐现有 examples/demo.py：
- 前台返回带账本情绪的固定句；
- 后台从对话片段抽实体钩子与情绪，产出可被 summarizer 校验的 JSON。
"""
from __future__ import annotations

import json
import re

MOOD_HINTS = (("伞", "怀念"), ("雨", "低落"), ("剑", "警觉"), ("秘密", "紧张"))


class MockFront:
    def __init__(self, name: str = "瑾瑜"):
        self.name = name
        self.last_messages: list[dict] | None = None

    def chat(self, messages, *args, **kwargs):
        self.last_messages = messages
        sys_n = sum(1 for m in messages if m.get("role") == "system")
        ledger = next((m.get("content", "") for m in messages if "账本" in m.get("content", "")), "")
        mood = ledger.split("情绪：")[-1].splitlines()[0] if "情绪：" in ledger else "?"
        return f"（{self.name}·读到{sys_n}条system·账本情绪[{mood}]）……主人请自便。"


class MockBack:
    def chat(self, messages, *args, **kwargs):
        body = messages[-1]["content"].split("对话片段：\n")[-1].strip()
        first = body.splitlines()[0][:40] if body else "(empty)"
        hooks = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fa5]{2}", body)))[:4]
        mood = next((value for key, value in MOOD_HINTS if key in body), "平静")
        return json.dumps(
            {
                "summary": first,
                "state_changes": {"mood": mood, "relationship": {"trust": 0.05}},
                "entity_hooks": hooks,
            },
            ensure_ascii=False,
        )
