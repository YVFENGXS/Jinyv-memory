# -*- coding: utf-8 -*-
"""DSH 兼容层：会话目录映射、读取、导入与幂等。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinyv_memory.runtime import JinyvRuntime  # noqa: E402


def _write_session(home: Path, name: str, cards: list[dict], facts: list[dict],
                   session_id: str | None = None) -> Path:
    directory = home / "jinyu-memory" / "sessions" / name
    directory.mkdir(parents=True)
    (directory / "turns.jsonl").write_text(
        json.dumps({"idx": 1, "turn": 1, "user": "你好", "assistant": "主人好"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (directory / "cards.json").write_text(json.dumps(cards, ensure_ascii=False),
                                          encoding="utf-8")
    (directory / "facts.json").write_text(json.dumps(facts, ensure_ascii=False),
                                          encoding="utf-8")
    return directory


class DshCompatTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="jinyv-test-dsh-")
        self.runtime = JinyvRuntime(ROOT, data_dir=self.data.name, mock_llm=True)
        self.runtime.load_plugins()
        self.runtime.start()
        self.record = self.runtime.loader.get("dsh-video-storyboard")
        self.plugin = self.record.plugin

    def tearDown(self):
        self.runtime.shutdown()
        self.data.cleanup()

    def test_import_maps_boundary_and_is_idempotent(self):
        home = Path(self.data.name) / "dsh-home"
        cards = [{
            "id": "C-1",
            "summary": "主人把秘密告诉了瑾瑜",
            "hooks": ["秘密"],
            "relHooks": ["主人"],
            "importance": 0.6,
            "boundary": "self",
            "turnStart": 2,
            "turnEnd": 3,
            "createdAt": "2026-11-01T12:00:00.000Z",
        }]
        facts = [{
            "id": "F-1",
            "text": "主人怕黑",
            "tags": ["主人"],
            "kind": "设定",
            "importance": 0.9,
            "turn": 3,
        }]
        _write_session(home, "sess-a", cards, facts)
        # 插件 config 为空时默认 ~/.dsh；测试里直接指向临时 home
        self.plugin.dsh_home = home
        self.plugin.sessions_dir = home / "jinyu-memory" / "sessions"
        self.plugin.session_count = 1

        first = self.runtime.loader.tools.call("dsh.import_session", {"session": "sess-a"})
        self.assertTrue(first.ok)
        self.assertEqual(first.value["cards_inserted"], 1)
        self.assertEqual(first.value["facts_inserted"], 1)

        second = self.runtime.loader.tools.call("dsh.import_session", {"session": "sess-a"})
        self.assertTrue(second.ok)
        self.assertEqual(second.value["cards_duplicate"], 1)
        self.assertEqual(second.value["facts_duplicate"], 1)

        card = next(c for c in self.runtime.core.memory.recent_cards("desktop", 20)
                    if c["event_id"] == "C-1")
        self.assertEqual(card["knowledge_boundary"], "self")

    def test_session_listing_and_status(self):
        home = Path(self.data.name) / "dsh-home"
        _write_session(home, "sess-a", [], [])
        self.plugin.dsh_home = home
        self.plugin.sessions_dir = home / "jinyu-memory" / "sessions"
        listed = self.runtime.loader.tools.call("dsh.sessions", {})
        self.assertTrue(listed.ok)
        self.assertEqual(listed.value["sessions"][0]["dir"], "sess-a")
        status = self.runtime.loader.tools.call("dsh.status", {})
        self.assertTrue(status.ok)
        self.assertEqual(status.value["install_dir"], str(self.plugin.install_dir))


if __name__ == "__main__":
    unittest.main()
