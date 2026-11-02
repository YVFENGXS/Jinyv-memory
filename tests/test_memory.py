# -*- coding: utf-8 -*-
"""记忆门面：写、归档、召回、多车道与租约/幂等。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinyv_memory.accounts import Lane  # noqa: E402
from jinyv_memory.errors import LeaseBusy  # noqa: E402
from jinyv_memory.runtime import JinyvRuntime  # noqa: E402

SCRIPT = [
    "客人来了，说这把旧伞是我去年落下的",
    "外面下雨了，茶凉了一半",
    "他问起那场雨夜的事，我没有回答",
    "他把伞修好了才送回来",
    "我收下伞，欠他一个人情",
    "他提起城里的剑客，我按住了柜台下的短刀",
    "他说茶要趁热喝，像个旧友",
    "我第一次把铺子的秘密告诉了他",
    "他走的时候雨停了",
    "我望着那把旧伞，把它挂回了墙上",
]


class MemoryAPITest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="jinyv-test-memory-")
        self.runtime = JinyvRuntime(ROOT, data_dir=self.data.name, mock_llm=True)
        self.runtime.load_plugins()
        self.runtime.start()
        self.memory = self.runtime.core.memory

    def tearDown(self):
        self.runtime.shutdown()
        self.data.cleanup()

    def test_write_archive_recall_and_idempotency(self):
        for text in SCRIPT:
            reply = self.memory.chat("owner_qq", text, speaker="客人")
            self.assertTrue(reply)
        self.memory.flush()
        status = self.memory.status("owner_qq")
        self.assertEqual(status["turns"], 20)
        self.assertEqual(status["cursor"], 8)
        cards = self.memory.recent_cards("owner_qq", limit=10)
        self.assertEqual(sorted((c["turn_start"], c["turn_end"]) for c in cards),
                         [(1, 2), (3, 4), (5, 6), (7, 8)])
        recall = self.memory.recall("owner_qq", "旧伞", limit=3)
        self.assertTrue(recall.cards or recall.facts)
        self.assertIn("【长期记忆】", recall.text)

        # 同一张外部卡重复导入：UNIQUE 幂等键兜住，不产生副本
        card = {
            "id": "C-dup", "summary": "客人把旧伞留在店里",
            "hooks": ["旧伞"], "relHooks": [], "importance": 0.8,
            "boundary": "both", "turnStart": 1, "turnEnd": 1,
        }
        fact = {"text": "客人有一把旧伞", "importance": 0.9, "turn": 1}
        first = self.memory.import_external("owner_qq", [card], [fact])
        second = self.memory.import_external("owner_qq", [card], [fact])
        self.assertEqual(first["cards_inserted"], 1)
        self.assertEqual(second["cards_duplicate"], 1)
        self.assertEqual(second["facts_duplicate"], 1)

    def test_lane_isolation(self):
        self.runtime.core.accounts.register(Lane(
            key="test_s2", platform="test", account="2", bot_id="test-bot",
            character_id="jinyu", session_id="test-s2", continuity_id="jinyu-main",
        ))
        self.memory.chat("owner_qq", "主人的会话")
        self.memory.chat("test_s2", "另一个人的会话")
        self.memory.flush()
        self.assertEqual(self.memory.status("owner_qq")["turns"], 2)
        self.assertEqual(self.memory.status("test_s2")["turns"], 2)
        self.assertEqual(len(self.memory.recent_cards("test_s2")), 0)

    def test_lease_exclusive_and_job_once(self):
        lease_a = self.runtime.core.leases.acquire("reply:test", "a", ttl=30)
        lease_b = self.runtime.core.leases.acquire("reply:test", "b", ttl=30)
        self.assertIsNotNone(lease_a)
        self.assertIsNone(lease_b)
        self.runtime.core.leases.release(lease_a)
        lease_c = self.runtime.core.leases.acquire("reply:test", "c", ttl=30)
        self.assertIsNotNone(lease_c)
        self.runtime.core.leases.release(lease_c)

        self.assertTrue(self.runtime.core.leases.claim_job("job.test", "1", "a"))
        self.assertFalse(self.runtime.core.leases.claim_job("job.test", "1", "b"))
        self.runtime.core.leases.finish_job("job.test", "1", False, "boom")
        self.assertTrue(self.runtime.core.leases.claim_job("job.test", "1", "b"))
        self.runtime.core.leases.finish_job("job.test", "1", False, "boom2")
        self.assertTrue(self.runtime.core.leases.claim_job("job.test", "1", "c"))
        self.runtime.core.leases.finish_job("job.test", "1", False, "boom3")
        self.assertFalse(self.runtime.core.leases.claim_job("job.test", "1", "d"))

    def test_busy_reply_lease(self):
        lease = self.runtime.core.leases.acquire("reply:jinyu:qq-u10001", "other", ttl=30)
        self.assertIsNotNone(lease)
        try:
            with self.assertRaises(LeaseBusy):
                self.memory.chat("owner_qq", "会被租约挡住", reply_lease=True,
                                 lease_owner="owner_qq")
        finally:
            self.runtime.core.leases.release(lease)


if __name__ == "__main__":
    unittest.main()
