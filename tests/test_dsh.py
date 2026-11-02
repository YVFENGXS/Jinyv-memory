# -*- coding: utf-8 -*-
"""DSH 特殊接入：检测、导入幂等、导出幂等、冲突（core-wins）与 journal。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinyv_memory.integrations.dsh import DshBridge  # noqa: E402
from jinyv_memory.runtime import JinyvRuntime  # noqa: E402


def _write_session(home: Path, name: str, cards: list[dict], facts: list[dict]) -> Path:
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


class DshBridgeTest(unittest.TestCase):
    def setUp(self):
        self.data = tempfile.TemporaryDirectory(prefix="jinyv-test-dsh-")
        self.runtime = JinyvRuntime(ROOT, data_dir=self.data.name, mock_llm=True)
        self.runtime.start()
        self.home = Path(self.data.name) / "dsh-home"
        self.session_dir = _write_session(self.home, "sess-a", [{
            "id": "C-1",
            "summary": "主人把秘密告诉了瑾瑜",
            "hooks": ["秘密"],
            "relHooks": ["主人"],
            "importance": 0.6,
            "boundary": "self",
            "turnStart": 2,
            "turnEnd": 3,
            "createdAt": "2026-11-01T12:00:00.000Z",
        }], [{
            "id": "F-1",
            "text": "主人怕黑",
            "tags": ["主人"],
            "kind": "设定",
            "importance": 0.9,
            "turn": 3,
        }])
        self.bridge = DshBridge.from_file(
            self.runtime.paths.config_dir / "integrations" / "dsh.toml",
            memory=self.runtime.core.memory,
            paths=self.runtime.paths,
            overrides={
                "dsh_home": str(self.home),
                "plugin_dir": "",
                "storyboard_dir": "",
                "account": "desktop",
            },
        )

    def tearDown(self):
        self.runtime.shutdown()
        self.data.cleanup()

    def test_detect_import_and_idempotency(self):
        status = self.bridge.status()
        self.assertEqual(status["sessions"], 1)
        self.assertFalse(status["managed"])
        first = self.bridge.import_session("sess-a")
        self.assertEqual(first["cards_inserted"], 1)
        self.assertEqual(first["facts_inserted"], 1)
        second = self.bridge.import_session("sess-a")
        self.assertEqual(second["skipped"], 2)
        self.assertEqual(second["conflicts"], 0)
        self.assertTrue((self.session_dir / ".jinyv-bridge.json").exists())
        card = next(c for c in self.runtime.core.memory.recent_cards("desktop", 20)
                    if c["event_id"] == "C-1")
        self.assertEqual(card["knowledge_boundary"], "self")

    def test_conflict_core_wins(self):
        self.bridge.import_session("sess-a")
        cards = json.loads((self.session_dir / "cards.json").read_text(encoding="utf-8"))
        cards[0]["summary"] = "DSH 侧改写的摘要"
        (self.session_dir / "cards.json").write_text(
            json.dumps(cards, ensure_ascii=False), encoding="utf-8"
        )
        result = self.bridge.import_session("sess-a")
        self.assertEqual(result["conflicts"], 1)
        self.assertEqual(result["cards_inserted"], 0)
        card = next(c for c in self.runtime.core.memory.recent_cards("desktop", 20)
                    if c["event_id"] == "C-1")
        self.assertEqual(card["summary"], "主人把秘密告诉了瑾瑜")

    def test_export_idempotency_and_journal(self):
        self.bridge.import_session("sess-a")
        self.runtime.core.memory.import_external("desktop", [{
            "event_id": "core-demo",
            "summary": "核心侧独有的卡片",
            "entity_hooks": ["米饭"],
            "rel_hooks": [],
            "knowledge_boundary": "both",
            "importance": 0.7,
            "turn_start": 9,
            "turn_end": 9,
        }], [])
        self.runtime.core.memory.remember("desktop", "瑾瑜喜欢米饭", speaker="瑾瑜", importance=0.8)
        first = self.bridge.export_session("sess-a")
        self.assertGreaterEqual(first["cards_new"], 1)
        self.assertGreaterEqual(first["facts_new"], 1)
        second = self.bridge.export_session("sess-a")
        self.assertEqual(second["cards_new"], 0)
        self.assertEqual(second["facts_new"], 0)
        self.assertEqual(second["conflicts"], 0)
        cards = json.loads((self.session_dir / "cards.json").read_text(encoding="utf-8"))
        self.assertTrue(any(card.get("jinyv_core_id") == "core-demo" for card in cards))

    def test_start_without_command_is_noop(self):
        self.assertFalse(self.bridge.start())
        self.assertFalse(self.bridge.status()["managed"])


if __name__ == "__main__":
    unittest.main()
