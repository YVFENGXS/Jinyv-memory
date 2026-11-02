# -*- coding: utf-8 -*-
"""视频通话桥接：用本地 mock HTTP 服务验证 health / turn / say。"""
from __future__ import annotations

import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

BRIDGE_PATH = Path(r"E:\Jinyv-plugins\video-call\bridge.py")


def _load_bridge_class():
    spec = importlib.util.spec_from_file_location("jinyv_call_bridge", BRIDGE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.CallBridge


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def _json(self, code, data):
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._json(200, {"ok": True, "mode": "mock"})
            return
        self._json(404, {"ok": False})

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
        self.seen.append({
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "body": body,
        })
        if self.path == "/call/turn":
            self._json(200, {"ok": True, "reply": f"通话回复：{body.get('text', '')}"})
            return
        if self.path == "/call/say":
            self._json(200, {"ok": True, "queued": True})
            return
        self._json(404, {"ok": False})

    def log_message(self, fmt, *args):
        pass


class CallBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.CallBridge = _load_bridge_class()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        _Handler.seen.clear()
        self.bridge = self.CallBridge(
            f"http://127.0.0.1:{self.port}", token="test-token",
        )

    def test_health_turn_and_say(self):
        self.assertTrue(self.bridge.health()["ok"])
        turn = self.bridge.turn("主人今天很累", session="call-1")
        self.assertEqual(turn["reply"], "通话回复：主人今天很累")
        said = self.bridge.say("瑾瑜想你了", session="call-1")
        self.assertTrue(said["queued"])
        self.assertEqual(_Handler.seen[0]["path"], "/call/turn")
        self.assertEqual(_Handler.seen[0]["auth"], "Bearer test-token")
        self.assertEqual(_Handler.seen[1]["path"], "/call/say")
        self.assertEqual(_Handler.seen[1]["body"]["source"], "jinyv")


if __name__ == "__main__":
    unittest.main()
