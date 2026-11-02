# -*- coding: utf-8 -*-
"""desktop-v1 协议纯函数测试：信封、标记、气泡、动作。"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PROTOCOL_PATH = Path(r"E:\Jinyv-plugins\desktop-pet\protocol.py")


def _load_protocol():
    spec = importlib.util.spec_from_file_location("jinyv_desktop_protocol", PROTOCOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class DesktopProtocolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol = _load_protocol()

    def test_envelope_and_parse(self):
        body = self.protocol.envelope("hello.ack", {"ok": True}, device_id="dev_1", seq=2)
        self.assertEqual(body["v"], 1)
        self.assertEqual(body["type"], "hello.ack")
        self.assertEqual(body["device_id"], "dev_1")
        self.assertEqual(body["seq"], 2)
        self.assertEqual(self.protocol.parse_incoming('{"type":"pong"}')["type"], "pong")
        self.assertIsNone(self.protocol.parse_incoming("not-json"))

    def test_tags_and_bubbles(self):
        clean, actions, moods = self.protocol.take_tags(
            "主人吃饭了吗？[[act:eat]][[mood:happy]]"
        )
        self.assertEqual(clean, "主人吃饭了吗？")
        self.assertEqual(actions, ["eat"])
        self.assertEqual(moods, ["happy"])
        bubbles = self.protocol.split_bubbles("第一句。第二句！", "happy")
        self.assertEqual(len(bubbles), 2)
        self.assertEqual(bubbles[0]["emotion"], "happy")
        self.assertEqual(bubbles[1]["emotion"], "neutral")
        self.assertEqual(self.protocol.actions_for("我饿了"), ["eat"])


if __name__ == "__main__":
    unittest.main()
