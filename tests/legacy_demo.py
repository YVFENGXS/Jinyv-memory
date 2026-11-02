# -*- coding: utf-8 -*-
"""旧回归测试使用的 Mock 模型与示例人格（行为对齐 examples/demo.py）。"""
import json
import re

MOOD_HINTS = (("伞", "怀念"), ("雨", "低落"), ("剑", "警觉"), ("秘密", "紧张"))


class MockFront:
    def __init__(self, name):
        self.name = name
        self.last_messages = None

    def chat(self, messages):
        self.last_messages = messages
        sys_n = sum(1 for m in messages if m["role"] == "system")
        ledger = next((m["content"] for m in messages if "账本" in m["content"]), "")
        mood = ledger.split("情绪：")[-1].splitlines()[0] if "情绪：" in ledger else "?"
        return f"（{self.name}·读到{sys_n}条system·账本情绪[{mood}]）……客人请自便。"


class MockBack:
    def chat(self, messages):
        body = messages[-1]["content"].split("对话片段：\n")[-1].strip()
        first = body.splitlines()[0][:40] if body else "(empty)"
        hooks = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fa5]{2}", body)))[:4]
        mood = next((v for k, v in MOOD_HINTS if k in body), "平静")
        return json.dumps({"summary": first,
                           "state_changes": {"mood": mood,
                                             "relationship": {"trust": 0.05}},
                           "entity_hooks": hooks}, ensure_ascii=False)


PERSONA = {"name": "沈青梧",
           "core": "你是沈青梧，江南伞铺主人。说话简短克制，称对方为「客人」。"}
SCRIPT = ["客人来了，说这把旧伞是我去年落下的", "外面下雨了，茶凉了一半",
          "他问起那场雨夜的事，我没有回答", "他把伞修好了才送回来",
          "我收下伞，欠他一个人情", "他提起城里的剑客，我按住了柜台下的短刀",
          "他说茶要趁热喝，像个旧友", "我第一次把铺子的秘密告诉了他",
          "他走的时候雨停了", "我望着那把旧伞，把它挂回了墙上"]
