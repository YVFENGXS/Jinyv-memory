# -*- coding: utf-8 -*-
"""桌宠 / 通话的 local·server 记忆核心接入测试（mock 宿主 HTTP API）。"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PLUGINS = Path(os.environ.get("JINYV_PLUGINS_DIR") or (ROOT.parent / "Jinyv-plugins"))
TOKEN = "core-token"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []
    token = TOKEN

    def _json(self, code: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        header = str(self.headers.get("Authorization") or "")
        return header == f"Bearer {self.token}"

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._json(200, {"ok": True, "name": "mock-core"})
            return
        if parsed.path == "/recall":
            if not self._authed():
                self._json(401, {"error": "unauthorized"})
                return
            query = urllib.parse.parse_qs(parsed.query)
            self.seen.append({"path": parsed.path, "query": query,
                              "auth": self.headers.get("Authorization", "")})
            self._json(200, {"ok": True, "text": "召回：" + (query.get("q") or [""])[0],
                             "cards": [], "facts": []})
            return
        self._json(404, {"ok": False})

    def do_POST(self):  # noqa: N802
        if not self._authed():
            self._json(401, {"error": "unauthorized"})
            return
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
        self.seen.append({"path": self.path, "body": body,
                          "auth": self.headers.get("Authorization", "")})
        stored = body.get("store_reply")
        self._json(200, {
            "ok": True,
            "reply": stored or ("回复：" + str(body.get("text") or "")),
            "session": body.get("session") or "",
        })

    def log_message(self, fmt, *args):
        pass


class CoreModeClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.DesktopClient = _load(
            "jinyv_desktop_core_client", PLUGINS / "desktop-pet" / "core_client.py"
        ).CoreClient
        cls.VideoClient = _load(
            "jinyv_video_core_client", PLUGINS / "video-call" / "core_client.py"
        ).CoreClient
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

    def test_desktop_local_chat_and_recall(self):
        client = self.DesktopClient(
            f"http://127.0.0.1:{self.port}", TOKEN,
            account="desktop", session="qq-u10001",
        )
        self.assertTrue(client.health()["ok"])
        result = client.chat("主人今天很累", speaker="主人", source="desktop")
        self.assertEqual(result["reply"], "回复：主人今天很累")
        body = _Handler.seen[0]["body"]
        self.assertEqual(body["account"], "desktop")
        self.assertEqual(body["session"], "qq-u10001")
        self.assertEqual(body["speaker"], "主人")
        self.assertEqual(body["source"], "desktop")
        recalled = client.recall("米饭", limit=3)
        self.assertEqual(recalled["text"], "召回：米饭")
        self.assertEqual(_Handler.seen[1]["query"]["limit"], ["3"])

    def test_video_server_store_reply(self):
        client = self.VideoClient(
            f"http://127.0.0.1:{self.port}", TOKEN,
            account="video_call", session="qq-u10001",
        )
        result = client.chat("通话里说的话", store_reply="通话页原文")
        self.assertEqual(result["reply"], "通话页原文")
        body = _Handler.seen[0]["body"]
        self.assertEqual(body["source"], "video-call")
        self.assertEqual(body["store_reply"], "通话页原文")
        self.assertEqual(_Handler.seen[0]["auth"], f"Bearer {TOKEN}")

    def test_missing_url_and_unauthorized(self):
        empty = self.DesktopClient("", TOKEN)
        self.assertFalse(empty.chat("你好")["ok"])
        bad = self.VideoClient(f"http://127.0.0.1:{self.port}", "wrong-token")
        result = bad.chat("你好")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], 401)


if __name__ == "__main__":
    unittest.main()
