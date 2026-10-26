# -*- coding: utf-8 -*-
"""10 轮 mock 演示（确定性版）：python examples/demo.py
验证点：
  1) turns 20 行、游标 8（归档 1..8，热窗口 9..10）
  2) 事件卡区间固定 [(1,2),(3,4),(5,6),(7,8)]，幂等键各异（P0-1）
  3) 账本 delta 累加 = 4 张卡 × 0.05 = 0.20，连跑多次不变（R2）
  4) 跨会话隔离 + 重启恢复（P0-2 / P0-4）
  5) 当前用户消息只出现一次（R1）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "jinyu_memory" / "core"))

import json, re
from pathlib import Path

from rp_skeleton import DB, RPContextMiddleware

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


class MockBack:  # 从对话文本抽 mood/实体钩子，产事件卡 JSON（模拟廉价后台模型）
    def chat(self, messages):
        body = messages[-1]["content"].split("对话片段：\n")[-1].strip()
        first = body.splitlines()[0][:40] if body else "(empty)"
        hooks = list(dict.fromkeys(re.findall(r"[\u4e00-\u9fa5]{2}", body)))[:4]
        mood = next((v for k, v in MOOD_HINTS if k in body), "平静")
        return json.dumps({"summary": first,
                           "state_changes": {"mood": mood, "relationship": {"trust": 0.05}},
                           "entity_hooks": hooks}, ensure_ascii=False)


PERSONA = {"name": "沈青梧", "core": "你是沈青梧，江南伞铺主人。说话简短克制，称对方为「客人」。"}
SCRIPT = ["客人来了，说这把旧伞是我去年落下的", "外面下雨了，茶凉了一半",
          "他问起那场雨夜的事，我没有回答", "他把伞修好了才送回来",
          "我收下伞，欠他一个人情", "他提起城里的剑客，我按住了柜台下的短刀",
          "他说茶要趁热喝，像个旧友", "我第一次把铺子的秘密告诉了他",
          "他走的时候雨停了", "我望着那把旧伞，把它挂回了墙上"]


def fresh_db(path):
    for suffix in ("", "-wal", "-shm"):
        Path(path + suffix).unlink(missing_ok=True)
    return DB(path)


def main():
    sid1, sid2, cid = "s1", "s2", "shen_qingwu"
    front = MockFront("沈青梧")
    mw = RPContextMiddleware(front, MockBack(), fresh_db("demo_memory.db"))
    for text in SCRIPT:
        mw.chat(text, PERSONA, sid1, cid)
    mw.shutdown()
    db = mw.db

    print("== 会话 s1：10 轮后 ==")
    print(f"turns 行数={db.count_turns(sid1, cid)}（10 轮）"
          f" 游标 summarized_until_turn={db.get_cursor(sid1, cid)}")
    for c in db.recent_cards(sid1, cid):
        print(f"  卡 {c['event_id']} t{c['turn_start']}-{c['turn_end']}"
              f" idem={c['idempotency_key'][:8]} 摘要: {c['summary']}")
    print("  s1 账本:", json.dumps(db.load_ledger(sid1, cid), ensure_ascii=False))

    # 确定性断言（R1 / R2 / R3）
    assert db.count_turns(sid1, cid) == 20
    assert db.get_cursor(sid1, cid) == 8
    ranges = sorted((c["turn_start"], c["turn_end"]) for c in db.recent_cards(sid1, cid))
    assert ranges == [(1, 2), (3, 4), (5, 6), (7, 8)], ranges
    assert db.load_ledger(sid1, cid)["relationship"]["trust"] == 0.2
    dup = sum(1 for m in front.last_messages
              if m["role"] == "user" and m["content"] == SCRIPT[-1])
    assert dup == 1, f"当前用户消息应只出现一次，实际 {dup}"

    mw2 = RPContextMiddleware(MockFront("沈青梧"), MockBack(), DB("demo_memory.db"))  # 新实例=模拟重启
    mw2.chat("又来一位客人，问起那把旧伞", PERSONA, sid2, cid)    # 提到 s1 的实体
    mw2.shutdown()
    print("== 会话 s2（同角色新会话，验证隔离与重启恢复）==")
    print("  s2 账本:", json.dumps(mw2._ledger(sid2, cid), ensure_ascii=False), "<- 应为初始值，不串味")
    print("  s2 可见事件卡:", len(db.recent_cards(sid2, cid)), "张 <- 应为 0")
    print("  重启后 s1 账本仍在:", db.load_ledger(sid1, cid) is not None)
    assert mw2._ledger(sid2, cid)["relationship"] == {}
    assert len(db.recent_cards(sid2, cid)) == 0
    assert db.load_ledger(sid1, cid) is not None
    print("all asserts passed")


if __name__ == "__main__":
    main()


