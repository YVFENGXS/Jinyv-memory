# -*- coding: utf-8 -*-
"""qqbot 适配层：工具、钩子、事件与降级。"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinyv_memory.runtime import JinyvRuntime  # noqa: E402


class QQBotPluginTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="jinyv-test-qqbot-")
        self.runtime = JinyvRuntime(ROOT, data_dir=self.data.name, mock_llm=True)
        self.runtime.load_plugins()
        self.runtime.start()
        self.record = self.runtime.loader.get("qqbot")
        self.assertIsNotNone(self.record)
        self.plugin = self.record.plugin
        self.events: list[dict] = []
        self.runtime.loader.events.subscribe(
            "qqbot.turn.completed", lambda payload: self.events.append(payload)
        )

    def tearDown(self):
        self.runtime.shutdown()
        self.data.cleanup()

    def test_message_loop_and_hooks(self):
        first = self.plugin.handle_message("主人今天很累", account="owner_qq", speaker="主人")
        second = self.plugin.handle_message("瑾瑜记得要早点睡", account="owner_qq", speaker="主人")
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(len(self.events), 2)
        self.assertIn("memory_recall", self.events[-1]["metadata"])
        self.runtime.core.memory.flush()
        status = self.runtime.core.memory.status("owner_qq")
        self.assertEqual(status["turns"], 4)

    def test_tools_registered_and_callable(self):
        remember = self.runtime.loader.tools.call(
            "qqbot.remember", {"text": "主人喜欢米饭", "speaker": "主人", "importance": 0.9}
        )
        self.assertTrue(remember.ok)
        self.assertTrue(remember.value["fact_id"])
        recall = self.runtime.loader.tools.call(
            "qqbot.recall", {"query": "米饭", "limit": 3}
        )
        self.assertTrue(recall.ok)
        self.assertTrue(recall.value["facts"])
        status = self.runtime.loader.tools.call("qqbot.status", {})
        self.assertTrue(status.ok)
        self.assertGreaterEqual(status.value["facts"], 1)

    def test_degrades_without_nonebot(self):
        has_nonebot = importlib.util.find_spec("nonebot") is not None
        if has_nonebot:
            self.assertIn(self.record.state.value, {"running", "degraded"})
        else:
            self.assertEqual(self.record.state.value, "degraded")
            self.assertIn("nonebot2 未安装", self.record.context.degraded_reason)


if __name__ == "__main__":
    unittest.main()
